#!/usr/bin/env python3
"""
server.py — Unified Obsidian MCP server.

Combines semantic search (pgvector) with full vault CRUD operations.
Replaces both obsidian-semantic AND mcp-obsidian with a single server
that works without Obsidian running (direct filesystem access).

Stack:
  - PostgreSQL + pgvector : vector storage
  - Ollama (nomic-embed-text) : local embeddings
  - watchdog : live file watcher
  - mcp : Model Context Protocol server

Environment variables:
  OBSIDIAN_VAULT    absolute path to your vault (required)
  DATABASE_URL      postgres connection string  (overrides POSTGRES_* vars)
  POSTGRES_HOST     postgres host               (default: localhost)
  POSTGRES_PORT     postgres port               (default: 5432)
  POSTGRES_DB       postgres database           (default: obsidian_brain)
  POSTGRES_USER     postgres user               (default: obsidian)
  POSTGRES_PASSWORD postgres password           (default: empty)
  OLLAMA_URL        ollama API endpoint         (default: http://localhost:11434)
  EMBEDDING_MODEL   ollama model name           (default: nomic-embed-text)
  EMBED_TIMEOUT     seconds before embed request times out (default: 30)
  EMBED_WORKERS     parallel embedding threads  (default: 1 — CPU-only Ollama
                    serves one request at a time; raise only if Ollama has
                    GPU/multi-slot capacity to match)
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import signal
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.pool
import requests
import yaml
from mcp.server import Server
import anyio
from mcp.shared.message import SessionMessage
from mcp.types import Tool, TextContent, JSONRPCMessage
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from dotenv import load_dotenv

# Load .env from the install data dir first, then fall back to the repo root.
# This allows the MCP server to start correctly when spawned by an MCP client
# (e.g. OpenCode, Claude Desktop) without any env vars pre-set in the shell.
_ENV_SEARCH_PATHS = [
    Path.home() / ".local" / "share" / "obsidian-semantic-mcp" / ".env",
    Path(__file__).resolve().parent.parent / ".env",
]
for _env_path in _ENV_SEARCH_PATHS:
    if _env_path.exists():
        load_dotenv(_env_path, override=False)
        break

try:
    from . import migrations
    from .config import build_dsn, REQUIRED_FRONTMATTER_DEFAULTS, REINDEX_LOCK_KEY  # installed as a package (uv tool / pip install)
except ImportError:
    import migrations
    from config import build_dsn, REQUIRED_FRONTMATTER_DEFAULTS, REINDEX_LOCK_KEY  # fallback: run directly from src/ during dev


# ─────────────────────────────────── Config ─────────────────────────────────

def _parse_vault_paths() -> list[str]:
    """Return vault path list from OBSIDIAN_VAULTS (comma-separated) or OBSIDIAN_VAULT."""
    multi = os.environ.get("OBSIDIAN_VAULTS", "")
    if multi:
        return [v.strip() for v in multi.split(",") if v.strip()]
    single = os.environ.get("OBSIDIAN_VAULT", "")
    return [single] if single else []


VAULT_PATHS: list[str] = _parse_vault_paths()
VAULT_PATH: str = VAULT_PATHS[0] if VAULT_PATHS else ""  # primary vault (backward compat)
# Snapshot of VAULT_PATHS used by path helpers. Patchable by tests via
# monkeypatch.setattr(server, "_VAULT_LIST", [...]).
_VAULT_LIST: list[str] = list(VAULT_PATHS)
OLLAMA_URL   = os.environ.get("OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL  = os.environ.get("EMBEDDING_MODEL", "nomic-embed-text")
EMBED_TIMEOUT = int(os.environ.get("EMBED_TIMEOUT", "30"))
EMBED_WORKERS       = int(os.environ.get("EMBED_WORKERS", "1"))       # parallel embedding threads — match Ollama's serving slots (default 1, CPU-only)
EMBED_BATCH_SIZE    = int(os.environ.get("EMBED_BATCH_SIZE", "16"))   # texts per /api/embed call (Ollama 0.4+)
RERANK_MODEL        = os.environ.get("RERANK_MODEL", "")               # cross-encoder model; empty = disabled
RERANK_CANDIDATES   = int(os.environ.get("RERANK_CANDIDATES", "20"))   # candidate pool size before re-ranking
# Polling watcher: "true" forces PollingObserver for all vaults (required for
# network filesystems like NFS/SMB where OS-level inotify/ReadDirectoryChangesW
# does not fire for writes from remote clients).  "auto" (default) uses a
# heuristic — UNC paths and mapped network drives get polling, local paths get
# native events.  "false" always uses native events.
VAULT_WATCH_POLLING = os.environ.get("VAULT_WATCH_POLLING", "auto").lower()
VAULT_POLL_INTERVAL = int(os.environ.get("VAULT_POLL_INTERVAL", "10"))  # seconds between polls

DATABASE_URL = build_dsn()

MAX_EMBED_CHARS = 2000  # nomic-embed-text context limit (approx 512 tokens)
_TIMESTAMP_FMT  = "%Y-%m-%d %H:%M"
_DEBOUNCE_SECS  = 0.5   # collapse rapid saves from Obsidian autosave

# Set during background_init so search_vault can return a useful message
# instead of the misleading "No indexed notes found. Try running reindex_vault."
# threading.Event is used rather than a bare bool to avoid any cross-thread
# visibility issues without relying on the GIL.
#
# Kept as a fast in-process short-circuit even after iteration 6 made
# index_state (Postgres) the source of truth for indexing status — a DB
# round-trip on every search would be wasteful when this process itself is
# the one indexing. It is NOT cross-process visible: a search hitting a
# *different* container while *this* one indexes will not see it set. That
# case is covered by index_state, which get_last_rebuild_failures() and
# /api/stats read instead.
_INDEXING_IN_PROGRESS = threading.Event()

# Wikilink graph: stem.lower() → absolute path for all indexed .md files.
# Rebuilt by index_vault(); incrementally updated by the file watcher.
_link_index: dict[str, str] = {}
_link_index_lock = threading.Lock()

# Matches [[note]], [[note|alias]], [[note#heading]], [[folder/note]]
_WIKILINK_RE = re.compile(r'\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]')


def extract_wikilinks(content: str) -> list[str]:
    """Return all wikilink targets found in content (deduplicated, order-preserved)."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _WIKILINK_RE.finditer(content):
        name = m.group(1).strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _build_link_index(vault: str) -> dict[str, str]:
    """Map stem.lower() → absolute path for every non-skipped .md file in vault."""
    index: dict[str, str] = {}
    for f in Path(vault).rglob("*.md"):
        if not _should_skip_path(f):
            index[f.stem.lower()] = str(f)
    return index


def _resolve_links(names: list[str], index: dict[str, str]) -> dict[str, str | None]:
    """Resolve wikilink names to absolute paths using a prebuilt stem index.

    Handles both short names ([[note]]) and path-style names ([[folder/note]])
    by using only the stem component for lookup.
    """
    result: dict[str, str | None] = {}
    for name in names:
        stem = Path(name).stem.lower()
        result[name] = index.get(stem)
    return result


logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

_DEFAULT_IGNORED_PATH_SEGMENTS = {"archive"}
_ALWAYS_SKIPPED_PATH_SEGMENTS = {".obsidian", ".trash", ".git"}


# ───────────────────────────────── LRU Cache ─────────────────────────────────

class _TTLCache:
    """Simple LRU cache with TTL expiry for search results."""

    def __init__(self, maxsize: int = 256, ttl: int = 600):
        self._cache: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._maxsize = maxsize
        self._ttl = ttl

    def get(self, key: str) -> Any | None:
        if key not in self._cache:
            return None
        ts, value = self._cache[key]
        if time.monotonic() - ts > self._ttl:
            del self._cache[key]
            return None
        self._cache.move_to_end(key)
        return value

    def set(self, key: str, value: Any) -> None:
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = (time.monotonic(), value)
        if len(self._cache) > self._maxsize:
            self._cache.popitem(last=False)

    def invalidate(self) -> None:
        self._cache.clear()


_search_cache = _TTLCache(maxsize=256, ttl=600)


# ──────────────────────────────── Database ───────────────────────────────────

_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()

