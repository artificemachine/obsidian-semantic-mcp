# obsidian-semantic-mcp

## Identity
You are working for the project owner.

## This Project
- What: obsidian-semantic-mcp
- Stack: Python (uv)
- Status: active development (version lives in `pyproject.toml` and the deployed image tag — don't duplicate in agent homes)
- Terminology: `osm` means the Obsidian Semantic MCP CLI (`osm init`, `osm dashboard`, etc.), not OpenStreetMap.

## Related projects
caasiopeia, obsidian-semantic-mcp and tessera-mcp work together. CaaSiopeia is the shared retrieval engine: the other two call it, and it calls neither of them.
- obsidian-semantic-mcp: `search_vault` delegates ranking to CaaSiopeia when `OSM_RETRIEVAL_BACKEND=caasiopeia`. Its file tools (`get_file`, `write_file`, `append_content`, `recent_changes`) act on the vault directly.
- tessera-mcp: pushes extracted non-markdown documents into CaaSiopeia, and its `search` and `show` always query CaaSiopeia.
Details: `docs/ARCH-caasiopeia-obsidian-tessera-relations.md`.

## Cross-Agent Protocol
- Read `.superharness/contract.yaml` before starting work.
- Keep task status, ledger, and handoff updated before stopping.

## Strict Installation Decoupling

Once installed (e.g., to ~/.local/bin), the project binary must NEVER depend on the local repository path for execution, configuration, or data. All paths must be relative to the installation root or use standard system config paths (~/.config).
