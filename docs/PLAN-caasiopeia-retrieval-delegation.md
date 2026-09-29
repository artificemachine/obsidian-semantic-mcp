# Plan: delegate Obsidian retrieval to Caasiopeia

## 1. Scope summary

Replace `search_vault`'s local pgvector ranking path with a typed, authenticated HTTP client for Caasiopeia `POST /v1/context`. OSM remains responsible for vault filesystem CRUD, watching, and wikilink expansion. The plan deliberately retains the local `notes` and `note_links` projection during v1 because `expand_via_links()` reads both tables; it stops using local embeddings for retrieval only after an explicit later migration. It does not invoke the stdio `caasiopeia-mcp` adapter. The architecture decision is recorded in [DECISION-obsidian-tessera-integration-direction.md](../../caasiopeia/docs/DECISION-obsidian-tessera-integration-direction.md).

Smallest possible v1: with an explicit `OSM_RETRIEVAL_BACKEND=caasiopeia`, `search_vault` sends a scoped request to Caasiopeia, renders its passages, and expands only verified local wikilinks.

Register: N/A — no `docs/REGISTER-*.md` exists. Freshness check is therefore N/A.

## 2. Prerequisites

- Dependencies: the existing `requests` dependency; a reachable Caasiopeia HTTP service only for the final disposable-environment demo. No new dependency and no live vault or production Caasiopeia service is permitted during automated tests.
- Source areas: `src/server.py` owns the MCP tool, local graph projection, and tool output; `src/config.py` owns shared configuration; `tests/test_unit.py` establishes `call_tool` conventions; `pyproject.toml` defines the test runner.
- Cross-repository contract: Caasiopeia `POST /v1/context` accepts `query`, `token_budget`, optional `source_ids`, `max_per_document`, `mode`, `prune`, and `trace_id`; passages include `external_id`, `document_id`, `source_id`, `score`, and `text` ([`src/domain/context.rs`](../caasiopeia/src/domain/context.rs)). Re-read this contract and its integration tests immediately before implementation; do not infer it from this plan.
- Risks: Caasiopeia `external_id` might not match OSM's canonical absolute note path; Caasiopeia source IDs are tenant-scoped; `graph_expand` depends on a local `note_links` projection; a silent fallback to the legacy ranking path would mix two retrieval policies. Each is tested or made a hard configuration error below.
- Baseline: observed revision `f24f771a8e9b221d56dd80141848c1fb8bfad8c0`; `uv run pytest tests/test_unit.py -m "not pg"` passed twice (85 passed each run) after operator approval. Existing failures outside that focused baseline are unknown; RED failures below are new, named assertions.
- Plan checker: resolved to `/Users/airm2max/DevOpsSec/skills-canonical/tools/plan_check.py` with Python 3.14.7. Before implementation, re-run `python3 /Users/airm2max/DevOpsSec/skills-canonical/tools/plan_check.py docs/PLAN-caasiopeia-retrieval-delegation.md --caller plan-iter --repo-root .` and require exit `0`.

**Execution architecture:** sequential — condition: every slice writes the same shared artifact, `src/server.py`. Parallel writers would serialize on that file and make integration ownership ambiguous. No subagents are proposed. `modrouter` selected tier `default`, but the configured `codex-cli/openai` bindings contain no `default` entry, so routing is unresolved; use the configured session model unless the operator supplies a valid binding. If cross-repository contract ambiguity remains after Iteration 1, escalate one tier before proceeding.

## 3. Iterations

#### Iteration 1 — Add a strict Caasiopeia HTTP boundary

**Goal:** Add a dormant, typed Caasiopeia client and validated configuration without changing the default local retrieval behavior.

**Shippable on its own?** Yes. The existing backend remains selected by default; the new client is independently contract-tested and cannot affect an existing search.

**Source references:**
- `src/config.py` — preserve the repository's shared configuration ownership and secret-handling rules.
- `src/server.py` — add dependency injection at the MCP boundary without changing the existing tool contract yet.
- `../caasiopeia/caasiopeia-mcp/src/server.rs` — reuse only the HTTP request semantics, not its stdio protocol; verify its current request headers, timeout behavior, and error mapping before coding.
- `../caasiopeia/src/domain/context.rs` — verify request and passage fields against the current service contract.

