#!/usr/bin/env python3
"""
Monitoring dashboard for obsidian-semantic-mcp.

Usage:
    source .venv/bin/activate
    OBSIDIAN_VAULT="/path/to/vault" python3 src/dashboard.py

    Open http://localhost:8484 in your browser.
"""
from __future__ import annotations

import hmac
import http.server
import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests

try:
    from .config import build_dsn, _redact_dsn, resolve_dashboard_token  # installed as a package (uv tool / pip install)
    from .server import db_conn, embed, index_vault, _vec_to_str, _relative, VAULT_PATHS, _should_skip_path, reindex_lock
except ImportError:
    from config import build_dsn, _redact_dsn, resolve_dashboard_token  # fallback: run directly from src/ during dev
    from server import db_conn, embed, index_vault, _vec_to_str, _relative, VAULT_PATHS, _should_skip_path, reindex_lock

# Importing server.py above already calls logging.basicConfig() — reuse the
# same handler/format rather than configuring a second one.
log = logging.getLogger(__name__)

VAULT_PATH  = VAULT_PATHS[0] if VAULT_PATHS else ""
OLLAMA_URL  = os.environ.get("OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL = os.environ.get("EMBEDDING_MODEL", "nomic-embed-text")
DASH_PORT   = int(os.environ.get("DASHBOARD_PORT", "8484"))
# Loopback by default — the dashboard exposes destructive mutating endpoints
# (see iteration 2's auth gate). Set DASHBOARD_BIND=0.0.0.0 explicitly to
# expose it beyond localhost (Docker sets this since the container's
# loopback isn't the host's).
DASHBOARD_BIND = os.environ.get("DASHBOARD_BIND", "127.0.0.1")

DATABASE_URL = build_dsn()


def _read_version() -> str:
    try:
        toml = (Path(__file__).parent.parent / "pyproject.toml").read_text()
        for line in toml.splitlines():
            if line.startswith("version"):
                return line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    return "dev"


APP_VERSION = _read_version()

# Bearer token guarding the mutating endpoints (/api/reindex, /api/reindex/full,
# /api/prune, /api/ollama/start). Resolved once at import time — see
# config.resolve_dashboard_token() for the DASHBOARD_TOKEN-env / config-file /
# generate-and-persist precedence.
DASHBOARD_TOKEN: str | None = None


def get_dashboard_token() -> str:
    """Resolve the bearer token on first use, caching it in DASHBOARD_TOKEN.

    Deliberately NOT resolved at import time. resolve_dashboard_token()
    generates and persists a token file on first call, so an import-time
    call would make merely importing this module write a secret to
    ~/.config/obsidian-semantic-mcp/ — which anything that imports for
    reasons of its own (test collection, a linter, an IDE indexer, a doc
    generator) would then trigger as a side effect. Resolve when a request
    actually needs the token instead.

    DASHBOARD_TOKEN stays a module attribute rather than a private cache so
    tests can monkeypatch it directly to a known value; a non-None value
    here short-circuits resolution entirely.
    """
    global DASHBOARD_TOKEN
    if DASHBOARD_TOKEN is None:
        DASHBOARD_TOKEN = resolve_dashboard_token()
    return DASHBOARD_TOKEN

# Ollama health cache: (result_dict, expiry_timestamp)
_ollama_cache: tuple[dict, float] | None = None
_ollama_cache_lock = threading.Lock()
_OLLAMA_CACHE_TTL = 10.0  # seconds

# Orphan check cache — counting missing files requires O(n) filesystem calls;
# cache for 5 minutes to avoid blocking the stats endpoint on every refresh.
_orphan_cache: tuple[int, float] | None = None
_orphan_cache_lock = threading.Lock()
_ORPHAN_CACHE_TTL = 300.0  # seconds

# Vault file count cache — rglob across the vault is slow on networked mounts
# (NAS, sshfs); cache to keep /api/stats fast under the dashboard auto-refresh.
_vault_count_cache: tuple[tuple, int, float] | None = None  # (key, total, expiry)
_vault_count_cache_lock = threading.Lock()
_VAULT_COUNT_CACHE_TTL = 30.0  # seconds


def search_notes(
    query: str,
    limit: int = 5,
    min_similarity: float = 0.0,
    mode: str = "hybrid",
    vault: str | None = None,
) -> list[dict]:
    """Search indexed notes. mode: 'hybrid' | 'semantic' | 'keyword'. vault: filter by vault name."""
    if mode not in ("hybrid", "semantic", "keyword"):
        mode = "hybrid"

    # Resolve vault filter to full path(s)
    vault_ids: list[str] | None = None
    if vault:
        vault_ids = [v for v in VAULT_PATHS
                     if v == vault or os.path.basename(v) == vault]

    vault_clause = "AND vault_id = ANY(%s)" if vault_ids else ""
    vault_param  = (vault_ids,) if vault_ids else ()

    # Compute the embedding BEFORE acquiring a DB connection — embed() can block
    # for up to EMBED_TIMEOUT seconds and must never hold a pool slot.
    vec_str: str | None = None
    if mode != "keyword":
        vec_str = _vec_to_str(embed(query))

    with db_conn() as conn:
        with conn.cursor() as cur:
            if mode == "keyword":
                # vault_clause is one of two hardcoded literals (never request input);
                # every real value below is %s-parameterized. Not a SQLi vector.
                cur.execute(f"""
                    SELECT path, content,
                           ts_rank(content_tsv, plainto_tsquery('english', %s)) AS similarity
                    FROM notes
                    WHERE content_tsv @@ plainto_tsquery('english', %s)
                    {vault_clause}
                    ORDER BY similarity DESC
                    LIMIT %s
                """, (query, query) + vault_param + (limit,))
            else:
                assert vec_str is not None
                if mode == "semantic":
                    # vault_clause is one of two hardcoded literals (never request input);
                    # every real value below is %s-parameterized. Not a SQLi vector.
                    cur.execute(f"""
                        SELECT path, content,
                               1 - (embedding <=> %s::vector) AS similarity
                        FROM notes
                        WHERE 1 - (embedding <=> %s::vector) >= %s
                        {vault_clause}
                        ORDER BY embedding <=> %s::vector
                        LIMIT %s
                    """, (vec_str, vec_str, min_similarity) + vault_param + (vec_str, limit))
                else:  # hybrid
                    # vault_clause is one of two hardcoded literals (never request input);
                    # every real value below is %s-parameterized. Not a SQLi vector.
                    cur.execute(f"""
                        SELECT path, content,
                               (1 - (embedding <=> %s::vector)) * 0.7 +
                               COALESCE(ts_rank(content_tsv,
                                   plainto_tsquery('english', %s)), 0) * 0.3
                               AS similarity
                        FROM notes
                        WHERE (1 - (embedding <=> %s::vector)) * 0.7 +
                              COALESCE(ts_rank(content_tsv,
                                  plainto_tsquery('english', %s)), 0) * 0.3 >= %s
                        {vault_clause}
                        ORDER BY similarity DESC
                        LIMIT %s
                    """, (vec_str, query, vec_str, query, min_similarity) + vault_param + (limit,))
            rows = cur.fetchall()

    results = []
    for path, content, sim in rows:
        if sim < min_similarity:
            continue
        preview = content[:400].strip()
        while "\n\n\n" in preview:
            preview = preview.replace("\n\n\n", "\n\n")
        results.append({
            "path": _relative(Path(path)),
            "content": content or "",
            "preview": preview,
            "similarity": round(float(sim), 3),
        })
    return results


def _get_db_stats(stats: dict) -> None:
    with db_conn() as conn:
        with conn.cursor() as cur:
            stats["db_ok"] = True

            cur.execute("SELECT version();")
            stats["pg_version"] = cur.fetchone()[0].split(",")[0]

            cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector';")
            row = cur.fetchone()
            stats["pgvector_version"] = row[0] if row else "not installed"

            cur.execute("SELECT COUNT(*), MAX(indexed_at), MIN(indexed_at) FROM notes;")
            count, last, oldest = cur.fetchone()
            stats["indexed_count"] = count or 0
            stats["last_indexed"] = last.isoformat() if last else None
            stats["oldest_indexed"] = oldest.isoformat() if oldest else None

            cur.execute("SELECT pg_total_relation_size('notes');")
            size = cur.fetchone()[0]
            stats["db_size_bytes"] = size
            if size < 1024:
                stats["db_size_human"] = f"{size} B"
            elif size < 1024 * 1024:
                stats["db_size_human"] = f"{size / 1024:.1f} KB"
            else:
                stats["db_size_human"] = f"{size / (1024 * 1024):.1f} MB"

            cur.execute(
                "SELECT path, indexed_at FROM notes ORDER BY indexed_at DESC LIMIT 10;"
            )
            all_vaults = VAULT_PATHS if VAULT_PATHS else ([VAULT_PATH] if VAULT_PATH else [])
            for path, ts in cur.fetchall():
                rel = path
                p = Path(path)
                for vp in all_vaults:
                    try:
                        rel = str(p.relative_to(vp))
                        break
                    except ValueError:
                        continue
                stats["recent_notes"].append(
                    {"path": rel, "indexed_at": ts.strftime("%Y-%m-%d %H:%M")}
                )

            # Fetch paths for orphan check outside the cursor — filesystem calls
            # happen after the DB connection is released (see below).
            cur.execute("SELECT path FROM notes")
            all_paths = [row[0] for row in cur.fetchall()]

    # O(n) filesystem check — cached for _ORPHAN_CACHE_TTL to avoid blocking
    # the stats endpoint on every 30-second dashboard refresh.
    global _orphan_cache
    now = time.monotonic()
    orphaned: int
    with _orphan_cache_lock:
        if _orphan_cache is not None and now < _orphan_cache[1]:
            orphaned = _orphan_cache[0]
        else:
            orphaned = sum(1 for p in all_paths if not os.path.exists(p))
            _orphan_cache = (orphaned, now + _ORPHAN_CACHE_TTL)
    stats["orphaned_embeddings"] = orphaned


def _get_vault_stats(stats: dict) -> None:
    global _vault_count_cache
    vaults = VAULT_PATHS if VAULT_PATHS else ([VAULT_PATH] if VAULT_PATH else [])
    if not vaults:
        return

    # Key the cache on the vault set + the ignore-paths env so any config
    # change invalidates immediately (and unit tests that swap vaults between
    # cases don't see stale counts).
    cache_key = (tuple(vaults), os.environ.get("OBSIDIAN_IGNORE_PATHS", "<unset>"))
    now = time.monotonic()
    with _vault_count_cache_lock:
        if _vault_count_cache is not None:
            cached_key, cached_total, expiry = _vault_count_cache
            if cached_key == cache_key and now < expiry:
                stats["vault_file_count"] = cached_total
                stats["unindexed_count"] = max(0, cached_total - stats["indexed_count"])
                return

    total = sum(
        1
        for vp in vaults
        for f in Path(vp).rglob("*.md")
        if not _should_skip_path(f)
    )
    with _vault_count_cache_lock:
        _vault_count_cache = (cache_key, total, now + _VAULT_COUNT_CACHE_TTL)
    stats["vault_file_count"] = total
    stats["unindexed_count"] = max(0, total - stats["indexed_count"])


def _get_ollama_stats(stats: dict) -> None:
    global _ollama_cache
    now = time.monotonic()

    with _ollama_cache_lock:
        if _ollama_cache is not None:
            cached_result, expiry = _ollama_cache
            if now < expiry:
                stats.update(cached_result)
                return

    # Fetch fresh result outside the lock to avoid blocking other threads
    result: dict = {}
    try:
        resp = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        models = [m["name"] for m in data.get("models", [])]
        result["ollama_ok"] = True
        result["model_loaded"] = any(EMBED_MODEL in m for m in models)
    except Exception as e:
        result["ollama_ok"] = False
        result["model_loaded"] = False
        result["ollama_error"] = str(e)

    with _ollama_cache_lock:
        _ollama_cache = (result, now + _OLLAMA_CACHE_TTL)

    stats.update(result)


def gather_stats() -> dict:
    stats = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "db_ok": False,
        "indexed_count": 0,
        "last_indexed": None,
        "oldest_indexed": None,
        "db_size_bytes": 0,
        "db_size_human": "—",
        "vault_file_count": 0,
        "unindexed_count": 0,
        "orphaned_embeddings": 0,
        "ollama_ok": False,
        "model_loaded": False,
        # True only in native mode (localhost) — Docker modes can't run `ollama serve`
        # inside the container, so the Start button is hidden for non-local Ollama URLs.
        "can_start_ollama": "localhost" in OLLAMA_URL,
        "reindex_busy": False,
        "last_rebuild_failed_count": 0,
        "last_rebuild_failed_sample": [],
        "dimension_mismatch": False,
        "dimension_mismatch_message": None,
        "recent_notes": [],
        "pg_version": "—",
        "pgvector_version": "—",
    }

    try:
        _get_db_stats(stats)
    except Exception as e:
        stats["db_error"] = str(e)

    try:
        _get_vault_stats(stats)
    except Exception:
        pass

    try:
        _get_ollama_stats(stats)
    except Exception as e:
        stats["ollama_error"] = str(e)

    # Probe the shared Postgres advisory lock rather than a process-local
    # threading.Lock — see reindex_lock()'s docstring. A probe acquire+
    # release in one `with` correctly reports busy=True whenever ANOTHER
    # process/container holds the lock, not just another thread in this one.
    with reindex_lock() as acquired:
        pass
    stats["reindex_busy"] = not acquired

    try:
        from server import get_last_rebuild_failures
        failed = get_last_rebuild_failures()
        stats["last_rebuild_failed_count"] = len(failed)
        stats["last_rebuild_failed_sample"] = failed[:5]
    except Exception:
        pass

    try:
        from server import get_index_state
        for row in get_index_state():
            if row["status"] == "dimension_mismatch":
                stats["dimension_mismatch"] = True
                stats["dimension_mismatch_message"] = row["error"]
                break
    except Exception:
        pass

    return stats


HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Obsidian Semantic MCP — Monitor</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Newsreader:ital,wght@0,400;0,500;0,700;1,400&family=Hanken+Grotesk:wght@400;500;600;700&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  /* Cobalt Grid palette, dashboard variant — cool blue-grey paper + navy-blue
     ink (see ../semblar/design_references/max_graphic_design_mood/cobalt_grid_01.json,
     recolored here from the source's warm-neutral paper for this UI) */
  :root {
    --paper: #E4E9F2; --paper-2: #D3DCEC; --ink: #1F3868; --ink-soft: #4F6FA8;
    --grid: rgba(31,56,104,.10); --ink-faint: rgba(31,56,104,.18);
    --serif: 'Newsreader', Georgia, serif;
    --sans: 'Hanken Grotesk', -apple-system, sans-serif;
    --mono: 'DM Mono', ui-monospace, monospace;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: var(--sans);
    background: var(--paper); color: var(--ink); padding: 24px;
    min-height: 100vh;
  }
  h1 { font-family: var(--serif); font-weight: 500; font-size: 1.6rem; color: var(--ink); margin-bottom: 8px; }
  .subtitle { font-size: 0.85rem; color: var(--ink-soft); margin-bottom: 24px; font-style: italic; }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 16px; margin-bottom: 24px;
  }
  .card {
    background: var(--paper-2); border-radius: 4px; padding: 20px;
  }
  .card-label { font-family: var(--sans); font-size: 0.75rem; text-transform: uppercase;
    letter-spacing: 0.08em; color: var(--ink-soft); font-weight: 600; margin-bottom: 8px; }
  .card-value { font-family: var(--mono); font-size: 1.8rem; font-weight: 500; }
  .card-detail { font-size: 0.8rem; color: var(--ink-soft); margin-top: 4px; }
  .status-row {
    display: flex; gap: 24px; margin-bottom: 24px; flex-wrap: wrap;
  }
  .status {
    display: flex; align-items: center; gap: 8px;
    font-size: 0.9rem; background: var(--paper-2); padding: 10px 16px;
    border-radius: 4px;
  }
  .dot {
    width: 10px; height: 10px; border-radius: 50%;
    display: inline-block; flex-shrink: 0;
  }
  .dot.green  { background: #22c55e; box-shadow: 0 0 6px #22c55e80; }
  .dot.red    { background: #ef4444; box-shadow: 0 0 6px #ef444480; }
  .dot.yellow { background: #eab308; box-shadow: 0 0 6px #eab30880; }
  .dot.grey   { background: #9a9488; }
  .recent { background: var(--paper-2); border-radius: 4px; padding: 20px;
    margin-bottom: 24px; }
  .recent h2 { font-family: var(--serif); font-weight: 500; font-size: 1.15rem; color: var(--ink); margin-bottom: 12px;
    cursor: pointer; user-select: none; display: flex; align-items: center; gap: 8px; }
  .recent h2 #recent-arrow { font-size: 0.8em; transition: transform 0.15s; display: inline-block; }
  .recent h2.collapsed #recent-arrow { transform: rotate(-90deg); }
  .recent-item {
    display: flex; justify-content: space-between; padding: 6px 0;
    border-bottom: 1px solid var(--ink-faint); font-size: 0.85rem;
  }
  .recent-item:last-child { border-bottom: none; }
  .recent-path { font-family: var(--mono); color: var(--ink); overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; max-width: 70%; }
  .recent-time { color: var(--ink-soft); white-space: nowrap; }
  .footer {
    text-align: center; margin-top: 24px; font-size: 0.75rem; color: var(--ink-soft);
  }
  .error-msg { color: #ef4444; font-size: 0.8rem; margin-top: 4px; }
  .btn {
    background: var(--ink); color: var(--paper); border: none; padding: 6px 14px;
    border-radius: 4px; font-size: 0.8rem; font-weight: 600;
    cursor: pointer; margin-left: 8px; transition: opacity 0.2s;
  }
  .btn:hover { opacity: 0.85; }
  .btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .btn-danger { background: #b3402f; color: #fff; }
  .btn-standalone { margin-left: 0; }
  .actions-row {
    display: flex; gap: 10px; margin-bottom: 24px; flex-wrap: wrap;
  }
  .hidden { display: none !important; }
  .indexing-banner {
    background: var(--paper-2); border: 1.5px solid var(--ink-soft); border-radius: 4px;
    padding: 10px 16px; margin-bottom: 24px; font-size: 0.85rem; color: var(--ink);
    display: flex; align-items: center; gap: 10px;
  }
  .spinner {
    width: 14px; height: 14px; border: 2px solid var(--ink-soft);
    border-top-color: transparent; border-radius: 50%;
    animation: spin 0.8s linear infinite; flex-shrink: 0;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .search-panel {
    background: var(--paper-2); border-radius: 4px; padding: 20px;
    margin-bottom: 24px;
  }
  .search-panel h2 { font-family: var(--serif); font-weight: 500; font-size: 1.15rem; color: var(--ink); margin-bottom: 14px; }
  .search-controls {
    display: flex; gap: 10px; flex-wrap: wrap; align-items: flex-end;
    margin-bottom: 14px;
  }
  .search-controls label {
    display: flex; flex-direction: column; gap: 4px;
    font-size: 0.75rem; color: var(--ink-soft); text-transform: uppercase;
    letter-spacing: 0.08em; font-weight: 600;
  }
  .search-controls input[type="text"] {
    background: var(--paper); border: 1.5px solid var(--ink-faint); border-radius: 4px;
    color: var(--ink); padding: 7px 10px; font-size: 0.85rem; width: 320px;
    outline: none; font-family: var(--sans);
  }
  .search-controls input[type="text"]:focus { border-color: var(--ink); }
  .search-controls input[type="number"] {
    background: var(--paper); border: 1.5px solid var(--ink-faint); border-radius: 4px;
    color: var(--ink); padding: 7px 10px; font-size: 0.85rem; width: 80px;
    outline: none; font-family: var(--mono);
  }
  .search-controls input[type="number"]:focus { border-color: var(--ink); }
  .search-result {
    border: 1.5px solid var(--ink-faint); border-radius: 4px; padding: 12px 14px;
    margin-bottom: 10px; background: var(--paper);
  }
  .search-result:last-child { margin-bottom: 0; }
  .search-result p { font-size: 0.82rem; color: var(--ink-soft); margin-top: 6px;
    line-height: 1.5; }
  .search-result code { font-family: var(--mono); font-size: 0.8rem; color: var(--ink); }
  @media (max-width: 600px) {
    .grid { grid-template-columns: 1fr 1fr; }
    .search-controls input[type="text"] { width: 100%; }
  }
</style>
</head>
<body>

<h1>Obsidian Semantic MCP</h1>
<p class="subtitle">Monitoring Dashboard {{VERSION}} — auto-refreshes every 30s</p>

<div class="status-row" id="statuses">
  <div class="status"><span class="dot grey" id="dot-db"></span><span id="lbl-db">PostgreSQL</span><span class="error-msg" id="err-db"></span></div>
  <div class="status"><span class="dot grey" id="dot-ollama"></span><span id="lbl-ollama">Ollama</span><button class="btn hidden" id="btn-ollama" onclick="startOllama()">Start</button></div>
  <div class="status"><span class="dot grey" id="dot-model"></span><span id="lbl-model">Embedding Model</span></div>
</div>

<div class="actions-row">
  <button class="btn btn-standalone" id="btn-reindex" onclick="triggerReindex(false)">Re-index</button>
  <button class="btn btn-standalone btn-danger" id="btn-rebuild" onclick="triggerReindex(true)">Clear &amp; Rebuild</button>
</div>

<div class="indexing-banner hidden" id="indexing-banner">
  <div class="spinner"></div>
  <span>Indexing in progress — stats will update on completion</span>
</div>

<div class="grid">
  <div class="card">
    <div class="card-label">Indexed Notes</div>
    <div class="card-value" id="v-indexed">—</div>
    <div class="card-detail" id="d-indexed"></div>
  </div>
  <div class="card">
    <div class="card-label">Vault Files</div>
    <div class="card-value" id="v-vault">—</div>
    <div class="card-detail" id="d-vault"></div>
  </div>
  <div class="card">
    <div class="card-label">Unindexed</div>
    <div class="card-value" id="v-gap">—</div>
    <div class="card-detail">files not yet embedded</div>
  </div>
  <div class="card">
    <div class="card-label">Orphaned Embeddings</div>
    <div class="card-value" id="v-orphaned">—</div>
    <div class="card-detail">in DB but not on disk</div>
  </div>
  <div class="card">
    <div class="card-label">DB Size</div>
    <div class="card-value" id="v-dbsize">—</div>
    <div class="card-detail" id="d-dbsize"></div>
  </div>
  <div class="card">
    <div class="card-label">Last Indexed</div>
    <div class="card-value" id="v-last">—</div>
    <div class="card-detail" id="d-last"></div>
  </div>
  <div class="card">
    <div class="card-label">pgvector</div>
    <div class="card-value" id="v-pgvec">—</div>
    <div class="card-detail" id="d-pgver"></div>
  </div>
</div>

<div class="recent">
  <h2 onclick="toggleRecent()" id="recent-toggle">Recently Indexed <span id="recent-arrow">&#9662;</span></h2>
  <div id="recent-list"><div class="recent-item"><span class="recent-path">Loading...</span></div></div>
</div>

<div class="search-panel">
  <h2>Test Search</h2>
  <div class="search-controls">
    <label>
      Query
      <input type="text" id="search-query" placeholder="Enter search query..." />
    </label>
    <label>
      Mode
      <select id="search-mode">
        <option value="hybrid" selected>Hybrid</option>
        <option value="semantic">Semantic</option>
        <option value="keyword">Keyword</option>
      </select>
    </label>
    <label id="vault-label" style="display:none">
      Vault
      <select id="search-vault"><option value="">All vaults</option></select>
    </label>
    <label>
      Limit
      <input type="number" id="search-limit" value="5" min="1" max="20" />
    </label>
    <label>
      Min Similarity
      <input type="number" id="search-min-sim" value="0.0" min="0.0" max="1.0" step="0.1" />
    </label>
    <button class="btn btn-standalone" onclick="testSearch()">Search</button>
  </div>
  <div id="search-results"></div>
</div>

<p class="footer" id="footer">Fetching...</p>

<script>
const DASHBOARD_TOKEN = "{{TOKEN}}";

function timeAgo(iso) {
  if (!iso) return '—';
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 60) return Math.floor(diff) + 's ago';
  if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
  if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
  return Math.floor(diff / 86400) + 'd ago';
}

function dot(el, ok) {
  el.className = 'dot ' + (ok ? 'green' : 'red');
}

function toggleRecent() {
  document.getElementById('recent-list').classList.toggle('hidden');
  document.getElementById('recent-toggle').classList.toggle('collapsed');
}

async function fetchStats() {
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 15000);
    const r = await fetch('/api/stats', { signal: ctrl.signal });
    clearTimeout(timer);
    const s = await r.json();

    dot(document.getElementById('dot-db'), s.db_ok);
    dot(document.getElementById('dot-ollama'), s.ollama_ok);
    dot(document.getElementById('dot-model'), s.model_loaded);

    document.getElementById('lbl-db').textContent =
      s.db_ok ? 'PostgreSQL' : 'PostgreSQL — DOWN';
    document.getElementById('err-db').textContent =
      (!s.db_ok && s.db_error) ? s.db_error.split('\\n')[0] : '';
    document.getElementById('lbl-ollama').textContent =
      s.ollama_ok ? 'Ollama' : 'Ollama — DOWN';
    document.getElementById('btn-ollama').classList.toggle('hidden', s.ollama_ok || !s.can_start_ollama);
    document.getElementById('lbl-model').textContent =
      s.model_loaded ? 'nomic-embed-text' : 'Model — NOT LOADED';

    document.getElementById('v-indexed').textContent = s.indexed_count;
    document.getElementById('v-vault').textContent = s.vault_file_count;
    document.getElementById('v-gap').textContent = s.unindexed_count;
    document.getElementById('v-orphaned').textContent =
      s.orphaned_embeddings !== undefined ? s.orphaned_embeddings : '—';
    document.getElementById('v-dbsize').textContent = s.db_size_human;
    document.getElementById('v-last').textContent = timeAgo(s.last_indexed);
    document.getElementById('d-last').textContent = s.last_indexed
      ? new Date(s.last_indexed).toLocaleString() : '';
    document.getElementById('v-pgvec').textContent = 'v' + s.pgvector_version;
    document.getElementById('d-pgver').textContent = s.pg_version;

    const coverage = s.vault_file_count > 0
      ? Math.round(s.indexed_count / s.vault_file_count * 100) : 0;
    document.getElementById('d-indexed').textContent = coverage + '% coverage';
    document.getElementById('d-vault').textContent = '.md files in vault';

    const list = document.getElementById('recent-list');
    if (s.recent_notes.length === 0) {
      list.innerHTML = '<div class="recent-item"><span class="recent-path">No notes indexed yet</span></div>';
    } else {
      list.innerHTML = '';
      s.recent_notes.forEach(n => {
        const row = document.createElement('div');
        row.className = 'recent-item';
        const pathEl = document.createElement('span');
        pathEl.className = 'recent-path';
        pathEl.textContent = n.path;
        const timeEl = document.createElement('span');
        timeEl.className = 'recent-time';
        timeEl.textContent = n.indexed_at;
        row.appendChild(pathEl);
        row.appendChild(timeEl);
        list.appendChild(row);
      });
    }

    // Keep banner in sync with actual reindex state — prevents it getting
    // stuck visible after a container restart or page reload.
    if (!s.reindex_busy) {
      document.getElementById('indexing-banner').classList.add('hidden');
    }

    document.getElementById('footer').textContent =
      'Last refresh: ' + new Date().toLocaleTimeString() + ' — auto-refresh 30s';

  } catch (e) {
    const msg = e.name === 'AbortError' ? 'timed out' : e.message;
    document.getElementById('footer').textContent =
      'Service unreachable (' + msg + ') — run: osm status';
    ['dot-db', 'dot-ollama', 'dot-model'].forEach(id => {
      document.getElementById(id).className = 'dot grey';
    });
  }
}

function pollReindexDone(id, label) {
  fetch('/api/reindex/status').then(r => r.json()).then(d => {
    if (!d.busy) {
      const btn = document.getElementById(id);
      btn.disabled = false;
      btn.textContent = label;
      document.getElementById('indexing-banner').classList.add('hidden');
      fetchStats();
    } else {
      setTimeout(() => pollReindexDone(id, label), 3000);
    }
  }).catch(() => {
    setTimeout(() => pollReindexDone(id, label), 5000);
  });
}

async function triggerReindex(full) {
  if (full && !confirm('Delete all embeddings and re-index from scratch?')) return;
  const id = full ? 'btn-rebuild' : 'btn-reindex';
  const label = full ? 'Clear & Rebuild' : 'Re-index';
  const btn = document.getElementById(id);
  btn.disabled = true;
  btn.textContent = 'Starting\u2026';
  try {
    const r = await fetch(full ? '/api/reindex/full' : '/api/reindex', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + DASHBOARD_TOKEN },
    });
    const d = await r.json();
    if (d.ok) {
      btn.textContent = 'Running\u2026';
      document.getElementById('indexing-banner').classList.remove('hidden');
      setTimeout(() => pollReindexDone(id, label), 3000);
    } else {
      btn.textContent = 'Failed: ' + (d.message || '');
      setTimeout(() => { btn.disabled = false; btn.textContent = label; }, 5000);
    }
  } catch (e) {
    btn.textContent = 'Error';
    setTimeout(() => { btn.disabled = false; btn.textContent = label; }, 5000);
  }
}

async function startOllama() {
  const btn = document.getElementById('btn-ollama');
  btn.disabled = true;
  btn.textContent = 'Starting...';
  try {
    const r = await fetch('/api/ollama/start', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + DASHBOARD_TOKEN },
    });
    const d = await r.json();
    btn.textContent = d.ok ? 'Started' : 'Failed';
    setTimeout(fetchStats, 3000);
  } catch (e) {
    btn.textContent = 'Error';
  }
  setTimeout(() => { btn.disabled = false; btn.textContent = 'Start'; }, 5000);
}

