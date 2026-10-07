# Retrieval backend choice for new installations

## 1. Scope summary

Add an explicit local/Caasiopeia choice to osm init for every existing supported installation mode, persist non-secret selection settings, validate Caasiopeia before activation, and keep that choice across restart/rebuild. Ranking, fallback behavior and corpus ingestion stay as currently implemented. ChatGPT Desktop and Langfuse are excluded by owner decision.

Smallest v1: local remains the compatible default; Caasiopeia is selected explicitly and requires an existing scoped service plus a runtime credential.

Sources: docs/PLAN-caasiopeia-retrieval-delegation.md, docs/PLAN-codex-chatgpt-desktop-mcp-registration.md, docs/ARCH-caasiopeia-obsidian-tessera-relations.md. This choice supersedes the older proposal to make Caasiopeia the unconditional default. No docs/REGISTER-*.md exists for this programme; no register rows are claimed.

## 2. Prerequisites

- Observed checkout revision: 67be775e805b6051b3047de4a67b9e6c96254dff; current remote main: 5ad396126c5e6f5340e21126f48997b863f40c61. Rebase a task branch onto current remote main before implementation; preserve unrelated untracked docs and all protected instruction files.
- Observed baseline on 2026-10-07: 637 passed, 31 skipped, reproduced full-suite runs and green CI. These are pre-plan results, not fresh tests of this feature. Existing broad Ruff findings are baseline debt; require no new findings rather than asserting whole-repo lint passes. Coverage percentage is unknown; preserve the configured 40% floor and measure before changes.
- Python/uv and existing project dependencies suffice; no new library. Docker is required only for the isolated Docker acceptance flow. Native mode retains current PostgreSQL/Ollama prerequisites.
- Canonical checker exists at ../skills-canonical/tools/plan_check.py; python3 resolves to the installed Python shim. Resolve CANONICAL_REPO to ../skills-canonical and PLAN_CHECK to its tools/plan_check.py. Authoring command: python3 "$PLAN_CHECK" "$SCRATCH_PLAN" --caller plan-iter --repo-root .; repeat against the saved plan before execution. An explicit PLAN_CHECK override takes precedence.
- Existing code: osm_init.py (write_env, mode_* handlers, cmd_init, _build_or_pull_custom_services), src/launcher.py (version-1 private runtime), src/config.py (backend/settings validation), src/caasiopeia_client.py (search protocol), docker-compose.yml (existing pass-through), and tests/conftest.py (host-write fences). Verify exact current signatures before reuse.
- Risk: host and container URLs differ; every vault basename needs exactly one source UUID and a correct source root; API keys expire and must be supplied again through environment. No secret lookup, minting, new credentials file, or fallback authentication is authorized by this plan. Missing credentials stop activation; unrelated independent work may continue.
- Private settings are outside the development checkout. No credentials are persisted in newly generated runtime JSON, .env, MCP configuration, command arguments, snapshots or logs. Existing credential stores remain owner-managed.

**Execution architecture:** sequential. Every slice writes the shared setup artifact osm_init.py and tests/test_retrieval_setup.py, so the shared-artifact condition requires serialized writers. Orchestrator + subagents is rejected for implementation: no independent writable slice here, and extra workers would serialize on the same files. Agent mechanisms exist; absence is not assumed. Execute with the configured model, no model switch or new delegation. A separately authorized read-only review may audit credential and persistence boundaries.

## 3. Iterations

#### Iteration 1 — Persist native retrieval settings without credentials

**Goal:** Make the installed native launcher retain a validated backend and non-secret Caasiopeia settings, with old runtime files remaining valid.

**Shippable on its own?** Yes — existing installations remain functional; this slice adds a complete bounded capability.

**Source references:**
- osm_init.py — verify current setup, environment writing, rebuild and removal signatures before reuse.
- src/launcher.py — native runtime validation/loading and installed-path resolution.
- src/config.py — reuse resolve_retrieval_backend(env) and load_caasiopeia_settings(vault_paths, env); verify their current validation contract first.
- src/caasiopeia_client.py — existing scoped search client and typed failures.
- docker-compose.yml — existing Caasiopeia environment pass-through.
- tests/conftest.py — synthetic config/vault isolation.

**Files touched:**
- osm_init.py (modified)
- src/launcher.py (modified)
- tests/test_launcher.py (modified)
- tests/test_retrieval_setup.py (new)
- CHANGELOG.md (modified)

**Commit message:**
`feat(setup): persist native retrieval settings without credentials`