**Files touched:**
- `src/caasiopeia_client.py` (new)
- `src/config.py` (modified)
- `src/server.py` (modified)
- `tests/test_caasiopeia_client.py` (new)
- `tests/test_unit.py` (modified)

**Commit message:**
`feat(caasiopeia): add typed retrieval HTTP client`

**TDD cycle:**
- RED (failing tests to write first):
  - `tests/test_caasiopeia_client.py::test_search_posts_exact_context_contract` — asserts JSON body, bearer header, generated trace ID, timeout, and response decoding against a fake `requests.Session`.
  - `tests/test_caasiopeia_client.py::test_search_rejects_blank_query_and_unknown_passage_shape` — asserts boundary validation fails before a network request.
  - `tests/test_caasiopeia_client.py::test_client_maps_timeout_unauthorized_and_invalid_json_to_typed_errors` — asserts callers can distinguish retryable from configuration failures without exposing credentials.
  - `tests/test_unit.py::test_caasiopeia_config_requires_base_url_and_source_mapping_when_backend_is_caas` — asserts invalid opt-in configuration fails at startup with variable names only.
- GREEN (minimal implementation to pass RED):
  - Add a `CaasClient` with one `search()` operation using `POST /v1/context`, bounded response size, explicit connect/read timeouts, `x-caasiopeia-trace-id`, and redacted typed errors.
  - Add `OSM_RETRIEVAL_BACKEND`, `CAASIOPEIA_BASE_URL`, `CAASIOPEIA_API_KEY`, `CAASIOPEIA_SOURCE_MAP`, and `CAASIOPEIA_TOKEN_BUDGET` parsers; retain `local` as the default and never write an API key to disk or logs.
  - Inject a client factory into `src/server.py` only when `OSM_RETRIEVAL_BACKEND=caasiopeia`.
- REFACTOR (cleanup planned after GREEN):
  - Extract request/response validation from transport code.
  - Centralize Caasiopeia environment parsing in `src/config.py`.

**Test pyramid for this iteration:**
- Smoke: `uv run python -c "import src.caasiopeia_client"` imports without a configured Caasiopeia backend.
- Unit: four new client/config tests in `tests/test_caasiopeia_client.py` and `tests/test_unit.py` using fake sessions only.
- Integration: N/A — no Caasiopeia service is contacted in this dormant slice.
- State machine: N/A — no state transition changes.
- Contract: fake-session assertions cover method, path, auth-header presence, JSON schema, and trace propagation.
- Regression: local `search_vault` unit tests remain green; the new backend is opt-in.
- Chaos: timeout, malformed JSON, oversized body, and HTTP 401/503 are injected through the fake session.
- E2E: N/A — no user-facing behavior is enabled yet.
- Performance: N/A — no performance acceptance criterion in this dormant slice.
- TDD Parity: all new boundary behavior is defined by named RED tests; no existing behavior changes.
- Coverage: unknown until measured; do not lower the existing 50% repository floor.

**Deploy + validate:**
- Install: N/A — repository-only change; no installation action during this iteration.
- Validate: from `../obsidian-semantic-mcp`, run `uv run pytest tests/test_caasiopeia_client.py tests/test_unit.py -m "not pg"`; expected result: the named tests pass with no HTTP connection to Caasiopeia.
- Rollback: remove only this iteration's new client/config wiring; default local retrieval remains intact.

**Side-effect fence:** Writable files are the listed repository files only. Tests use in-memory fake HTTP sessions and monkeypatched environment variables; no Keychain, live Caasiopeia endpoint, vault, PostgreSQL instance, or MCP registration may be touched.

**Checkpoint evidence:** Record the base revision, failing then passing test names, the exact validated request schema, and confirmation that no credential value appeared in output.

**Acceptance criteria (binary):**
- [ ] `OSM_RETRIEVAL_BACKEND=local` leaves `search_vault` on its existing local path.
- [ ] Invalid Caasiopeia opt-in configuration fails before an HTTP request.
- [ ] The client sends one validated `POST /v1/context` request with a trace ID and never logs the bearer token.
- [ ] Timeout, invalid response, 401, and 503 map to distinct typed client outcomes.

