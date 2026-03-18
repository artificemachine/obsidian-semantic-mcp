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
  EMBED_TIMEOUT     seconds before embed request times out (default: 15)
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
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
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from config import build_dsn


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
EMBED_TIMEOUT = int(os.environ.get("EMBED_TIMEOUT", "15"))
EMBED_WORKERS       = int(os.environ.get("EMBED_WORKERS", "4"))       # parallel embedding threads
RERANK_MODEL        = os.environ.get("RERANK_MODEL", "")               # cross-encoder model; empty = disabled
RERANK_CANDIDATES   = int(os.environ.get("RERANK_CANDIDATES", "20"))   # candidate pool size before re-ranking

DATABASE_URL = build_dsn()

MAX_EMBED_CHARS = 2000  # nomic-embed-text context limit (approx 512 tokens)
_TIMESTAMP_FMT  = "%Y-%m-%d %H:%M"
_DEBOUNCE_SECS  = 0.5   # collapse rapid saves from Obsidian autosave

# Set during background_init so search_vault can return a useful message
# instead of the misleading "No indexed notes found. Try running reindex_vault."
# threading.Event is used rather than a bare bool to avoid any cross-thread
# visibility issues without relying on the GIL.
_INDEXING_IN_PROGRESS = threading.Event()

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


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


