# Architecture

## Overview

Obsidian Semantic MCP is a local-first semantic memory layer for Claude Desktop. It indexes your Obsidian vault as vector embeddings and exposes search + CRUD via the Model Context Protocol (MCP).

## Component Map

```
Claude Desktop
    │
    │  MCP protocol (stdio)
    ▼
src/server.py  ──────────────────────────────────────────────────────┐
    │                                                                 │
    ├── search_vault()      cosine similarity via pgvector            │
    ├── simple_search()     grep-style text search across .md files   │
    ├── list_files()        filesystem directory listing              │
    ├── get_file()          direct filesystem read                    │
    ├── get_files_batch()   parallel filesystem reads                 │
    ├── append_content()    filesystem append                         │
    ├── write_file()        filesystem write (vault-root enforced)    │
    ├── recent_changes()    mtime-sorted filesystem scan              │
    ├── list_indexed_notes() DB query                                 │
    └── reindex_vault()     triggers background re-index              │
         │                                                            │
         │  ThreadedConnectionPool(1,5)                               │
         ▼                                                            │
PostgreSQL + pgvector                           src/dashboard.py ─────┘
    │  IVFFlat index (cosine)                       │
    │  768-dim float vectors                        │  HTTP :8484
    │                                               ▼
    ▼                                           Browser UI
Ollama / nomic-embed-text                      /api/stats
    │  HTTP :11434                             /api/search
    │  768-dim embeddings                      /api/reindex/*
    ▼
Your Obsidian vault ($OBSIDIAN_VAULT)
    watchdog file watcher (debounce 0.5s)
```

## Key Design Decisions

### Why pgvector instead of a dedicated vector DB?

Pinecone, Weaviate, and Qdrant require running additional services or cloud accounts. pgvector runs inside the existing PostgreSQL container, keeping the stack at exactly three services (postgres, ollama, server). For vault sizes under 100K notes, pgvector's IVFFlat index is fast enough (sub-10ms queries).

### Why Ollama instead of API-based embeddings?

Local-first is a core requirement. `nomic-embed-text` produces 768-dim embeddings locally at no cost and with no data leaving the machine. API-based embeddings (OpenAI, Cohere) would require an API key, cost money, and break the privacy guarantee.

### Why watchdog for live sync?

The file watcher gives sub-second re-indexing on note saves without polling. The 0.5s debounce absorbs rapid successive saves (e.g., autosave on every keystroke) and collapses them into a single embedding operation.

### Why a unified server (search + CRUD in one process)?

Separating search and CRUD into two MCP servers would require Claude to switch contexts mid-conversation. A single server exposes all 10 tools under one MCP namespace, making it simpler to configure in `claude_desktop_config.json`.

### Why ThreadedConnectionPool instead of async DB?

The MCP library uses asyncio, but psycopg2 is synchronous. Using a thread pool (size 1–5) allows the async event loop to dispatch DB calls without blocking, while keeping the dependency stack simple (no asyncpg). For the expected load (one Claude Desktop session), 5 connections is sufficient.

### Why IVFFlat instead of HNSW?

IVFFlat is the default pgvector index and has lower build-time cost. HNSW has better recall and faster queries at scale, but requires more memory and longer build time. For vaults under 50K notes, the difference is negligible. HNSW is a natural upgrade path as vault size grows.

## Database Schema

```sql
CREATE TABLE notes (
    id          SERIAL PRIMARY KEY,
    path        TEXT UNIQUE NOT NULL,      -- relative path from vault root
    content     TEXT,                      -- raw markdown content
    embedding   vector(768),               -- nomic-embed-text output
    hash        TEXT,                      -- SHA-256 of content (skip unchanged)
    indexed_at  TIMESTAMP DEFAULT NOW()    -- last successful embed time
);

CREATE INDEX notes_embedding_idx
    ON notes USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
```

## Data Flow: Indexing

```
Vault file change (watchdog)
    │
    ├── read file → SHA-256 hash
    ├── compare hash to DB (skip if unchanged)
    ├── call Ollama /api/embeddings → 768-dim vector
    └── upsert into notes table (path, content, embedding, hash, indexed_at)
```

## Data Flow: Search

```
Claude calls search_vault(query, limit)
    │
    ├── call Ollama /api/embeddings(query) → query vector
    └── SELECT path, content, 1 - (embedding <=> query_vec) AS similarity
        FROM notes
        ORDER BY embedding <=> query_vec
        LIMIT limit
```

## Security Boundaries

- **Path traversal:** `_resolve_vault_path()` resolves symlinks and asserts the result is within `OBSIDIAN_VAULT` using `Path.is_relative_to()`.
- **No network exposure:** MCP server communicates via stdio only (no open port).
- **Vault isolation:** All file operations are scoped to `OBSIDIAN_VAULT`. No access to the broader filesystem.
- **Credentials:** Postgres password and vault path sourced from env vars / `.env` file (gitignored).

## Extension Points

- **Embedding backend:** Replace `embed()` in `server.py` with a different provider (HuggingFace, OpenAI) by swapping the HTTP call. The vector dimension must match the IVFFlat index (768 for nomic-embed-text).
- **Additional MCP tools:** Add new `@server.call_tool()` handlers in `server.py`. Register the tool schema in `list_tools()`.
- **Dashboard panels:** Add new `/api/<endpoint>` routes in `dashboard.py` and corresponding HTML panels in the `HTML` constant.