**Estimated effort:** S (2h; basis: new transport boundary, configuration validation, and synthetic contract tests)

**Executor:** default

**Isolation:** shared

**Delegation:** in-session

**Blocked by:** None

#### Iteration 2 — Route `search_vault` through Caasiopeia

**Goal:** Make opt-in Caasiopeia retrieval power `search_vault` while preserving the public tool arguments and explicit, non-silent failure behavior.

**Shippable on its own?** Yes. Operators can opt in per deployment and roll back to `local`; unchanged installations keep their existing behavior.

**Source references:**
- `src/server.py` — replace only the retrieval branch in `call_tool`, retaining vault validation, formatting conventions, and MCP error handling.
- `src/caasiopeia_client.py` — reuse its validated `search()` call; verify its current signature before use.
- `tests/test_unit.py` — reuse the existing monkeypatch and fake database conventions without relying on a live database.
- `../caasiopeia/src/domain/context.rs` — verify `hybrid`/`dense`/`lexical` mode names and passage provenance fields before mapping OSM modes.

**Files touched:**
- `src/server.py` (modified)
- `src/caasiopeia_client.py` (modified)
- `tests/test_caasiopeia_retrieval.py` (new)
- `tests/test_unit.py` (modified)

**Commit message:**
`feat(search): delegate opt-in vault retrieval to caasiopeia`

**TDD cycle:**
- RED (failing tests to write first):
  - `tests/test_caasiopeia_retrieval.py::test_search_vault_maps_hybrid_semantic_and_keyword_modes_to_caas` — asserts `hybrid`, `dense`, and `lexical` mapping without local `embed()` or SQL ranking calls.
  - `tests/test_caasiopeia_retrieval.py::test_search_vault_selects_only_the_configured_source_for_a_vault` — asserts a valid vault name maps to exactly one configured Caasiopeia source ID.
  - `tests/test_caasiopeia_retrieval.py::test_search_vault_preserves_limit_threshold_and_empty_result_semantics` — asserts output is capped, scores are threshold-filtered, and empty passages report no Caasiopeia results rather than suggesting reindex.
  - `tests/test_caasiopeia_retrieval.py::test_search_vault_does_not_fall_back_to_local_results_when_caas_fails` — asserts an unavailable Caasiopeia response is visible and cannot silently mix stores.
- GREEN (minimal implementation to pass RED):
  - Map OSM `hybrid`, `semantic`, and `keyword` to Caasiopeia `hybrid`, `dense`, and `lexical` respectively.
  - Resolve `vault` only through the parsed source map; reject an unknown or unconfigured vault before calling Caasiopeia.
  - Use `CAASIOPEIA_TOKEN_BUDGET` for retrieval packing, then apply the existing public `limit` and `min_similarity` as display filters.
  - Render Caasiopeia provenance (`external_id`, heading path where present, score, and passage text) and return a clear degraded/unavailable message without disclosing endpoint credentials.
- REFACTOR (cleanup planned after GREEN):
  - Extract result rendering from `call_tool`.
  - Replace nested conditionals with an explicit mode mapping constant.

**Test pyramid for this iteration:**
- Smoke: `uv run python -c "from src import server; assert callable(server.call_tool)"` succeeds with the default local backend.
- Unit: four named tests in `tests/test_caasiopeia_retrieval.py` mock `CaasClient`; existing local-search tests remain unchanged.
- Integration: mocked `call_tool` to `CaasClient` verifies the complete tool-to-HTTP request mapping.
- State machine: N/A — no persistent state transitions change.
- Contract: all three exposed OSM modes map to the three current Caasiopeia modes; unknown modes retain the established OSM `hybrid` normalization.
- Regression: the no-fallback test guards against reintroducing split-brain retrieval.
- Chaos: Caasiopeia unavailable and malformed passage fixtures return a safe tool error with a trace ID, not an exception traceback.
- E2E: N/A — Caasiopeia opt-in is not yet the default and graph expansion is not enabled for Caasiopeia results.
- Performance: N/A — measure latency only after a disposable end-to-end environment exists.
- TDD Parity: all changed `search_vault` branches have named RED coverage; the legacy local branch remains covered by existing tests.
- Coverage: unknown until measured; preserve the configured 50% floor.

