# PLAN — Codex CLI and ChatGPT Desktop MCP registration

**Status:** Validated — `plan_check.py` passes with 0 findings (command and
result recorded in §7).

## 1. Scope summary

Add shared MCP initialization for Codex CLI and ChatGPT Desktop to `osm init`
without duplicating the entry or storing secrets in `config.toml`. Preserve
the existing Claude Desktop, Claude Code, OpenCode, and pi support.

Smallest v1: one portable `obsidian-semantic` entry in
`mcp_servers.obsidian-semantic`, with safe TOML merge tests.

Source: [BUG-codex-cli-mcp-registration-gap-2026-09-19.md](BUG-codex-cli-mcp-registration-gap-2026-09-19.md).

Register: none — no `docs/REGISTER-*.md` exists.

## 2. Prerequisites

- Files: `osm_init.py`, `src/launcher.py`, `tests/test_osm_commands.py`,
  `tests/test_launcher.py`, `README.md`, `pyproject.toml`, `uv.lock`.
- Add a TOML writer which preserves unrelated entries; `tomllib` is
  read-only.
- Baseline: 439 passed, 32 skipped; not rerun.
- Plan checker: `$CANONICAL_REPO/tools/plan_check.py`.
- Execution architecture: sequential — same shared artifact. Every slice
  writes `osm_init.py` or its shared tests.

## 3. Iterations

#### Iteration 1 — Decouple native runtime configuration from MCP clients

**Goal:** Store the native launcher configuration in OSM's private runtime
configuration rather than in each MCP client.

**Shippable on its own?** Yes.

**Source references:**

- `osm_init.py::_native_entry`, `mode_native_macos` — remove runtime
  variables from the MCP entry.
- `src/launcher.py::main` — load the native runtime configuration before
  `_validate_env()`.

**Files touched:**

- `osm_init.py` (modified)
- `src/launcher.py` (modified)
- `tests/test_osm_commands.py` (modified)
- `tests/test_launcher.py` (modified)

**Commit message:**
`fix(native): keep runtime configuration outside MCP clients`

**TDD cycle:**

- RED: add tests for an empty native MCP `env`, loading private runtime
  configuration, and safe handling of a missing or invalid file.
- GREEN: write the runtime file with mode `0600` and load it before
  validation.
- REFACTOR: centralize the OSM configuration path.

**Test pyramid for this iteration:**

- Smoke: import the launcher and resolve its runtime configuration.
- Unit: four tests for entry construction, load, missing file, and invalid file.
- Integration: write the fixture runtime file then invoke launcher validation.
- State machine: absent → written → loaded.
- Contract: assert owner-only mode `0600`.
- Regression: replace `test_native_entry_env_is_launchable`.
- Chaos: inject a write error and assert that configuration is not silently
  registered.
- E2E: N/A — the complete multi-client path closes in Iteration 2.
- Performance: N/A.
- TDD Parity: all changed runtime behavior has RED/GREEN evidence.
- Coverage: unknown until measured; do not lower the 50% threshold.

**Deploy + validate:**

- Install: N/A — use temporary fixtures only.
- Validate: `uv run pytest tests/test_osm_commands.py tests/test_launcher.py -q`.
- Rollback: revert this iteration's source changes only.

**Side-effect fence:** Repository files and temporary test fixtures only; no
live client configuration, vault, service, or database.

**Checkpoint evidence:** Record RED failure, green test output, affected files,
and the base revision.

**Acceptance criteria:**

- [ ] A native MCP entry has no environment values.
- [ ] The launcher starts when given a private OSM runtime file.
- [ ] An invalid runtime file is not overwritten silently.

**Estimated effort:** S (2h; basis: a shared native setup and launcher path)

**Executor:** default

**Isolation:** shared

**Delegation:** in-session

**Blocked by:** None

#### Iteration 2 — Register shared Codex configuration

