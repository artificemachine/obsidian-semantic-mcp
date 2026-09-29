"""
caasiopeia_client.py — typed HTTP boundary to Caasiopeia's ``POST /v1/context``.

Transport only: this module never imports sibling OSM modules, never touches
the database, and never reads the environment. Configuration arrives through
the constructor (see config.load_caasiopeia_settings), so the bearer token
lives in process memory only and is never written to disk or logs.

Wire contract (verified against caasiopeia ``src/domain/context.rs`` and
``src/mcp/client.rs``):
  * request body fields: query, token_budget, source_ids, max_per_document,
    mode (hybrid | dense | lexical), prune, trace_id. The service rejects
    unknown fields, so nothing else is ever sent.
  * trace correlation header: ``x-caas-trace-id``.
  * auth: ``Authorization: Bearer <api key>``.
  * responses over 8 MiB are a protocol violation, not buffered without bound.
"""
from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import requests

log = logging.getLogger("obsidian-semantic-mcp.caasiopeia")

TRACE_HEADER = "x-caas-trace-id"
CONTEXT_PATH = "/v1/context"
MODES = ("hybrid", "dense", "lexical")
DEFAULT_CONNECT_TIMEOUT = 3.05
DEFAULT_READ_TIMEOUT = 30.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_CHUNK_BYTES = 64 * 1024
_CACHE_OUTCOMES = ("hit", "miss", "disabled")


# ───────────────────────────────── Errors ────────────────────────────────────

class CaasError(Exception):
    """Base for every failure of a Caasiopeia call.

    ``retryable`` tells the caller whether trying the same request again can
    help. Messages never include the credential, the response body or the URL.
    """

    retryable = False

    def __init__(self, message: str, *, trace_id: str | None = None,
                 status: int | None = None):
        super().__init__(message)
        self.trace_id = trace_id
        self.status = status


class CaasRequestError(CaasError, ValueError):
    """The caller's request is invalid; raised before any network I/O."""


class CaasTimeout(CaasError):
    retryable = True


class CaasUnavailable(CaasError):
    """Connection failure, HTTP 429 or a 5xx answer."""

    retryable = True


class CaasUnauthorized(CaasError):
    """HTTP 401/403: a configuration problem, retrying cannot help."""


class CaasRejected(CaasError):
    """Any other non-success HTTP status."""


class CaasInvalidResponse(CaasError):
    """Malformed, oversized or wrongly shaped response from the service."""


# ───────────────────────────────── Result types ──────────────────────────────

@dataclass(frozen=True)
class Passage:
    chunk_id: str
    document_id: str
    source_id: str
    external_id: str
    title: str | None
    heading_path: tuple[str, ...]
    ordinal: int
    score: float
    text: str
    tokens: int
    pruned: bool = False


@dataclass(frozen=True)
class ContextResult:
    passages: tuple[Passage, ...]
    tokens_used: int
    cache: str
    trace_id: str
    degraded: bool
    degradation_reason: str | None
    model_id: str


# ─────────────────────────── Validation (no transport) ───────────────────────

def _as_uuid(value: Any, what: str, error: type[CaasError]) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise error(f"{what} is not a UUID") from None


def _require(obj: dict, key: str, kind: type | tuple, what: str) -> Any:
    value = obj.get(key)
    kinds = kind if isinstance(kind, tuple) else (kind,)
    # bool is an int subclass; a JSON true must not satisfy a numeric field.
    if not isinstance(value, kinds) or (isinstance(value, bool) and bool not in kinds):
        raise CaasInvalidResponse(f"{what}.{key} missing or of the wrong type")
    return value


def _parse_passage(raw: Any) -> Passage:
    if not isinstance(raw, dict):
        raise CaasInvalidResponse("passage is not an object")
    heading_path = _require(raw, "heading_path", list, "passage")
    if not all(isinstance(part, str) for part in heading_path):
        raise CaasInvalidResponse("passage.heading_path must hold strings")
    title = raw.get("title")
    if title is not None and not isinstance(title, str):
        raise CaasInvalidResponse("passage.title must be a string or null")
    pruned = raw.get("pruned", False)
    if not isinstance(pruned, bool):
        raise CaasInvalidResponse("passage.pruned must be a boolean")
    return Passage(
        chunk_id=_as_uuid(raw.get("chunk_id"), "passage.chunk_id", CaasInvalidResponse),
        document_id=_as_uuid(raw.get("document_id"), "passage.document_id", CaasInvalidResponse),
        source_id=_as_uuid(raw.get("source_id"), "passage.source_id", CaasInvalidResponse),
        external_id=_require(raw, "external_id", str, "passage"),
        title=title,
        heading_path=tuple(heading_path),
        ordinal=_require(raw, "ordinal", int, "passage"),
        score=float(_require(raw, "score", (int, float), "passage")),
        text=_require(raw, "text", str, "passage"),
        tokens=_require(raw, "tokens", int, "passage"),
        pruned=pruned,
    )


def _parse_response(payload: Any, trace_id: str) -> ContextResult:
    if not isinstance(payload, dict):
        raise CaasInvalidResponse("response is not a JSON object", trace_id=trace_id)
    try:
        passages = _require(payload, "passages", list, "response")
        cache = _require(payload, "cache", str, "response")
        if cache not in _CACHE_OUTCOMES:
            raise CaasInvalidResponse("response.cache has an unknown value")
        reason = payload.get("degradation_reason")
        if reason is not None and not isinstance(reason, str):
            raise CaasInvalidResponse("response.degradation_reason must be a string or null")
        return ContextResult(
            passages=tuple(_parse_passage(p) for p in passages),
            tokens_used=_require(payload, "tokens_used", int, "response"),
            cache=cache,
            trace_id=trace_id,
            degraded=_require(payload, "degraded", bool, "response"),
            degradation_reason=reason,
            model_id=_require(payload, "model_id", str, "response"),
        )
    except CaasInvalidResponse as exc:
        exc.trace_id = trace_id
        raise


