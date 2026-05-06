# MCP Startup Race — 2026-05-06

Sequel to `mcp_startup_incident_2026-04-30.md`. The April fix added a local-venv fallback when Docker was absent. This documents a separate race that the previous fix did not cover.

## Symptom

Claude Code session starts. The MCP dialog shows `obsidian-semantic` initially, then within seconds it flips to `failed`. Other Docker-independent MCPs (pencil, serena) stay connected. Restarting via the MCP dialog Reconnect button sometimes recovers it; sometimes you have to wait and try again.

The April fallback fires correctly when Docker is fully absent. The failure mode here is different: Docker daemon is up, the container is marked as starting, but `docker compose exec` cannot attach yet.

## Root Cause

Claude Code spawns all MCP servers in parallel at session boot and immediately writes the JSON-RPC `initialize` message to each child's stdin. The MCP server has roughly 5 seconds to respond on stdout or it is marked failed for the whole session — there is no retry, no readiness gate, and no health-check field in the MCP config schema.

The wrapper `scripts/obsidian-semantic-mcp` does:

```bash
if docker compose ps --status running -q mcp-server >/dev/null 2>&1; then
  exec docker compose exec -T mcp-server python3 /app/src/server.py
fi
```

There are three states the system can be in when Claude Code spawns this:

| Docker daemon | Container | Result |
|---|---|---|
| down | n/a | falls through to local venv (April fix) |
| up | running and healthy | exec succeeds, MCP works |
| **up** | **starting / not yet ready** | **`ps --status running -q` returns empty, falls through to local venv — but local venv may also not be ready, and the unintended fallback is slower than waiting** |

The third row is the race. It happens after a Mac wake from sleep, after a Docker Desktop restart, or after `docker compose up -d` has just been issued.

## Fix — Block until ready inside the wrapper

Add a bounded polling loop at the top of the wrapper. If Docker is reachable but the container is not yet running, wait up to 30 seconds for it before deciding whether to use Docker or the local fallback.

This converts the race into a deterministic wait inside the child process. Claude Code's spawn blocks until the wrapper either exec's into Docker or starts the local venv. By the time stdin receives the `initialize` message, the server is ready.

Updated `scripts/obsidian-semantic-mcp`:

```bash
DOCKER_BIN="${DOCKER_BIN:-docker}"
WAIT_SECONDS="${OSM_DOCKER_WAIT:-30}"

if command -v "$DOCKER_BIN" >/dev/null 2>&1 && "$DOCKER_BIN" info >/dev/null 2>&1; then
  for _ in $(seq 1 "$WAIT_SECONDS"); do
    container_id="$("$DOCKER_BIN" compose --project-directory "$PROJECT_ROOT" ps --status running -q mcp-server 2>/dev/null || true)"
    if [[ -n "$container_id" ]]; then
      exec "$DOCKER_BIN" compose --project-directory "$PROJECT_ROOT" exec -T mcp-server python3 /app/src/server.py "$@"
    fi
    sleep 1
  done
fi

# Fall through to local venv (April 2026 fallback)
```

Three deliberate properties:

1. **`docker info` short-circuit.** If the daemon is dead (Docker Desktop not running), the `info` call fails immediately and we drop straight to the local fallback. No 30-second penalty in the common case where Docker is intentionally off.
2. **Bounded wait.** Caps at 30 seconds. Tunable via `OSM_DOCKER_WAIT`. After timeout, falls through to local venv rather than hanging forever.
3. **No behaviour change when Docker is healthy.** First iteration of the loop succeeds, exec fires immediately, identical to the previous code path.

## Why not the alternative paths

- **Pre-warm Docker via launchd at login.** Works but is global state with side effects. Adds boot time even on sessions that do not use this MCP.
- **Claude Code feature request (`startup_delay_ms`, `health_check`, `retry`, `spawn: lazy`).** The MCP config schema does not support any of these today. Filing a feature request is correct but cannot be the operational fix.
- **Make the Python server start eagerly outside Docker.** Defeats the point of the Docker container, which holds the Postgres/Ollama wiring.

## Operational notes

- `OSM_DOCKER_WAIT=0` disables the wait entirely, restoring the pre-fix race behaviour. Useful for debugging.
- If the container takes longer than 30 seconds to come up, raise `OSM_DOCKER_WAIT` rather than removing the bound — the bound is what protects against indefinite hangs.
- The fallback path remains the April 2026 local-venv path. Make sure `.env` contains `OBSIDIAN_VAULT` and `POSTGRES_PASSWORD` (or `DATABASE_URL`) for the fallback to function.

## Related

- `docs/mcp_startup_incident_2026-04-30.md` — original Docker-absent incident
- `docs/mcp_failures_2026-04-30.md` — broader MCP reliability notes
- `scripts/obsidian-semantic-mcp` — the wrapper this document patches
