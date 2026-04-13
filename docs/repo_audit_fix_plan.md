# Repo Audit Fix Plan

## Scope

This plan addresses the issues found in the repo audit:

1. Windows launcher generation is still Unix-only in `osm_init.py`.
2. Dashboard stats do not honor multi-vault or archive exclusion behavior.
3. The documented native E2E flow is broken by current DB env requirements.
4. CI installs `shipguard` unpinned, which makes the security gate non-reproducible.

This is a fix plan only. It does not implement the changes.

## Goals

- Make the installer and launcher behavior consistent across macOS, Linux, and Windows.
- Make dashboard stats reflect the same vault-selection and ignore rules as indexing.
- Make the native test docs and scripts truthful and runnable.
- Make CI deterministic enough that a green pipeline means something stable.

## Non-Goals

- Redesign the installer UX beyond what is needed to remove current defects.
- Add a package manager release flow for `osm`.
- Rewrite the dashboard UI.
- Replace `shipguard` with a different scanner.

## Execution Order

1. Fix launcher generation in `osm_init.py`.
2. Add installer and launcher regression tests.
3. Fix dashboard stats logic and add coverage.
4. Fix native E2E/test docs mismatch.
5. Pin CI scanner installation.
6. Reconcile docs after behavior is green.

This order matters. The launcher issue is platform-critical. The dashboard issue is user-visible. The docs issue should be fixed after the behavior is corrected so the docs do not drift again during implementation.

## Workstream 1: Windows Launcher Fix

### Problem

`install.ps1` now creates `osm.cmd`, but `osm_init.py` still writes a Bash launcher to `$HOME/.local/bin/osm` for every successful wizard path. On Windows, that creates conflicting installation behavior and leaves the manual PowerShell path broken.

### Files

- `osm_init.py`
- `scripts/osm.ps1`
- `README.md`
- `docs/RUNBOOK.md`

### RED

- Add tests that simulate `platform.system() == "Windows"` and verify the wizard installs a Windows-compatible launcher.
- Add tests that verify the reinstall hint points to `install.ps1` on Windows and `install.sh` on Unix.
- Add tests that verify the launcher path is `osm.cmd` on Windows and `osm` on Unix.

### GREEN

- Split `_link_osm_to_path()` into platform-specific launcher writers.
- Keep the current Bash launcher for macOS/Linux.
- Add a Windows launcher writer that creates `osm.cmd` and dispatches to `scripts\\osm.ps1`.
- Update `_osm_launcher_path()` to return the platform-correct launcher path.
- Ensure all `_done_*()` flows use the platform-aware launcher path.

### REFACTOR

- Extract platform-dependent launcher constants into helper functions.
- Remove duplicated path-building logic between `install.ps1` and `osm_init.py` where practical.
- Keep the launcher generation self-contained and explicit rather than clever.

### Acceptance Criteria

- `osm init` produces a usable launcher on Windows without depending on Bash.
- Reinstall hints are platform-correct.
- Existing macOS/Linux behavior remains unchanged.

## Workstream 2: Dashboard Stats Consistency

### Problem

The dashboard does not use the same path filtering rules as indexing and only counts one vault. This makes coverage and gap stats wrong when `archive/` is excluded or when `OBSIDIAN_VAULTS` is used.

### Files

- `src/dashboard.py`
- `src/server.py`
- `tests/test_dashboard_smoke.py`
- `tests/test_unit.py`
- `README.md`
- `docs/ARCHITECTURE.md`

### RED

- Add tests that verify dashboard vault counts exclude `archive/` by default.
- Add tests that verify `OBSIDIAN_IGNORE_PATHS=""` includes archived notes again.
- Add tests that verify multi-vault counts sum across all configured vaults.
- Add tests that verify recent note paths are rendered relative to the correct vault root in multi-vault mode.

### GREEN

- Reuse the canonical skip logic from `server.py` instead of reimplementing filtering in `dashboard.py`.
- Replace single-vault counting with iteration across `VAULT_PATHS`.
- Make `recent_notes` path rendering multi-vault aware.
- Ensure `vault_file_count` and `unindexed_count` are computed from the same effective file set the indexer uses.

### REFACTOR

- Extract shared vault/file enumeration helpers if needed, but do not create an abstraction that only saves a few lines.
- Keep dashboard-side code dependent on shared helpers, not duplicated ignore logic.