# Watcher observers — one per vault; held here so the shutdown handler can stop them.
_observers: list[Observer] = []


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    """Return the shared connection pool, initialising it on first call."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = psycopg2.pool.ThreadedConnectionPool(
                    1, 5, DATABASE_URL, connect_timeout=5
                )
    return _pool


@contextlib.contextmanager
def db_conn():
    """Acquire a connection from the pool and return it on exit.

    On exception the connection is discarded (close=True) so any open
    transaction is rolled back and the pool gets a fresh connection next time.
    """
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
    except Exception:
        # Return the connection as broken so the pool replaces it rather than
        # recycling a connection that may have an aborted transaction.
        pool.putconn(conn, close=True)
        raise
    else:
        pool.putconn(conn)


@contextlib.contextmanager
def reindex_lock():
    """Postgres advisory lock guarding re-index mutual exclusion.

    Replaces the process-local `threading.Lock` that used to live in
    dashboard.py: a `threading.Lock` only coordinates threads within one
    process, so the dashboard container and the MCP server container (or
    two dashboard replicas) could each acquire their own lock and run a
    full re-index concurrently — the Stage 6 HIGH finding this fixes.

    Session-level (`pg_try_advisory_lock` / `pg_advisory_unlock`), not
    `pg_advisory_xact_lock`: the lock must outlive the short transaction
    that acquires it and span the whole re-index pass, which can run for
    hours on a CPU-only Ollama.

    Held on a DEDICATED connection checked out from the pool for the whole
    critical section — never a connection returned to the pool while the
    lock is still held. Session-level advisory locks are bound to the
    session (the connection): returning the connection to the pool mid-hold
    would let some unrelated caller borrow that same connection and would
    silently release the lock as a side effect of that connection's next
    use, not of an explicit unlock call.

    Non-blocking: `pg_try_advisory_lock` returns immediately rather than
    waiting. Callers that get `False` must NOT proceed with the re-index —
    another holder already has it (dashboard's 409 / the MCP tool's "busy"
    message both key off this).

    Yields `False` (rather than raising) when no DB connection can be
    obtained at all, so a caller can treat "DB unreachable" and "lock held
    by someone else" the same way — neither should be started as if a lock
    signal were missing entirely.

    The connection pool is `ThreadedConnectionPool(1, 5)`. Holding one
    connection for the duration of a re-index leaves 4 free — acceptable,
    but do not raise the pool minimum without understanding this: a
    minconn bump would eagerly open connections this lock's hold pattern
    doesn't need released early.

    Usable both as `with reindex_lock() as acquired:` for a single
    acquire-do-release scope, and via manual `.__enter__()` / `.__exit__()`
    when the acquiring thread (an HTTP request handler) and the releasing
    thread (a background re-index worker) are different — the generator-
    based context manager object has no thread affinity of its own.
    """
    try:
        pool = _get_pool()
        conn = pool.getconn()
    except Exception as e:
        log.warning("reindex_lock: could not obtain a DB connection: %s", e)
        yield False
        return

    acquired = False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (REINDEX_LOCK_KEY,))
            acquired = bool(cur.fetchone()[0])
        conn.commit()
        yield acquired
    finally:
        if acquired:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (REINDEX_LOCK_KEY,))
                conn.commit()
                pool.putconn(conn)
            except Exception as e:
                log.warning("reindex_lock: failed to release advisory lock: %s", e)
                pool.putconn(conn, close=True)
        else:
            pool.putconn(conn)


def init_db(embed_dim: int = 768) -> None:
    # The `notes` table itself is created here, not in migrations.py: its
    # embedding column dimension is a runtime value (probed from the
    # configured Ollama model) that a static, versioned migration list
    # can't parameterize. Everything that doesn't depend on embed_dim
    # (note_links, the three pre-existing indexes, index_state) lives in
    # migrations.py — see its module docstring.
    with db_conn() as conn:
        with conn:
            with conn.cursor() as cur:
                # init_db runs on EVERY boot and issues ALTER TABLE, which needs
                # ACCESS EXCLUSIVE on `notes`. Unbounded, a single open reader
                # transaction (a dashboard search, an MCP query, a psql session
                # left idle in transaction) stalls startup indefinitely — and
                # every reader arriving after the blocked ALTER queues behind
                # it, so a slow boot escalates into a fully unavailable table.
                #
                # Observed 2026-07-20 against a real Postgres: `ALTER TABLE
                # notes ADD COLUMN IF NOT EXISTS content_tsv` blocked behind an
                # idle-in-transaction SELECT until the process was killed.
                # Bound the wait so boot fails fast and visibly instead.
                cur.execute("SET LOCAL lock_timeout = '5s';")
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
                # embed_dim is an int from the embedding model config, never request
                # input, and pgvector's vector(N) type doesn't accept a %s parameter
                # for its dimension — DDL, not a SQLi vector.
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS notes (
                        id          SERIAL PRIMARY KEY,
                        path        TEXT UNIQUE NOT NULL,
                        content     TEXT NOT NULL,
                        hash        TEXT NOT NULL,
                        embedding   vector({embed_dim}),
                        content_tsv tsvector,
                        vault_id    TEXT,
                        indexed_at  TIMESTAMP DEFAULT NOW()
                    );
                """)
                # Add columns to tables that predate them
                cur.execute("""
                    ALTER TABLE notes
                    ADD COLUMN IF NOT EXISTS content_tsv tsvector;
                """)
                cur.execute("""
                    ALTER TABLE notes
                    ADD COLUMN IF NOT EXISTS vault_id TEXT;
                """)
                # Backfill vault_id for rows indexed before multi-vault support
                if VAULT_PATH:
                    cur.execute(
                        "UPDATE notes SET vault_id = %s WHERE vault_id IS NULL",
                        (VAULT_PATH,),
                    )

    # Versioned, idempotent migrations — note_links, its index, the two
    # notes_* indexes, and index_state. Must run after the notes table
    # exists (migration 1's indexes reference its columns) and before the
    # auto-tune below (which needs index_state to already exist for
    # consistency, and note_links/notes_* indexes to be in place).
    with db_conn() as conn:
        migrations.apply_pending(conn)

    with db_conn() as conn:
        with conn:
            with conn.cursor() as cur:
                # Check existing embedding dimension vs current model.
                # Boot DETECTS a mismatch, it does NOT migrate — a container
                # restart must never be able to begin an hours-long re-embed.
                # The old column keeps serving search results exactly as
                # before; the operator runs `osm migrate --embedding-dim`
                # when ready (see migrations.py's migrate_embedding_dimension).
                cur.execute("""
                    SELECT format_type(a.atttypid, a.atttypmod)
                    FROM pg_attribute a
                    JOIN pg_class c ON c.oid = a.attrelid
                    WHERE c.relname = 'notes' AND a.attname = 'embedding'
                      AND a.attnum > 0 AND NOT a.attisdropped
                """)
                row = cur.fetchone()
                dimension_mismatch = False
                existing_dim = None
                if row:
                    m = re.search(r'vector\((\d+)\)', row[0])
                    if m:
                        existing_dim = int(m.group(1))
                        if existing_dim != embed_dim:
                            dimension_mismatch = True
                            log.warning(
                                "Embedding dimension mismatch: DB has vector(%d) but "
                                "%s produces %d. Existing index keeps serving on "
                                "vector(%d) — no automatic migration runs. Operator "
                                "action: `osm migrate --embedding-dim %d` migrates "
                                "without data loss.",
                                existing_dim, EMBED_MODEL, embed_dim, existing_dim, embed_dim,
                            )
                # Auto-tune IVFFlat lists based on vault size
                cur.execute("SELECT COUNT(*) FROM notes")
                note_count = cur.fetchone()[0]
                lists = max(10, min(note_count // 50, 500)) if note_count > 0 else 100
                lists = int(lists)
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS notes_embedding_idx "
                    "ON notes USING ivfflat (embedding vector_cosine_ops) "
                    f"WITH (lists = {int(lists)});"
                )
    log.info("Database initialised (IVFFlat lists=%d, embed_dim=%d)", lists, embed_dim)

    if dimension_mismatch:
        mismatch_error = (
            f"DB has vector({existing_dim}) but {EMBED_MODEL} produces "
            f"vector({embed_dim}). Run `osm migrate --embedding-dim {embed_dim}` "
            f"to migrate without data loss."
        )
        # Recorded per vault (index_state is keyed by vault_id) — the
        # mismatch is a database-wide condition, but every configured
        # vault's search is equally affected by it.
        for vp in VAULT_PATHS:
            set_index_state(vp, "dimension_mismatch", error=mismatch_error)


def _row_to_index_state_dict(row: tuple) -> dict:
    vault_id, status, started_at, finished_at, failed_paths, error = row
    return {
        "vault_id": vault_id,
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "failed_paths": list(failed_paths or []),
        "error": error,
    }


def set_index_state(
    vault_id: str,
    status: str,
    *,
    failed_paths: list[str] | None = None,
    error: str | None = None,
) -> None:
    """Upsert this vault's row in index_state.

    Called at the start ("indexing"), end ("idle" or "failed"), and
    exception path of background_init/index_vault so indexing progress and
    failures are visible across process/container boundaries — the
    module-global list this replaces was only ever readable by the process
    that set it, which is why the dashboard's rebuild-failure panel was
    structurally always empty (it runs in a separate container).

    Never raises — a failure writing observability data must not abort an
    in-flight index pass. Logs a warning and returns on any DB error.
    """
    try:
        with db_conn() as conn:
            with conn:
                with conn.cursor() as cur:
                    if status == "indexing":
                        cur.execute(
                            """
                            INSERT INTO index_state
                                (vault_id, status, started_at, finished_at, failed_paths, error)
                            VALUES (%s, %s, NOW(), NULL, %s, NULL)
                            ON CONFLICT (vault_id) DO UPDATE
                                SET status       = EXCLUDED.status,
                                    started_at   = NOW(),
                                    finished_at  = NULL,
                                    failed_paths = EXCLUDED.failed_paths,
                                    error        = NULL
                            """,
                            (vault_id, status, failed_paths or []),
                        )
                    else:
                        cur.execute(
                            """
                            INSERT INTO index_state
                                (vault_id, status, started_at, finished_at, failed_paths, error)
                            VALUES (%s, %s, NULL, NOW(), %s, %s)
                            ON CONFLICT (vault_id) DO UPDATE
                                SET status       = EXCLUDED.status,
                                    finished_at  = NOW(),
                                    failed_paths = EXCLUDED.failed_paths,
                                    error        = EXCLUDED.error
                            """,
                            (vault_id, status, failed_paths or [], error),
                        )
    except Exception as e:
        log.warning(
            "set_index_state failed for vault_id=%s status=%s: %s", vault_id, status, e
        )


def get_index_state(vault_id: str | None = None):
    """Return index_state row(s).

    With `vault_id`: a single dict, or None if that vault has no row yet.
    Without: a list of dicts, one per vault, ordered by vault_id.

    Unlike set_index_state, this DOES raise on a DB error — callers that
    must degrade gracefully (get_last_rebuild_failures, the /api/stats
    path) catch it explicitly rather than have failure silently disguised
    as "no vaults indexed yet."
    """
    with db_conn() as conn:
        with conn:
            with conn.cursor() as cur:
                if vault_id is not None:
                    cur.execute(
                        "SELECT vault_id, status, started_at, finished_at, failed_paths, error "
                        "FROM index_state WHERE vault_id = %s",
                        (vault_id,),
                    )
                    row = cur.fetchone()
                    return _row_to_index_state_dict(row) if row else None
                cur.execute(
                    "SELECT vault_id, status, started_at, finished_at, failed_paths, error "
                    "FROM index_state ORDER BY vault_id"
                )
                return [_row_to_index_state_dict(r) for r in cur.fetchall()]


def get_last_rebuild_failures() -> list[str]:
    """Union of failed_paths across every vault's index_state row.

    Falls back to an empty list on any DB error rather than raising —
    observability (the dashboard's rebuild-failure panel) must never break
    the /api/stats endpoint that surfaces it.
    """
    try:
        rows = get_index_state()
    except Exception as e:
        log.warning("get_last_rebuild_failures: could not read index_state: %s", e)
        return []
    failed: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for path in row["failed_paths"]:
            if path not in seen:
                seen.add(path)
                failed.append(path)
    return failed


# ──────────────────────────────── Embeddings ─────────────────────────────────

def _vec_to_str(vec: list[float]) -> str:
    """Format a float list as a pgvector literal, e.g. '[0.1,0.2,...]'."""
    if not vec:
        raise ValueError("Cannot convert empty list to vector literal")
    return "[" + ",".join(str(v) for v in vec) + "]"


def embed(text: str) -> list[float]:
    """Embed text with Ollama. Truncates to MAX_EMBED_CHARS to stay within model limits.

    Retries up to 3 times with exponential backoff (1s → 2s → 4s) on transient errors.
    """
    text = text[:MAX_EMBED_CHARS]
    for attempt in range(3):
        try:
            resp = requests.post(
                f"{OLLAMA_URL}/api/embeddings",
                json={"model": EMBED_MODEL, "prompt": text},
                timeout=EMBED_TIMEOUT,
            )
            resp.raise_for_status()
            vec = resp.json().get("embedding", [])
            if not vec:
                raise ValueError(f"Empty embedding returned by Ollama for text: {text[:50]!r}")
            return vec
        except (requests.RequestException, ValueError) as e:
            if attempt == 2:
                raise
            wait = 2 ** attempt
            log.warning("embed attempt %d failed: %s — retrying in %ds", attempt + 1, e, wait)
            time.sleep(wait)
    raise RuntimeError("embed: exhausted retries without raising — should not reach here")


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed multiple texts in a single Ollama /api/embed call (Ollama 0.4+).

    Returns embeddings in the same order as the input list. Truncates each
    text to MAX_EMBED_CHARS. Raises on transient errors after 3 attempts —
    the caller is responsible for falling back to per-item embed() if the
    batch endpoint isn't available (older Ollama) or returns an empty
    embedding for any item.
    """
    if not texts:
        return []
    truncated = [t[:MAX_EMBED_CHARS] for t in texts]
    for attempt in range(3):
        try:
            resp = requests.post(
                f"{OLLAMA_URL}/api/embed",
                json={"model": EMBED_MODEL, "input": truncated},
                timeout=EMBED_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            vecs = data.get("embeddings", [])
            if len(vecs) != len(truncated):
                raise ValueError(
                    f"Ollama batch returned {len(vecs)} embeddings for "
                    f"{len(truncated)} inputs"
                )
            return vecs
        except (requests.RequestException, ValueError) as e:
            if attempt == 2:
                raise
            wait = 2 ** attempt
            log.warning(
                "embed_batch attempt %d failed (%d items): %s — retrying in %ds",
                attempt + 1, len(truncated), e, wait,
            )
            time.sleep(wait)
    raise RuntimeError("embed_batch: exhausted retries — should not reach here")


def get_embed_dim() -> int:
    """Return the embedding dimension by probing Ollama. Falls back to 768 on failure."""
    try:
        return len(embed("test"))
    except Exception as e:
        log.warning("Could not determine embedding dimension: %s — using 768", e)
        return 768


# ───────────────────────────────── Indexing ──────────────────────────────────

def file_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _rerank_score(query: str, doc: str) -> float:
    """Call RERANK_MODEL to score (query, doc) relevance. Returns 0.0 on any failure."""
    prompt = (
        "Score how relevant the document is to the query. "
        "Output only a single decimal number between 0.0 and 1.0. Nothing else.\n\n"
        f"Query: {query}\n\nDocument: {doc[:400]}\n\nScore:"
    )
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": RERANK_MODEL, "prompt": prompt, "stream": False},
            timeout=EMBED_TIMEOUT,
        )
        resp.raise_for_status()
        text = resp.json().get("response", "0").strip()
        for token in text.split():
            try:
                return max(0.0, min(1.0, float(token)))
            except ValueError:
                continue
        return 0.0
    except Exception as e:
        log.warning("rerank_score failed: %s", e)
        return 0.0


def _rerank(query: str, rows: list[tuple], limit: int) -> list[tuple]:
    """Re-rank candidate rows with RERANK_MODEL cross-encoder, return top `limit`.

    Runs re-scoring in parallel (up to EMBED_WORKERS threads). Falls back to
    the original order if RERANK_MODEL is not configured.
    """
    if not RERANK_MODEL or not rows:
        return rows[:limit]

    scores: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=min(EMBED_WORKERS, len(rows))) as pool:
        futures = {
            pool.submit(_rerank_score, query, content): path
            for path, content, _ in rows
        }
        for fut in as_completed(futures):
            path = futures[fut]
            try:
                scores[path] = fut.result()
            except Exception:
                scores[path] = 0.0

    reranked = sorted(rows, key=lambda r: scores.get(r[0], 0.0), reverse=True)
    log.info("reranked %d candidates → top %d", len(rows), limit)
    return reranked[:limit]


def _bulk_load_hashes(paths: list[str]) -> dict[str, str]:
    """Fetch existing path→hash pairs in one DB query."""
    if not paths:
        return {}
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT path, hash FROM notes WHERE path = ANY(%s)", (paths,))
            return {row[0]: row[1] for row in cur.fetchall()}


def _upsert_note(path: str, content: str, h: str, vec: list[float], vault_id: str = "") -> None:
    """Upsert a single note row given a precomputed embedding vector. Retries
    on serialization deadlocks (40P01). Also extracts and stores wikilinks."""
    links = extract_wikilinks(content)
    with _link_index_lock:
        idx_snapshot = dict(_link_index)
    resolved = _resolve_links(links, idx_snapshot) if links else {}

    for attempt in range(3):
        try:
            with db_conn() as conn:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO notes (path, content, hash, embedding, content_tsv, vault_id, indexed_at)
                            VALUES (%s, %s, %s, %s::vector, to_tsvector('english', %s), %s, NOW())
                            ON CONFLICT (path) DO UPDATE
                                SET content     = EXCLUDED.content,
                                    hash        = EXCLUDED.hash,
                                    embedding   = EXCLUDED.embedding,
                                    content_tsv = EXCLUDED.content_tsv,
                                    vault_id    = EXCLUDED.vault_id,
                                    indexed_at  = NOW()
                        """, (path, content, h, _vec_to_str(vec), content, vault_id or None))
                        # Replace all wikilinks for this source in the same transaction
                        cur.execute(
                            "DELETE FROM note_links WHERE source_path = %s", (path,)
                        )
                        if resolved:
                            cur.executemany(
                                "INSERT INTO note_links (source_path, target_name, target_path) "
                                "VALUES (%s, %s, %s)",
                                [(path, name, tgt) for name, tgt in resolved.items()],
                            )
            return
        except psycopg2.Error as e:
            if e.pgcode == "40P01" and attempt < 2:  # deadlock — retry
                time.sleep(0.1 * (attempt + 1))
                continue
            raise


