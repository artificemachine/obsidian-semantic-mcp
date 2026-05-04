# Changelog

All notable changes to this project will be documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [0.8.0] — 2026-04-28

### Added
- **PollingObserver fallback for network filesystem vaults.** The native watchdog `Observer` relies on OS-level filesystem events (`inotify` on Linux, `ReadDirectoryChangesW` on Windows) that don't fire for writes made by remote clients on NFS/SMB network mounts, so new/changed notes were silently missed. Added a `PollingObserver` fallback controlled by `VAULT_WATCH_POLLING` (auto/true/false, default: auto) and `VAULT_POLL_INTERVAL` (seconds, default: 10). Auto-detection uses UNC path detection, `GetDriveTypeW` → `net use` → `wmic` on Windows, and `/proc/mounts` fstype check on Linux. Contributed by @yjjoeathome-byte in #14.

### Fixed
- `osm init` wrote the OpenCode MCP config entry to `~/.opencode.json` (standard MCP format), but OpenCode v1.14+ reads from `~/.config/opencode/opencode.json` using its own native format (`mcp` key, `command` as a flat array, `type: "local"`, `enabled: true`). Fixed `_opencode_cfg_path()` to target the correct path, `update_opencode_config()` to convert the standard entry to OpenCode's native format, and `remove_opencode_config()` / `osm status` to read from the `mcp` key instead of `mcpServers`. OpenCode now correctly sees `obsidian-semantic` as connected.

## [0.7.1] — 2026-04-20

### Fixed
- `osm update` was a silent no-op for the default install. The command previously ran only `docker compose pull mcp-server dashboard`, but those two services use `build: .` in `docker-compose.yml` — `pull` does nothing for build-based services, so no local code was ever refreshed. Fixed by running `compose pull postgres ollama` (refreshes image-based services) followed by `compose build --pull mcp-server dashboard` (pulls the latest base image and rebuilds the two custom services from the current source tree), then `compose up -d mcp-server dashboard`. Covers both the default source-build install and a hypothetical image-based install without branching logic.

## [0.7.0] — 2026-04-20

### Added
- **OpenCode support.** `osm init` now writes the obsidian-semantic MCP entry into `~/.opencode.json` (cross-platform) alongside Claude Desktop and the Claude Code CLI. `osm status` reports OpenCode registration; `osm remove` cleans it up. New helper `register_with_clients()` fans out to every supported MCP client in one call, so adding a fourth (Continue / Cursor / Codex CLI) is a one-line change.

### Changed
- `_docker_entry()` now resolves the actual `mcp-server` container name via `docker compose ps` instead of guessing `<dir>-mcp-server-1`. The generated MCP client entries stay correct when users set `COMPOSE_PROJECT_NAME`, run `docker compose -p custom`, or cloned the repo into a renamed directory. Falls back to the previous behavior if docker is unreachable.

## [0.6.0] — 2026-04-20

### Added
- `osm version` — print installed CLI version and check the latest GitHub release tag, with a one-line hint when a newer release is available.
- `osm update` — pull the latest `mcp-server` and `dashboard` Docker images and restart only those services (matches `osm rebuild`'s scope so non-full-docker installs aren't disturbed). Reports CLI drift and prints the reinstall one-liner when the CLI itself is behind.

### Changed
- Consolidated the `celstnblacc/obsidian-semantic-mcp` repo identifier into a single `_GITHUB_REPO` constant; `_INSTALL_URL` and the generated launcher scripts now derive from it.

## [0.5.13] — 2026-04-20

### Changed
- `index_vault` now batches embeddings via Ollama's `/api/embed` endpoint (Ollama 0.4+) — `EMBED_BATCH_SIZE` items (default 16) per HTTP call instead of one request per note. Eliminates ~14 of every 15 round-trips during a full rebuild and dramatically reduces the surface area for Ollama timeouts under load.
- On batch failure (older Ollama, network blip, or empty embedding for an item), each chunk transparently falls back to per-item `embed()` so no notes are silently dropped.

