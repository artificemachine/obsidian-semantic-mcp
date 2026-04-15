# Changelog

All notable changes to this project will be documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

## [0.5.8] — 2026-04-15

### Changed
- Add `nul` to `.gitignore` to prevent accidental Windows/Git-Bash redirection artifact from being tracked

---

## [0.5.7] — 2026-04-15

### Changed
- Clarify `osm` CLI terminology in `CLAUDE.md` and `AGENTS.md` to avoid confusion with OpenStreetMap
- Expand `README.md` with full `osm` command reference and flag documentation
- Add `CONTRIBUTING.md` guidelines

---

## [0.5.6] — 2026-04-14

### Changed
- Installer wizard and help banner now display the release version so users can verify the tagged build they are running

---

## [0.5.5] — 2026-04-14

### Added
- `osm dashboard` command — opens the monitoring dashboard (http://localhost:8484) in the default browser; warns if the stack is not running
- `osm init` now offers to install Docker Desktop automatically when missing (`brew` on macOS, `winget` on Windows, `get.docker.com` on Linux)
- `osm init` offers to start Docker Desktop when the daemon is not running and waits for it to become ready
- `[build-system]` added to `pyproject.toml` so `uv` registers the `osm` console script entry point

### Fixed
- Unicode output (box-drawing, checkmarks) no longer crashes on Windows cp1252 consoles — stdout/stderr are wrapped with UTF-8 encoding

### Changed
- Docker Hub CI workflow now runs tests before publishing and builds multi-arch images (amd64 + arm64)
- Tests workflow is now callable as a reusable workflow

---

## [0.5.4] — 2026-04-13

### Fixed
- `osm_init.py` Windows launcher parity: `_link_osm_to_path()` now writes `osm.cmd` (batch wrapper → `scripts\osm.ps1`) on Windows and a bash script on macOS/Linux; `_osm_launcher_path()` returns the platform-correct path; `cmd_remove()` deletes the right file on every platform
- Dashboard `_get_vault_stats()` now uses `_should_skip_path()` instead of a hand-rolled dotfile filter, so `archive/` and `OBSIDIAN_IGNORE_PATHS` are respected; counts now span all `VAULT_PATHS` in multi-vault mode
- Dashboard `_get_db_stats()` recent-notes paths are now relativized against the correct vault root in multi-vault mode
- `test_e2e.py` harness now validates `DATABASE_URL` or `POSTGRES_PASSWORD` is present before spawning the server subprocess, with a clear actionable error if missing
- CI `shipguard` scanner pinned as a dev dependency in `pyproject.toml` (managed via `uv.lock`) and removed from the workflow install step

### Changed
- `src/dashboard.py` imports `_should_skip_path` from `server` — vault stats and indexer now share a single exclusion code path
- `tests/test_unit.py` extended with 21 new tests covering launcher platform parity, dashboard archive exclusion, multi-vault stat counting, multi-vault recent-note relativization, E2E harness env validation, and CI pinning governance
- README native test commands updated to include the required `DATABASE_URL` env var
- README test count and osm Windows launcher description updated
- `docs/RUNBOOK.md` Install/Repair section now documents the platform-specific launcher shape (`osm` bash script vs `osm.cmd` batch wrapper)
- `docs/ARCHITECTURE.md` dashboard extension note updated to reflect that stats span all configured vaults via the shared skip filter

---

## [0.5.3] — 2026-04-06

### Added
- `OBSIDIAN_IGNORE_PATHS` support for vault-relative exclusion segments, with `archive` excluded by default and opt-in override support for archived notes

### Changed
- `src/server.py` now skips `archive/` content during indexing and watcher handling by default
- `README.md` and `docs/RUNBOOK.md` now document the archive exclusion behavior and override

### Fixed
- `osm_init.py` now resolves the osm launcher path through a helper so `cmd_remove()` is testable without touching the real home directory
- `tests/test_osm_commands.py` now redirects launcher deletion to a temp path during tests

---

## [0.5.1] — 2026-03-22

### Added
- `scripts/osm.ps1` — PowerShell CLI wrapper for Windows users

### Changed
- README updated to document both `scripts/osm` (macOS/Linux) and `scripts/osm.ps1` (Windows) wrappers

---

## [0.5.0] — 2026-03-22

### Added
- Multi-vault support in setup wizard — collect multiple vault paths interactively
- `docker-compose.override.yml` auto-generated for multi-vault Docker volume mounts
- `OBSIDIAN_VAULTS` env var written to `.env` when multiple vaults selected

---

## [0.4.0] — 2026-03-22

### Added
- Windows support in setup wizard — Docker-only modes with WSL2 backend detection
- Claude Desktop config path detection for Windows (`%APPDATA%\Claude\`)
- Windows uv installer in README Quick Start section
- `Dockerfile.dashboard` — dedicated Docker image for the dashboard (enables Docker Hub publish)
- ShipGuard SAST scan step in GitHub Actions CI pipeline
- `docs/RUNBOOK.md` — operational runbook for incident response, recovery, and monitoring

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
