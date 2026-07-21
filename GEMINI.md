# Obsidian Semantic MCP — Gemini CLI Context

## Project
Obsidian Semantic MCP is a Python-based Model Context Protocol (MCP) server that provides semantic search capabilities for Obsidian vaults. It uses pgvector (PostgreSQL) for vector storage and Ollama for generating embeddings.

## Stack & Tools
- **Language:** Python 3.10+
- **MCP Framework:** `mcp`
- **Database:** PostgreSQL with `pgvector`
- **Embeddings:** Ollama (`nomic-embed-text` by default)
- **Environment:** `uv` for dependency management, Docker Compose for services
- **CLI Commands:**
  - `osm init`: Setup wizard (pgvector, Ollama, vault path)
  - `osm dashboard`: Launch monitoring dashboard
  - `osm status`: Check service health
  - `osm tunnel`: Reconnect SSH tunnel (for remote Ollama)
  - `uv run pytest`: Run unit and smoke tests
- **Docker:** `docker compose up -d` to start the full stack

## Operational Rules
- **osm CLI:** The `osm` command is the primary management tool. Use `--yes` for non-interactive teardown.
- **DB Connection:** Always use the `db_conn()` context manager for database access; never connect directly via `psycopg2`.
- **Watchdog:** The `_handle_upsert` function must be resilient to prevent the file-watching thread from dying.
- **Indexing Gate:** Search tools are gated by the `_INDEXING_IN_PROGRESS` flag to ensure valid results during first-boot.
- **Vault Root:** Path traversal is prevented by `_resolve_vault_path()`; all operations are scoped to the vault root.

## Workspace Conventions
- `CHANGELOG.md` is append-only and required per commit.
- Never edit `.env`, credentials, or secrets.
- Use `shux contract` for task management.