### Added
- `EMBED_BATCH_SIZE` env var (default 16) to tune the per-call batch size.
- `prune_orphans()` deletes DB rows whose `path` no longer exists on disk — fixes the slow drift between `indexed_count` and `vault_file_count` that builds up after files are deleted, vault paths change, or `OBSIDIAN_IGNORE_PATHS` is updated. Exposed as `POST /api/prune` on the dashboard.

## [0.5.12] — 2026-04-19

### Added
- `--vault-fs <auto|local|nfs|cifs>` flag for `osm init`. With `nfs` / `cifs`, the generated `docker-compose.override.yml` backs each vault with a Docker named volume using NFS or CIFS driver_opts instead of a bind mount — the path that finally works for Windows + NAS setups where bind-mounts of network drives silently mount empty directories.
- `--vault-cifs-user` / `--vault-cifs-pass` flags for SMB credentials.
- `osm remove` now drops `obsidian_vault_*` named volumes referenced by the generated override, so a teardown after `--vault-fs=nfs|cifs` doesn't leak Docker volume references that re-attach on next install.
- README "Alternative: native NFS / CIFS named volumes" section documenting the new flag, syntax, and v1 limitations (no Kerberos, no credential files).

## [0.5.11] — 2026-04-19

### Fixed
- `osm init` now surfaces `docker compose ps -a` and per-container logs immediately when `docker compose up` exits non-zero, instead of letting the postgres health check time out 90 s later. On Windows, a UNC / drive-letter bind-mount failure now points the user at the WSL2 workaround.

### Added
- README "Windows + network vault" section covering the WSL2 mount recipe for NFS / SMB vaults.

## [0.5.10] — 2026-04-19

### Fixed
- `index_vault` now retries failed embeds once and surfaces persistent failures via `get_last_rebuild_failures()`, exposed as `last_rebuild_failed_count` / `last_rebuild_failed_sample` in `/api/stats` — prevents silent data loss when Ollama wedges mid-rebuild

## [0.5.9] — 2026-04-17

### Fixed
- `osm status` now probes Ollama embeddings in addition to daemon reachability, so it reports the "daemon is up but inference is broken" failure mode directly

### Changed
- `docs/RUNBOOK.md` and `README.md` now document the Ollama inference probe and the macOS/Homebrew recovery path: `brew services restart ollama`

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
- 2026-04-29: Add wikilink graph augmentation (Path A) — note_links table, extract_wikilinks, expand_via_links, graph_expand param on search_vault, get_note_connections tool
- 2026-04-29: Bump version to 0.9.0 (feat: wikilink graph augmentation)

- 2026-04-30: docs: add Example Output section and bootstrap installer warning to README

## [0.9.1] — 2026-04-30
### Changed
- docs: added Example Output section to README with ranked search results
- docs: bootstrap installer warns about uncommitted-changes failure at ~/.local/share/obsidian-semantic-mcp

## [0.9.2] — 2026-05-04
### Fixed
- `osm init` MCP client entries now route through `scripts/obsidian-semantic-mcp`, a runtime-agnostic wrapper that auto-detects a running `mcp-server` Docker container and falls back to the local `.venv` Python. Replaces the inline `docker compose exec` and `.venv/bin/python3` invocations that broke on container restarts and venv path changes. See `docs/mcp_startup_incident_2026-04-30.md` for context.
- Pin `.python-version` to `3.11.6` (was `3.14`, which was unintended and unavailable on the install host).

## [0.9.3] — 2026-05-04
### Fixed
- MCP server auto-loads `.env` on startup (searches `~/.local/share/obsidian-semantic-mcp/.env` then repo root, with `override=False` so shell env wins). Fixes startup when spawned by an MCP client (OpenCode, Claude Desktop) that doesn't inherit shell env vars.
- Imports inside `src/server.py` and `src/dashboard.py` try relative form first (`from .config import build_dsn`) and fall back to absolute, so the package works both when installed via `uv tool install` and when run directly from `src/` during development.

### Added
- `obsidian-semantic-mcp` console script entry point (`src.server:run_server`) for global `uv tool install`.