async function testSearch() {
  const q = document.getElementById('search-query').value.trim();
  if (!q) return;
  const limit = document.getElementById('search-limit').value;
  const minSim = document.getElementById('search-min-sim').value;
  const mode = document.getElementById('search-mode').value;
  const vault = document.getElementById('search-vault').value;
  let url = `/api/search?q=${encodeURIComponent(q)}&limit=${limit}&min_similarity=${minSim}&mode=${mode}`;
  if (vault) url += `&vault=${encodeURIComponent(vault)}`;
  const res = await fetch(url);
  const data = await res.json();
  const div = document.getElementById('search-results');
  if (!data.results || data.results.length === 0) {
    div.innerHTML = '<p>No results.</p>';
    return;
  }
  div.innerHTML = data.results.map(r => `
    <div class="search-result">
      <strong>${(r.similarity * 100).toFixed(1)}%</strong> \u2014 <code>${r.path}</code>
      <p>${r.content.substring(0, 200)}...</p>
    </div>
  `).join('');
}

async function fetchVaults() {
  try {
    const r = await fetch('/api/vaults');
    const d = await r.json();
    if (!d.vaults || d.vaults.length <= 1) return;
    const sel = document.getElementById('search-vault');
    d.vaults.forEach(v => {
      const opt = document.createElement('option');
      opt.value = v.name;
      opt.textContent = v.name;
      sel.appendChild(opt);
    });
    document.getElementById('vault-label').style.display = '';
  } catch (e) { /* single-vault — keep hidden */ }
}