**TDD cycle:**
- RED (failing tests to write first):
  - `tests/test_retrieval_setup.py::test_native_retrieval_settings_round_trip` — selection, URL and source mappings reach the installed launcher.
  - `tests/test_retrieval_setup.py::test_native_credentials_never_persist` — API keys are absent from stored runtime data and client entries.
  - `tests/test_retrieval_setup.py::test_legacy_native_runtime_remains_valid` — old version-1 files still load with local defaults.
  - `tests/test_retrieval_setup.py::test_native_explicit_backend_override` — explicit environment selection takes precedence without mixing incompatible settings.
- GREEN (minimal implementation to pass RED):
  - Extend write_native_runtime(vaults, db_url, config_dir=None) and the osm_init wrapper with optional non-secret retrieval settings. Keep version-1 legacy input readable. Validate permitted values and reject key material in persisted retrieval fields. Read credentials only from the process environment, validate the effective merged Caasiopeia configuration before server launch, and retain owner-only atomic writes and narrow removal.
- REFACTOR (cleanup planned after GREEN):
  - Extract the shared non-secret retrieval-settings resolution boundary only when duplicated; otherwise None.

**Test pyramid for this iteration:**
- Smoke: Native launcher loads a synthetic runtime file and reaches a mocked server entry point.
- Unit: 8 cases covering allowed fields, malformed data, legacy input and precedence.
- Integration: 2 installed-launcher/runtime round trips with synthetic paths and injected credentials.
- State machine: legacy runtime → local; new local → local; new Caasiopeia + credential → Caasiopeia; missing credential → actionable error.
- Contract: 2 allowlist/privacy cases: persisted fields exclude CAASIOPEIA_API_KEY; generated MCP client entries remain unchanged.
- Regression: Existing TestNativeRuntime/launcher tests protect the recently shipped private-file, explicit Docker selector and legacy .env behavior.
- Chaos: 1 malformed/unsafe runtime case preserves existing bytes; no live file access.
- E2E: N/A — transport configuration is usable, but the setup prompt is delivered in iteration 2.
- Performance: N/A — no ranking/latency target changes; network preflight has a bounded 10-second request timeout with no automatic retry loop.
- TDD Parity: Every changed behavior above has a named RED test; record failures before implementation and passes afterwards. Existing compatibility tests must remain green. Installation-only checks are observed separately, not counted as RED evidence.
- Coverage: Measure before changes; preserve the configured floor and report focused module coverage after this slice. No invented percentage delta.

**Deploy + validate:**
- Install: `uv sync` at repository root for fixture-only verification; final artifact check uses `uv build --wheel` and `uv pip install --python <temporary-venv>/bin/python <built-wheel>`. No editable production install.
- Validate: `uv run pytest tests/test_retrieval_setup.py tests/test_launcher.py -q` at repository root; expected exit 0 with every newly named behavior collected. Use temporary HOME/config/deployment directories and mocks for all external processes.
- Rollback: Restore this slice's files on the task branch; temporary instance configuration is backed up before replacement and restored on failure. Never revert another agent's work or uninstall the live stack.

**Side-effect fence:** Repository files named above and disposable temporary fixtures only. No live Docker, PostgreSQL, Ollama, client configuration, vault or secret-store mutations. No commits, pushes, merge or production installation are implied by approving only a plan.

**Checkpoint evidence:** Base revision, RED/GREEN outputs, changed paths, coverage, generated-config privacy checks and observable acceptance results; redact credentials and do not store result text. 

**Acceptance criteria (binary):**
- [ ] Selection, URL and source mappings reach the installed launcher.
- [ ] API keys are absent from stored runtime data and client entries.
- [ ] Old version-1 files still load with local defaults.
- [ ] Explicit environment selection takes precedence without mixing incompatible settings.
- [ ] Existing local installation and configuration tests still pass.

**Estimated effort:** XS (45 min; basis: one existing runtime contract extension, two adapters and focused compatibility/privacy tests)

**Executor:** default

**Isolation:** shared

**Delegation:** in-session

**Blocked by:** None

#### Iteration 2 — Offer both engines in every setup mode

**Goal:** Let new installations choose local or Caasiopeia explicitly through osm init and persist the selected configuration in Docker or native mode.

**Shippable on its own?** Yes — existing installations remain functional; this slice adds a complete bounded capability.