def _validate_base_url(base_url: str) -> str:
    parts = urlsplit(base_url) if isinstance(base_url, str) else None
    if (
        parts is None
        or parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
    ):
        raise ValueError("invalid Caasiopeia base URL: expected http(s)://host[:port] without credentials")
    return base_url.rstrip("/")


# ───────────────────────────────── Client ────────────────────────────────────

class CaasClient:
    """One ``search()`` operation over ``POST /v1/context``."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        session: requests.Session | None = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ):
        self._base_url = _validate_base_url(base_url)
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("Caasiopeia API key must be a non-empty string")
        self._api_key = api_key
        self._session = session if session is not None else requests.Session()
        self._timeout = (connect_timeout, read_timeout)
        self._max_response_bytes = max_response_bytes

    def __repr__(self) -> str:
        return f"CaasClient(base_url={self._base_url!r}, api_key=<redacted>)"

    __str__ = __repr__

    def search(
        self,
        query: str,
        *,
        token_budget: int,
        source_ids: Iterable[str],
        mode: str = "hybrid",
        max_per_document: int | None = None,
        trace_id: str | None = None,
    ) -> ContextResult:
        body, trace = self._build_request(
            query, token_budget, source_ids, mode, max_per_document, trace_id
        )
        response = self._send(body, trace)
        try:
            self._raise_for_status(response.status_code, trace)
            payload = self._decode(response, trace)
        finally:
            response.close()
        return _parse_response(payload, trace)

    # ── internals ────────────────────────────────────────────────────────────

    @staticmethod
    def _build_request(query, token_budget, source_ids, mode, max_per_document, trace_id):
        if not isinstance(query, str) or not query.strip():
            raise CaasRequestError("query must be a non-empty string")
        if isinstance(token_budget, bool) or not isinstance(token_budget, int) or token_budget < 1:
            raise CaasRequestError("token_budget must be a positive integer")
        if mode not in MODES:
            raise CaasRequestError(f"mode must be one of {', '.join(MODES)}")
        sources = [_as_uuid(s, "source_ids entry", CaasRequestError) for s in source_ids]
        if not sources:
            raise CaasRequestError("source_ids must name at least one source")
        if max_per_document is not None and (
            isinstance(max_per_document, bool)
            or not isinstance(max_per_document, int)
            or max_per_document < 1
        ):
            raise CaasRequestError("max_per_document must be a positive integer")
        trace = (
            _as_uuid(trace_id, "trace_id", CaasRequestError)
            if trace_id is not None else str(uuid.uuid4())
        )
        body: dict[str, Any] = {
            "query": query.strip(),
            "token_budget": token_budget,
            "source_ids": sources,
            "mode": mode,
            "trace_id": trace,
        }
        if max_per_document is not None:
            body["max_per_document"] = max_per_document
        return body, trace

    def _send(self, body: dict, trace: str):
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            TRACE_HEADER: trace,
        }
        try:
            return self._session.post(
                self._base_url + CONTEXT_PATH,
                json=body,
                headers=headers,
                timeout=self._timeout,
                allow_redirects=False,
                stream=True,
            )
        except requests.exceptions.Timeout:
            raise CaasTimeout("Caasiopeia request timed out", trace_id=trace) from None
        except requests.exceptions.RequestException:
            raise CaasUnavailable("Caasiopeia connection failed", trace_id=trace) from None

    @staticmethod
    def _raise_for_status(status: int, trace: str) -> None:
        if 200 <= status < 300:
            return
        if status in (401, 403):
            raise CaasUnauthorized(
                f"Caasiopeia rejected the credential (HTTP {status})",
                trace_id=trace, status=status,
            )
        if status == 429 or status >= 500:
            raise CaasUnavailable(
                f"Caasiopeia is unavailable (HTTP {status})",
                trace_id=trace, status=status,
            )
        raise CaasRejected(
            f"Caasiopeia rejected the request (HTTP {status})",
            trace_id=trace, status=status,
        )

    def _decode(self, response, trace: str) -> Any:
        declared = response.headers.get("Content-Length")
        if declared is not None:
            try:
                if int(declared) > self._max_response_bytes:
                    raise CaasInvalidResponse("Caasiopeia response too large", trace_id=trace)
            except ValueError:
                raise CaasInvalidResponse(
                    "Caasiopeia response has an invalid Content-Length", trace_id=trace
                ) from None
        buffered = bytearray()
        try:
            for chunk in response.iter_content(chunk_size=_CHUNK_BYTES):
                buffered.extend(chunk)
                if len(buffered) > self._max_response_bytes:
                    raise CaasInvalidResponse("Caasiopeia response too large", trace_id=trace)
        except requests.exceptions.Timeout:
            raise CaasTimeout("Caasiopeia response timed out", trace_id=trace) from None
        except requests.exceptions.RequestException:
            raise CaasUnavailable("Caasiopeia connection failed", trace_id=trace) from None
        try:
            return json.loads(bytes(buffered))
        except ValueError:
            raise CaasInvalidResponse(
                "Caasiopeia returned a body that is not JSON", trace_id=trace
            ) from None