**Deploy + validate:**
- Install: N/A — no deployment in this iteration.
- Validate: from `../obsidian-semantic-mcp`, run `uv run pytest tests/test_caasiopeia_client.py tests/test_caasiopeia_retrieval.py tests/test_unit.py -m "not pg"`; expected result: Caasiopeia-mode tests prove no local embedding or retrieval query occurs.
- Rollback: set `OSM_RETRIEVAL_BACKEND=local`; do not delete Caasiopeia data or modify any source configuration.

**Side-effect fence:** Writable files are the listed repository files only. Tests use fake Caasiopeia responses and synthetic vault identifiers. Do not start an MCP process, contact a real Caasiopeia endpoint, run a reindex, or read secrets.

**Checkpoint evidence:** Record request/response fixtures, mode mapping, test output, and a code search proving the Caasiopeia branch does not call `embed()` or issue the local ranking SQL.

**Acceptance criteria (binary):**
- [ ] In Caasiopeia mode, each supported OSM retrieval mode produces the matching Caasiopeia mode.
- [ ] A vault filter sends only its configured Caasiopeia `source_id`.
- [ ] Caasiopeia failure never returns locally ranked notes.
- [ ] `limit` and `min_similarity` still bound the displayed result set.

**Estimated effort:** S (2h; basis: shared MCP dispatch change, output compatibility, and four behavior tests)

**Executor:** default

**Isolation:** shared

**Delegation:** in-session

**Blocked by:** Iteration 1

#### Iteration 3 — Preserve safe wikilink expansion for Caasiopeia passages

**Goal:** Add an explicit external-ID-to-local-path resolver so Caasiopeia results can use OSM's existing graph projection without trusting arbitrary paths.

**Shippable on its own?** Yes. Caasiopeia search remains usable without graph expansion; this slice safely restores the optional `graph_expand` feature where identity mapping is valid.

**Source references:**
- `src/server.py` — `expand_via_links()` reads local `notes` and `note_links`, and `_resolve_vault_path()` defines the existing vault-escape boundary.
- `src/caasiopeia_client.py` — verify the current passage type exposes `external_id` before resolving it.
- `../caasiopeia/src/domain/context.rs` — verify `external_id` provenance remains part of the response contract.

**Files touched:**
- `src/server.py` (modified)
- `src/config.py` (modified)
- `tests/test_caasiopeia_retrieval.py` (modified)
- `tests/test_unit.py` (modified)

**Commit message:**
`feat(graph): expand verified caasiopeia result paths`

**TDD cycle:**
- RED (failing tests to write first):
  - `tests/test_caasiopeia_retrieval.py::test_caasiopeia_passage_external_id_resolves_to_one_configured_vault_path` — asserts canonical Caasiopeia external IDs map to a local indexed note path.
  - `tests/test_caasiopeia_retrieval.py::test_caasiopeia_graph_expand_rejects_path_escape_and_unmapped_external_id` — asserts `../`, absolute out-of-vault paths, and unknown IDs never reach `expand_via_links`.
  - `tests/test_caasiopeia_retrieval.py::test_caasiopeia_graph_expand_uses_only_verified_seed_paths` — asserts graph expansion receives resolved local paths and produces the existing neighbor output shape.
- GREEN (minimal implementation to pass RED):
  - Add a documented source-map identity rule that maps each configured Caasiopeia source to one vault root and accepts only paths safely below that root.
  - Resolve Caasiopeia `external_id` before graph expansion; omit expansion for an unmapped passage and report the trace ID in logs only.
  - Reuse `expand_via_links()` unchanged after the resolver has produced verified paths.
- REFACTOR (cleanup planned after GREEN):
  - Extract the path resolver into a narrowly named helper.
  - Consolidate duplicate vault-root checks with `_resolve_vault_path()` where signatures permit.

