# MCP Server Failure Report — 2026-04-30

Project: `obsidian-semantic-mcp`
Session: `c831e2e8-6878-4ac9-a8c5-2a2acc44c725`
Logs: `~/Library/Caches/claude-cli-nodejs/<project-cache-dir>/mcp-logs-*/2026-04-30T15-55-03-569Z.jsonl`

## Summary

| Server | Status | Root cause |
|---|---|---|
| MCP_DOCKER | failed | `docker mcp` subcommand not installed |
| obsidian-semantic | failed | Docker daemon (OrbStack) not running |
| serena | failed | `.python-version` pins 3.14, not installed in pyenv |
| voice-toolkit | failed | `.python-version` pins 3.14, not installed in pyenv |
| nemoclaw | connected | — |
| pencil | connected | — |
| tilth | connected | — |

Two underlying issues account for all four failures.

## Issue 1 — Docker stack down (kills 2 servers)

**Evidence:**
- `MCP_DOCKER`: `Server stderr: docker: unknown command: docker mcp`
- `obsidian-semantic`: `Cannot connect to the Docker daemon at unix://$HOME/.orbstack/run/docker.sock. Is the docker daemon running?`

**State now:**
- `docker ps` → daemon unreachable (OrbStack not started)
- `docker mcp` → `unknown command` (Docker MCP CLI plugin not installed)

**Fixes:**
1. Start OrbStack (or whichever Docker runtime). That alone restores `obsidian-semantic`.
2. Install the Docker MCP plugin to restore `MCP_DOCKER`. Without the plugin, the server entry in `~/.claude.json` invoking `docker mcp ...` will keep failing even with the daemon up.

## Issue 2 — pyenv pin mismatch (kills 2 servers)

**Evidence (both serena and voice-toolkit):**
```
pyenv: version `3.14' is not installed (set by <project-root>/.python-version)
pyenv: serena-mcp-server: command not found
The `serena-mcp-server' command exists in these Python versions:
  3.11.6
```

**State now:**
- `cat .python-version` → `3.14`
- `pyenv versions` → only `system` and `3.11.6` are installed
- The MCP entry-point shims (`serena-mcp-server`, `voice-toolkit`) live in the 3.11.6 environment

So pyenv reads `.python-version`, asks for 3.14, can't find it, and refuses to dispatch the command. This happens regardless of how the MCP server is registered, because Claude Code spawns it from the project directory.

**Fixes (pick one):**
1. Drop the project pin: `rm <project-root>/.python-version` (falls back to `system` or whatever global pin is in effect, then locates the shim under 3.11.6 if that's the active pyenv version).
2. Re-pin to a version that has the shims: `pyenv local 3.11.6`.
3. Install Python 3.14 in pyenv: `pyenv install 3.14`, then re-install `serena-mcp-server` and `voice-toolkit` into that environment. Heavier; only do this if the project genuinely needs 3.14.

Recommended: option 2 (`pyenv local 3.11.6`). It's surgical and matches where the tooling already lives. The project's `pyproject.toml`/`uv` will still manage its own venv independently.

## Verification

After fixes, restart Claude Code and re-check:
```bash
docker ps                 # daemon up
docker mcp --help          # plugin present
pyenv version              # resolves to an installed version
which serena-mcp-server    # resolves
which voice-toolkit        # resolves
```
Then `/mcp` in Claude Code should show all four green.
