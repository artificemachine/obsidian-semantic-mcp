# MCP Startup Incident — 2026-04-30

## Symptom
- Claude Desktop reported `obsidian-semantic` failed during MCP initialize.

## Root Cause
- The configured MCP command used `docker exec` directly.
- When OrbStack/Docker was not running, the process exited before returning the MCP `initialize` response.
- The repo already had a working local `.venv` and `.env`, but the desktop entry had no native fallback.

## Fix
- Added `scripts/obsidian-semantic-mcp` as the stable launcher.
- The launcher prefers the running Docker container when available.
- If Docker is unavailable, it falls back to the repo-local `.venv` and loads `.env`.
- Updated `osm_init.py` so future MCP registrations use the launcher instead of a raw Docker command.

## Operational Note
- If startup still fails after this change, verify `.env` still contains a vault path and database credentials.