**Test pyramid for this iteration:**
- Smoke: import the resolver and resolve a synthetic in-vault external ID.
- Unit: three named resolver and graph-seed tests with temporary directories and fake Caasiopeia passages.
- Integration: `call_tool(..., {"graph_expand": true})` runs through Caasiopeia rendering into a monkeypatched `expand_via_links`.
- State machine: N/A — graph projection state is read-only in this slice.
- Contract: the configured source-to-vault identity map rejects unknown keys and duplicate source IDs.
- Regression: path-escape test guards the existing vault boundary when Caasiopeia provenance is introduced.
- Chaos: malformed external IDs, missing local files, and duplicate passages yield no unsafe graph read and no traceback.
- E2E: N/A — default cutover remains deferred.
- Performance: N/A — one or two path resolutions per returned passage are not an acceptance criterion.
- TDD Parity: every new path-resolution outcome starts as a named RED test.
- Coverage: unknown until measured; preserve the configured 50% floor.

**Deploy + validate:**
- Install: N/A — repository-only change.
- Validate: from `../obsidian-semantic-mcp`, run `uv run pytest tests/test_caasiopeia_retrieval.py tests/test_unit.py -m "not pg"`; expected result: only verified paths can seed graph expansion.
- Rollback: leave `graph_expand` disabled for Caasiopeia by reverting this resolver; Caasiopeia retrieval itself remains operational.

**Side-effect fence:** Writable files are the listed repository files only. Use `tmp_path` fixtures and mocked graph calls. Do not read a real vault, start watchers, touch the live local graph database, or access Caasiopeia.

**Checkpoint evidence:** Record the identity-map syntax, resolver test results, and proof that path-escape inputs never invoke graph expansion.

**Acceptance criteria (binary):**
- [ ] Only a configured source and an in-vault external ID can seed graph expansion.
- [ ] Invalid or unmapped external IDs cannot read outside an OSM vault.
- [ ] Valid Caasiopeia passages preserve the existing neighbor output shape.

**Estimated effort:** S (2h; basis: cross-boundary identity rule, filesystem safety, and graph integration tests)

**Executor:** default

**Isolation:** shared

**Delegation:** in-session

**Blocked by:** Iteration 2

#### Iteration 4 — Make Caasiopeia the documented default and prove the complete path

**Goal:** Cut the default retrieval backend to Caasiopeia after a disposable end-to-end demo proves source scoping, ranking, and safe graph expansion.

**Shippable on its own?** Yes. The explicit `local` backend remains a documented rollback switch; default installations use the single Caasiopeia retrieval authority.

**Source references:**
- `README.md` — document only validated environment names, tool behavior, startup requirements, and rollback.
- `src/server.py` — verify startup validation and local-backend fallback behavior remain current before changing the default.
- `tests/test_e2e.py` — reuse the JSON-RPC stdio harness only after verifying it can run against a disposable fixture environment.
- `../caasiopeia/tests/mcp_contract.rs` — verify Caasiopeia HTTP/MCP parity remains a service-owned contract; OSM must exercise HTTP only.

**Files touched:**
- `src/config.py` (modified)
- `src/server.py` (modified)
- `README.md` (modified)
- `tests/test_caasiopeia_e2e.py` (new)
- `tests/test_caasiopeia_retrieval.py` (modified)

**Commit message:**
`feat(retrieval): make caasiopeia the default vault search backend`

**TDD cycle:**
- RED (failing tests to write first):
  - `tests/test_caasiopeia_e2e.py::test_stdio_search_vault_uses_disposable_caas_and_expands_verified_links` — starts the OSM stdio server with a fake disposable Caasiopeia endpoint and temporary vault projection; asserts source scope, returned passage, and one safe neighbor.
  - `tests/test_caasiopeia_retrieval.py::test_default_backend_is_caas_and_local_requires_explicit_opt_out` — asserts the default is Caasiopeia and `local` is the only rollback selection.
  - `tests/test_caasiopeia_retrieval.py::test_startup_refuses_default_caas_without_required_configuration` — asserts an installation cannot silently select an empty or global source scope.
- GREEN (minimal implementation to pass RED):
  - Change the default backend to `caasiopeia`; make source map and base URL mandatory in that mode.
  - Retain `OSM_RETRIEVAL_BACKEND=local` as explicit rollback only; do not add automatic fallback.
  - Document configuration by variable name, the source identity requirement, no-secret storage rule, demo procedure, and rollback.