**Source references:**
- osm_init.py — verify current setup, environment writing, rebuild and removal signatures before reuse.
- src/launcher.py — native runtime validation/loading and installed-path resolution.
- src/config.py — reuse resolve_retrieval_backend(env) and load_caasiopeia_settings(vault_paths, env); verify their current validation contract first.
- src/caasiopeia_client.py — existing scoped search client and typed failures.
- docker-compose.yml — existing Caasiopeia environment pass-through.
- tests/conftest.py — synthetic config/vault isolation.

**Files touched:**
- osm_init.py (modified)
- tests/test_retrieval_setup.py (modified)
- tests/test_osm_commands.py (modified)
- README.md (modified)
- CHANGELOG.md (modified)

**Commit message:**
`feat(setup): offer both engines in every setup mode`

**TDD cycle:**
- RED (failing tests to write first):
  - `tests/test_retrieval_setup.py::test_init_backend_prompt_and_flag` — the interactive choice and --retrieval-backend local|caasiopeia converge.
  - `tests/test_retrieval_setup.py::test_caas_setup_preflight_before_mutations` — missing settings, authentication failure and timeout stop before setup writes or service changes.
  - `tests/test_retrieval_setup.py::test_local_setup_ignores_stale_caas_settings` — local selection neither contacts Caasiopeia nor forwards its credentials.
  - `tests/test_retrieval_setup.py::test_init_backend_choice_reaches_every_mode` — each existing installation mode receives the same selection and complete per-vault mapping.
  - `tests/test_retrieval_setup.py::test_docker_selection_written_without_key` — the deployment .env contains non-secret settings while the key stays in process/container environment.
  - `tests/test_retrieval_setup.py::test_setup_dry_run_redacts_credentials` — dry-run performs no writes/network calls and prints no credential.
- GREEN (minimal implementation to pass RED):
  - Add the non-secret --retrieval-backend flag and one prompt shared by every install mode. Selection precedence: explicit flag, explicit OSM_RETRIEVAL_BACKEND, existing saved choice, then an interactive prompt defaulting to local; noninteractive legacy invocations without a choice use local. Resolve Caasiopeia URL and per-vault source/root mappings from existing named environment variables; prompt only for missing non-secret fields. Require CAASIOPEIA_API_KEY through the environment, with no new key flag or credential minting. Reuse configuration validation and a bounded existing CaasClient.search call using a fixed public installation-check query, source-scoped to configured vaults, without printing results. Check typed failures before setup mutates files or services. For Docker, correctly translate a host localhost URL to host.docker.internal only when needed for the existing supported transport, while probing from the host with the original URL. Persist selection/non-secret settings in the private deployment .env or native runtime; pass credentials via environment only. Preserve existing data, other configuration keys and client registrations. Document both setup paths and where credentials must be supplied.
- REFACTOR (cleanup planned after GREEN):
  - Extract the shared non-secret retrieval-settings resolution boundary only when duplicated; otherwise None.

**Test pyramid for this iteration:**
- Smoke: CLI help exposes --retrieval-backend; each mode accepts a mocked explicit local choice.
- Unit: 10 cases for choices, precedence, parameter parsing and per-vault mappings.
- Integration: 4 wizard-to-mode/storage tests, with service calls stubbed and vaults/config rooted in tmp_path.
- State machine: fresh → local; fresh → configured Caasiopeia; existing choice → retained choice; explicit override → validated replacement; invalid choice/config/auth → unchanged installation.
- Contract: 2 cases for complete source scope and zero keys in generated .env/native/client configuration.
- Regression: Existing setup tests retain --mode behavior, vault discovery, storage selection and local installation support.
- Chaos: 3 typed preflight failures: timeout, HTTP401 and HTTP503; no writes or fallback selection.
- E2E: 2 setup workflows against a loopback HTTP fixture: local without Caasiopeia calls and Caasiopeia with a scoped successful request.
- Performance: N/A — no ranking/latency target changes; network preflight has a bounded 10-second request timeout with no automatic retry loop.
- TDD Parity: Every changed behavior above has a named RED test; record failures before implementation and passes afterwards. Existing compatibility tests must remain green. Installation-only checks are observed separately, not counted as RED evidence.
- Coverage: Measure before changes; preserve the configured floor and report focused module coverage after this slice. No invented percentage delta.