fetchStats();
fetchVaults();
setInterval(fetchStats, 30000);
</script>
</body>
</html>"""

HTML_PAGE = HTML_PAGE.replace("{{VERSION}}", f"v{APP_VERSION}")
# {{TOKEN}} is deliberately NOT substituted here — see get_dashboard_token().
# Import-time substitution would force token resolution (and its file write)
# at import. The placeholder is filled per-request in _render_page().


def _render_page() -> bytes:
    """Return the dashboard HTML with the live bearer token injected."""
    return HTML_PAGE.replace("{{TOKEN}}", get_dashboard_token()).encode()


class DashboardHandler(http.server.BaseHTTPRequestHandler):

    def _json_response(self, code: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _require_auth(self) -> bool:
        """Gate mutating endpoints behind a bearer token.

        Returns True and does nothing further when the request carries a
        valid `Authorization: Bearer <token>` header. On any mismatch (header
        missing, malformed, or wrong token) writes a 401 in the same
        `{"ok": ..., "message": ...}` shape every other branch uses and
        returns False — callers must `return` immediately when this is False.

        GET endpoints intentionally do not call this: they are read-only and,
        as of iteration 1, loopback-bound by default. Gating them would
        require solving initial page-load auth, which is out of this plan's
        scope.
        """
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        supplied = header[len(prefix):] if header.startswith(prefix) else ""
        if not supplied or not hmac.compare_digest(supplied, get_dashboard_token()):
            log.warning("dashboard: unauthorized %s %s from %s", self.command, self.path, self.client_address[0])
            self._json_response(401, {"ok": False, "message": "unauthorized"})
            return False
        return True

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/search":
            qs = parse_qs(parsed.query)
            query = qs.get("q", [""])[0].strip()
            limit = min(int(qs.get("limit", ["5"])[0]), 20)
            min_similarity = float(qs.get("min_similarity", ["0.0"])[0])
            mode = qs.get("mode", ["hybrid"])[0]
            vault = qs.get("vault", [""])[0].strip() or None
            if not query:
                self._json_response(400, {"error": "missing ?q="})
                return
            try:
                results = search_notes(query, limit, min_similarity, mode, vault)
                self._json_response(200, {"query": query, "mode": mode, "vault": vault, "results": results})
            except Exception as e:
                log.error("dashboard: /api/search failed for query %r: %s", query, e)
                self._json_response(500, {"error": str(e)})
        elif parsed.path == "/api/vaults":
            vaults = [{"name": os.path.basename(v), "path": v} for v in VAULT_PATHS]
            self._json_response(200, {"vaults": vaults})
        elif parsed.path == "/api/reindex/status":
            with reindex_lock() as acquired:
                pass
            self._json_response(200, {"busy": not acquired})
        elif parsed.path == "/api/stats":
            self._json_response(200, gather_stats())
        else:
            body = _render_page()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def do_POST(self) -> None:
        if not self._require_auth():
            return
        path = urlparse(self.path).path
        if path == "/api/ollama/start":
            self._handle_ollama_start()
        elif path in ("/api/reindex", "/api/reindex/full"):
            self._handle_reindex(full=path == "/api/reindex/full")
        elif path == "/api/prune":
            self._handle_prune()
        else:
            self._json_response(404, {"error": "not found"})

    def _handle_ollama_start(self) -> None:
        try:
            subprocess.Popen(
                ["ollama", "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            log.info("dashboard: ollama serve started via /api/ollama/start")
            self._json_response(200, {"ok": True, "message": "ollama serve started"})
        except Exception as e:
            log.error("dashboard: /api/ollama/start failed: %s", e)
            self._json_response(500, {"ok": False, "message": str(e)})

    def _busy_response(self) -> None:
        self._json_response(409, {"ok": False, "message": "Re-index already in progress"})

    def _handle_reindex(self, *, full: bool) -> None:
        if not VAULT_PATHS:
            self._json_response(400, {"ok": False, "message": "No vault configured"})
            return

        # Acquired here, on the request-handling thread, so we can respond
        # 409 synchronously; released inside the background worker thread
        # once indexing finishes — see reindex_lock()'s docstring for why
        # acquire and release deliberately happen on different threads.
        lock_cm = reindex_lock()
        acquired = lock_cm.__enter__()
        if not acquired:
            lock_cm.__exit__(None, None, None)
            log.info("dashboard: reindex (full=%s) rejected — already in progress", full)
            self._busy_response()
            return

        log.info("dashboard: reindex (full=%s) started for %d vault(s)", full, len(VAULT_PATHS))

        def _run():
            try:
                if full:
                    with db_conn() as conn:
                        with conn:
                            with conn.cursor() as cur:
                                cur.execute("DELETE FROM notes;")
                for vp in VAULT_PATHS:
                    index_vault(vp)
                log.info("dashboard: reindex (full=%s) finished", full)
            except Exception:
                log.exception("dashboard: reindex (full=%s) failed", full)
            finally:
                lock_cm.__exit__(None, None, None)

        threading.Thread(target=_run, daemon=True).start()
        self._json_response(200, {"ok": True, "message": "started"})

    def _handle_prune(self) -> None:
        try:
            from server import prune_orphans
            n = prune_orphans()
            log.info("dashboard: /api/prune deleted %d orphaned note(s)", n)
            self._json_response(200, {"ok": True, "deleted": n})
        except Exception as e:
            log.error("dashboard: /api/prune failed: %s", e)
            self._json_response(500, {"ok": False, "message": str(e)})

    def log_message(self, format, *args):
        # Suppress default request logging
        pass


if __name__ == "__main__":
    server = http.server.HTTPServer((DASHBOARD_BIND, DASH_PORT), DashboardHandler)
    print(f"Dashboard running at http://{DASHBOARD_BIND}:{DASH_PORT}")
    print(f"Vault: {VAULT_PATH or '(not set)'}")
    print(f"Database: {_redact_dsn(DATABASE_URL)}")
    print("Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()
