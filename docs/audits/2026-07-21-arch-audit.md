# Architecture Audit — obsidian-semantic-mcp
**Date:** 2026-07-21
**Scope:** broad (invoked as Stage 6 of `/portfolio-ready`)
**Auditor:** Claude (arch-audit)

## Summary

The system's core data-layer architecture is genuinely solid: versioned migrations (`schema_version` table), Postgres advisory locks for cross-process coordination, persisted index state, and a bearer-token-gated dashboard with `hmac.compare_digest`. The gap that matters is upstream of all of that: **native (non-Docker) install registers a Claude Desktop/Code MCP entry that cannot launch**, because the required environment variables are never persisted anywhere the launcher process can find them. This is a correctness bug in a currently-advertised install path (`--mode 1`), not a hypothetical scale problem. Two secondary findings — an unlogged dashboard and a 3,080-line single-file CLI — round out real, if lower-urgency, technical debt.

## CRITICAL — fix before next deploy

**Native install (`--mode 1`) produces a non-functional MCP server registration.**
- Evidence: `_native_entry(vault, db_url)` (`osm_init.py:1430-1432`) accepts `vault` and `db_url` as parameters but its body ignores both, returning `{"command": "obsidian-semantic-mcp", "args": [], "env": {}}` — an empty env dict. `mode_native_macos()` (`osm_init.py:1720-1785`) never calls `write_env()` (grep confirms zero `.env`-writing calls in that function), so no `.env` file exists at `PROJECT_ROOT` for a native install. `src/launcher.py:main()` (lines 101-141) resolves `project_root` via `~/.config/obsidian-semantic-mcp/project_root` (written unconditionally by `_write_project_root_config()`, called from `register_with_clients()` for every mode including native — `osm_init.py:1419`), attempts `load_dotenv(project_root/.env)` — a no-op since the file doesn't exist for native — then falls through to `_validate_env()`, which exits immediately if `OBSIDIAN_VAULT`/`OBSIDIAN_VAULTS` or `DATABASE_URL`/`POSTGRES_PASSWORD` are unset (`src/launcher.py:53-62`). None of these are set anywhere in the native path: not in the empty MCP-registered `env`, not in a persisted `.env`, not in the OS environment.
- Recommended fix: either (a) populate `_native_entry()`'s `env` dict with `OBSIDIAN_VAULT`/`DATABASE_URL` (matches the parameters it already receives but discards), or (b) have `mode_native_macos()` call `write_env()` (or an equivalent native-only writer) so `load_dotenv` has something to hydrate — option (a) is the smaller, more targeted fix since the values are already in scope at the call site (`osm_init.py:1782`).
- Suggested iteration: standalone bugfix PR, TDD (write a launcher-level test asserting a native `osm init` run produces an MCP entry that `_validate_env()` accepts without an inherited shell environment).

## HIGH — fix before scale

**`src/dashboard.py` has zero structured logging in 972 lines handling bearer-token auth and mutating endpoints.**
- Evidence: `grep -c 'log\.\(info\|warning\|error\|debug\|critical\)' src/dashboard.py` → `0`. The file's only output is 5 startup-banner `print()` calls (`src/dashboard.py:964-971`). Compare `src/server.py`, which has 46 `log.*` calls following the project's own documented convention (`CLAUDE.md`: "Logging uses `%s` lazy format"). A failed `_require_auth()` check, a `/api/reindex` trigger, or an unhandled exception mid-request in the dashboard leaves zero trace — no way to answer "who hit this and when" after the fact, and inconsistent with the rest of the codebase.
- Recommended fix: add `log = logging.getLogger(__name__)` and at minimum log auth failures (security-relevant) and unhandled request exceptions.
- Suggested iteration: small follow-up PR, no schema/behavior change.

**`osm_init.py` is a 3,080-line single-file module covering the entire install wizard plus every CLI subcommand.**
- Evidence: `wc -l osm_init.py` → 3080, next-largest module `src/server.py` at 2099. One file owns `mode_native_macos`, `mode_full_docker`, `mode_docker_host_ollama`, `cmd_status`, `cmd_rebuild`, `cmd_migrate`, `cmd_tunnel`, `cmd_vaults`, plus all client-registration and compose helpers. Not currently causing a bug, but the CRITICAL finding above (a params-accepted-but-ignored function, `_native_entry`) is exactly the class of defect a file this size makes easy to introduce and hard to spot in review — the mistake sat undetected through the CLI dispatch registration hardening (PR #31) and the fresh-clone/dependency audits (Stage 4) because nothing exercises native-mode's actual MCP launch path end-to-end.
- Recommended fix: not urgent as a standalone refactor; consider splitting `mode_*` (install wizards) from `cmd_*` (post-install operational commands) into separate modules the next time either group needs substantial new code, rather than as a dedicated iteration now.
- Suggested iteration: opportunistic, bundle with the CRITICAL fix's PR since that PR will already be editing `_native_entry`/`mode_native_macos`.

## MEDIUM — recoverable technical debt

**No test exercises the native install's actual MCP launch path.**
- Evidence: `tests/test_dimension_migration.py`, `tests/test_osm_commands.py`, and `tests/test_osm_init.py` cover `cmd_migrate`'s native branch (this session's own addition) and various `osm_init.py` helpers in isolation, but no test asserts that `_native_entry()`'s returned config, combined with `src/launcher.py`'s `_validate_env()`, actually succeeds — which is precisely how the CRITICAL finding above went unnoticed. This is the concrete "which test is missing" answer for arch-audit category 9.
- Recommended fix: a test that calls `_native_entry(vault, db_url)`, feeds the resulting `env` dict (merged with an *empty* base environment, not the test runner's own) into `launcher._validate_env()`, and asserts it does not exit.
- Suggested iteration: bundle with the CRITICAL fix's PR as its regression test.

## LOW — nice-to-have polish

- `docs/README.md`'s Audits section describes the folder as `<date>-job-ready.md` only; it now also contains `production-ready`/`portfolio-ready` naming (cross-referenced from Stage 3 of the parent portfolio-ready audit — not re-detailed here to avoid duplication).
- `v0.15.1`'s git tag was cut one commit before its own `pyproject.toml` version-bump commit landed (cross-referenced from Stage 2 of the parent audit).

## Out of scope

UI/UX quality of the dashboard's HTML/JS, embedding-model choice and retrieval quality, and CI workflow governance (covered separately by Stage 7 of the parent `/portfolio-ready` run). Did not re-audit the already-verified migration versioning, advisory-locking, or dashboard-auth mechanisms in depth — those were confirmed solid in this pass and are unchanged since the prior full architecture audit today.

## Recommended next iterations

1. **Iteration A (bugfix, ship first):** fix `_native_entry()`'s discarded env params + add the regression test from the MEDIUM finding. This is the only finding that breaks a currently-advertised feature for an end user.
2. **Iteration B (small, independent):** add structured logging to `src/dashboard.py`, matching `server.py`'s convention.
3. **Opportunistic, no dedicated iteration needed:** split `osm_init.py` the next time either the install-wizard or operational-command surface grows substantially — not urgent today.