**Deploy + validate:**
- Install: `uv sync` at repository root for fixture-only verification; final artifact check uses `uv build --wheel` and `uv pip install --python <temporary-venv>/bin/python <built-wheel>`. No editable production install.
- Validate: `uv run pytest tests/test_retrieval_setup.py tests/test_osm_commands.py tests/test_launcher.py -q` at repository root; expected exit 0 with every newly named behavior collected. Use temporary HOME/config/deployment directories and mocks for all external processes.
- Rollback: Restore this slice's files on the task branch; temporary instance configuration is backed up before replacement and restored on failure. Never revert another agent's work or uninstall the live stack.

**Side-effect fence:** Repository files named above and disposable temporary fixtures only. No live Docker, PostgreSQL, Ollama, client configuration, vault or secret-store mutations. No commits, pushes, merge or production installation are implied by approving only a plan.

**Checkpoint evidence:** Base revision, RED/GREEN outputs, changed paths, coverage, generated-config privacy checks and observable acceptance results; redact credentials and do not store result text. 

**Acceptance criteria (binary):**
- [ ] The interactive choice and --retrieval-backend local|caasiopeia converge.
- [ ] Missing settings, authentication failure and timeout stop before setup writes or service changes.
- [ ] Local selection neither contacts Caasiopeia nor forwards its credentials.
- [ ] Each existing installation mode receives the same selection and complete per-vault mapping.
- [ ] The deployment .env contains non-secret settings while the key stays in process/container environment.
- [ ] Dry-run performs no writes/network calls and prints no credential.
- [ ] Existing local installation and configuration tests still pass.

**Estimated effort:** XS (60 min; basis: shared prompt/flag integration across existing modes, persistence wiring and mocked setup failure paths)

**Executor:** default

**Isolation:** shared

**Delegation:** in-session

**Blocked by:** Iteration 1

#### Iteration 3 — Keep the choice across rebuild and prove fresh installs

**Goal:** Preserve the selected engine through Docker rebuild/update and verify install/uninstall behavior using built artifacts and disposable instances.

**Shippable on its own?** Yes — existing installations remain functional; this slice adds a complete bounded capability.

**Source references:**
- osm_init.py — verify current setup, environment writing, rebuild and removal signatures before reuse.
- src/launcher.py — native runtime validation/loading and installed-path resolution.
- src/config.py — reuse resolve_retrieval_backend(env) and load_caasiopeia_settings(vault_paths, env); verify their current validation contract first.
- src/caasiopeia_client.py — existing scoped search client and typed failures.
- docker-compose.yml — existing Caasiopeia environment pass-through.
- tests/conftest.py — synthetic config/vault isolation.

**Files touched:**
- osm_init.py (modified)
- tests/test_retrieval_setup.py (modified)
- tests/test_caasiopeia_e2e.py (modified)
- README.md (modified)
- docs/RUNBOOK.md (modified)
- CHANGELOG.md (modified)

**Commit message:**
`feat(setup): keep the choice across rebuild and prove fresh installs`

**TDD cycle:**
- RED (failing tests to write first):
  - `tests/test_retrieval_setup.py::test_rebuild_preserves_saved_backend` — rebuild/update compose recreation keeps non-secret retrieval settings.
  - `tests/test_retrieval_setup.py::test_rebuild_missing_caas_key_stops_before_recreate` — missing runtime credentials leave an existing Caasiopeia deployment unchanged.
  - `tests/test_retrieval_setup.py::test_installed_setup_independent_of_checkout` — a wheel installed outside the checkout retains the engine choice after restart.
  - `tests/test_retrieval_setup.py::test_remove_clears_owned_retrieval_settings` — removal deletes only OSM-owned configuration and preserves unrelated files.
- GREEN (minimal implementation to pass RED):
  - Reuse _build_or_pull_custom_services(pull_base=False) only after checking its current compose/environment flow. Resolve effective saved backend before recreating containers and require a current environment credential for Caasiopeia; preserve non-secret .env fields when OSM_VERSION changes. Do not cache or print the API key. Extend osm remove only as needed for newly owned settings, keeping paired uninstall and existing data retention behavior. Validate both choices with a built wheel in a temporary venv and a disposable Docker Compose project/database, never the live installed stack. Document credential re-supply for restarts/rebuilds and the owner-selected local rollback.
- REFACTOR (cleanup planned after GREEN):
  - Extract the shared non-secret retrieval-settings resolution boundary only when duplicated; otherwise None.

