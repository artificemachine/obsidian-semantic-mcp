# Documentation

Start here. This folder is a mix of reference docs, design records, incident postmortems, and forward-looking proposals — grouped below so you don't have to guess which is authoritative.

## Architecture & design

| Doc | What it covers |
|-----|----------------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | System overview: MCP server, embedding pipeline, pgvector schema, the watchdog. |
| [DESIGN-install-decoupling.md](DESIGN-install-decoupling.md) | Why the installed binary must not depend on the repo path, and how config resolution achieves it. |
| [PLAN-security-correctness.md](PLAN-security-correctness.md) | The security & correctness hardening plan (dashboard auth, advisory locking, versioned migrations) and its build outcome. |
| [PLAN-portable-mcp-config.md](PLAN-portable-mcp-config.md) | Portable MCP client configuration across Claude Desktop / Code / OpenCode. |

## Operations

| Doc | What it covers |
|-----|----------------|
| [RUNBOOK.md](RUNBOOK.md) | Operating the stack: bring-up, re-index, teardown, common failure recovery. |

## Explainers

Longer-form pieces on how and why the system works — useful if you're evaluating the approach rather than running it.

| Doc | What it covers |
|-----|----------------|
| [rag-explained-via-obsidian.md](rag-explained-via-obsidian.md) | Retrieval-augmented generation explained through this project's concrete pipeline. |
| [extending-with-llamaindex.md](extending-with-llamaindex.md) | How the design could extend to a LlamaIndex-based retrieval layer. |

## Proposals (speculative — not shipped)

| Doc | Status |
|-----|--------|
| [graphrag_integration.md](graphrag_integration.md) | GraphRAG integration — scoping only. |
| [graphrag_path_a_scope.md](graphrag_path_a_scope.md) | GraphRAG "path A" scope detail. |
| [proposed-rename-mnemosyne.md](proposed-rename-mnemosyne.md) | Proposed project rename — not decided. |

## Incident postmortems

Dated engineering records of production failures and their fixes. Evidence of how the system behaves under stress, not front-door documentation.

| Doc | Incident |
|-----|----------|
| [mcp_startup_incident_2026-04-30.md](mcp_startup_incident_2026-04-30.md) | MCP startup failure. |
| [mcp_failures_2026-04-30.md](mcp_failures_2026-04-30.md) | MCP connection failures. |
| [mcp_startup_race_2026-05-06.md](mcp_startup_race_2026-05-06.md) | Startup race condition. |
| [mcp_raw_stdin_fix_2026-05-07.md](mcp_raw_stdin_fix_2026-05-07.md) | Raw stdin handling fix. |
| [pi_mcp_bridge_heartbeat.md](pi_mcp_bridge_heartbeat.md) | pi MCP bridge heartbeat. |

## Audits

`audits/` holds dated job-readiness audit reports (`<date>-job-ready.md`, `<date>-production-ready.md`, `<date>-portfolio-ready.md` — the audit command evolved through those names) and their progress trackers. Internal QA output — useful history, not user documentation.
