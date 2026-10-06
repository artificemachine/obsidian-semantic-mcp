# CaaSiopeia, Obsidian MCP and Tessera relations

Observed on 2026-10-04 against the running `obsidian-semantic-mcp-mcp-server-1` container.

obsidian-semantic does call CaaSiopeia, but only in that direction and only for search. An earlier claim of "two independent vector indexes" was wrong for this installation.

## Obsidian MCP to CaaSiopeia

- The running container has `OSM_RETRIEVAL_BACKEND=caasiopeia` and `CAASIOPEIA_BASE_URL=http://host.docker.internal:3000`.
- `search_vault` therefore sends its ranking to CaaSiopeia over HTTP (`obsidian-semantic-mcp/src/server.py:1937`), falling back to local pgvector and Ollama only for temporary CaaSiopeia failures (connection failure, timeout, HTTP 429 or 5xx).
- The other tools (`get_file`, `write_file`, `recent_changes`, `append_content`) act directly on the vault files.

## CaaSiopeia to Obsidian MCP

- This direction does not exist. CaaSiopeia never calls the Obsidian MCP.
- CaaSiopeia indexes the vault by reading the directory itself, through `caasiopeia-sync`.

## Tessera to CaaSiopeia

- Also one-way into CaaSiopeia, at both ingestion and search time.
- Tessera extracts text from non-text files and pushes the documents into CaaSiopeia (`caasiopeia/src/ingest/provenance.rs`).
- Since its iteration 9, Tessera has no local index: its `search` and `show` always query CaaSiopeia (`tessera-mcp/src/cli.rs:4-8`, `TESSERA_CAAS_BASE_URL` required).
- Correction (2026-10-04): an earlier version of this note said Tessera only pushes at ingestion. Its search path also goes through CaaSiopeia.

## Consequence for Kabao

- `search_vault` and `context.search` go through the same CaaSiopeia ranking, so the routing ambiguity in Kabao's prompt matters less than first assumed.
- The remaining difference: `search_vault` only sees the vault sources declared in `CAASIOPEIA_SOURCE_MAP`, while `context.search` also sees `repo-docs`.
