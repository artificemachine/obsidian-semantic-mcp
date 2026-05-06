# Plan: Python launcher + Docker Hub images (v0.9.5)

## Goal

Two related fixes shipped together:

1. **Path-agnostic launcher** — replace the bash wrapper with a Python entry point so
   `obsidian-semantic-mcp` works from a global `uv tool install` with no hardcoded paths.

2. **Eliminate duplicate images** — `docker-compose.yml` currently uses `build: .` which
   produces a local image (`obsidian-semantic-mcp-mcp-server:latest`) alongside the
   published Docker Hub image. Switch to `image:` to pull from Docker Hub instead of
   building locally. One image per service, version-pinned, no local build artifacts.

Current MCP config (fragile):
```json
"command": "/Users/.../obsidian-semantic-mcp/scripts/obsidian-semantic-mcp"
```

Target MCP config (global):
```json
"command": "obsidian-semantic-mcp"
```

Current compose (builds locally, creates duplicate):
```yaml
mcp-server:
  build: .
```

Target compose (pulls published image, version-pinned):
```yaml
mcp-server:
  image: celestinmax/obsidian-semantic-mcp:${OSM_VERSION:-0.9.5}
```

---

## What changes

### New file: `src/launcher.py`

Replicates all bash wrapper logic in Python:

1. **Docker detection** — `subprocess.run(["docker", "info"], capture_output=True, timeout=5)`
2. **Polling loop** — up to `OSM_DOCKER_WAIT` (default 30) seconds, 1s sleep between checks
3. **Container check** — `docker compose --project-directory PROJECT_ROOT ps --status running -q mcp-server`
4. **Exec into container** — `os.execvp("docker", ["docker", "compose", "--project-directory", ..., "exec", "-T", "mcp-server", "python3", "/app/src/server.py"] + sys.argv[1:])`
5. **Fallback: .env load** — `python-dotenv` already a dep, call `load_dotenv()` on `PROJECT_ROOT/.env`
6. **Env validation** — check `OBSIDIAN_VAULTS`/`OBSIDIAN_VAULT` and `DATABASE_URL`/`POSTGRES_PASSWORD`
7. **Fallback: run server** — call `run_server()` from `src.server`

`PROJECT_ROOT` is derived from `__file__` (same as the bash `$SCRIPT_DIR/..` pattern):
```python
PROJECT_ROOT = Path(__file__).resolve().parent.parent
```

This works both from the dev checkout and from an installed package (the wheel includes `src/`).

### Updated `pyproject.toml`

Change the entry point:
```toml
[project.scripts]
obsidian-semantic-mcp = "src.launcher:main"
```

Version bump: `0.9.4` → `0.9.5`

### Bash wrapper: `scripts/obsidian-semantic-mcp`

Simplify to a thin shim that delegates to the Python launcher. Keeps backwards compat for
anyone who has the old config pointing to the script:
```bash
#!/usr/bin/env bash
exec "$(dirname "$0")/../.venv/bin/python3" -m src.launcher "$@"
```

Or leave it in place and add a deprecation note. Does not block the PR.

### Claude MCP config: `~/Library/Application Support/Claude/claude_desktop_config.json`

Update after `uv tool install`:
```json
"obsidian-semantic": {
  "command": "obsidian-semantic-mcp",
  "args": [],
  "env": {}
}
```

---

## TDD cycle

### RED — write tests first (before launcher exists)

File: `tests/test_launcher.py`

```python
# 1. Docker daemon absent → calls run_server() directly (no exec)
# 2. Docker daemon present, container running → os.execvp called with correct args
# 3. Docker daemon present, container starting → polls, then exec once running
# 4. Docker daemon present, container never starts (timeout) → falls through to run_server()
# 5. Missing OBSIDIAN_VAULT env → sys.exit(1)
# 6. Missing DATABASE_URL and POSTGRES_PASSWORD → sys.exit(1)
# 7. OSM_DOCKER_WAIT=0 → no polling, immediate fallback
```

All tests use `unittest.mock` to patch `subprocess.run`, `os.execvp`, `time.sleep`, and
`src.server.run_server` — no real Docker or Postgres required.

### GREEN — implement `src/launcher.py` to pass all tests

### REFACTOR — review for clarity; no new behavior

---

## Risks / open questions

- **`PROJECT_ROOT` from `__file__` in an installed wheel**: when installed via `uv tool install`,
  the `src/` package lives under the tool's virtualenv (e.g. `~/.local/share/uv/tools/obsidian-semantic-mcp/lib/python3.x/site-packages/src/`). `PROJECT_ROOT` would resolve to that path, which has no `docker-compose.yml`.
  
  **Fix**: `PROJECT_ROOT` for Docker should come from an env var (`OSM_PROJECT_ROOT`) with
  the dev checkout path as the default only when running from source. When installed globally
  and Docker mode is desired, the user sets `OSM_PROJECT_ROOT` in the MCP config env block.
  
  Alternatively, Docker mode is opt-in via `OSM_DOCKER=1` — if unset, skip Docker entirely
  and run the local server. This is simpler and matches the most common installed usage.

- **Bash wrapper deprecation**: leave in place for v0.9.5, add a comment pointing to launcher.
  Remove in v1.0.

---

## Files touched

| File | Change |
|---|---|
| `src/launcher.py` | new |
| `tests/test_launcher.py` | new |
| `pyproject.toml` | entry point + version bump |
| `docker-compose.yml` | `build: .` → `image: celestinmax/...:${OSM_VERSION:-0.9.5}` for mcp-server and dashboard |
| `CHANGELOG.md` | append entry |
| `scripts/obsidian-semantic-mcp` | thin shim or deprecation note |
| `claude_desktop_config.json` | update command (post-install step, not committed) |

### `docker-compose.yml` change detail

```yaml
# mcp-server — before
mcp-server:
  build: .

# mcp-server — after
mcp-server:
  image: celestinmax/obsidian-semantic-mcp:${OSM_VERSION:-0.9.5}

# dashboard — before
dashboard:
  build: .

# dashboard — after
dashboard:
  image: celestinmax/obsidian-semantic-dashboard:${OSM_VERSION:-0.9.5}
```

`OSM_VERSION` in `.env` lets users pin or override without editing the compose file.
Dev workflow that needs a local build: `docker compose up -d --build mcp-server`.

---

## Decision: Docker mode strategy

**Chosen: B — `OSM_DOCKER=1` opt-in.**

Default global install runs the server locally (no Docker awareness).
Docker mode activates only when `OSM_DOCKER=1` is set, paired with `OSM_PROJECT_ROOT`
pointing to the compose project directory.

```json
"obsidian-semantic": {
  "command": "obsidian-semantic-mcp",
  "env": {
    "OSM_DOCKER": "1",
    "OSM_PROJECT_ROOT": "/path/to/obsidian-semantic-mcp"
  }
}
```

For a fully global install (no local checkout), omit `OSM_DOCKER` entirely — the launcher
runs the server in-process using the installed package.
