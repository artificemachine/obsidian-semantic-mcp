# Plan: Search Observability and Langfuse Provenance

## 1. Scope summary

Build privacy-safe search observability: durable OSM rollups, dashboard and
Prometheus exposure, then Langfuse trace metadata through the existing bridge.
No raw query, note path, passage, parameter, or result content may be persisted
or exported.

Smallest v1: OSM records aggregate-only backend, fallback, latency, result, and
error metrics and exposes them at `/api/stats` and `/metrics`.

Sources: `src/server.py`, `src/dashboard.py`, and
`../langfuse-bridge-mcp/docs/DESIGN-langfuse-bridge-mcp.md`. No
`docs/REGISTER-*.md` exists.

## 2. Prerequisites

- Baseline: `72a9b068f9bc`; last suite: 539 passed, 31 skipped. Current
  collection: 570 tests.
- OSM services: PostgreSQL and dashboard. Langfuse bridge credentials remain
  only in `~/.config/langfuse-bridge-mcp/credentials.env`.
- `PLAN_CHECK` must be restored before implementation: set `CANONICAL_REPO` to
  the canonical skills checkout containing `tools/plan_check.py`.
- Execution architecture: sequential. Only the OSM rollup slice is ready;
  dashboard work depends on its schema, while bridge work is blocked by that
  private repository's `plan_approved` lifecycle state.

## 3. Iterations

### Iteration 1 — Durable privacy-safe search rollups

**Goal:** Persist bounded hourly aggregates for every real `search_vault`
outcome.

**Shippable on its own?** Yes.

**Source references:**

- `src/server.py` — `init_db()`, Caasiopeia and local search paths.
- `tests/test_caasiopeia_retrieval.py` — backend and fallback fixtures.

**Files touched:**

- `src/server.py` (modified)
- `tests/test_search_metrics.py` (new)

**Commit message:** `feat(metrics): persist privacy-safe search rollups`

**TDD cycle:**

- RED: direct CaaS, local, fallback, error, and metrics-write-failure tests.
- GREEN: additive rollup table keyed by UTC hour/backend/mode/outcome; record count,
  result count, total duration, fallback, and degraded flags.
- REFACTOR: extract one metric-recording helper; ensure nested fallback records
  once.

**Test pyramid for this iteration:**

- Smoke: import server and initialize a synthetic schema.
- Unit: five outcome paths and no raw query storage.
- Integration: fake PostgreSQL upsert and 24 UTC-hour-bucket aggregation.
- State machine: CaaS to local fallback; direct local; terminal error.
- Contract: only allowlisted enum labels become dimensions.
- Regression: metrics database failure never changes a search result.
- Chaos: unavailable metrics write logs a safe warning and returns the original
  result.
- E2E: N/A — dashboard exposure is iteration 2.
- Performance: recording must not add a network call.
- TDD Parity: 100% of new outcomes.
- Coverage: baseline unknown; no threshold reduction.

**Deploy + validate:**

- Install: N/A — source-only slice.
- Validate: `uv run pytest tests/test_search_metrics.py tests/test_caasiopeia_retrieval.py -q`
- Rollback: application rollback ignores the additive table.

**Side-effect fence:** Repository and synthetic PostgreSQL fixtures only.

**Acceptance criteria:**

- [ ] No metric field contains raw query or result text.
- [ ] Each completed search increments exactly one rollup.
- [ ] A metrics write failure does not fail `search_vault`.

**Estimated effort:** S (2h; schema, nested fallback semantics, five behavior
tests).

**Executor:** default

**Isolation:** shared

**Delegation:** in-session

**Blocked by:** None

### Iteration 2 — Dashboard and Prometheus search metrics

**Goal:** Expose the rollups in dashboard cards, `/api/stats`, and Prometheus
`/metrics`.

**Shippable on its own?** Yes.

**Source references:**

- `src/dashboard.py` — `gather_stats()`, `DashboardHandler.do_GET()`, and the
  HTML refresh loop.
- `tests/test_dashboard_smoke.py` — API and live-dashboard contracts.

**Files touched:**

- `src/dashboard.py` (modified)
- `tests/test_dashboard_metrics.py` (new)
- `tests/test_dashboard_smoke.py` (modified)

**Commit message:** `feat(metrics): expose search metrics in dashboard`

**TDD cycle:**