def _embed_and_upsert(path: str, content: str, h: str, vault_id: str = "") -> None:
    """Single-item embed + upsert. Kept for backwards compatibility and as
    the per-item fallback when batch embedding fails for any reason."""
    vec = embed(content)
    _upsert_note(path, content, h, vec, vault_id)
    log.info("Indexed: %s", path)


def _embed_and_upsert_batch(items: list[tuple[str, str, str]], vault_id: str = "") -> list[str]:
    """Embed a chunk of (path, content, hash) tuples in one Ollama call and
    upsert each. On batch failure (e.g. Ollama doesn't support /api/embed),
    falls back to single-item embed for the whole chunk so we never lose
    notes silently. Returns the list of paths that failed even with the
    single fallback — handed up to the caller for the v0.5.10 retry pass."""
    if not items:
        return []
    failed: list[str] = []
    try:
        vecs = embed_batch([content for _, content, _ in items])
    except Exception as e:
        log.warning(
            "Batch embed failed for %d items (%s) — falling back to single embed",
            len(items), e,
        )
        for path, content, h in items:
            try:
                _embed_and_upsert(path, content, h, vault_id)
            except Exception as inner:
                log.warning("Failed to index %s: %s", path, inner)
                failed.append(path)
        return failed

    for (path, content, h), vec in zip(items, vecs):
        if not vec:
            log.warning("Empty embedding from batch for %s — falling back to single embed", path)
            try:
                _embed_and_upsert(path, content, h, vault_id)
            except Exception as e:
                log.warning("Failed to index %s: %s", path, e)
                failed.append(path)
            continue
        try:
            _upsert_note(path, content, h, vec, vault_id)
            log.info("Indexed: %s", path)
        except Exception as e:
            log.warning("Failed to index %s: %s", path, e)
            failed.append(path)
    return failed


