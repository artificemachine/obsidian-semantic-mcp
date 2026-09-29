"""
Contract tests for the Caasiopeia HTTP boundary (src/caasiopeia_client.py).

Only in-memory fake sessions are used: no test here opens a socket.
"""
import json
import sys
import uuid
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import caasiopeia_client as cc

SOURCE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
API_KEY = f"synthetic-{uuid.uuid4().hex}"


def _passage(**overrides):
    passage = {
        "chunk_id": "00000000-0000-4000-8000-000000000001",
        "document_id": "00000000-0000-4000-8000-000000000002",
        "source_id": SOURCE_ID,
        "external_id": "notes/foo.md",
        "title": "Foo",
        "heading_path": ["Top", "Sub"],
        "ordinal": 0,
        "score": 0.83,
        "text": "the passage text",
        "tokens": 4,
        "pruned": False,
    }
    passage.update(overrides)
    return passage


def _body(**overrides):
    body = {
        "passages": [_passage()],
        "tokens_used": 4,
        "cache": "miss",
        "trace_id": "00000000-0000-4000-8000-0000000000aa",
        "degraded": False,
        "degradation_reason": None,
        "model_id": "fixture-model",
    }
    body.update(overrides)
    return body


class FakeResponse:
    def __init__(self, status_code=200, body=None, raw=None, headers=None):
        self.status_code = status_code
        payload = raw if raw is not None else json.dumps(body if body is not None else _body())
        self._raw = payload.encode() if isinstance(payload, str) else payload
        self.headers = dict(headers or {})
        self.closed = False

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._raw), chunk_size):
            yield self._raw[i:i + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response if response is not None else FakeResponse()
        self.error = error
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        return self.response


def _client(session, **kwargs):
    return cc.CaasClient(
        "http://caas.invalid:8080/", API_KEY, session=session, **kwargs
    )


def test_search_posts_exact_context_contract():
    session = FakeSession()
    client = _client(session)

    result = client.search(
        "  find the plan  ",
        token_budget=1500,
        source_ids=[SOURCE_ID],
        mode="dense",
    )

    assert len(session.calls) == 1
    url, kwargs = session.calls[0]
    assert url == "http://caas.invalid:8080/v1/context"
    body = kwargs["json"]
    assert set(body) <= {
        "query", "token_budget", "source_ids", "max_per_document",
        "mode", "prune", "trace_id",
    }, "the service rejects unknown request fields"
    assert body["query"] == "find the plan"
    assert body["token_budget"] == 1500
    assert body["source_ids"] == [SOURCE_ID]
    assert body["mode"] == "dense"
    generated = uuid.UUID(body["trace_id"])
    headers = kwargs["headers"]
    assert headers["Authorization"] == f"Bearer {API_KEY}"
    assert headers["x-caas-trace-id"] == str(generated)
    assert kwargs["timeout"] == (cc.DEFAULT_CONNECT_TIMEOUT, cc.DEFAULT_READ_TIMEOUT)
    assert kwargs["allow_redirects"] is False
    assert result.trace_id == str(generated)
    assert result.cache == "miss"
    assert result.degraded is False
    assert len(result.passages) == 1
    passage = result.passages[0]
    assert passage.external_id == "notes/foo.md"
    assert passage.source_id == SOURCE_ID
    assert passage.heading_path == ("Top", "Sub")
    assert passage.score == pytest.approx(0.83)
    assert passage.text == "the passage text"


def test_search_uses_the_caller_supplied_trace_id():
    session = FakeSession()
    trace = str(uuid.uuid4())

    result = _client(session).search(
        "q", token_budget=10, source_ids=[SOURCE_ID], trace_id=trace
    )

    _, kwargs = session.calls[0]
    assert kwargs["json"]["trace_id"] == trace
    assert kwargs["headers"]["x-caas-trace-id"] == trace
    assert result.trace_id == trace


@pytest.mark.parametrize(
    "call, expected",
    [
        ({"query": "   ", "token_budget": 10, "source_ids": [SOURCE_ID]}, "query"),
        ({"query": "q", "token_budget": 0, "source_ids": [SOURCE_ID]}, "token_budget"),
        ({"query": "q", "token_budget": 10, "source_ids": []}, "source_ids"),
        ({"query": "q", "token_budget": 10, "source_ids": ["not-a-uuid"]}, "source_ids"),
        ({"query": "q", "token_budget": 10, "source_ids": [SOURCE_ID], "mode": "fuzzy"}, "mode"),
    ],
)
def test_search_rejects_blank_query_and_unknown_request_shape(call, expected):
    session = FakeSession()

    with pytest.raises(cc.CaasRequestError, match=expected):
        _client(session).search(**call)

    assert session.calls == [], "boundary validation must fail before any request"


@pytest.mark.parametrize(
    "passage",
    [
        {k: v for k, v in _passage().items() if k != "external_id"},
        _passage(heading_path="Top"),
        _passage(score="high"),
        _passage(source_id="not-a-uuid"),
        "not-an-object",
    ],
)
def test_search_rejects_unknown_passage_shape(passage):
    session = FakeSession(FakeResponse(body=_body(passages=[passage])))

    with pytest.raises(cc.CaasInvalidResponse):
        _client(session).search("q", token_budget=10, source_ids=[SOURCE_ID])


def test_search_tolerates_additive_passage_fields():
    extra = _passage(future_field="ignored")
    session = FakeSession(FakeResponse(body=_body(passages=[extra])))

    result = _client(session).search("q", token_budget=10, source_ids=[SOURCE_ID])

    assert result.passages[0].external_id == "notes/foo.md"


def test_client_maps_timeout_unauthorized_and_invalid_json_to_typed_errors():
    def failure(session):
        with pytest.raises(cc.CaasError) as info:
            _client(session).search("q", token_budget=10, source_ids=[SOURCE_ID])
        return info.value

    timeout = failure(FakeSession(error=requests.exceptions.ReadTimeout("slow")))
    assert type(timeout) is cc.CaasTimeout and timeout.retryable is True

    refused = failure(FakeSession(error=requests.exceptions.ConnectionError("down")))
    assert type(refused) is cc.CaasUnavailable and refused.retryable is True

    unauthorized = failure(FakeSession(FakeResponse(status_code=401, raw="{}")))
    assert type(unauthorized) is cc.CaasUnauthorized
    assert unauthorized.retryable is False

    unavailable = failure(FakeSession(FakeResponse(status_code=503, raw="{}")))
    assert type(unavailable) is cc.CaasUnavailable and unavailable.retryable is True
    assert unavailable.status == 503

    rejected = failure(FakeSession(FakeResponse(status_code=422, raw="{}")))
    assert type(rejected) is cc.CaasRejected and rejected.retryable is False
    assert rejected.status == 422

    invalid = failure(FakeSession(FakeResponse(raw="<html>not json</html>")))
    assert type(invalid) is cc.CaasInvalidResponse and invalid.retryable is False

    not_an_object = failure(FakeSession(FakeResponse(raw="[]")))
    assert type(not_an_object) is cc.CaasInvalidResponse


def test_errors_carry_the_trace_id_and_never_expose_the_credential():
    session = FakeSession(FakeResponse(status_code=401, raw=f"echo {API_KEY}"))
    trace = str(uuid.uuid4())
    client = _client(session)

    with pytest.raises(cc.CaasUnauthorized) as info:
        client.search("q", token_budget=10, source_ids=[SOURCE_ID], trace_id=trace)

    assert info.value.trace_id == trace
    assert API_KEY not in str(info.value)
    assert API_KEY not in repr(info.value)
    assert API_KEY not in repr(client)
    assert API_KEY not in str(client)


def test_oversized_response_is_rejected_without_buffering_it_all():
    big = FakeResponse(raw=b"x" * 5000)
    session = FakeSession(big)

    with pytest.raises(cc.CaasInvalidResponse, match="too large"):
        _client(session, max_response_bytes=1000).search(
            "q", token_budget=10, source_ids=[SOURCE_ID]
        )

    assert big.closed is True


def test_declared_oversized_content_length_is_rejected_up_front():
    declared = FakeResponse(headers={"Content-Length": "999999"})
    session = FakeSession(declared)

    with pytest.raises(cc.CaasInvalidResponse, match="too large"):
        _client(session, max_response_bytes=1000).search(
            "q", token_budget=10, source_ids=[SOURCE_ID]
        )


def test_search_does_not_log_the_bearer_token(caplog):
    session = FakeSession()
    with caplog.at_level("DEBUG"):
        _client(session).search("q", token_budget=10, source_ids=[SOURCE_ID])
        with pytest.raises(cc.CaasUnauthorized):
            _client(FakeSession(FakeResponse(status_code=401, raw="{}"))).search(
                "q", token_budget=10, source_ids=[SOURCE_ID]
            )

    assert API_KEY not in caplog.text


@pytest.mark.parametrize(
    "base_url",
    ["", "ftp://caas.invalid", "http://user:pw@caas.invalid", "caas.invalid", "http://"],
)
def test_client_rejects_a_malformed_base_url(base_url):
    with pytest.raises(ValueError, match="base URL"):
        cc.CaasClient(base_url, API_KEY, session=FakeSession())


def test_client_requires_an_api_key():
    with pytest.raises(ValueError, match="API key"):
        cc.CaasClient("http://caas.invalid", "", session=FakeSession())
