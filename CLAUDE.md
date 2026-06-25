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

## Version

Current: `0.12.2` (pyproject.toml). Entry points: `osm` (CLI via `osm_init:main`), `obsidian-semantic-mcp` (MCP server via `src.launcher:main`).

## Key Files

| File | Purpose |
|------|---------|
| `src/server.py` | MCP server — tools, watchdog, embedding pipeline |
| `src/launcher.py` | Path-agnostic entry point; Docker + native mode |
| `src/dashboard.py` | Starlette monitoring dashboard |
| `src/config.py` | Shared configuration helpers |
| `osm_init.py` | `osm` CLI wizard (init/status/remove/rebuild/tunnel) |
| `obsidian_semantic_mcp.py` | Thin wrapper for pip-installed native launch |
| `docker-compose.yml` | Full stack (postgres, ollama, mcp-server, dashboard) |
| `install.sh` / `install.ps1` | Bootstrap installer (macOS/Linux / Windows) |
| `tests/` | Unit, osm-init, osm-commands, dashboard smoke tests |

## Environment Variables

| Variable | Required | Notes |
|----------|----------|-------|
| `OBSIDIAN_VAULT` | Yes (or `OBSIDIAN_VAULTS`) | Path to a single vault |
| `OBSIDIAN_VAULTS` | Yes (or `OBSIDIAN_VAULT`) | Comma-separated paths for multi-vault mode |
| `DATABASE_URL` | Yes (or `POSTGRES_PASSWORD`) | Full postgres connection string |
| `POSTGRES_PASSWORD` | Yes (or `DATABASE_URL`) | Used by launcher to build a default `DATABASE_URL` |
| `OSM_DOCKER` | No | Set to `1` to enable Docker mode in launcher |
| `OSM_PROJECT_ROOT` | No (Docker mode) | Path to docker-compose project dir |
| `OSM_DOCKER_WAIT` | No | Seconds to poll for container; default 30 |

## Guardrails

- Never hardcode vault paths or postgres credentials — all config via env vars or `osm init`
- Never call `psycopg2.connect()` directly — always use the `db_conn()` context manager
- Config state lives in `~/.config/obsidian-semantic-mcp/` — never in the repo checkout
- The installed binary must not depend on the repo path (see Strict Installation Decoupling)
- Never edit `.env`, credentials, or secrets in CI or committed files
- Do not push directly to `main` — use a feature branch and PR

## Project Conventions

- DB access via `db_conn()` context manager — uses `ThreadedConnectionPool(1,5)`, never call `psycopg2.connect()` directly
- `_handle_upsert` must catch all exceptions — watchdog thread must never die
- Empty Ollama embeddings (`[]`) raise `ValueError` — never insert invalid vectors
- `_resolve_vault_path()` enforces vault root — no path traversal
- Logging uses `%s` lazy format — no f-strings in log calls
- `_INDEXING_IN_PROGRESS` flag gates first-boot search messages

## Strict Installation Decoupling

Once installed (e.g., to ~/.local/bin), the project binary must NEVER depend on the local repository path for execution, configuration, or data. All paths must be relative to the installation root or use standard system config paths (~/.config).