def _ignored_path_segments() -> set[str]:
    """Return ignored vault path segments.

    OBSIDIAN_IGNORE_PATHS replaces the default archive exclusion when set.
    Set it to an empty string to allow archive/ content to be indexed.
    """
    raw = os.environ.get("OBSIDIAN_IGNORE_PATHS")
    if raw is None:
        return set(_DEFAULT_IGNORED_PATH_SEGMENTS)
    return {segment.strip() for segment in raw.split(",") if segment.strip()}


def _should_skip_path(path: Path) -> bool:
    """Skip hidden/system directories and archive/ relative to any vault root.

    Falls back to VAULT_PATH when VAULT_PATHS is empty (test environments and
    single-vault setups that patch VAULT_PATH directly).
    """
    # _VAULT_LIST is computed at import time; fall back to current VAULT_PATH so
    # tests that monkey-patch VAULT_PATH after import still work correctly.
    vaults = _VAULT_LIST or ([VAULT_PATH] if VAULT_PATH else [])
    ignored_segments = _ignored_path_segments()
    for vp in vaults:
        try:
            rel = path.relative_to(Path(vp))
            return any(
                part.startswith(".")
                or part in _ALWAYS_SKIPPED_PATH_SEGMENTS
                or part in ignored_segments
                for part in rel.parts
            )
        except ValueError:
            continue
    return True  # not under any known vault — skip


# Backward-compatible alias used by existing tests
_is_system_path = _should_skip_path


def index_note(path: str, content: str, vault_id: str = "") -> None:
    """Embed a single note and upsert into the database. Skips unchanged files.

    The hash check uses a short-lived DB connection that is released before
    embedding — embedding can block for EMBED_TIMEOUT seconds and must never
    hold a pool slot.
    """
    h = file_hash(content)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT hash FROM notes WHERE path = %s", (path,))
            row = cur.fetchone()
            if row and row[0] == h:
                return  # unchanged — skip embedding call
    # DB connection released above before any network call.
    # _embed_and_upsert handles embed + write with its own connection and retry logic.
    _embed_and_upsert(path, content, h, vault_id)


def delete_note(path: str) -> None:
    with db_conn() as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM notes WHERE path = %s", (path,))
                cur.execute("DELETE FROM note_links WHERE source_path = %s", (path,))
    stem = Path(path).stem.lower()
    with _link_index_lock:
        _link_index.pop(stem, None)
    log.info("Removed: %s", path)


def _safe_delete_note(path: str) -> None:
    """delete_note(), guarded so a DB outage can never kill the watchdog
    observer thread. watchdog invokes handler callbacks directly on its
    observer thread with no supervising try/except of its own — an
    unguarded exception here (e.g. psycopg2.OperationalError while Postgres
    is unreachable) propagates out of the callback and silently ends all
    further event dispatch on that observer, which looks like indexing has
    just stopped rather than like a crash."""
    try:
        delete_note(path)
    except Exception as e:
        log.warning("Watcher: delete_note failed for %s: %s", path, e)


def _run_embed_pass(batch: list[tuple[str, str, str]], vault: str) -> list[str]:
    """Embed + upsert by chunking into EMBED_BATCH_SIZE-sized batches and
    submitting each batch to a worker pool. Each batch is one /api/embed
    call — saves one HTTP round-trip per note. Returns paths that failed
    even after the single-embed fallback inside _embed_and_upsert_batch."""
    if not batch:
        return []
    chunks = [batch[i:i + EMBED_BATCH_SIZE] for i in range(0, len(batch), EMBED_BATCH_SIZE)]
    failed: list[str] = []
    with ThreadPoolExecutor(max_workers=min(EMBED_WORKERS, len(chunks))) as pool:
        futures = [pool.submit(_embed_and_upsert_batch, chunk, vault) for chunk in chunks]
        for fut in as_completed(futures):
            try:
                failed.extend(fut.result())
            except Exception as e:
                log.warning("Batch worker crashed: %s", e)
    return failed


def prune_orphans() -> int:
    """Delete DB rows whose `path` no longer exists on disk. Returns the
    number of rows deleted. Resyncs `indexed_count` with `vault_file_count`
    after files are deleted, the vault path changes, or OBSIDIAN_IGNORE_PATHS
    is updated — the slow drift the watcher can't catch."""
    with db_conn() as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT path FROM notes")
                paths = [row[0] for row in cur.fetchall()]
                missing = [p for p in paths if not Path(p).exists()]
                if not missing:
                    return 0
                cur.execute("DELETE FROM notes WHERE path = ANY(%s)", (missing,))
    log.info("Pruned %d orphan rows from DB", len(missing))
    return len(missing)


def index_vault(vault: str) -> None:
    """Walk the vault and index every markdown file (parallel, hash-skipping)."""
    set_index_state(vault, "indexing")
    try:
        root = Path(vault)
        # rglob on a nonexistent directory yields nothing rather than raising,
        # so without this check a vault that vanished (unmounted NAS, deleted
        # folder, typo'd OBSIDIAN_VAULT) indexes zero notes and reports
        # SUCCESS — the exact silent-failure mode index_state exists to make
        # visible. Fail loudly instead so the except branch records it.
        if not root.is_dir():
            raise FileNotFoundError(
                f"Vault path does not exist or is not a directory: {vault}"
            )
        md_files = [f for f in root.rglob("*.md") if not _should_skip_path(f)]
        log.info("Indexing %d notes in %s…", len(md_files), vault)

        # Build link index before embedding so _upsert_note can resolve wikilinks.
        new_index = _build_link_index(vault)
        with _link_index_lock:
            _link_index.update(new_index)
        log.info("Link index built: %d entries for %s", len(new_index), vault)

        # Read all contents and compute hashes in the main thread (fast, no DB)
        file_data: list[tuple[str, str, str]] = []  # (path_str, content, hash)
        for f in md_files:
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
                file_data.append((str(f), content, file_hash(content)))
            except Exception as e:
                log.warning("Skipped reading %s: %s", f, e)

        # Single DB query to fetch all existing hashes
        paths = [item[0] for item in file_data]
        existing = _bulk_load_hashes(paths)

        # Filter to only files that are new or changed
        changed = [(p, c, h) for p, c, h in file_data if existing.get(p) != h]
        skipped = len(file_data) - len(changed)
        log.info("Changed: %d, Skipped (unchanged): %d", len(changed), skipped)

        # Parallel embed + upsert. Track failed paths so we can retry once —
        # Ollama tends to wedge under heavy concurrent load, and a single retry
        # after the first pass usually catches transient timeouts. Without this,
        # a wedged Ollama silently drops notes from a full rebuild.
        by_path = {p: (p, c, h) for p, c, h in changed}
        failed: list[str] = _run_embed_pass(list(by_path.values()), vault)

        if failed:
            log.warning("First pass had %d failures — retrying once", len(failed))
            retry_batch = [by_path[p] for p in failed if p in by_path]
            failed = _run_embed_pass(retry_batch, vault)

        if failed:
            log.warning("Indexing finished with %d persistent failures", len(failed))
            set_index_state(
                vault, "failed", failed_paths=failed,
                error=f"{len(failed)} note(s) failed to index after retry",
            )
        else:
            set_index_state(vault, "idle", failed_paths=[])

        # Rebuild IVFFlat index now that data exists — an index built on an empty
        # table has no list centroids and returns zero results.
        try:
            with db_conn() as conn:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute("REINDEX INDEX notes_embedding_idx;")
            log.info("Rebuilt IVFFlat index")
        except Exception as e:
            log.warning("Index rebuild skipped: %s", e)

        log.info("Vault indexing complete")
    except Exception as e:
        set_index_state(vault, "failed", error=str(e))
        raise