- RED: assert the 24 UTC-hour-bucket stats schema and Prometheus exposition labels.
- GREEN: add search cards plus `/metrics`; render aggregate-only dimensions.
- REFACTOR: share one rollup reader between JSON and Prometheus renderers.

**Test pyramid for this iteration:**

- Smoke: `GET /api/stats` and `GET /metrics` return 200.
- Unit: aggregation shape and Prometheus escaping.
- Integration: seeded rollups appear identically in both surfaces.
- State machine: N/A — read-only aggregation.
- Contract: fixed metric names and labels.
- Regression: unknown GET paths retain current SPA behavior.
- Chaos: absent rollup table degrades to zero metrics, not HTTP 500.
- E2E: dashboard displays the 24 UTC-hour-bucket search count and effective backend.
- Performance: `/api/stats` remains below its existing 15-second contract.
- TDD Parity: 100%.
- Coverage: measured after implementation; no lowering.

**Deploy + validate:**

- Install: `cd "$HOME/.local/share/obsidian-semantic-mcp" && osm rebuild`
- Validate: `curl -fsS http://localhost:8484/api/stats` and `curl -fsS http://localhost:8484/metrics`
- Rollback: rebuild the prior image; the additive rollup table remains harmless.

**Side-effect fence:** Repository and synthetic fixtures, then local OSM only
after explicit deployment approval.

**Acceptance criteria:**

- [ ] `/metrics` contains requests, duration, results, fallback, and error series.
- [ ] Dashboard shows the last-24 UTC-hour-bucket aggregates.
- [ ] No endpoint returns a query, note path, or passage.

**Estimated effort:** S (2h; API, exposition format, UI, and contracts).

**Executor:** default

**Isolation:** shared

**Delegation:** in-session

**Blocked by:** Iteration 1

### Iteration 3 — Minimal provenance capture in the local wrapper

**Goal:** Produce backend/fallback enums from real OSM response markers without logging requests or results.

**Shippable on its own?** Yes, local installation only; keylogger is local-only.

**Source references:**
- `../keylogger-mcp/src/keylogger_mcp/wrapper.py` — transparent byte forwarding and metadata summaries.

**Files touched:**
- `../keylogger-mcp/src/keylogger_mcp/wrapper.py` (modified)
- `../keylogger-mcp/tests/test_wrapper_provenance.py` (new)
- `../keylogger-mcp/README.md` (modified)

**Commit message:** `feat(capture): record allowlisted OSM retrieval provenance`

**TDD cycle:**
- RED: real wrapper summaries lack provenance while forwarded bytes remain identical.
- GREEN: opt in only for obsidian-semantic; correlate search request IDs in bounded temporary memory; extract strict response-prefix enums and discard payload.
- REFACTOR: centralize enum validation and bounded correlation cleanup.

**Test pyramid for this iteration:**
- Smoke: wrapper summaries remain parseable JSONL.
- Unit: marker allowlist and request matching.
- Integration: forwarding synthetic JSONRPC emits metadata-only rows.
- State machine: out-of-order replies, errors, and pending-ID eviction.
- Contract: backend enum and fallback boolean only; no parameters or results.
- Regression: byte-identical forwarding and unchanged generic summaries.
- Chaos: malformed payloads and conflicting markers omit provenance.
- E2E: generated rows consumed by bridge tests in iteration 4.
- Performance: bounded pending map; no additional network operation.
- TDD Parity: every new capture branch has a failing-before test.
- Coverage: unchanged thresholds; measure affected branches.

**Deploy + validate:**
- Install: build and install a pinned wheel; never push this local-only repository.
- Validate: `uv run --project ../keylogger-mcp pytest ../keylogger-mcp/tests/test_wrapper_provenance.py -q`
- Rollback: reinstall previous artifact; remove only the added OSM capture configuration.

**Side-effect fence:** Local wrapper repository and synthetic fixtures; local installation only after tests.

**Acceptance criteria:**
- [ ] Wire bytes are unchanged.
- [ ] Only correlated OSM search responses produce allowlisted provenance.
- [ ] Log rows contain no parameters or result text.

**Estimated effort:** XS (30 min; bounded ID correlation, prefix allowlist, and synthetic forwarding tests).

**Executor:** default

**Isolation:** shared

**Delegation:** scoped worker

**Blocked by:** None

### Iteration 4 — Bridge-derived Langfuse provenance

**Goal:** Add only the allowlisted effective-backend and fallback enums to
`search_vault` Langfuse spans.

**Shippable on its own?** Yes.

