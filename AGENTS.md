# obsidian-semantic-mcp

## Identity
You are working for the project owner.

## This Project
- What: obsidian-semantic-mcp
- Stack: Python (uv)
- Status: v0.6.0 — active development
- Terminology: `osm` means the Obsidian Semantic MCP CLI (`osm init`, `osm dashboard`, etc.), not OpenStreetMap.

## Cross-Agent Protocol
- Read `.superharness/contract.yaml` before starting work.
- Keep task status, ledger, and handoff updated before stopping.

## Strict Installation Decoupling

Once installed (e.g., to ~/.local/bin), the project binary must NEVER depend on the local repository path for execution, configuration, or data. All paths must be relative to the installation root or use standard system config paths (~/.config).