# ─────────────────────────────── File Watcher ────────────────────────────────

class VaultEventHandler(FileSystemEventHandler):

    def __init__(self, vault_id: str = ""):
        super().__init__()
        self._vault_id = vault_id
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def _schedule(self, path: str):
        """Debounce rapid events for the same path (e.g. Obsidian autosave)."""
        if not path.endswith(".md") or _should_skip_path(Path(path)):
            return
        with self._lock:
            existing = self._timers.pop(path, None)
            if existing:
                existing.cancel()
            t = threading.Timer(_DEBOUNCE_SECS, self._handle_upsert, args=(path,))
            self._timers[path] = t
            t.start()

    def _handle_upsert(self, path: str):
        with self._lock:
            self._timers.pop(path, None)
        try:
            content = Path(path).read_text(encoding="utf-8", errors="ignore")
            # Keep link index current so wikilink resolution stays accurate
            stem = Path(path).stem.lower()
            with _link_index_lock:
                _link_index[stem] = path
            index_note(path, content, self._vault_id)
        except FileNotFoundError:
            # File vanished between the debounce firing and this read (e.g.
            # a rapid create+delete, or an editor's atomic-save temp-file
            # dance). Recover via the guarded delete so a DB outage during
            # *this* recovery path cannot escape and kill the observer
            # thread — the same defect class this iteration fixes below in
            # on_deleted/on_moved, just reached via a different trigger.
            _safe_delete_note(path)
        except Exception as e:
            log.warning("Watcher: skipped %s: %s", path, e)

    def on_created(self, event):
        if not event.is_directory:
            self._schedule(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._schedule(event.src_path)

    def on_deleted(self, event):
        if not event.is_directory and event.src_path.endswith(".md") and not _should_skip_path(Path(event.src_path)):
            _safe_delete_note(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            if event.src_path.endswith(".md") and not _should_skip_path(Path(event.src_path)):
                _safe_delete_note(event.src_path)
            self._schedule(event.dest_path)


def _needs_polling(vault: str) -> bool:
    """Heuristic: return True if the vault path looks like a network filesystem.

    Network mounts (NFS, SMB/CIFS) on Windows appear as UNC paths or mapped
    drive letters backed by a network redirector.  OS-level filesystem events
    (ReadDirectoryChangesW on Windows, inotify on Linux) are unreliable or
    completely absent for writes made by *remote* clients on these mounts.
    PollingObserver is the only safe choice.

    On Linux, ``/proc/mounts`` is checked for nfs/cifs/smb mount types.
    On Windows, ``GetDriveType`` via ctypes is checked for DRIVE_REMOTE,
    with ``net use`` and ``wmic`` fallbacks for NFS client mounts that
    the standard network redirector does not register.
    """
    vp = Path(vault)
    # UNC paths are always network (\\server\share on Windows, //server/share on POSIX)
    vault_s = str(vp)
    if vault_s[:2] in ("\\\\", "//"):
        return True
    if sys.platform == "win32":
        try:
            import ctypes
            import subprocess
            drive_letter = str(vp.resolve()).split(":")[0]
            drive_root = drive_letter + ":\\"
            # Check 1: GetDriveTypeW (catches SMB/CIFS mapped drives)
            DRIVE_REMOTE = 4
            if ctypes.windll.kernel32.GetDriveTypeW(drive_root) == DRIVE_REMOTE:
                return True
            # Check 2: `net use` output (catches NFS and other mounts that
            # Windows NFS Client registers outside the standard redirector)
            result = subprocess.run(
                ["net", "use", drive_letter + ":"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and ("remote" in result.stdout.lower()
                                            or "\\\\" in result.stdout
                                            or "nfs" in result.stdout.lower()):
                return True
            # Check 3: WMI via wmic (last resort — catches all network drives)
            result = subprocess.run(
                ["wmic", "logicaldisk", "where", f"DeviceID='{drive_letter}:'",
                 "get", "DriveType", "/value"],
                capture_output=True, text=True, timeout=5,
            )
            # DriveType=4 is network drive in WMI
            if "DriveType=4" in result.stdout:
                return True
            return False
        except Exception:
            return False
    else:
        # Linux: check /proc/mounts for nfs/cifs
        try:
            mount_point = str(vp.resolve())
            with open("/proc/mounts", "r") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 3 and mount_point.startswith(parts[1]):
                        if parts[2] in ("nfs", "nfs4", "cifs", "smb", "smbfs", "9p", "fuse.sshfs"):
                            return True
        except (OSError, IndexError):
            pass
        return False


def start_watcher(vault: str) -> Observer | PollingObserver:
    """Start a filesystem watcher for the given vault.

    Uses PollingObserver when VAULT_WATCH_POLLING is "true" or when auto-
    detection identifies the vault as a network mount.  Falls back to the
    native Observer otherwise.
    """
    use_polling = (
        VAULT_WATCH_POLLING == "true"
        or (VAULT_WATCH_POLLING == "auto" and _needs_polling(vault))
    )
    if use_polling:
        obs = PollingObserver(timeout=VAULT_POLL_INTERVAL)
        log.info("Watching vault (polling, %ds interval): %s", VAULT_POLL_INTERVAL, vault)
    else:
        obs = Observer()
        log.info("Watching vault (native events): %s", vault)
    obs.schedule(VaultEventHandler(vault), vault, recursive=True)
    obs.start()
    _observers.append(obs)
    return obs


# ──────────────────────────── Background Init ────────────────────────────────

def background_init(vaults: list[str]):
    """Full index + start watchers for all vaults — runs in a background thread at startup."""
    time.sleep(1)  # give the MCP server a moment to start
    _INDEXING_IN_PROGRESS.set()
    try:
        embed_dim = get_embed_dim()
        init_db(embed_dim)
        for vault in vaults:
            index_vault(vault)
            start_watcher(vault)
    except Exception as e:
        log.error("Background init failed: %s", e)
    finally:
        _INDEXING_IN_PROGRESS.clear()


# ─────────────────────────── Shutdown Handler ────────────────────────────────

_STARTED_AT: float | None = None


def _log_external_kill_diagnostic(signame: str) -> None:
    """Log a hint when killed early — likely an MCP host Stop-hook regression.

    In Claude Code, the `Stop` event fires at the end of every assistant turn,
    not at session exit. If a Stop hook pkills MCP children, this server gets
    killed between turns. Run `verify-mcp-stop-hook` to detect the regression
    in the Claude Code settings file.
    """
    import time as _time
    if _STARTED_AT is None:
        return
    uptime = _time.monotonic() - _STARTED_AT
    log.warning(
        "obsidian-semantic-mcp received %s after %.1fs of uptime — exiting",
        signame, uptime,
    )
    if uptime < 60:
        log.warning(
            "Killed within 60s of startup. If this also fires every assistant turn, "
            "your MCP host is killing child processes via a Stop hook. Run "
            "`verify-mcp-stop-hook` to detect a regression in the Stop hook entry. "
            "The `Stop` event fires per-turn, not per-session."
        )


def _shutdown(signame: str = "signal"):
    """Stop the watcher and close the DB pool, then cancel the event loop.

    Called via loop.add_signal_handler() so it runs on the event loop thread,
    making it safe to call asyncio-adjacent code without deadlocking.
    Blocking operations (observer.join) are intentionally absent — the daemon
    thread will be killed when the process exits.
    """
    _log_external_kill_diagnostic(signame)
    log.info("Shutting down…")
    for obs in _observers:
        obs.stop()
    if _pool is not None:
        _pool.closeall()
    asyncio.get_event_loop().stop()


# ─────────────────────────── Wikilink Graph Expansion ───────────────────────

def expand_via_links(paths: list[str], hops: int = 1) -> list[tuple[str, str, str]]:
    """Return notes reachable within `hops` link-steps from `paths`.

    Traverses both outgoing ([[linked to]]) and incoming (linked from) edges.
    Returns (path, content, via_path) tuples where via_path is the note that
    bridged the connection. Excludes nodes already in `paths`.
    """
    seen: set[str] = set(paths)
    frontier: set[str] = set(paths)
    expansions: list[tuple[str, str, str]] = []

    for _ in range(hops):
        if not frontier:
            break
        frontier_list = list(frontier)
        seen_list = list(seen)
        with db_conn() as conn:
            with conn.cursor() as cur:
                # Outgoing: notes that the frontier links to
                cur.execute("""
                    SELECT nl.target_path, n.content, nl.source_path
                    FROM note_links nl
                    JOIN notes n ON n.path = nl.target_path
                    WHERE nl.source_path = ANY(%s)
                      AND nl.target_path IS NOT NULL
                      AND nl.target_path != ALL(%s)
                """, (frontier_list, seen_list))
                out_rows = cur.fetchall()

                # Incoming: notes that link back into the frontier
                cur.execute("""
                    SELECT nl.source_path, n.content, nl.target_path
                    FROM note_links nl
                    JOIN notes n ON n.path = nl.source_path
                    WHERE nl.target_path = ANY(%s)
                      AND nl.source_path != ALL(%s)
                """, (frontier_list, seen_list))
                in_rows = cur.fetchall()

        new_frontier: set[str] = set()
        for p, content, via in out_rows + in_rows:
            if p not in seen:
                expansions.append((p, content, via))
                seen.add(p)
                new_frontier.add(p)
        frontier = new_frontier

    return expansions


# ──────────────────────────── Vault Filesystem Helpers ───────────────────────

def _vault_root() -> Path:
    return Path(VAULT_PATH)


def _resolve_vault_path(relpath: str) -> Path:
    """Resolve a vault-relative path safely (no escaping the vault)."""
    resolved = (_vault_root() / relpath).resolve()  # resolve symlinks
    vault_resolved = _vault_root().resolve()
    if not resolved.is_relative_to(vault_resolved):
        raise ValueError(f"Path escapes vault: {relpath}")
    return resolved


def _ensure_frontmatter(content: str) -> str:
    """Guarantee every note written via write_file carries the mandatory
    frontmatter keys (see REQUIRED_FRONTMATTER_DEFAULTS + created/updated).

    `created` is set once and never overwritten. `updated` always reflects
    this write. Every other required key is added only if missing -- an
    already-present value (e.g. a caller-supplied `category`) is never
    silently replaced. Any extra keys the caller already set (project-
    specific fields) are preserved untouched.
    """
    today = datetime.now().date()

    body = content
    frontmatter: dict[str, object] = {}
    if content.startswith("---\n"):
        end = content.find("\n---", 4)
        if end != -1:
            raw_fm = content[4:end]
            body = content[end + 4:].lstrip("\n")
            frontmatter = yaml.safe_load(raw_fm) or {}

    frontmatter.setdefault("created", today)
    frontmatter["updated"] = today
    for key, default in REQUIRED_FRONTMATTER_DEFAULTS.items():
        frontmatter.setdefault(key, default)

    dumped = yaml.safe_dump(frontmatter, sort_keys=False, default_flow_style=False, allow_unicode=True)
    return f"---\n{dumped}---\n\n{body}"


def _relative(abspath: Path) -> str:
    """Return vault-relative path string. With multiple vaults, prefixes with vault basename."""
    vaults = _VAULT_LIST or ([VAULT_PATH] if VAULT_PATH else [])
    for vp in vaults:
        try:
            rel = abspath.relative_to(Path(vp))
            if len(vaults) > 1:
                return f"{Path(vp).name}/{rel}"
            return str(rel)
        except ValueError:
            continue
    return str(abspath)


# ───────────────────────────────── MCP Server ────────────────────────────────

server = Server("obsidian-semantic")


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="search_vault",
            description=(
                "Search across your Obsidian vault(s). "
                "Three modes: 'hybrid' (default) combines semantic meaning with keyword matching for best results; "
                "'semantic' searches by meaning only; 'keyword' matches exact words using full-text search. "
                "Use this to retrieve context, past decisions, notes, or research from the vault."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language search query",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of results to return (default: 5, max: 20)",
                        "default": 5,
                    },
                    "min_similarity": {
                        "type": "number",
                        "description": "Minimum similarity score (0.0–1.0). Results below this threshold are excluded. Default: 0.0",
                        "default": 0.0,
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["hybrid", "semantic", "keyword"],
                        "description": "Search mode: 'hybrid' (default) combines semantic + keyword; 'semantic' uses vector similarity only; 'keyword' uses full-text search only.",
                        "default": "hybrid",
                    },
                    "vault": {
                        "type": "string",
                        "description": "Filter results to a specific vault by its name (basename of vault path). Omit to search all vaults.",
                    },
                    "graph_expand": {
                        "type": "boolean",
                        "description": "Follow wikilinks from top results to surface connected notes that didn't rank semantically. Useful for discovering missed connections.",
                        "default": False,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="list_indexed_notes",
            description="List all notes that have been indexed, with their last indexed timestamp.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="reindex_vault",
            description=(
                "Force a full re-index of all notes in the vault. "
                "Runs in the background — use list_indexed_notes to check progress."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        # ── Vault CRUD tools ─────────────────────────────────────────────────
        Tool(
            name="list_files",
            description="List all files and directories in a vault directory. Defaults to vault root.",
            inputSchema={
                "type": "object",
                "properties": {
                    "dirpath": {
                        "type": "string",
                        "description": "Directory path relative to vault root (default: root)",
                        "default": "",
                    },
                },
            },
        ),
        Tool(
            name="get_file",
            description="Read the full content of a file in the vault.",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "File path relative to vault root",
                    },
                },
                "required": ["filepath"],
            },
        ),
        Tool(
            name="get_files_batch",
            description="Read the contents of multiple files at once.",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepaths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of file paths relative to vault root",
                    },
                },
                "required": ["filepaths"],
            },
        ),
        Tool(
            name="append_content",
            description="Append content to the end of a file. Creates the file if it doesn't exist.",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "File path relative to vault root",
                    },
                    "content": {
                        "type": "string",
                        "description": "Content to append",
                    },
                },
                "required": ["filepath", "content"],
            },
        ),
        Tool(
            name="write_file",
            description=(
                "Write or overwrite a file in the vault. Creates parent directories if needed. "
                "WARNING: overwrites existing content without confirmation — use append_content "
                "if you want to add to an existing file without replacing it."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "File path relative to vault root",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full content to write",
                    },
                },
                "required": ["filepath", "content"],
            },
        ),
        Tool(
            name="simple_search",
            description=(
                "Text/keyword search across vault files. "
                "Use search_vault for semantic/meaning-based search, "
                "use this for exact text matching."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Text to search for (case-insensitive)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default: 10)",
                        "default": 10,
                    },
                    "context_length": {
                        "type": "integer",
                        "description": "Characters of context around each match (default: 100)",
                        "default": 100,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_note_connections",
            description=(
                "Return all notes connected to a given note via wikilinks — "
                "both notes it links to (outgoing) and notes that link back to it (incoming). "
                "Useful for exploring your knowledge graph and finding related notes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "Vault-relative path to the note (e.g. 'notes/concepts/resilience.md')",
                    },
                    "hops": {
                        "type": "integer",
                        "description": "How many link-hops to traverse (default: 1, max: 2)",
                        "default": 1,
                    },
                },
                "required": ["filepath"],
            },
        ),
        Tool(
            name="recent_changes",
            description="Get recently modified files in the vault.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max files to return (default: 10)",
                        "default": 10,
                    },
                    "days": {
                        "type": "integer",
                        "description": "Only files modified within this many days (default: 30)",
                        "default": 30,
                    },
                },
            },
        ),
    ]


