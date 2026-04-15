# Obsidian Semantic MCP

Python MCP server — semantic search across an Obsidian vault using pgvector + Ollama.

Terminology: `osm` means the Obsidian Semantic MCP CLI (`osm init`, `osm dashboard`, etc.), not OpenStreetMap.

## Build & Run

```bash
# Full stack (postgres, ollama, mcp-server, dashboard)
OBSIDIAN_VAULT="/path/to/vault" docker compose up -d

# Rebuild after code changes
docker compose up -d --build mcp-server dashboard

# Wipe all data and re-index from scratch
docker compose down -v
```

## Test

```bash
uv run pytest -q
```

## Key Commands

```bash
# Install deps
uv sync

# Run server natively
OBSIDIAN_VAULT="/path/to/vault" DATABASE_URL="postgresql://localhost/obsidian_brain" uv run python3 src/server.py

# Run dashboard
OBSIDIAN_VAULT="/path/to/vault" uv run python3 src/dashboard.py
```

## osm CLI

```bash
osm init                                              # Interactive setup wizard
osm init --mode 3 --vault /path --persistent          # Non-interactive (agent/script friendly)
osm init --dry-run                                    # Preview all actions without making changes
osm status                                            # Check service health
osm dashboard                                         # Open monitoring dashboard in browser
osm tunnel                                            # Reconnect SSH tunnel (remote Ollama)
osm rebuild                                           # Rebuild Docker images
osm remove                                            # Stop services, wipe volumes and config
osm remove --yes                                      # Non-interactive teardown (agent/script friendly)
osm help                                              # Full flag reference
```

**init flags:** `--mode`, `--vault`, `--pg-password`, `--persistent` / `--no-persistent`, `--data-dir`, `--ssh-host`, `--ssh-user`, `--ssh-port`, `--ssh-key`, `--vault-remote`
**remove flags:** `--yes` (skip confirmation)

## Project Conventions

- DB access via `db_conn()` context manager — uses `ThreadedConnectionPool(1,5)`, never call `psycopg2.connect()` directly
- `_handle_upsert` must catch all exceptions — watchdog thread must never die
- Empty Ollama embeddings (`[]`) raise `ValueError` — never insert invalid vectors
- `_resolve_vault_path()` enforces vault root — no path traversal
- Logging uses `%s` lazy format — no f-strings in log calls
- `_INDEXING_IN_PROGRESS` flag gates first-boot search messages