**Test pyramid for this iteration:**
- Smoke: Built wheel imports outside the checkout and reports its installed version.
- Unit: 4 cases for effective rebuild settings, absent credential, unrelated keys and removal.
- Integration: 2 compose-environment tests, proving source-root mapping and engine selection survive recreation.
- State machine: configured → rebuild → same engine; missing credential → unchanged running instance; configured → explicit local re-init → local; remove → owned config absent.
- Contract: Installed artifact contains the edited setup/launcher modules; generated config and captured output contain no credential.
- Regression: Guard the observed loss of Caasiopeia selection on osm rebuild; preserve all existing install/uninstall and Docker mode tests.
- Chaos: 1 compose recreation failure leaves stored selection unchanged and surfaces failure; no success claim on partial activation.
- E2E: 2 installed-artifact workflows in temporary roots: local setup/restart/search/remove and Caasiopeia setup/restart/rebuild/search/remove using a synthetic corpus and loopback service. Reproduce both workflows before claiming the installed result.
- Performance: N/A — no ranking/latency target changes; network preflight has a bounded 10-second request timeout with no automatic retry loop.
- TDD Parity: Every changed behavior above has a named RED test; record failures before implementation and passes afterwards. Existing compatibility tests must remain green. Installation-only checks are observed separately, not counted as RED evidence.
- Coverage: Measure before changes; preserve the configured floor and report focused module coverage after this slice. No invented percentage delta.

**Deploy + validate:**
- Install: `uv sync` at repository root for fixture-only verification; final artifact check uses `uv build --wheel` and `uv pip install --python <temporary-venv>/bin/python <built-wheel>`. No editable production install.
- Validate: `uv run pytest tests/test_retrieval_setup.py tests/test_caasiopeia_e2e.py tests/test_launcher.py tests/test_osm_commands.py -q` at repository root; expected exit 0 with every newly named behavior collected. Use temporary HOME/config/deployment directories and mocks for all external processes.
- Rollback: Restore this slice's files on the task branch; temporary instance configuration is backed up before replacement and restored on failure. Never revert another agent's work or uninstall the live stack.

**Side-effect fence:** Repository files named above and disposable temporary fixtures only. A separate temporary Docker Compose project and synthetic corpus may be created and removed, with explicit test-owned resource names; no live vault/database/service restart. No commits, pushes, merge or production installation are implied by approving only a plan.

**Checkpoint evidence:** Base revision, RED/GREEN outputs, changed paths, coverage, generated-config privacy checks and observable acceptance results; redact credentials and do not store result text. Record artifact version/digest, temporary install root, repeated workflow results and cleanup receipt.

**Acceptance criteria (binary):**
- [ ] Rebuild/update compose recreation keeps non-secret retrieval settings.
- [ ] Missing runtime credentials leave an existing Caasiopeia deployment unchanged.
- [ ] A wheel installed outside the checkout retains the engine choice after restart.
- [ ] Removal deletes only OSM-owned configuration and preserves unrelated files.
- [ ] Existing local installation and configuration tests still pass.

**Estimated effort:** XS (45 min; basis: one rebuild preflight boundary, artifact integration tests and paired-removal documentation; build/CI waiting is separate)

**Executor:** default

**Isolation:** shared

**Delegation:** in-session

**Blocked by:** Iteration 2

## 4. Test inventory summary

Counts below are planning estimates, not observed results; assertions are not duplicated to fill categories.

| Iter | Smoke | Unit | Integration | State machine | Contract | Regression | Chaos | E2E | Performance | TDD Parity | Coverage Δ |
|------|-------|------|-------------|---------------|----------|------------|-------|-----|-------------|------------|------------|
| 1 | 1 | 8 | 2 | 1 | 2 | existing suite | 1 | N/A | N/A | named RED behaviors | measure |
| 2 | 1 | 10 | 4 | 1 | 2 | existing suite | 3 | 2 | N/A | named RED behaviors | measure |
| 3 | 1 | 4 | 2 | 1 | 1 | 1 | 1 | 2 | N/A | named RED behaviors | measure |

## 4b. Effort summary

| Iter | Size | Duration | Basis |
|------|------|----------|-------|
| 1 | XS | 45 min | Extend private runtime contract and compatibility tests |
| 2 | XS | 60 min | Shared setup selection, preflight and Docker/native integration |
| 3 | XS | 45 min | Rebuild retention and isolated artifact workflows |

**Estimated total:** 2h30 active work, approximately 3h with contingency; slices serialize on the setup module.
Sanity check: normal for one shared setup flow, one private-runtime contract, and one rebuild boundary; the estimates include reading, tests, implementation, review and integration, without repeating source discovery. These are estimates, not a measured completion guarantee.
External waiting: allow about 15–30 min separately for artifact builds, full-suite hooks and CI when shipping is requested; endpoint availability or owner credential provision can add unbounded waiting and is not counted as active work.