**Goal:** Write one portable, idempotent, secret-free TOML entry shared by
Codex CLI and ChatGPT Desktop. The registration shape
`mcp_servers.obsidian-semantic` follows the official OpenAI MCP documentation.

**Shippable on its own?** Yes.

**Source references:**

- `osm_init.py::register_with_clients` — add Codex to the client fan-out.
- `tests/test_osm_commands.py::TestRegisterWithClients` — extend the current
  fan-out contract.

**Files touched:**

- `osm_init.py` (modified)
- `pyproject.toml` (modified)
- `uv.lock` (modified)
- `tests/test_osm_commands.py` (modified)

**Commit message:**
`feat(codex): register shared MCP configuration`

**TDD cycle:**

- RED: cover an empty config, unrelated-server preservation, a byte-identical
  second run, malformed TOML without writes, fan-out inclusion, and absence of
  a fake secret.
- GREEN: add a preserving TOML writer, `update_codex_config`, and call it
  from `register_with_clients`.
- REFACTOR: extract portable OpenAI entry construction.

**Test pyramid for this iteration:**

- Smoke: parse the generated configuration.
- Unit: five configuration writer behaviors.
- Integration: invoke the complete registration fan-out with patched paths.
- State machine: absent → added → idempotent.
- Contract: generated table is `mcp_servers.obsidian-semantic`.
- Regression: BUG-codex-cli-mcp-registration-gap-2026-09-19.
- Chaos: malformed TOML leaves the original bytes untouched.
- E2E: N/A — live client configuration is outside the test fence.
- Performance: N/A.
- TDD Parity: all changed registration behavior has RED/GREEN evidence.
- Coverage: unknown until measured; do not lower the 50% threshold.

**Deploy + validate:**

- Install: N/A — never target the user's Codex configuration in tests.
- Validate: `uv run pytest tests/test_osm_commands.py tests/test_launcher.py -q`.
- Rollback: revert this slice, including `uv.lock`.

**Side-effect fence:** Repository files and temporary fixtures only; never
write user configuration while testing.

**Checkpoint evidence:** Record RED failure, green test output, byte-identical
rerun proof, affected files, and base revision.

**Acceptance criteria:**

- [ ] Exactly one `mcp_servers.obsidian-semantic` table is written.
- [ ] Codex CLI and ChatGPT Desktop consume that same entry.
- [ ] A second registration creates no file diff.
- [ ] Malformed TOML is left unchanged.
- [ ] No secret value appears in the client configuration.

**Estimated effort:** S (2h; basis: TOML merge, dependency, and six behaviors)

**Executor:** default

**Isolation:** shared

**Delegation:** in-session

**Blocked by:** Iteration 1

#### Iteration 3 — Document the full client matrix

**Goal:** Make the shared OpenAI registration explicit in installation
documentation.

**Shippable on its own?** Yes.

**Source references:**

- `README.md` — current client list and restart guidance.
- `docs/BUG-codex-cli-mcp-registration-gap-2026-09-19.md` — acceptance
  expectations.

**Files touched:**

- `README.md` (modified)

**Commit message:**
`docs(mcp): document Codex and ChatGPT Desktop setup`

**TDD cycle:**

- RED: N/A — documentation-only change.
- GREEN: add Codex CLI and ChatGPT Desktop as a shared configuration.
- REFACTOR: remove wording implying separate OpenAI registrations.

**Test pyramid for this iteration:**

- Smoke: text check for the complete client matrix.
- Unit: N/A — no executable behavior.
- Integration: N/A — no executable behavior.
- State machine: N/A.
- Contract: README names the shared OpenAI configuration.
- Regression: N/A — no code defect is fixed in this slice.
- Chaos: N/A.
- E2E: N/A.
- Performance: N/A.
- TDD Parity: N/A — documentation-only change.
- Coverage: N/A.

**Deploy + validate:**

- Install: N/A.
- Validate: verify README names Claude Desktop, Claude Code, Codex CLI,
  ChatGPT Desktop, OpenCode, and pi.