def init_db(embed_dim: int = 768) -> None:
    with db_conn() as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
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
                # Index for per-vault filtered searches
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS notes_vault_idx ON notes (vault_id);
                """)
                # GIN index for full-text search
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS notes_tsv_idx
                    ON notes USING GIN (content_tsv);
                """)
                # Check existing embedding dimension vs current model
                cur.execute("""
                    SELECT format_type(a.atttypid, a.atttypmod)
                    FROM pg_attribute a
                    JOIN pg_class c ON c.oid = a.attrelid
                    WHERE c.relname = 'notes' AND a.attname = 'embedding'
                      AND a.attnum > 0 AND NOT a.attisdropped
                """)
                row = cur.fetchone()
                if row:
                    m = re.search(r'vector\((\d+)\)', row[0])
                    if m:
                        existing_dim = int(m.group(1))
                        if existing_dim != embed_dim:
                            log.warning(
                                "Embedding dimension mismatch: DB has vector(%d) but "
                                "%s produces %d. Run `docker compose down -v` to wipe "
                                "and reindex with the new model.",
                                existing_dim, EMBED_MODEL, embed_dim,
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


def _embed_and_upsert(path: str, content: str, h: str, vault_id: str = "") -> None:
    """Embed a note and upsert into DB. Used by parallel workers during bulk index."""
    for attempt in range(3):
        try:
            vec = embed(content)
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
            log.info("Indexed: %s", path)
            return
        except psycopg2.Error as e:
            if e.pgcode == "40P01" and attempt < 2:  # deadlock — retry
                time.sleep(0.1 * (attempt + 1))
                continue
            raise


def _should_skip_path(path: Path) -> bool:
    """Skip hidden/system directories (.obsidian, .trash, .git) relative to any vault root.

    Falls back to VAULT_PATH when VAULT_PATHS is empty (test environments and
    single-vault setups that patch VAULT_PATH directly).
    """
    # _VAULT_LIST is computed at import time; fall back to current VAULT_PATH so
    # tests that monkey-patch VAULT_PATH after import still work correctly.
    vaults = _VAULT_LIST or ([VAULT_PATH] if VAULT_PATH else [])
    for vp in vaults:
        try:
            rel = path.relative_to(Path(vp))
            return any(part.startswith(".") for part in rel.parts)
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
    log.info("Removed: %s", path)


def index_vault(vault: str) -> None:
    """Walk the vault and index every markdown file (parallel, hash-skipping)."""
    root = Path(vault)
    md_files = [f for f in root.rglob("*.md") if not _should_skip_path(f)]
    log.info("Indexing %d notes in %s…", len(md_files), vault)

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

    # Parallel embed + upsert
    errors = 0
    with ThreadPoolExecutor(max_workers=EMBED_WORKERS) as pool:
        futures = {pool.submit(_embed_and_upsert, p, c, h, vault): p for p, c, h in changed}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                fut.result()
            except Exception as e:
                log.warning("Failed to index %s: %s", p, e)
                errors += 1

    if errors:
        log.warning("Indexing finished with %d errors", errors)

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


# ─────────────────────────────── File Watcher ────────────────────────────────

class VaultEventHandler(FileSystemEventHandler):

    def __init__(self, vault_id: str = ""):
        super().__init__()
        self._vault_id = vault_id
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def _schedule(self, path: str):
        """Debounce rapid events for the same path (e.g. Obsidian autosave)."""
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
        if not path.endswith(".md") or _should_skip_path(Path(path)):
            return
        try:
            content = Path(path).read_text(encoding="utf-8", errors="ignore")
            index_note(path, content, self._vault_id)
        except FileNotFoundError:
            delete_note(path)
        except Exception as e:
            log.warning("Watcher: skipped %s: %s", path, e)

    def on_created(self, event):
        if not event.is_directory:
            self._schedule(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._schedule(event.src_path)

    def on_deleted(self, event):
        if not event.is_directory and event.src_path.endswith(".md"):
            delete_note(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            if event.src_path.endswith(".md"):
                delete_note(event.src_path)
            self._schedule(event.dest_path)


def start_watcher(vault: str) -> Observer:
    obs = Observer()
    obs.schedule(VaultEventHandler(vault), vault, recursive=True)
    obs.start()
    _observers.append(obs)
    log.info("Watching vault: %s", vault)
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

def _shutdown():
    """Stop the watcher and close the DB pool, then cancel the event loop.

    Called via loop.add_signal_handler() so it runs on the event loop thread,
    making it safe to call asyncio-adjacent code without deadlocking.
    Blocking operations (observer.join) are intentionally absent — the daemon
    thread will be killed when the process exits.
    """
    log.info("Shutting down…")
    for obs in _observers:
        obs.stop()
    if _pool is not None:
        _pool.closeall()
    asyncio.get_event_loop().stop()


# ──────────────────────────── Vault Filesystem Helpers ───────────────────────

def _vault_root() -> Path:
    return Path(VAULT_PATH)


def _resolve_vault_path(relpath: str) -> Path:
    """Resolve a vault-relative path safely (no escaping the vault)."""
    resolved = (_vault_root() / relpath).resolve()
    vault_resolved = _vault_root().resolve()
    if not resolved.is_relative_to(vault_resolved):
        raise ValueError(f"Path escapes vault: {relpath}")
    return resolved


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


@server.call_tool()
async def call_tool(name: str, arguments: dict):

    # ── search_vault ──────────────────────────────────────────────────────────
    if name == "search_vault":
        query = arguments.get("query", "").strip()
        limit = max(1, min(int(arguments.get("limit", 5)), 20))
        min_similarity = float(arguments.get("min_similarity", 0.0))
        mode = arguments.get("mode", "hybrid")
        vault_filter = arguments.get("vault", "").strip()
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
            f"{query}:{limit}:{min_similarity}:{mode}:{RERANK_MODEL}:{vault_filter}".encode()
        ).hexdigest()
        cached = _search_cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            _t0 = time.monotonic()
            loop = asyncio.get_running_loop()

            # When re-ranking is enabled, fetch a wider candidate pool first
            fetch_limit = max(limit, RERANK_CANDIDATES) if RERANK_MODEL else limit

            # Build optional vault filter clause
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

        _search_cache.invalidate()

        def _reindex_all():
            for vp in VAULT_PATHS:
                index_vault(vp)

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

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGTERM, _shutdown)
    loop.add_signal_handler(signal.SIGINT, _shutdown)

    # Full index + watchers start in background — server is immediately ready
    threading.Thread(
        target=background_init,
        args=(VAULT_PATHS,),
        daemon=True,
    ).start()

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