## 5. End-to-end definition of done

- Both backend choices work in every currently supported setup mode. Invalid flags fail explicitly; legacy scripted installs remain local unless a backend is supplied.
- Existing installed choices are retained unless explicitly changed. Local selection makes no Caasiopeia setup request. Caasiopeia requires a valid endpoint, runtime key and complete per-vault source/root mapping; failures leave the existing installation unchanged.
- Non-secret selection survives restart/rebuild/update. Generated configuration and output contain no API key; current credentials are supplied through environment when activation requires them.
- Current retrieval ranking/fallback policy, native compatibility, client config preservation and vault/data retention remain unchanged. Install and uninstall remain paired, and installed executables do not depend on the development checkout.
- Manual demo: in two isolated fresh installation roots using a synthetic vault, run osm init once choosing local and once choosing Caasiopeia against the loopback fixture. Restart each installed launcher and call search_vault through MCP; verify the selected backend prefix. Rebuild the disposable Docker instance and repeat; verify saved selection and per-vault mapping. Remove only the disposable installations and prove unrelated fixture files survive. Repeat both workflows before asserting success. No live production cutover is needed.
- Exact feature test command: `uv run pytest tests/test_retrieval_setup.py tests/test_launcher.py tests/test_osm_commands.py tests/test_caasiopeia_e2e.py -q`.
- Final full-suite command: `uv run pytest tests/ -q`; report skipped pg tests accurately and run available required PostgreSQL-backed CI checks before shipping. Record measured coverage without lowering the configured floor.

## 6. Out of scope

- Automatic CaaSiopeia service installation, credential minting or vault ingestion — owned by its service/corpus workflows.
- CaaSiopeia as an unconditional default — replaced by the user-requested choice and compatible local default.
- Ranking/fallback redesign or removing local PostgreSQL/Ollama indexing — independent architecture changes.
- Rewriting unrelated existing MCP entries, installing global clients or controlling ChatGPT Desktop — outside setup selection and explicitly excluded by owner.
- Langfuse activation, scheduled jobs and telemetry changes — owner-deferred or unrelated.
- Persisting an API key or introducing a new secret store — credential delivery stays environment-only with the existing owner-managed provider.

## 7. Open questions

None for the implementation contract. Actual service URL, runtime credential and source mappings are installation inputs, not planning decisions. If no reachable scoped Caasiopeia service is supplied, local is usable and Caasiopeia activation fails with an actionable error.


## Execution amendment — 2026-10-07

Owner explicitly requested GPT-6 Luna subagents for implementation. This supersedes the in-session execution choice: orchestrator plus subagents, with all writers serialized on osm_init.py and tests/test_retrieval_setup.py. One active writer maximum; independent read-only review may overlap. Workers implement only their ready iteration, leave diffs uncommitted, return RED/GREEN evidence and ambiguities; root owns integration, checkpoints, final tests and separately authorized delivery. No automatic model escalation.

## Privacy clarification — 2026-10-07

The new credential non-persistence requirement applies to CAASIOPEIA_API_KEY. Existing owner-only PostgreSQL/dashboard configuration and its established installation contract remain unchanged; this plan does not migrate unrelated credential storage. Local child environments override saved Caasiopeia fields with empty values where required to defeat Docker Compose .env interpolation.

## Build outcome — 2026-10-07

All three implementation slices are complete on the feature branch. Native runtime persistence remains version-1 compatible; every setup mode offers local or Caasiopeia with preflight before provisioning; Docker rebuild/update preserve the selected backend and reject missing runtime credentials before changes. Removal preserves unowned environment lines. The API key remains environment-only.

Evidence: final prescribed suite 283 passed; independent full suite 685 passed, 31 skipped, src coverage 67.37% (50% configured floor); SAST zero findings. Installed-wheel imports, native subprocess restart and synthetic MCP search workflows were reproduced outside the checkout. External provisioning, database/indexing and local ranking are simulated in those workflows; they do not prove fresh live infrastructure installation. Earlier interrupted runs exposed and corrected pre-existing setup-fixture isolation gaps and are not passing evidence.

Delivery remains pending the mandatory commit hook, VM740 secret scan, PR CI, merge, authorized tag and installed validation. See PLAN-retrieval-backend-choice.execution.md for exact checkpoints and artifact evidence.
