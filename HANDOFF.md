# obsidian-semantic-mcp — Session Handoff

**Latest update:** 2026-05-08 (PM) — Phase 2 portable-paths cleanup + stdin pipe-hang fix
**Branch:** chore/portable-mcp-config (uncommitted)
**Previous milestone:** v0.10.0 (PR #41, pi agent support, merged earlier 2026-05-08)

---

## 2026-05-08 PM session: Phase 2 — portable-paths cleanup + transport fix

Cross-repo cleanup tracked in `$HOME/DevOpsSec/superharness/docs/PLAN-portable-paths-cleanup.md`.
This project is phase 2 of 4.

### Root cause of MCP failure (separate from portable-paths)

`/mcp` showed obsidian-semantic as `failed`. Claude Code's MCP client
spawns the server with `subprocess.spawn(stdio: 'pipe')`, writes the
JSON-RPC `initialize` request, and waits for a response. The server
hung — 30s timeout, no response.

Detailed reproduction was done with `subprocess.Popen(stdin=PIPE,
stdout=PIPE)` — same hang. Closing stdin after writing produced an
immediate response. Diagnosis: the May 7 raw-stdin transport in
`src/server.py` used `for line in sys.stdin.buffer:` inside an async
coroutine. That's a synchronous blocking iterator that **freezes the
asyncio event loop between reads**, so `_stdout_writer` cannot schedule
and the queued response never gets written.

The May 7 fix solved a different (real) bug — anyio's wrapped stdin
exiting on EOF and killing the server between cycles. But it introduced
the event-loop freeze. Symptoms only surfaced under anonymous pipe
stdio (Claude Code, Node `child_process.spawn`); shell-redirected
file/FIFO inputs masked it because they triggered EOF after the data,
which closed the pipe and unblocked the loop incidentally.

### Fix in `src/server.py:1722-1748`

```python
async def _stdin_reader():
    async with read_writer:
        while True:
            line = await anyio.to_thread.run_sync(sys.stdin.buffer.readline)
            if not line:
                # EOF: don't exit (preserve no-EOF-death property).
                # Sleep + retry; in-thread readline blocks until data.
                await anyio.sleep(0.1)
                continue
            ...
```

`anyio.to_thread.run_sync` offloads the blocking syscall, leaving the
event loop free for `_stdout_writer` and `server.run`. Empty-bytes
return (true EOF) is treated as idle-retry rather than termination,
preserving the May 7 fix's no-EOF-death property.

### Verified manually

| Test | Result |
|---|---|
| Initialize over pipe, stdin held open | Response in 1.0s (was: 30s timeout) |
| Initialize → notifications/initialized → tools/list × 2, sequential | All four messages handled cleanly |

The fix was applied to the running container via `docker cp` for
verification. **The published image (`celestinmax/obsidian-semantic-mcp:latest`)
is NOT yet rebuilt — `docker compose up --force-recreate mcp-server` will
revert to the broken image.** Rebuild is owner-driven.

### Portable-paths contract (phase 2a)

`tests/test_portable_invocation.py` — 6 new tests, all pass. They lock:
- `OSM_PROJECT_ROOT` env var > `$HOME/.config/obsidian-semantic-mcp/project_root` > dev auto-detect
- bare `obsidian-semantic-mcp` console script must be on PATH
- `main()` must NOT require `--project-directory` in argv (anti-regression)

This guarantees `$HOME/.claude.json` etc. can drop the
`--project-directory /Users/.../obsidian-semantic-mcp` arg and just say
`"command": "obsidian-semantic-mcp"`.

### Files changed (this session)

| File | Change |
|------|--------|
| `src/server.py` | Stdin reader rewritten with `anyio.to_thread.run_sync(readline)` + EOF-as-retry. Comments document both bugs (May 7 EOF death + May 8 event-loop freeze). |
| `tests/test_portable_invocation.py` | New: 6 tests for project-root resolution + bare-CLI contract. |
| `tests/test_stdin_pipe_response.py` | New: integration test reproducing the stdin hang (skips on host without postgres + watchdog; verifies via container in CI). |
| `CHANGELOG.md` | Entry appended (append-only policy). |
| `docs/PLAN-portable-mcp-config.md` | New: per-project context note + memory aid + reference to master plan. |

### What the next session should do

1. **Rebuild the image so the fix persists.** Tag a new version (v0.10.1 or
   v0.11.0). The current `latest` tag still has the broken stdin reader.
   ```bash
   cd $HOME/DevOpsSec/obsidian-semantic-mcp
   # bump OSM_VERSION in .env or pyproject.toml
   docker compose build mcp-server
   docker tag celestinmax/obsidian-semantic-mcp:latest celestinmax/obsidian-semantic-mcp:<new-version>
   docker push celestinmax/obsidian-semantic-mcp:<new-version>
   docker compose up -d --force-recreate mcp-server
   ```
2. Review and commit `chore/portable-mcp-config` (per global rule, no commits
   without explicit confirmation).
3. Migrate `$HOME/.claude.json` to drop `--project-directory` once the new image
   is live (phase 4 of master plan).
4. Phase 1 (superharness CLI) is independent and can be tackled in parallel.

### Pre-existing test failures NOT caused by this session

- `tests/test_launcher.py::test_missing_vault_exits` and
  `test_missing_db_config_exits` — both fail with `ModuleNotFoundError:
  No module named 'watchdog'` because the host pyenv 3.11.6 doesn't
  have project deps installed. These tests require a hydrated venv;
  the bug is in test setup, not in production code.

---

## 2026-05-08 AM session: pi agent support (v0.10.0)

### pi agent support (primary)

obsidian-semantic was permanently deadlocking on startup with the `pi` CLI (earendil-works/pi). Root cause: pi's community `mcp-bridge.ts` started the heartbeat timer *after* receiving the `initialize` response, but `initialize` itself required the heartbeat to be running to unblock the asyncio event loop. Permanent chicken-and-egg deadlock.

**Fix in `$HOME/.pi/agent/extensions/mcp-bridge.ts`:**
- Moved heartbeat `setInterval` from `initializeServer()` to `spawnServer()` — runs from the moment the subprocess is created, covering the `initialize` exchange.
- Added `proc.on("close")` handler to reject all pending requests on unexpected process exit.

**`osm init` / `osm update` auto-apply:**
- `register_pi_agent()` — detects `pi` on PATH, writes `$HOME/.pi/agent/mcp.json` with `heartbeat: true`
- `_patch_pi_mcp_bridge()` — detects unfixed `mcp-bridge.ts` (checks for `_MCP_BRIDGE_SPAWN_HEARTBEAT_MARKER`), patches it in place
- `cmd_update()` now calls full client re-registration (Claude Desktop, Claude Code CLI, OpenCode, pi)

### asyncio yield fix in `src/server.py`

Added `await anyio.sleep(0)` on empty heartbeat lines inside `_stdin_reader()`. This yields the event loop on every heartbeat pulse, ensuring `_stdout_writer` can flush queued responses even when no real MCP message arrived.

### docker-compose.yml

Added bind-mount for `./src/server.py:/app/src/server.py:ro` so the asyncio yield fix is applied inside the Docker container without a full image rebuild.

### Documentation

`docs/pi_mcp_bridge_heartbeat.md` — full root cause analysis, deadlock diagram, manual patch instructions, correct `mcp.json` entry, verification steps.

---

## Files Changed

| File | Change |
|------|--------|
| `osm_init.py` | +`register_pi_agent()`, +`_patch_pi_mcp_bridge()`, updated `register_with_clients()` and `cmd_update()` |
| `src/server.py` | +`await anyio.sleep(0)` on heartbeat lines |
| `docker-compose.yml` | bind-mount patched `server.py` |
| `docs/pi_mcp_bridge_heartbeat.md` | new — root cause doc |
| `README.md` | pi added to supported clients, heartbeat callout |
| `CHANGELOG.md` | v0.10.0 entry appended |
| `tests/test_osm_commands.py` | fixed pre-existing assertions for keylogger-mcp-wrapper |

---

## Verification

After `osm init` on a machine with `pi` installed:

```
pi
[mcp-bridge:obsidian-semantic] Starting...
[mcp-bridge:obsidian-semantic] Registered 11 tools: search_vault, ...
[mcp-bridge] Total: 48 tools from 4 servers
```

---

## Known State

- `voice-toolkit` repo has uncommitted changes on `feat/rust-migration-phase-1` — **do not touch**, owner manages separately
- `$HOME/.pi/agent/extensions/mcp-bridge.ts` on this machine is already patched (marker present)
- `$HOME/.pi/agent/mcp.json` is already configured with `heartbeat: true`

---

## Next Steps (if any)

- Consider upstreaming the mcp-bridge heartbeat fix to earendil-works/pi as a PR
- `osm update` could also detect stale Docker images and prompt rebuild