- REFACTOR (cleanup planned after GREEN):
  - Remove now-redundant default-local branches without deleting the explicit rollback implementation.
  - Keep docs and tool descriptions aligned with the final behavior.

**Test pyramid for this iteration:**
- Smoke: start the MCP server in a temp environment with a disposable fake Caasiopeia endpoint and complete `initialize` plus `tools/list`.
- Unit: default-selection and startup-validation tests in `tests/test_caasiopeia_retrieval.py`.
- Integration: real stdio JSON-RPC transport calls `search_vault`, which calls the disposable HTTP endpoint and local graph resolver.
- State machine: N/A — no persistent state transition is changed.
- Contract: E2E fixture asserts required request fields and bounded response shape without using `caasiopeia-mcp`.
- Regression: explicit-local rollback test preserves the emergency escape hatch.
- Chaos: disposable endpoint returns 503; the stdio response reports failure safely and never falls back to local ranking.
- E2E: one temporary-vault, fake-Caasiopeia JSON-RPC search proves the full user path.
- Performance: N/A — establish a latency target only after real deployment measurements are approved and reproduced.
- TDD Parity: default change, configuration failure, no-fallback behavior, and end-to-end behavior all have named RED tests.
- Coverage: measure the changed files with `uv run pytest --cov=src --cov-report=term-missing`; preserve or exceed the existing 50% floor.

**Deploy + validate:**
- Install: N/A — do not install, register, or restart an MCP server in this plan.
- Validate: from `../obsidian-semantic-mcp`, run `uv run pytest tests/test_caasiopeia_client.py tests/test_caasiopeia_retrieval.py tests/test_caasiopeia_e2e.py tests/test_unit.py -m "not pg"`; expected result: all synthetic tests pass and no live network service is contacted.
- Rollback: set `OSM_RETRIEVAL_BACKEND=local` and restart only the operator-approved OSM process; no Caasiopeia release, tag, deployment, or data mutation is authorized by this plan.

**Side-effect fence:** Automated tests use a temporary HTTP fixture, temporary vault, and repository test files only. A real Caasiopeia endpoint, Keychain, local production database, watcher, MCP registration, and deployment remain out of bounds. The manual demo requires separate operator approval and a disposable environment.

**Checkpoint evidence:** Record the default/rollback behavior, E2E trace ID, request source scope, test output, coverage result, and the exact temporary fixture used.

**Acceptance criteria (binary):**
- [ ] A configured default OSM instance routes `search_vault` to Caasiopeia over HTTP.
- [ ] An unconfigured default Caasiopeia instance fails startup instead of searching locally or globally.
- [ ] `OSM_RETRIEVAL_BACKEND=local` is the only tested rollback path.
- [ ] The disposable JSON-RPC demo returns a Caasiopeia passage and one verified graph neighbor.

**Estimated effort:** S (2h; basis: default cutover, temporary stdio/HTTP E2E fixture, documentation, and rollback coverage)

**Executor:** default

**Isolation:** shared

**Delegation:** in-session

**Blocked by:** Iteration 3

## 4. Test inventory summary

| Iter | Smoke | Unit | Integration | State machine | Contract | Regression | Chaos | E2E | Performance | TDD Parity | Coverage Δ |
|------|-------|------|-------------|---------------|----------|------------|-------|-----|-------------|------------|------------|
| 1 | 1 | 4 | 0 | N/A | 1 | existing local tests | 4 | N/A | N/A | 100% of new client behavior | unknown; preserve floor |
| 2 | 1 | 4 | 1 | N/A | 1 | 1 | 2 | N/A | N/A | 100% of changed branches | unknown; preserve floor |
| 3 | 1 | 3 | 1 | N/A | 1 | 1 | 3 | N/A | N/A | 100% of resolver branches | unknown; preserve floor |
| 4 | 1 | 2 | 1 | N/A | 1 | 1 | 1 | 1 | N/A | 100% of default/cutover behavior | measure; preserve floor |

## 4b. Effort summary

