# Portable MCP Config — obsidian-semantic-mcp

## The problem

Agent configs hardcode this repo's project directory as `--project-directory`
arg to `docker compose exec`:

```
$HOME/.claude.json (mcpServers.obsidian-semantic):
  command: "docker"
  args: ["compose", "--project-directory",
         "/Users/airm2max/DevOpsSec/obsidian-semantic-mcp",
         "exec", "-T", "mcp-server", "python3", "/app/src/server.py"]

$HOME/.opencode.json:5:  "command": "/Users/airm2max/DevOpsSec/obsidian-semantic-mcp/scripts/obsidian-semantic-mcp"
$HOME/.pi/agent/mcp.json:35:  hardcoded project-directory
```

This breaks for:
- Repo moves
- Anyone installing via `uv tool install obsidian-semantic-mcp` or `pip install`
- Multiple checkouts of the repo on the same machine

## What this project must provide

The launcher (`src/launcher.py`) already supports project-root resolution via:
1. `OSM_PROJECT_ROOT` env var
2. `$HOME/.config/obsidian-semantic-mcp/project_root` config file (v0.9.6+)
3. Dev-checkout auto-detection (looks for sibling `docker-compose.yml`)

So the launcher already handles relocation. The gap is that **agent configs
still call `docker compose exec` directly**, bypassing the launcher.

### Proposed migration

Agent configs should use the bare CLI name on PATH:

```json
"obsidian-semantic": {
  "command": "obsidian-semantic-mcp",
  "args": [],
  "env": {"OSM_DOCKER": "1"}
}
```

The launcher then:
1. Reads `OSM_PROJECT_ROOT` (env > config file > dev auto-detect)
2. If `OSM_DOCKER=1`, polls for the container per the May 6 race fix
3. Falls through to in-process if Docker is unavailable

This preserves the existing transport (`docker compose exec` under the hood)
while making the agent config relocatable.

## Acceptance tests

```bash
# 1. Bare CLI invocation works
which obsidian-semantic-mcp
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}' | obsidian-semantic-mcp
# Must respond with initialize result within 30s

# 2. Move repo, verify still works
mv $HOME/DevOpsSec/obsidian-semantic-mcp $HOME/elsewhere/obsidian-semantic-mcp
echo "$(pwd)/.." > $HOME/.config/obsidian-semantic-mcp/project_root
echo '...initialize...' | obsidian-semantic-mcp
# Must still respond

# 3. Release install
uv tool install obsidian-semantic-mcp
which obsidian-semantic-mcp  # → $HOME/.local/share/uv/tools/.../bin/...
echo '...initialize...' | obsidian-semantic-mcp
```

## Known separate bug — May 8 transport hang

When invoked over an anonymous pipe (Claude Code, Node `child_process.spawn`,
Python `subprocess.PIPE`), `server.py`'s stdin reader hangs waiting for data
that never arrives — even with stdin held open. Closing stdin produces an
immediate response.

This is independent of project-directory hardcoding. Tracked separately;
see `docs/mcp_raw_stdin_fix_2026-05-07.md` (which solved a different EOF
death bug) and any future hang-investigation note.

The `for line in sys.stdin.buffer:` loop in `src/server.py:1729` likely
needs to use `anyio.to_thread.run_sync(sys.stdin.buffer.readline)` or
adopt the SDK's `stdio_server()` (now that the EOF death is fixed
upstream) so the event loop isn't blocked between reads.

## Cross-cutting plan

See `$HOME/DevOpsSec/superharness/docs/PLAN-portable-paths-cleanup.md` — this
project is phase 2 of the cleanup.

## Memory aid for future Claude sessions

Before editing any agent config to reference this MCP server: use the bare
`obsidian-semantic-mcp` CLI name with optional `OSM_*` env vars. Do not
hardcode `--project-directory` or absolute paths to `scripts/` or `src/`.