**Source references:**

- `../langfuse-bridge-mcp/src/langfuse_bridge_mcp/parser.py` — JSONL boundary.
- `../langfuse-bridge-mcp/src/langfuse_bridge_mcp/trace_builder.py` — current
  metadata privacy contract.
- `../langfuse-bridge-mcp/tests/test_trace_builder.py` — span metadata
  assertions.
- `../langfuse-bridge-mcp/docs/DESIGN-langfuse-bridge-mcp.md` — required
  privacy-contract update.

**Files touched:**

- `../langfuse-bridge-mcp/src/langfuse_bridge_mcp/parser.py` (modified)
- `../langfuse-bridge-mcp/src/langfuse_bridge_mcp/trace_builder.py` (modified)
- `../langfuse-bridge-mcp/tests/fixtures/session_osm_provenance.jsonl` (new)
- `../langfuse-bridge-mcp/tests/test_trace_builder.py` (modified)
- `../langfuse-bridge-mcp/docs/DESIGN-langfuse-bridge-mcp.md` (modified)

**Commit message:** `feat(tracing): add safe OSM retrieval provenance`

**TDD cycle:**

- RED: response marker yields only `caasiopeia`, `local`, or `local_fallback`.
- GREEN: parse the strict marker locally, discard the response immediately,
  and export only enums.
- REFACTOR: centralize allowlist validation.

**Test pyramid for this iteration:**

- Smoke: fixture to one trace to one `tools/call` span.
- Unit: valid marker, malformed marker, absent marker, and fallback marker.
- Integration: shipper receives metadata without payload content.
- State machine: request/response pairing with and without provenance.
- Contract: exact allowlisted metadata keys; raw content absent.
- Regression: existing generic MCP traces retain their four current metadata
  fields.
- Chaos: malformed JSON/result never adds provenance.
- E2E: staging-only Langfuse smoke after bridge approval.
- Performance: N/A — no new network call.
- TDD Parity: 100%.
- Coverage: bridge baseline measured there; no lowering.

**Deploy + validate:**

- Install: N/A until the bridge task is `plan_approved`.
- Validate: `uv run --project ../langfuse-bridge-mcp pytest ../langfuse-bridge-mcp/tests/test_trace_builder.py ../langfuse-bridge-mcp/tests/test_shipper.py -q`
- Rollback: disable provenance extraction; existing metadata remains intact.

**Side-effect fence:** Bridge repository and fixtures only; no credentials,
daemon, or live Langfuse writes.

**Acceptance criteria:**

- [ ] Langfuse receives backend/fallback enums only.
- [ ] It never receives MCP parameters or result text.
- [ ] Existing bridge traces remain compatible.

**Estimated effort:** S (2h; privacy contract, parser boundary, and
cross-project approval).

**Executor:** default

**Isolation:** worktree

**Delegation:** in-session

**Blocked by:** Iterations 1–3 and bridge `plan_approved`

### Iteration 5 — Controlled local activation

**Goal:** Enable OSM capture through the existing keylogger-to-bridge path and
prove one trace end to end.

**Shippable on its own?** Yes.

**Source references:**

- `../langfuse-bridge-mcp/docs/RUNBOOK-operations.md`
- `../langfuse-bridge-mcp/docs/RUNBOOK-credentials.md`
- `../langfuse-ops/README.md`

**Files touched:** N/A — operator configuration only.

**Commit message:** N/A — deployment receipt only.

**TDD cycle:**

- RED: N/A — operational activation.
- GREEN: run one real `search_vault` through the wrapped MCP configuration.
- REFACTOR: N/A.

**Test pyramid for this iteration:**

- Smoke: `langfuse-bridge-mcp status`.
- Unit: N/A.
- Integration: bridge `once` processes one OSM JSONL session.
- State machine: bridge queue empty to queued to flushed.
- Contract: expected `mcp:obsidian-semantic` span has only allowed metadata.
- Regression: normal OSM search still succeeds if bridge is stopped.
- Chaos: stop bridge; MCP search remains unaffected.
- E2E: Langfuse UI displays one OSM span with backend enum.
- Performance: no MCP latency dependency on bridge.
- TDD Parity: N/A — operational slice.
- Coverage: N/A.

**Deploy + validate:**

- Install: re-read the current bridge runbook before changing local service
  configuration.
- Validate: run a real OSM `search_vault`; verify one
  `mcp:obsidian-semantic` trace in Langfuse.