| Iter | Size | Duration | Basis |
|------|------|----------|-------|
| 1 | S | 2h | new HTTP boundary, configuration validation, synthetic contract tests |
| 2 | S | 2h | shared MCP dispatch, result compatibility, behavior tests |
| 3 | S | 2h | source/path identity boundary and safe graph integration |
| 4 | S | 2h | default cutover, temporary E2E fixture, rollback/docs |

**Estimated total:** 8h of sequential work (four dependent iterations). External waiting for Caasiopeia contract clarification or operator-approved manual validation is excluded.

## 5. End-to-end definition of done

- All four iteration acceptance criteria are checked with recorded RED then GREEN evidence.
- `search_vault` uses the Caasiopeia HTTP API by default, accepts only configured source scopes, never silently falls back to local ranking, and applies graph expansion only to verified in-vault Caasiopeia provenance.
- OSM filesystem CRUD, watcher, and local graph projection continue to work; this v1 does not remove their storage.
- Demo script: start a temporary fake Caasiopeia HTTP fixture and temporary vault projection, launch OSM over stdio with `OSM_RETRIEVAL_BACKEND=caasiopeia`, call `initialize`, `tools/list`, then `search_vault` with `graph_expand=true`; verify one scoped Caasiopeia passage, one verified neighbor, and no local ranking call.
- Required automated command from `../obsidian-semantic-mcp`: `uv run pytest tests/test_caasiopeia_client.py tests/test_caasiopeia_retrieval.py tests/test_caasiopeia_e2e.py tests/test_unit.py -m "not pg"` must return green. Then run `uv run pytest --cov=src --cov-report=term-missing` and preserve the configured coverage floor.

## 6. Out of scope

- Removing OSM's `notes`/`note_links` projection — deferred because `graph_expand`, indexing status, and dashboard behavior consume it; separate migration and data-retention decision required.
- Pushing notes from OSM into Caasiopeia — deferred because the existing Caasiopeia `vault` source is assumed as an external prerequisite; source ownership and lifecycle need a separate plan.
- Calling `caasiopeia-mcp` from OSM — deferred permanently for this integration because it is an LLM-facing stdio adapter, not a service-to-service client contract.
- Caasiopeia API changes — deferred unless Iteration 1 proves a required provenance field is absent; that would need an approved cross-repository plan.
- Live deployment, MCP registration, Caasiopeia release/tag/publish, Keychain changes, and real-vault migration — deferred because none is authorized by this plan.

## 7. Open questions

- What exact canonical `external_id` format does the configured Caasiopeia `vault` source emit for each OSM vault? Iteration 1 must inspect a disposable or already-approved non-production response and either validate the source-map rule or stop before Iteration 3.
- Is the existing Caasiopeia `vault` source complete and current enough to become OSM's default retrieval corpus? This requires an owner-approved, reproducible corpus validation before Iteration 4.

## Build outcome — 2026-09-29

- Shipped: iterations 1, 2 and 3 in full, and the opt-in part of iteration 4 (stdio E2E, README section, compose pass-through), in one commit on `feat/caasiopeia-client`.
- Deviations from plan:
  - The default backend was NOT changed to `caasiopeia`. Open question 2 requires an owner-approved corpus validation before iteration 4, and no Caasiopeia service was available. `local` stays the default; the default-cutover RED tests were not written.
  - The trace header is `x-caas-trace-id` (Caasiopeia `src/domain/context.rs`), not the `x-caasiopeia-trace-id` this plan named.
  - Wikilink expansion is seeded from `external_id` read as a vault-root-relative, forward-slash path with one source per vault. That comes from the Caasiopeia sync plan, not from a live response (open question 1 is still open).
  - The stdio E2E stubs `expand_via_links` and the background indexer in the subprocess, because the suite may not require PostgreSQL. The transport, the HTTP client, the source scoping and the resolver over a temporary vault are real.
  - `docker-compose.yml` was touched (not in the plan) so the opt-in variables reach the containerized server.
  - The E2E tests were written after the implementation, so they had no RED phase.
  - One commit instead of one per iteration, because the commit authorization covered a single operation.
- Learned: Caasiopeia rejects unknown request fields, so the client sends only the documented ones. The docker install mounts the vault at `/vault`, so its `CAASIOPEIA_SOURCE_MAP` key is `vault`. Full-suite time was 1h50 on this host (6630s), so run it in the background.