### Acceptance Criteria

- Dashboard counts match indexed reality for:
  - single-vault installs
  - multi-vault installs
  - default archive exclusion
  - explicit archive opt-in

## Workstream 3: Native Test and Docs Repair

### Problem

`test_e2e.py` only forwards `OBSIDIAN_VAULT`, but the server now requires DB config via `DATABASE_URL` or `POSTGRES_PASSWORD`. The README documents a command that does not satisfy current runtime requirements.

### Files

- `tests/test_e2e.py`
- `tests/test_setup.py`
- `README.md`
- `docs/RUNBOOK.md`
- optionally `src/config.py` if behavior needs softening

### RED

- Add a regression test for the E2E harness that verifies the spawned subprocess receives the required DB env.
- Add a test that fails if the E2E harness launches the server without a usable DSN.

### GREEN

- Decide one source of truth for native test configuration:
  - Option A: require `DATABASE_URL` explicitly in docs and pass it through untouched.
  - Option B: make the harness construct env from `POSTGRES_*` if provided.
- Update `test_e2e.py` so its subprocess env matches real runtime requirements.
- Update README examples so native test commands include the minimum required DB env.
- Update `test_setup.py` usage text if its current defaults are misleading for the supported native path.

### REFACTOR

- Centralize shared native test env examples in one README section instead of repeating slightly different commands.
- Avoid weakening `build_dsn()` just to make old docs pass.

### Acceptance Criteria

- The documented native test commands are runnable as written.
- The E2E harness does not fail before MCP initialization because of missing DB env.

## Workstream 4: CI Reproducibility

### Problem

The workflow pins actions by SHA but installs `shipguard` from PyPI without a version pin. That makes the security gate mutable.

### Files

- `.github/workflows/tests.yml`
- optionally `pyproject.toml` or a dedicated CI requirements file if that is the chosen pinning strategy

### RED

- Add a workflow validation check or repository note that records the expected pinned scanner version.
- If tests cover workflow content, add a static assertion for the pinned install command.

### GREEN

- Pin `shipguard` to an explicit version in CI.
- Prefer the narrowest change:
  - `pip install shipguard==<version>` is acceptable.
  - A hash-pinned requirements file is better if the repo wants stronger supply-chain control.

### REFACTOR

- Keep security-tool pinning consistent with the rest of the workflow style.
- Do not add a large dependency-management layer for a single CLI unless the repo already wants that direction.

### Acceptance Criteria

- CI pulls a deterministic `shipguard` version.
- Updating the scanner becomes an explicit reviewed change.

## Workstream 5: Documentation Reconciliation

### Problem

Docs were updated during debugging, but the final behavior still needs one pass after code fixes land.

### Files

- `README.md`
- `docs/RUNBOOK.md`
- `docs/ARCHITECTURE.md`
- `docs/archive_exclusion_plan.md`

### Tasks

- Remove any remaining wording that implies clone-first install is the default.
- Ensure Windows examples mention the actual launcher shape after the `osm_init.py` fix.
- Make dashboard wording match actual multi-vault support after implementation, not before.
- Keep `archive_exclusion_plan.md` as a historical note or rename it if that is clearer.

### Acceptance Criteria

- Install docs match bootstrap behavior.
- Runbook repair steps match actual generated launchers.
- Dashboard docs do not overstate multi-vault support.

## Test Plan

Run after implementation, not during this planning pass:

1. Unit tests for launcher generation and platform-specific paths.
2. Unit tests for dashboard vault counting and archive exclusion.
3. `tests/test_dashboard_smoke.py`
4. Native `tests/test_e2e.py` with documented env.
5. Manual smoke checks:
   - macOS/Linux bootstrap
   - Windows bootstrap
   - `osm init` rerun after partial install
   - dashboard stats on single-vault and multi-vault setups

## Risks

- Reusing server helpers in dashboard code may introduce import-side coupling if done carelessly.
- Windows path handling can regress silently if not tested under simulated platform branches.
- Changing docs before code lands will create another mismatch cycle.

## Suggested Delivery Shape

Split the work into small reviewable commits:

1. launcher fix + launcher tests
2. dashboard stats fix + dashboard tests
3. native E2E/doc fix
4. CI pinning
5. final docs sync

That keeps regressions attributable and avoids mixing installer, runtime, and documentation changes in one blob.