- Rollback: remove only the OSM wrapper registration and restart bridge; OSM
  stays functional.

**Side-effect fence:** Explicitly approved local bridge configuration and
self-hosted Langfuse only; never production secrets in shell arguments or
files.

**Acceptance criteria:**

- [ ] One successful search produces one Langfuse trace.
- [ ] Trace metadata includes only allowlisted fields.
- [ ] Stopping the bridge does not interrupt MCP search.

**Estimated effort:** XS (30 min excluding owner credential/configuration
wait).

**Executor:** default

**Isolation:** shared

**Delegation:** in-session

**Blocked by:** Iteration 4 and operator credentials

## 4. Test inventory summary

| Iter | Smoke | Unit | Integration | State machine | Contract | Regression | Chaos | E2E | Performance | TDD Parity | Coverage delta |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| 1 | 1 | 5 | 1 | 3 | 1 | 1 | 1 | 0 | N/A | 100% | measured |
| 2 | 2 | 3 | 1 | N/A | 1 | 1 | 1 | 1 | 1 | 100% | measured |
| 3 | 1 | 4 | 1 | 2 | 1 | 1 | 1 | 1 | N/A | 100% | measured |
| 4 | 1 | N/A | 1 | 1 | 1 | 1 | 1 | 1 | 1 | N/A | N/A |

## 4b. Effort summary

| Iter | Size | Duration | Basis |
|---|---|---:|---|
| 1 | S | 2h | durable schema and fallback-safe metric ownership |
| 2 | S | 2h | API, exposition format, UI, and contracts |
| 3 | S | 2h | privacy contract across a private dependent repository |
| 4 | XS | 30 min | controlled activation after dependencies are ready |

**Estimated total:** 6h30 sequential, excluding bridge lifecycle approval and
credential wait.

## 5. End-to-end definition of done

- [ ] OSM dashboard and `/metrics` expose aggregate-only search metrics.
- [ ] CaaS fallback is distinguished from direct local search.
- [ ] Langfuse has one privacy-safe OSM trace with backend provenance.
- [ ] A stopped bridge cannot affect MCP search.

Manual demo: run `search_vault`, confirm dashboard counters, scrape `/metrics`,
then verify the matching `mcp:obsidian-semantic` Langfuse trace.

Final commands:

```text
uv run pytest tests/test_search_metrics.py tests/test_dashboard_metrics.py tests/test_dashboard_smoke.py tests/test_caasiopeia_retrieval.py tests/test_caasiopeia_e2e.py -q
uv run --project ../langfuse-bridge-mcp pytest ../langfuse-bridge-mcp/tests/test_trace_builder.py ../langfuse-bridge-mcp/tests/test_shipper.py -q
```

## 6. Out of scope

- Direct Langfuse SDK in OSM — duplicates the existing bridge and adds
  credentials to OSM.
- Query/result export — prohibited by the bridge privacy contract.
- Percentile histograms — defer until aggregate count/sum metrics prove
  insufficient.
- Changes to `langfuse-ops` infrastructure — the existing endpoint is
  sufficient.

## 7. Open questions

1. Should Langfuse receive only backend/fallback provenance, or should the
   bridge also derive a safe `result_count` enum/bucket? The recommendation is
   backend/fallback only.

## Execution reference correction — 2026-10-07

- Normalized test-pyramid block labels for the canonical eleven-pass checker.
- Qualified bridge test paths against this plan's OSM repository root; the test targets and acceptance criteria are unchanged.
- Checker located at the canonical skills checkout tools/plan_check.py.

## Authorized execution amendments — 2026-10-07

- Owner approved minimal keylogger capture before bridge extraction; the previous bridge-only response parsing could not work with metadata-only logs. Wrapper code stays local-only, OSM is public, and bridge remains private.
- Owner requested advice for the daily-versus-24-hour mismatch. Selected bounded hourly aggregates: current UTC hour plus previous 23 buckets, explicitly labelled in the dashboard; no claim of a precise second-level rolling window. Retain at most 720 UTC hourly buckets.
- Execute independent capture and rollup slices with scoped subagents; dashboard waits for rollup schema, bridge waits for generated capture rows, activation waits for both. Original dependent slices remain serialized.
- Langfuse receives backend/fallback only. No result-count bucket is exported.
- Revised active-work estimate: 7h sequential equivalent; independent capture may overlap OSM work. External CI and credential waits are separate.
