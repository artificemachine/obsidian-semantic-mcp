# Changelog

All notable changes to this project will be documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

---

## [0.4.0] — 2026-03-22

### Added
- Windows support in setup wizard — Docker-only modes with WSL2 backend detection
- Claude Desktop config path detection for Windows (`%APPDATA%\Claude\`)
- Windows uv installer in README Quick Start section

### Changed
- Dockerfile runs as non-root `appuser` (was root)
- `.dockerignore` expanded with IDE dirs, `.claude/`, `.superharness/`, secret file patterns
- `.gitignore` now excludes `.claude/` and `.superharness/` session directories

### Fixed
- README Quick Start now shows platform-specific uv install commands (macOS/Linux + Windows)

---

## [0.3.4] — 2026-03-20

### Changed
- Vault volume mounts no longer forced read-only (`:ro` removed) — enables write-back features

### Fixed
- README multi-vault example now matches docker-compose.yml (removed stale `:ro` flags)

---

## [0.3.3] — 2026-03-18

### Fixed
- Dashboard JS completely broken by bare `\n` in Python triple-quoted string (`s.db_error.split('\n')`) — caused silent JS parse failure on every page load (regression in 0.3.2)
- Dashboard stats stuck on `—` / "Fetching…" forever when PostgreSQL is unreachable — DB connection pool now has `connect_timeout=5`
- Dashboard fetch hangs indefinitely when services are down — `AbortController` timeout (15s) added; footer now shows `"Service unreachable — run: osm status"`
- `osm init` wizard loops forever on invalid input — typing `q`, `quit`, `exit`, or `skip` now exits cleanly; prompt hints show `(q to quit)`

### Added
- Status indicator dots now start grey on page load (visible before first fetch completes)
- `tests/test_dashboard_smoke.py` — offline JS/DOM static analysis + live HTTP smoke tests for the dashboard

---

## [0.3.2] — 2026-03-18

### Fixed
- Dashboard: PostgreSQL status now shows the actual error message (e.g. "authentication failed") instead of just "DOWN"

---

## [0.3.1] — 2026-03-18

### Fixed
- `osm init` no longer shows a false warning when `obsidian-semantic` MCP server is already registered — re-running from any project is now fully idempotent
- Claude Desktop config skips update if `obsidian-semantic` already present

### Changed
- `obsidian-semantic` is treated as a single global server shared across all projects — re-running `osm init` detects existing registration and informs the user instead of failing

---

## [0.3.0] — 2026-03-15

### Added
- LRU search cache (256-entry, 10-min TTL) — repeated queries skip Ollama entirely
- `min_similarity` parameter on `search_vault` — filter low-relevance results
- Embedding retry with exponential backoff (3 attempts, 1s → 2s)
- Configurable `EMBED_TIMEOUT` env var (default 15s)
- Structured search logging: query hash, result count, duration_ms
- IVFFlat `lists` auto-tuned from vault size (10–500 range)
- Search testing UI panel in dashboard — test queries without leaving the browser
- `/api/search` endpoint with `min_similarity` support
- Orphaned embeddings count in dashboard stats
- Ollama health check: 5s timeout, 10s result cache
- SSH tunnel connectivity test before launching tunnel (mode 4/3)
- Vault health check during `osm init` — warns if path has no `.md` files
- Ollama model verification after pull
- Docker pull progress streamed in real-time during setup
- `CONTRIBUTING.md` — dev setup, code style, commit conventions, PR checklist
- `ARCHITECTURE.md` — component map, design decisions, DB schema, data flows
- GitHub issue templates (bug report, feature request)
- CI workflow: run unit tests on push/PR (`.github/workflows/tests.yml`)
- CI workflow: publish Docker images on version tags (`.github/workflows/docker-hub.yml`)

### Changed
- Ollama and PostgreSQL ports restricted to `127.0.0.1` (localhost only)
- Resource limits added to all containers (postgres: 1GB, ollama: 4GB, server: 512MB, dashboard: 256MB)
- Log rotation enabled: 100MB max, 3 files per service
- Dashboard port configurable via `DASHBOARD_PORT` env var
- Internal bridge network (`obsidian-internal`) isolates container traffic
- All dependencies pinned to exact versions
- Python minimum bumped to 3.11
- `_get_db_stats` uses `db_conn()` pool (was calling `psycopg2.connect()` directly)
- Type hints added throughout `server.py` and `dashboard.py`

### Fixed
- Vault validation warns without blocking in `--vault`, `$OBSIDIAN_VAULT`, and interactive paths

---

## [0.2.0] — 2026-03-14

### Added
- 183 unit tests covering server, osm CLI wizard, and all user-facing decision paths
- `tests/test_osm_commands.py` — 129 tests for every osm command and install mode
- `tests/conftest.py` — shared `_reset()` helper extracted from both test suites
- Non-interactive `osm init` flags (`--mode`, `--vault`, `--pg-password`, `--persistent`, etc.) for script/agent use
- `--dry-run` flag — preview all actions without making any changes
- `osm remove` command — stop services, wipe volumes and config
- README "Using with Claude" section — example prompts and osm CLI command reference

### Fixed
- README test count updated to reflect current suite (183 tests)
- README Quick Start now mentions `--dry-run` tip
- Path containment check uses `Path.is_relative_to()` instead of `str.startswith()`
- LIMIT clamping assertion checks parameterized query tuple, not SQL string

---

## [0.1.0] — 2026-01-01

### Added
- Initial release
- Semantic search MCP server for Obsidian vaults (pgvector + Ollama)
- PostgreSQL connection pool (`ThreadedConnectionPool(1,5)`)
- Vault file watcher with debounce (watchdog)
- Full CRUD MCP tools: `search_vault`, `simple_search`, `list_files`, `get_file`, `get_files_batch`, `append_content`, `write_file`, `recent_changes`, `list_indexed_notes`, `reindex_vault`
- Monitoring dashboard at `http://localhost:8484`
- Docker Compose full-stack setup (postgres, ollama, mcp-server, dashboard)
- `osm init` interactive wizard — macOS modes 1–4, Linux modes 1–3
- SSH tunnel support for remote Ollama hosts (mode 4)
- sshfs vault mounting for remote vaults
- Persistent bind-mount volumes option (`--persistent`, `--data-dir`)
- Graceful shutdown handling
- Apache 2.0 license
