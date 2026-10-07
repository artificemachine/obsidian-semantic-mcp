# PLAN — live check of the OSM Caasiopeia backend

## Scope
Run a real Caasiopeia instance against a throwaway Postgres and validate the
opt-in `OSM_RETRIEVAL_BACKEND=caasiopeia` path end to end: response contract,
OSM client parsing, rendering, and graph-seed resolution for both possible sync
roots. No OSM code changes, no commit, nothing touches the existing
`caasiopeia-pg-vchord` database or the real vault. Rejected alternative: reuse
the existing database (unknown contents, so rejected).

## Iterations
#### Iteration 1 — throwaway stack and live query
**Goal:** a live `POST /v1/context` answered to the OSM client.
**Files touched:** none in this repo.
**Commit message:** none (no commit authorized).
**TDD cycle:**
- RED: N/A, operational check, no repo behavior changes.
- GREEN: N/A.
- REFACTOR: None.
**Test pyramid:** smoke = live query returns 200 with passages; integration =
OSM client and `_search_vault_caasiopeia` against the live service; the rest N/A.
**Acceptance (binary):**
- [x] HTTP 200 with passages from the synced synthetic vault.
- [x] `CaasClient.search` parses the live response.
- [x] Root = vault dir: every seed resolves to a real file.
- [x] Root = `notes/` subfolder: seeds resolve to `None` (recorded).
**Blocked by:** None

## Definition of done
Throwaway container, service, worker and secret files removed; findings
recorded below and in `HANDOFF.md`.

## Build outcome — 2026-09-29
- Shipped: nothing committed; live check only.
- Deviations from plan: config passed through a scratchpad env file, not
  `.env` (guardrail); branch `chore/caasiopeia-live-check` created only to
  satisfy the not-on-main preflight; full test suite not rerun (no repo code
  changed).
- Learned: the live response carries `degraded`, `degradation_reason`,
  `model_id` beside `passages`; the worker needs an owner-registered
  `embedding_spaces` row and rejects `markdown-v1` when the shell exports
  `CAAS_CONTEXTUALIZER_ENDPOINT`; a sync root that is a vault subfolder makes
  graph expansion skip every seed (logged at info level only).
