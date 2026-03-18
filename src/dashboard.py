#!/usr/bin/env python3
"""
Monitoring dashboard for obsidian-semantic-mcp.

Usage:
    source .venv/bin/activate
    OBSIDIAN_VAULT="/path/to/vault" python3 src/dashboard.py

    Open http://localhost:8484 in your browser.
"""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests

from config import build_dsn
from server import db_conn, embed, index_vault, _vec_to_str, _relative, VAULT_PATHS

VAULT_PATH  = VAULT_PATHS[0] if VAULT_PATHS else ""
OLLAMA_URL  = os.environ.get("OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL = os.environ.get("EMBEDDING_MODEL", "nomic-embed-text")
DASH_PORT   = int(os.environ.get("DASHBOARD_PORT", "8484"))

DATABASE_URL = build_dsn()

_reindex_lock = threading.Lock()

# Ollama health cache: (result_dict, expiry_timestamp)
_ollama_cache: tuple[dict, float] | None = None
_ollama_cache_lock = threading.Lock()
_OLLAMA_CACHE_TTL = 10.0  # seconds

# Orphan check cache — counting missing files requires O(n) filesystem calls;
# cache for 5 minutes to avoid blocking the stats endpoint on every refresh.
_orphan_cache: tuple[int, float] | None = None
_orphan_cache_lock = threading.Lock()
_ORPHAN_CACHE_TTL = 300.0  # seconds


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
            for path, ts in cur.fetchall():
                try:
                    rel = str(Path(path).relative_to(VAULT_PATH)) if VAULT_PATH else path
                except ValueError:
                    rel = path
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
    if not VAULT_PATH:
        return
    vault = Path(VAULT_PATH)
    md_files = [
        f
        for f in vault.rglob("*.md")
        if not any(p.startswith(".") for p in f.relative_to(vault).parts)
    ]
    stats["vault_file_count"] = len(md_files)
    stats["unindexed_count"] = max(0, len(md_files) - stats["indexed_count"])


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

    acquired = _reindex_lock.acquire(blocking=False)
    if acquired:
        _reindex_lock.release()
    stats["reindex_busy"] = not acquired

    return stats


HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Obsidian Semantic MCP — Monitor</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: #1a1a2e; color: #e0e0e0; padding: 24px;
    min-height: 100vh;
  }
  h1 { font-size: 1.4rem; color: #a78bfa; margin-bottom: 8px; }
  .subtitle { font-size: 0.85rem; color: #666; margin-bottom: 24px; }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 16px; margin-bottom: 24px;
  }
  .card {
    background: #16213e; border-radius: 12px; padding: 20px;
    border: 1px solid #1a1a3e;
  }
  .card-label { font-size: 0.75rem; text-transform: uppercase;
    letter-spacing: 0.05em; color: #888; margin-bottom: 8px; }
  .card-value { font-size: 1.8rem; font-weight: 700; }
  .card-detail { font-size: 0.8rem; color: #666; margin-top: 4px; }
  .status-row {
    display: flex; gap: 24px; margin-bottom: 24px; flex-wrap: wrap;
  }
  .status {
    display: flex; align-items: center; gap: 8px;
    font-size: 0.9rem; background: #16213e; padding: 10px 16px;
    border-radius: 8px; border: 1px solid #1a1a3e;
  }
  .dot {
    width: 10px; height: 10px; border-radius: 50%;
    display: inline-block; flex-shrink: 0;
  }
  .dot.green { background: #22c55e; box-shadow: 0 0 6px #22c55e80; }
  .dot.red { background: #ef4444; box-shadow: 0 0 6px #ef444480; }
  .dot.yellow { background: #eab308; box-shadow: 0 0 6px #eab30880; }
  .recent { background: #16213e; border-radius: 12px; padding: 20px;
    border: 1px solid #1a1a3e; margin-bottom: 24px; }
  .recent h2 { font-size: 1rem; color: #a78bfa; margin-bottom: 12px; }
  .recent-item {
    display: flex; justify-content: space-between; padding: 6px 0;
    border-bottom: 1px solid #1a1a3e; font-size: 0.85rem;
  }
  .recent-item:last-child { border-bottom: none; }
  .recent-path { color: #c4b5fd; overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; max-width: 70%; }
  .recent-time { color: #666; white-space: nowrap; }
  .footer {
    text-align: center; margin-top: 24px; font-size: 0.75rem; color: #444;
  }
  .error-msg { color: #ef4444; font-size: 0.8rem; margin-top: 4px; }
  .btn {
    background: #a78bfa; color: #1a1a2e; border: none; padding: 6px 14px;
    border-radius: 6px; font-size: 0.8rem; font-weight: 600;
    cursor: pointer; margin-left: 8px; transition: opacity 0.2s;
  }
  .btn:hover { opacity: 0.85; }
  .btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .btn-danger { background: #ef4444; color: #fff; }
  .btn-standalone { margin-left: 0; }
  .actions-row {
    display: flex; gap: 10px; margin-bottom: 24px; flex-wrap: wrap;
  }
  .hidden { display: none !important; }
  .indexing-banner {
    background: #1e3a5f; border: 1px solid #3b82f6; border-radius: 8px;
    padding: 10px 16px; margin-bottom: 24px; font-size: 0.85rem; color: #93c5fd;
    display: flex; align-items: center; gap: 10px;
  }
  .spinner {
    width: 14px; height: 14px; border: 2px solid #3b82f6;
    border-top-color: transparent; border-radius: 50%;
    animation: spin 0.8s linear infinite; flex-shrink: 0;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .search-panel {
    background: #16213e; border-radius: 12px; padding: 20px;
    border: 1px solid #1a1a3e; margin-bottom: 24px;
  }
  .search-panel h2 { font-size: 1rem; color: #a78bfa; margin-bottom: 14px; }
  .search-controls {
    display: flex; gap: 10px; flex-wrap: wrap; align-items: flex-end;
    margin-bottom: 14px;
  }
  .search-controls label {
    display: flex; flex-direction: column; gap: 4px;
    font-size: 0.75rem; color: #888; text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .search-controls input[type="text"] {
    background: #1a1a2e; border: 1px solid #2a2a5e; border-radius: 6px;
    color: #e0e0e0; padding: 7px 10px; font-size: 0.85rem; width: 320px;
    outline: none;
  }
  .search-controls input[type="text"]:focus { border-color: #a78bfa; }
  .search-controls input[type="number"] {
    background: #1a1a2e; border: 1px solid #2a2a5e; border-radius: 6px;
    color: #e0e0e0; padding: 7px 10px; font-size: 0.85rem; width: 80px;
    outline: none;
  }
  .search-controls input[type="number"]:focus { border-color: #a78bfa; }
  .search-result {
    border: 1px solid #2a2a5e; border-radius: 8px; padding: 12px 14px;
    margin-bottom: 10px; background: #1a1a2e;
  }
  .search-result:last-child { margin-bottom: 0; }
  .search-result p { font-size: 0.82rem; color: #aaa; margin-top: 6px;
    line-height: 1.5; }
  .search-result code { font-size: 0.8rem; color: #c4b5fd; }
  @media (max-width: 600px) {
    .grid { grid-template-columns: 1fr 1fr; }
    .search-controls input[type="text"] { width: 100%; }
  }
</style>
</head>
<body>

<h1>Obsidian Semantic MCP</h1>
<p class="subtitle">Monitoring Dashboard — auto-refreshes every 30s</p>

<div class="status-row" id="statuses">
  <div class="status"><span class="dot" id="dot-db"></span><span id="lbl-db">PostgreSQL</span><span class="error-msg" id="err-db"></span></div>
  <div class="status"><span class="dot" id="dot-ollama"></span><span id="lbl-ollama">Ollama</span><button class="btn hidden" id="btn-ollama" onclick="startOllama()">Start</button></div>
  <div class="status"><span class="dot" id="dot-model"></span><span id="lbl-model">Embedding Model</span></div>
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
  <h2>Recently Indexed</h2>
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

async function fetchStats() {
  try {
    const r = await fetch('/api/stats');
    const s = await r.json();

    dot(document.getElementById('dot-db'), s.db_ok);
    dot(document.getElementById('dot-ollama'), s.ollama_ok);
    dot(document.getElementById('dot-model'), s.model_loaded);

    document.getElementById('lbl-db').textContent =
      s.db_ok ? 'PostgreSQL' : 'PostgreSQL — DOWN';
    document.getElementById('err-db').textContent =
      (!s.db_ok && s.db_error) ? s.db_error.split('\n')[0] : '';
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
    document.getElementById('footer').textContent = 'Fetch error: ' + e.message;
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
    const r = await fetch(full ? '/api/reindex/full' : '/api/reindex', { method: 'POST' });
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
    const r = await fetch('/api/ollama/start', { method: 'POST' });
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


class DashboardHandler(http.server.BaseHTTPRequestHandler):

    def _json_response(self, code: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
                self._json_response(500, {"error": str(e)})
        elif parsed.path == "/api/vaults":
            vaults = [{"name": os.path.basename(v), "path": v} for v in VAULT_PATHS]
            self._json_response(200, {"vaults": vaults})
        elif parsed.path == "/api/reindex/status":
            acquired = _reindex_lock.acquire(blocking=False)
            if acquired:
                _reindex_lock.release()
            self._json_response(200, {"busy": not acquired})
        elif parsed.path == "/api/stats":
            self._json_response(200, gather_stats())
        else:
            body = HTML_PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/ollama/start":
            try:
                subprocess.Popen(
                    ["ollama", "serve"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                self._json_response(200, {"ok": True, "message": "ollama serve started"})
            except Exception as e:
                self._json_response(500, {"ok": False, "message": str(e)})

        elif path in ("/api/reindex", "/api/reindex/full"):
            if not VAULT_PATHS:
                self._json_response(400, {"ok": False, "message": "No vault configured"})
                return
            if not _reindex_lock.acquire(blocking=False):
                self._json_response(409, {"ok": False, "message": "Re-index already in progress"})
                return

            full = path == "/api/reindex/full"

            def _run():
                try:
                    if full:
                        with db_conn() as conn:
                            with conn:
                                with conn.cursor() as cur:
                                    cur.execute("DELETE FROM notes;")
                    for vp in VAULT_PATHS:
                        index_vault(vp)
                finally:
                    _reindex_lock.release()

            threading.Thread(target=_run, daemon=True).start()
            self._json_response(200, {"ok": True, "message": "started"})

        else:
            self._json_response(404, {"error": "not found"})

    def log_message(self, format, *args):
        # Suppress default request logging
        pass


if __name__ == "__main__":
    server = http.server.HTTPServer(("0.0.0.0", DASH_PORT), DashboardHandler)
    print(f"Dashboard running at http://localhost:{DASH_PORT}")
    print(f"Vault: {VAULT_PATH or '(not set)'}")
    print(f"Database: {DATABASE_URL}")
    print("Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()