# SECURITY: MCP protocol has no built-in auth. Access control relies on
# the transport layer (stdio). Do not expose this server over network without auth proxy.
@server.call_tool()
async def call_tool(name: str, arguments: dict):

    # ── search_vault ──────────────────────────────────────────────────────────
    if name == "search_vault":
        query = arguments.get("query", "").strip()
        limit = max(1, min(int(arguments.get("limit", 5)), 20))
        min_similarity = float(arguments.get("min_similarity", 0.0))
        mode = arguments.get("mode", "hybrid")
        vault_filter = arguments.get("vault", "").strip()
        graph_expand = bool(arguments.get("graph_expand", False))
        if mode not in ("hybrid", "semantic", "keyword"):
            mode = "hybrid"

        if not query:
            return [TextContent(type="text", text="Please provide a search query.")]

        # Resolve vault filter: match by name (basename) or full path
        vault_ids: list[str] | None = None
        if vault_filter:
            vault_ids = [v for v in VAULT_PATHS
                         if v == vault_filter or os.path.basename(v) == vault_filter]
            if not vault_ids:
                return [TextContent(
                    type="text",
                    text=f"No vault matching '{vault_filter}' found. "
                         f"Available: {', '.join(os.path.basename(v) for v in VAULT_PATHS)}",
                )]

        # Check LRU cache before hitting Ollama + DB
        cache_key = hashlib.sha256(
            f"{query}:{limit}:{min_similarity}:{mode}:{RERANK_MODEL}:{vault_filter}:{graph_expand}".encode()
        ).hexdigest()
        cached = _search_cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            _t0 = time.monotonic()
            loop = asyncio.get_running_loop()

            # When re-ranking is enabled, fetch a wider candidate pool first
            fetch_limit = max(limit, RERANK_CANDIDATES) if RERANK_MODEL else limit

            # Build optional vault filter clause. vault_clause is always one of these
            # two hardcoded literals (never request input) — every real value in the
            # three queries below is %s-parameterized. Not a SQLi vector.
            vault_clause = "AND vault_id = ANY(%s)" if vault_ids else ""
            vault_param  = (vault_ids,) if vault_ids else ()

            if mode == "keyword":
                # Full-text search only — no embedding needed
                with db_conn() as conn:
                    with conn:
                        with conn.cursor() as cur:
                            cur.execute(f"""
                                SELECT path, content,
                                       ts_rank(content_tsv, plainto_tsquery('english', %s)) AS similarity
                                FROM notes
                                WHERE content_tsv @@ plainto_tsquery('english', %s)
                                {vault_clause}
                                ORDER BY similarity DESC
                                LIMIT %s
                            """, (query, query) + vault_param + (fetch_limit,))
                            rows = cur.fetchall()
            else:
                vec = await loop.run_in_executor(None, embed, query)
                vec_str = _vec_to_str(vec)

                if mode == "semantic":
                    with db_conn() as conn:
                        with conn:
                            with conn.cursor() as cur:
                                cur.execute(f"""
                                    SELECT path, content,
                                           1 - (embedding <=> %s::vector) AS similarity
                                    FROM notes
                                    WHERE 1=1 {vault_clause}
                                    ORDER BY embedding <=> %s::vector
                                    LIMIT %s
                                """, (vec_str,) + vault_param + (vec_str, fetch_limit))
                                rows = cur.fetchall()
                else:  # hybrid
                    with db_conn() as conn:
                        with conn:
                            with conn.cursor() as cur:
                                cur.execute(f"""
                                    SELECT path, content,
                                           (1 - (embedding <=> %s::vector)) * 0.7 +
                                           COALESCE(ts_rank(content_tsv,
                                               plainto_tsquery('english', %s)), 0) * 0.3
                                           AS similarity
                                    FROM notes
                                    WHERE 1=1 {vault_clause}
                                    ORDER BY similarity DESC
                                    LIMIT %s
                                """, (vec_str, query) + vault_param + (fetch_limit,))
                                rows = cur.fetchall()

            # Optional cross-encoder re-ranking (runs only when RERANK_MODEL is set)
            rows = await loop.run_in_executor(None, _rerank, query, list(rows), limit)

            # Apply similarity threshold filter
            results = [r for r in rows if r[2] >= min_similarity]

            if not results:
                if _INDEXING_IN_PROGRESS.is_set():
                    return [TextContent(
                        type="text",
                        text="Vault indexing is in progress — no results yet. Try again in a moment.",
                    )]
                return [TextContent(
                    type="text",
                    text="No indexed notes found. Try running reindex_vault first.",
                )]

            parts = []
            for path, content, sim in results:
                rel = _relative(Path(path))
                preview = content[:600].strip()
                while "\n\n\n" in preview:
                    preview = preview.replace("\n\n\n", "\n\n")
                parts.append(f"**{rel}** _(similarity: {sim:.2f})_\n\n{preview}\n")

            if graph_expand and results:
                result_paths = [r[0] for r in results]
                neighbors = await loop.run_in_executor(
                    None, expand_via_links, result_paths, 1
                )
                if neighbors:
                    parts.append("\n**Wikilink neighbors** _(connected notes not in top results)_\n")
                    for npath, ncontent, via in neighbors:
                        nrel = _relative(Path(npath))
                        via_rel = _relative(Path(via))
                        npreview = ncontent[:300].strip()
                        while "\n\n\n" in npreview:
                            npreview = npreview.replace("\n\n\n", "\n\n")
                        parts.append(
                            f"**{nrel}** _(linked via {via_rel})_\n\n{npreview}\n"
                        )
                    log.info("graph_expand added %d neighbor(s)", len(neighbors))

            result = [TextContent(type="text", text="\n---\n".join(parts))]

            _duration_ms = int((time.monotonic() - _t0) * 1000)
            _query_hash = hashlib.sha256(query.encode()).hexdigest()[:8]
            log.info(
                "search mode=%s query_hash=%s limit=%d found=%d duration_ms=%d",
                mode, _query_hash, limit, len(results), _duration_ms,
            )

            _search_cache.set(cache_key, result)
            return result

        except Exception as e:
            log.error("search_vault error: %s", e)
            return [TextContent(type="text", text=f"Search error: {e}")]

    # ── list_indexed_notes ────────────────────────────────────────────────────
    elif name == "list_indexed_notes":
        try:
            with db_conn() as conn:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            SELECT path, indexed_at
                            FROM notes
                            ORDER BY indexed_at DESC
                        """)
                        rows = cur.fetchall()

            if not rows:
                return [TextContent(
                    type="text",
                    text="No notes indexed yet. Run reindex_vault to start.",
                )]

            lines = [f"**{len(rows)} notes indexed**\n"]
            for path, ts in rows:
                rel = _relative(Path(path))
                lines.append(f"- {rel}  _(indexed {ts.strftime(_TIMESTAMP_FMT)})_")

            return [TextContent(type="text", text="\n".join(lines))]

        except Exception as e:
            log.error("list_indexed_notes error: %s", e)
            return [TextContent(type="text", text=f"Error: {e}")]

    # ── reindex_vault ─────────────────────────────────────────────────────────
    elif name == "reindex_vault":
        if not VAULT_PATHS:
            return [TextContent(
                type="text",
                text="No vault configured. Set OBSIDIAN_VAULTS or OBSIDIAN_VAULT.",
            )]

        # Acquired here (synchronously, on the tool-call coroutine) so we can
        # report busy immediately; released inside the background thread once
        # indexing finishes — the acquiring and releasing call sites are
        # deliberately on different threads, see reindex_lock()'s docstring.
        lock_cm = reindex_lock()
        acquired = lock_cm.__enter__()
        if not acquired:
            lock_cm.__exit__(None, None, None)
            return [TextContent(
                type="text",
                text="Re-index already in progress (held by another process or container). "
                     "Use list_indexed_notes to check its progress.",
            )]

        _search_cache.invalidate()

        def _reindex_all():
            try:
                for vp in VAULT_PATHS:
                    index_vault(vp)
            finally:
                lock_cm.__exit__(None, None, None)

        threading.Thread(target=_reindex_all, daemon=True).start()

        vault_list = ", ".join(VAULT_PATHS)
        return [TextContent(
            type="text",
            text=(
                f"Re-indexing started in background for: {vault_list}\n"
                "Use list_indexed_notes to check progress."
            ),
        )]

    # ── list_files ─────────────────────────────────────────────────────────────
    elif name == "list_files":
        try:
            dirpath = arguments.get("dirpath", "")
            target = _resolve_vault_path(dirpath) if dirpath else _vault_root()
            if not target.is_dir():
                return [TextContent(type="text", text=f"Not a directory: {dirpath}")]

            entries = sorted(target.iterdir())
            lines = []
            for e in entries:
                if e.name.startswith("."):
                    continue
                rel = _relative(e)
                prefix = "📁 " if e.is_dir() else "📄 "
                lines.append(f"{prefix}{rel}")

            return [TextContent(type="text", text="\n".join(lines) or "Empty directory")]
        except Exception as e:
            return [TextContent(type="text", text=f"Error: {e}")]

    # ── get_file ──────────────────────────────────────────────────────────────
    elif name == "get_file":
        try:
            filepath = arguments.get("filepath", "")
            target = _resolve_vault_path(filepath)
            if not target.is_file():
                return [TextContent(type="text", text=f"File not found: {filepath}")]
            content = target.read_text(encoding="utf-8", errors="ignore")
            return [TextContent(type="text", text=content)]
        except Exception as e:
            return [TextContent(type="text", text=f"Error: {e}")]

    # ── get_files_batch ───────────────────────────────────────────────────────
    elif name == "get_files_batch":
        try:
            filepaths = arguments.get("filepaths", [])
            parts = []
            for fp in filepaths:
                target = _resolve_vault_path(fp)
                if target.is_file():
                    content = target.read_text(encoding="utf-8", errors="ignore")
                    parts.append(f"--- {fp} ---\n{content}")
                else:
                    parts.append(f"--- {fp} ---\n[File not found]")
            return [TextContent(type="text", text="\n\n".join(parts))]
        except Exception as e:
            return [TextContent(type="text", text=f"Error: {e}")]

    # ── append_content ────────────────────────────────────────────────────────
    elif name == "append_content":
        try:
            filepath = arguments.get("filepath", "")
            content = arguments.get("content", "")
            target = _resolve_vault_path(filepath)
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "a", encoding="utf-8") as f:
                f.write(content)
            log.info("Appended to: %s", filepath)
            return [TextContent(type="text", text=f"Appended to {filepath}")]
        except Exception as e:
            return [TextContent(type="text", text=f"Error: {e}")]

    # ── write_file ────────────────────────────────────────────────────────────
    elif name == "write_file":
        try:
            filepath = arguments.get("filepath", "")
            content = arguments.get("content", "")
            content = _ensure_frontmatter(content)
            target = _resolve_vault_path(filepath)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            log.info("Wrote: %s", filepath)
            return [TextContent(type="text", text=f"Wrote {filepath} ({len(content)} chars)")]
        except Exception as e:
            return [TextContent(type="text", text=f"Error: {e}")]

    # ── simple_search ─────────────────────────────────────────────────────────
    elif name == "simple_search":
        try:
            query = arguments.get("query", "").strip()
            limit = max(1, min(int(arguments.get("limit", 10)), 50))
            ctx_len = max(1, int(arguments.get("context_length", 100)))
            if not query:
                return [TextContent(type="text", text="Please provide a search query.")]

            query_lower = query.lower()
            results = []
            root = _vault_root()
            for f in root.rglob("*.md"):
                if _should_skip_path(f):
                    continue
                try:
                    text = f.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                text_lower = text.lower()
                idx = text_lower.find(query_lower)
                if idx == -1:
                    continue
                # Collect match contexts
                matches = []
                search_from = 0
                while len(matches) < 3:
                    idx = text_lower.find(query_lower, search_from)
                    if idx == -1:
                        break
                    start = max(0, idx - ctx_len)
                    end = min(len(text), idx + len(query) + ctx_len)
                    matches.append(text[start:end].strip())
                    search_from = idx + len(query)

                results.append((_relative(f), matches))
                if len(results) >= limit:
                    break

            if not results:
                return [TextContent(type="text", text=f"No matches for: {query}")]

            parts = []
            for rel, matches in results:
                match_text = "\n".join(f"  ...{m}..." for m in matches)
                parts.append(f"**{rel}**\n{match_text}")
            return [TextContent(type="text", text="\n\n".join(parts))]
        except Exception as e:
            return [TextContent(type="text", text=f"Error: {e}")]

    # ── get_note_connections ──────────────────────────────────────────────────
    elif name == "get_note_connections":
        try:
            filepath = arguments.get("filepath", "").strip()
            hops = max(1, min(int(arguments.get("hops", 1)), 2))
            if not filepath:
                return [TextContent(type="text", text="Please provide a filepath.")]

            target = _resolve_vault_path(filepath)
            abs_path = str(target)

            neighbors = await asyncio.get_running_loop().run_in_executor(
                None, expand_via_links, [abs_path], hops
            )

            if not neighbors:
                return [TextContent(
                    type="text",
                    text=f"No linked notes found for {filepath}. "
                         "The note may have no wikilinks, or linked notes are not yet indexed.",
                )]

            lines = [f"**{len(neighbors)} connected note(s)** for `{filepath}` (hops={hops})\n"]
            for npath, ncontent, via in neighbors:
                nrel = _relative(Path(npath))
                via_rel = _relative(Path(via))
                preview = ncontent[:200].strip().split("\n")[0]
                lines.append(f"- **{nrel}** _(via {via_rel})_ — {preview}")

            return [TextContent(type="text", text="\n".join(lines))]
        except Exception as e:
            return [TextContent(type="text", text=f"Error: {e}")]

    # ── recent_changes ────────────────────────────────────────────────────────
    elif name == "recent_changes":
        try:
            limit = min(int(arguments.get("limit", 10)), 100)
            days = int(arguments.get("days", 30))
            cutoff = time.time() - (days * 86400)
            root = _vault_root()

            files = []
            for f in root.rglob("*.md"):
                if _should_skip_path(f):
                    continue
                try:
                    mtime = f.stat().st_mtime
                    if mtime >= cutoff:
                        files.append((mtime, f))
                except Exception:
                    continue

            files.sort(key=lambda x: x[0], reverse=True)
            files = files[:limit]

            if not files:
                return [TextContent(type="text", text=f"No files modified in the last {days} days.")]

            lines = [f"**{len(files)} recently modified files** (last {days} days)\n"]
            for mtime, f in files:
                dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
                lines.append(f"- {_relative(f)}  _{dt.strftime(_TIMESTAMP_FMT)}_")

            return [TextContent(type="text", text="\n".join(lines))]
        except Exception as e:
            return [TextContent(type="text", text=f"Error: {e}")]

    return [TextContent(type="text", text=f"Unknown tool: {name}")]


# ──────────────────────────────── Entry Point ────────────────────────────────

async def main():
    if not VAULT_PATHS:
        log.error("No vault configured. Set OBSIDIAN_VAULTS or OBSIDIAN_VAULT.")
        sys.exit(1)

    log.info("Vaults: %s", ", ".join(VAULT_PATHS))

    import time as _time
    global _STARTED_AT
    _STARTED_AT = _time.monotonic()

    loop = asyncio.get_event_loop()
    if sys.platform != "win32":
        loop.add_signal_handler(signal.SIGTERM, lambda: _shutdown("SIGTERM"))
        loop.add_signal_handler(signal.SIGINT, lambda: _shutdown("SIGINT"))
        try:
            loop.add_signal_handler(signal.SIGHUP, lambda: _shutdown("SIGHUP"))
        except (AttributeError, NotImplementedError):
            pass
    else:
        # Windows: add_signal_handler is not implemented on ProactorEventLoop.
        # signal.signal() on the main thread is the fallback.
        signal.signal(signal.SIGINT, lambda *_: _shutdown("SIGINT"))
        signal.signal(signal.SIGTERM, lambda *_: _shutdown("SIGTERM"))

    # Full index + watchers start in background — server is immediately ready
    threading.Thread(
        target=background_init,
        args=(VAULT_PATHS,),
        daemon=True,
    ).start()

    # Raw stdin/stdout transport — avoids two known bugs:
    #   1. anyio.wrap_file() EOF death (May 7 2026): anyio's async-for over
    #      stdin exits when the client closes stdin between cycles, taking
    #      the server down. Solved by using a blocking readline that
    #      waits forever and treats EOF as "idle, retry."
    #   2. Blocking-loop event-loop freeze (May 8 2026): a synchronous
    #      `for line in sys.stdin.buffer:` in an async coroutine blocks
    #      the entire asyncio event loop, so `_stdout_writer` cannot
    #      schedule and the response is never written. Symptom: Claude
    #      Code times out at 30s during initialize over a pipe that
    #      stays open. Solved by offloading the blocking readline to a
    #      worker thread via anyio.to_thread.run_sync, which keeps the
    #      event loop free between reads.
    read_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_reader = anyio.create_memory_object_stream(0)

    async def _stdin_reader():
        async with read_writer:
            while True:
                # Thread-offload the blocking readline so the event loop
                # stays free for _stdout_writer and server.run.
                line = await anyio.to_thread.run_sync(sys.stdin.buffer.readline)
                if not line:
                    # EOF: stdin closed. Don't exit — Claude Desktop
                    # closes stdin between cycles. Sleep briefly and
                    # retry; the thread offload will block in-thread
                    # until new data arrives or stdin is fully gone.
                    await anyio.sleep(0.1)
                    continue
                line_str = line.decode("utf-8").strip()
                if not line_str:
                    continue
                try:
                    message = JSONRPCMessage.model_validate_json(line_str)
                    await read_writer.send(SessionMessage(message))
                except Exception as exc:
                    await read_writer.send(exc)

    async def _stdout_writer():
        async with write_reader:
            async for session_message in write_reader:
                json_str = session_message.message.model_dump_json(
                    by_alias=True, exclude_none=True
                )
                sys.stdout.write(json_str + "\n")
                sys.stdout.flush()

    async with anyio.create_task_group() as tg:
        tg.start_soon(_stdin_reader)
        tg.start_soon(_stdout_writer)
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def run_server():
    """Sync entry point for the ``obsidian-semantic-mcp`` console script."""
    asyncio.run(main())


if __name__ == "__main__":
    asyncio.run(main())