- Rollback: revert `README.md`.

**Side-effect fence:** `README.md` only.

**Checkpoint evidence:** Record the changed wording and validation result.

**Acceptance criteria:**

- [ ] The README names all six requested clients.
- [ ] It states that Codex CLI and ChatGPT Desktop share one configuration.

**Estimated effort:** XS (30 min; basis: one documentation page)

**Executor:** default

**Isolation:** shared

**Delegation:** in-session

**Blocked by:** Iteration 2

## 4. Test inventory summary

| Iter | Smoke | Unit | Integration | State machine | Contract | Regression | Chaos | E2E | Performance | TDD Parity | Coverage Δ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | 1 | 4 | 1 | 2 | 1 | 1 | 1 | 0 | 0 | 100% | unknown |
| 2 | 1 | 5 | 1 | 2 | 1 | 1 | 1 | 0 | 0 | 100% | unknown |
| 3 | 1 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | N/A | N/A |

## 4b. Effort summary

| Iter | Size | Duration | Basis |
|---|---|---:|---|
| 1 | S | 2h | Shared native setup and launcher path |
| 2 | S | 2h | TOML merge, dependency, and six behaviors |
| 3 | XS | 30 min | One documentation page |

**Estimated total:** 4 h 30 min, sequential because the slices share setup and
test artifacts.

## 5. End-to-end definition of done

- `osm init` registers Claude Desktop, Claude Code, Codex CLI, ChatGPT
  Desktop, OpenCode, and pi.
- Codex CLI and ChatGPT Desktop share exactly one configuration entry.
- No client configuration contains a secret value.
- The final test command is
  `uv run pytest tests/test_osm_commands.py tests/test_launcher.py -q`.
- After explicit authorization, demonstrate with `osm init`, `codex mcp
  list`, and `/mcp` in ChatGPT Desktop.

## 6. Out of scope

- ChatGPT Web connectors — these use hosted remote integrations, not local
  Codex configuration.
- OAuth and MCP HTTP — this server remains local stdio.
- Automatic cleanup of pre-existing client configurations — it risks changing
  unrelated user settings.

## 7. Open questions

No product question remains. The draft was mechanically validated before
implementation:

```
python3 $CANONICAL_REPO/tools/plan_check.py \
  docs/PLAN-codex-chatgpt-desktop-mcp-registration.md \
  --caller plan-iter --repo-root .
# EXIT=0 — no findings (2026-09-19)
```

The earlier six findings — one non-path source reference, three backtick-wrapped
effort values, a `4h30` total with no parseable wall-clock unit, and an
unjustified `sequential` verdict — were fixed at authoring time.

## Execution amendments — 2026-10-07

- Owner requested continuation of remaining plans and implementation; Langfuse activation is explicitly deferred.
- Base revision: d6f0aa3bccad21e8cf5256f86e22c78c50511bba. OSM 0.18.0 full suite: 591 passed, 31 skipped twice in the preceding implementation.
- Current official OpenAI documentation confirms shared MCP configuration for ChatGPT Desktop, Codex CLI, and the IDE extension on the same Codex host: https://learn.chatgpt.com/docs/extend/mcp?surface=cli . Desktop consumption requires a live check on the installed client; fixture tests alone do not prove it.
- Sequential scoped worker implementation with parent integration: private runtime first, then TOML registration, then documentation. Temporary test fixtures only until shipping validation.
- Native-to-Docker reinitialization requires an explicit non-secret OSM_DOCKER=1 client flag so an older native runtime cannot select native mode.
- Pair the new installed artifacts with removal: native uninstall removes only the private runtime file; Codex uninstall removes only its OSM table while preserving unrelated TOML. Validate these paths using synthetic fixtures, never run a destructive live uninstall.
- Privacy acceptance applies to newly generated OSM entries. Existing client entries and unrelated settings are preserved as required by the original side-effect fence; this implementation does not silently scrub historical configuration.
