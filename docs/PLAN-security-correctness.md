# Implementation Plan — Security & Correctness Hardening (obsidian-semantic-mcp)

**Status:** approved 2026-07-20. Input contract for `/plan-implement`.
**Source:** `docs/audits/2026-07-20-job-ready.md`, Stage 5 (security/quality) and Stage 6 (architecture).

## 1. Scope summary

Close the Stage 5 and Stage 6 findings from the job-ready audit. Concretely: put authentication and a loopback default in front of the dashboard's destructive endpoints, stop the file watcher from dying on a database blip, replace two process-local `threading.Lock`s with Postgres advisory locks so coordination works across containers, persist indexing state in the database instead of module globals so the dashboard can actually report failures, and introduce a real schema-version mechanism plus an operator-triggered, non-destructive path for embedding-dimension changes. **Not** in scope: README/docs/badges (Stage 1–3), dependency CVE bumps and `.env.example` (Stage 4), CI branch protection (Stage 7), doc-drift guards (Stage 8), the `osm_init.py` god-module split, and the multi-vault `_resolve_vault_path` bug.

**Smallest possible v1:** Iterations 0–3 (test wiring, dashboard bind, auth, watchdog guard). That clears every HIGH in Stage 5 and is independently releasable.

**Related work, deliberately not in this plan:** the audit's Stage 1–3 presentation findings (README badges, dashboard GIF, community files). A reviewer reaches the code this plan hardens only after the README convinces them to. Treat that as the sibling plan, not a follow-up.

## 2. Prerequisites

**Dependencies:** none new. Postgres advisory locks are core Postgres, available on the pinned `pgvector/pgvector:pg16` image. `secrets` and `hmac` are stdlib. No new entries in `pyproject.toml`; iteration 4 *removes* two.

**Existing code areas touched:**

| Path | Role in this work |
|------|-------------------|
| `src/server.py` (1825 lines) | `init_db` (:270-355), `delete_note` (:664-673), `VaultEventHandler.on_deleted`/`on_moved` (:820-830), `_handle_upsert` (:795-808), `background_init` (:920-933), `reindex_vault` tool (:1508-1525), `_INDEXING_IN_PROGRESS` (:123), `_LAST_REBUILD_FAILED` (:129-147), `db_conn` (:243-266) |
| `src/dashboard.py` (872 lines) | `_reindex_lock` (:53), `DashboardHandler` (:762), `do_GET` (:774), `do_POST` (:807-855), `__main__` bind (:863-866) |
| `src/config.py` (40 lines) | `build_dsn()` — extended with DSN redaction and the advisory-lock key |
| `osm_init.py` (2878 lines) | gains the `osm migrate` subcommand in iteration 8 |
| `pyproject.toml` | `[tool.pytest.ini_options] testpaths` (iteration 0), dependency removal (iteration 4) |
| `.github/workflows/tests.yml` | gains a `postgres` service in iteration 5 |
| `docker-compose.yml` | `:76-116` mcp-server, `:118-152` dashboard — env additions in iterations 1, 2, 4 |
| `tests/conftest.py` | shared fixtures; gains the `pg` marker and its safety guard in iteration 5 |

**Risks:**

- **Iteration 1 changes the dashboard's default bind.** User-visible behavior change for anyone reaching it from another machine. Mitigated by `DASHBOARD_BIND` and an explicit banner line; must land in `CHANGELOG.md`.
- **Iteration 5 introduces the first tests that need a live Postgres.** The existing suite is almost entirely mocked. A `pg` marker plus a CI `postgres` service handles this; the marker's safety guard (refuses any database not named `*_test`) is itself tested before any `pg` test runs.
- **Iteration 8 is the only iteration that can destroy user data.** Its fence is the tightest in the plan and it is operator-triggered rather than automatic, precisely so a container restart can never begin it.
- `_reindex_lock` is read by `do_GET` at `src/dashboard.py:787-790` (`/api/reindex/status`) as well as written by `do_POST`. Iteration 5 must update **both** call sites or the status endpoint silently reports `busy: false` forever.

## 3. Iterations

---

#### Iteration 0 — Make the untested tests run

**Goal:** `pytest` with no arguments collects every test file in `tests/`, so the 19 tests that currently never execute start gating changes.

**Shippable on its own?** Yes — pure test-infrastructure change, no source behavior touched.

**Source references:**
- `pyproject.toml:52-58` — the `[tool.pytest.ini_options] testpaths` list, currently naming only `test_unit.py`, `test_osm_init.py`, `test_osm_commands.py`, `test_dashboard_smoke.py`. The fix is to make collection directory-based rather than enumerated.
- `tests/test_e2e.py`, `tests/test_launcher.py`, `tests/test_portable_invocation.py`, `tests/test_setup.py`, `tests/test_stdin_pipe_response.py`, `tests/test_v096_config.py` — the six orphaned files. Read each and confirm it passes before wiring it in; a file may have rotted precisely because nothing ran it.

**Verified baseline (re-measure, do not trust):** `pytest --collect-only -q` → **332 collected**; `pytest tests/ --collect-only -q` → **351 collected**. The 19-test delta is the target. If the numbers have moved, the counts below move with them.

**Files touched:**
- `pyproject.toml` (modified)
- `tests/test_collection_contract.py` (new)
- any of the six orphaned test files (modified) — only if a rotted test must be repaired

**Commit message:**
`test(collection): run every tests/ file, not the four named in testpaths`

**TDD cycle:**
- RED:
  - `tests/test_collection_contract.py::test_every_test_file_is_collected` — globs `tests/test_*.py`, runs `pytest --collect-only -q` in a subprocess, asserts every globbed filename appears in the output. Fails today because six files are absent.
- GREEN:
  - Replace the four-entry `testpaths` list in `pyproject.toml` with `testpaths = ["tests"]`.
  - Run the full suite. Repair any orphaned test that fails; each repair gets its own regression assertion.
- REFACTOR:
  - None.

**Side-effect fence:** repo tree only. If `tests/test_e2e.py` reaches a live Ollama or Postgres, mark it `@pytest.mark.pg` (defined in iteration 5) or `skipif` rather than letting it hit live services.

**Test pyramid for this iteration:**
- Smoke: `pytest --collect-only` exits 0 and reports ≥351 tests.
- Unit: 1 — `test_every_test_file_is_collected`.
- Integration: N/A — no cross-component flow.
- State machine: N/A — no FSM.
- Contract: 1 — the collection contract enforces that `testpaths` and the filesystem agree.
- Regression: 1 per orphaned test repaired (0 if all six pass as-is).
- Chaos: N/A.
- E2E: N/A.
- Performance: N/A.
- TDD Parity: 100% — one new public symbol, directly tested.
- Coverage: **record the real baseline here** with `pytest --cov=src --cov-report=term` and correct §4's absolute numbers from it. No `fail_under` is configured; do not add one in this iteration.

**Acceptance criteria (binary):**
- [ ] `pytest --collect-only -q` reports ≥ 351 tests (was 332).
- [ ] `pytest -q` exits 0.
- [ ] `tests/test_collection_contract.py::test_every_test_file_is_collected` passes.
- [ ] `grep -c 'tests/test_' pyproject.toml` returns 0.
- [ ] Measured baseline coverage is recorded in the commit body.

**Estimated effort:** S

**Blocked by:** None

---

#### Iteration 1 — Dashboard binds loopback by default

**Goal:** The dashboard listens on `127.0.0.1` unless explicitly told otherwise, closing LAN exposure of the destructive API on the documented native path.

**Shippable on its own?** Yes — reduces attack surface immediately, independent of the auth work in iteration 2.

**Source references:**
- `src/dashboard.py:863` — `http.server.HTTPServer(("0.0.0.0", DASH_PORT), DashboardHandler)`. The literal to replace.
- `src/dashboard.py:33` — `DASH_PORT = int(os.environ.get("DASHBOARD_PORT", "8484"))`. Match this exact pattern for `DASHBOARD_BIND`; do not invent a different config style.
- `docker-compose.yml:140` — the dashboard's `ports:` entry, already `127.0.0.1:${DASHBOARD_PORT}:8484`. **Verify this is still the current value.** The container must set `DASHBOARD_BIND=0.0.0.0` explicitly or the dashboard becomes unreachable from the host. This is the one way iteration 1 breaks a working install.
- `src/dashboard.py:866` — `print(f"Database: {DATABASE_URL}")`, which leaks the Postgres password to stdout. Fixed here since it is three lines away.

**Files touched:**
- `src/dashboard.py` (modified)
- `src/config.py` (modified)
- `docker-compose.yml` (modified)
- `tests/test_dashboard_security.py` (new)
- `CHANGELOG.md` (modified)

**Commit message:**
`fix(dashboard): bind loopback by default and stop printing the DSN`

**TDD cycle:**
- RED:
  - `tests/test_dashboard_security.py::test_default_bind_is_loopback` — asserts `dashboard.DASHBOARD_BIND == "127.0.0.1"` with no env set.
  - `tests/test_dashboard_security.py::test_bind_is_overridable_via_env` — sets `DASHBOARD_BIND=0.0.0.0`, reloads the module, asserts the override takes.
  - `tests/test_dashboard_security.py::test_startup_banner_does_not_leak_password` — captures the startup print block, asserts the password substring from a synthetic `DATABASE_URL` does not appear in stdout.
- GREEN:
  - Add `DASHBOARD_BIND = os.environ.get("DASHBOARD_BIND", "127.0.0.1")` next to `DASH_PORT` at `src/dashboard.py:33`.
  - Use it in the `HTTPServer(...)` constructor at `:863`.
  - Add `_redact_dsn(dsn: str) -> str` to `src/config.py` returning host and dbname only, never user or password. Use it in the startup banner.
  - Add `DASHBOARD_BIND=0.0.0.0` to the dashboard service's `environment:` block in `docker-compose.yml`.
- REFACTOR:
  - None beyond the `_redact_dsn` extraction already in GREEN.

**Side-effect fence:** repo tree only. Do **not** restart the running `obsidian-semantic-mcp-dashboard-1` container to verify. The change is verified by unit test and by reading the compose file; a live restart is the operator's call.

**Test pyramid for this iteration:**
- Smoke: `python3 -c "import sys; sys.path.insert(0,'src'); import dashboard"` succeeds with `DATABASE_URL` set.
- Unit: 3 — the three RED tests.
- Integration: 1 — `test_compose_dashboard_sets_explicit_bind` parses `docker-compose.yml` with `yaml.safe_load` and asserts the dashboard service sets `DASHBOARD_BIND`, catching the container-unreachable regression this iteration risks.
- State machine: N/A.
- Contract: 1 — `test_env_example_documents_dashboard_bind` asserts `DASHBOARD_BIND` appears in `.env.example`.
- Regression: 1 — `test_startup_banner_does_not_leak_password` guards the Stage 5 MEDIUM finding at `src/dashboard.py:866`.
- Chaos: N/A — deferred to iteration 3.
- E2E: N/A — the user-facing path closes in iteration 2.
- Performance: N/A.
- TDD Parity: 100% — two new public symbols (`DASHBOARD_BIND`, `_redact_dsn`), both directly tested.
- Coverage: +1%.

**Acceptance criteria (binary):**
- [ ] `dashboard.DASHBOARD_BIND == "127.0.0.1"` when the env var is unset.
- [ ] `grep -n '"0.0.0.0"' src/dashboard.py` returns no match.
- [ ] The dashboard service in `docker-compose.yml` sets `DASHBOARD_BIND=0.0.0.0`.
- [ ] Startup output contains no substring of the `DATABASE_URL` password.
- [ ] Full suite exits 0.

**Estimated effort:** S

**Blocked by:** Iteration 0

---

#### Iteration 2 — Authenticate the destructive endpoints

**Goal:** `/api/reindex`, `/api/reindex/full`, `/api/prune`, and `/api/ollama/start` reject unauthenticated requests, the token survives restarts, and the dashboard UI still works.

**Shippable on its own?** Yes — closes the HIGH CSRF finding and is the security core of the plan.

**Source references:**
- `src/dashboard.py:807-855` — the entire `do_POST` body. Every branch becomes auth-gated. The four branches have inconsistent error shapes; the auth check sits above all of them.
- `src/dashboard.py:766-772` — `_json_response`. Reuse for the 401; do not hand-roll a second response writer.
- `src/dashboard.py:753` — `HTML_PAGE = HTML_PAGE.replace("{{VERSION}}", f"v{APP_VERSION}")`. Inject the token by the same substitution mechanism; do not template differently.
- `src/dashboard.py:700-750` — the inline `<script>` block. **Verify the current POST call sites before editing**; the script has grown and the audit did not enumerate every POST the UI issues.
- `osm_init.py` — read how `~/.config/obsidian-semantic-mcp/project_root` is written (path construction, directory creation, permissions). The token file follows that exact pattern. Search for `project_root` to find it; do not assume the helper's name.

**Design decisions (resolved — do not re-litigate at implementation time):**
- Mechanism is a **bearer token**, not a session cookie. Cookies are auto-attached by browsers, which is exactly the CSRF property being defended against; a header token is not.
- **Token persists at `~/.config/obsidian-semantic-mcp/dashboard_token`, mode `0600`.** Generated with `secrets.token_urlsafe(32)` on first run if absent, read on every subsequent start. **Not** `.env` in the repo root: the audit already found `osm_init` writing runtime secrets there to be a guardrail violation (`CLAUDE.md`: "Config state lives in `~/.config/obsidian-semantic-mcp/` — never in the repo checkout"), and adding a second secret would make that violation load-bearing.
- `DASHBOARD_TOKEN` env var **overrides** the file when set. Docker uses the env path; the container must not read the host's config directory.
- Comparison uses `hmac.compare_digest`, never `==`.
- GET endpoints stay unauthenticated. They are read-only and loopback-bound after iteration 1. Gating them would require solving initial page-load auth, scope the audit did not justify.
- On failure return **401** with body `{"ok": false, "message": "unauthorized"}`, matching the existing `{"ok": ..., "message": ...}` shape.

**Files touched:**
- `src/dashboard.py` (modified)
- `src/config.py` (modified)
- `docker-compose.yml` (modified)
- `tests/test_dashboard_security.py` (modified)
- `CHANGELOG.md` (modified)

**Commit message:**
`feat(dashboard): require a bearer token for all mutating endpoints`

**TDD cycle:**
- RED:
  - `tests/test_dashboard_security.py::test_post_reindex_without_token_returns_401`
  - `tests/test_dashboard_security.py::test_post_reindex_full_without_token_returns_401`
  - `tests/test_dashboard_security.py::test_post_prune_without_token_returns_401`
  - `tests/test_dashboard_security.py::test_post_ollama_start_without_token_returns_401`
  - `tests/test_dashboard_security.py::test_post_with_valid_token_is_accepted`
  - `tests/test_dashboard_security.py::test_post_with_wrong_token_returns_401`
  - `tests/test_dashboard_security.py::test_token_comparison_uses_compare_digest` — guards against a later `==` regression.
  - `tests/test_dashboard_security.py::test_get_endpoints_remain_unauthenticated` — pins the deliberate decision above so a future reader does not "fix" it.
  - `tests/test_dashboard_security.py::test_token_file_created_with_0600_when_absent` — uses a `tmp_path` config dir, asserts the file is created and `stat().st_mode & 0o777 == 0o600`.
  - `tests/test_dashboard_security.py::test_token_file_is_reused_across_restarts` — asserts a second load returns the same token, not a fresh one.
  - `tests/test_dashboard_security.py::test_env_token_overrides_file` — asserts `DASHBOARD_TOKEN` wins and no file is written.
  - `tests/test_dashboard_security.py::test_token_is_never_written_to_repo_root` — asserts no `.env`, `dashboard_token`, or similar appears under the repo root after token resolution. Guards the decoupling guardrail directly.
- GREEN:
  - Add `resolve_dashboard_token() -> str` to `src/config.py`: returns `DASHBOARD_TOKEN` if set; else reads `~/.config/obsidian-semantic-mcp/dashboard_token`; else generates, writes with `0600`, returns.
  - Add `_require_auth(self) -> bool` to `DashboardHandler`: reads `Authorization`, expects `Bearer <token>`, compares with `hmac.compare_digest`, writes 401 via `_json_response` and returns `False` on failure.
  - Call it as the first statement of `do_POST`: `if not self._require_auth(): return`.
  - Inject the token into `HTML_PAGE` via a `{{TOKEN}}` placeholder following the `{{VERSION}}` pattern at `:753`.
  - Update every POST `fetch(...)` in the inline script to send `headers: {'Authorization': 'Bearer ' + TOKEN}`.
  - Pass `DASHBOARD_TOKEN` through in the dashboard service's `environment:` block.
- REFACTOR:
  - Extract the four `do_POST` branches into `_handle_ollama_start`, `_handle_reindex`, `_handle_prune` so the auth gate and the dispatch are visually separate. Only after GREEN.

**Side-effect fence:** repo tree plus a `tmp_path`-scoped fake config dir. **No test may read or write the real `~/.config/obsidian-semantic-mcp/`** — monkeypatch the config-dir resolution in every token test. Test servers bind an ephemeral port via `HTTPServer(("127.0.0.1", 0), ...)`. No test touches the running dashboard container or the real Postgres.

**Test pyramid for this iteration:**
- Smoke: the test server starts on an ephemeral port and answers `GET /api/reindex/status` with 200.
- Unit: 12 — the twelve RED tests.
- Integration: 2 — `test_ui_script_sends_auth_header_on_every_post` asserts every `method:'POST'` in the rendered `HTML_PAGE` carries an `Authorization` header; `test_compose_dashboard_passes_token_env` asserts `docker-compose.yml` forwards `DASHBOARD_TOKEN`.
- State machine: 1 — `test_token_resolution_precedence` covers env-set → env wins; env-unset + file-present → file wins; env-unset + file-absent → generate and persist.
- Contract: 1 — `test_401_body_matches_existing_error_shape` asserts the 401 body carries the `{"ok", "message"}` keys the other branches use.
- Regression: 1 — `test_cross_origin_simple_post_is_rejected` reproduces the audit's CSRF scenario: POST with no `Authorization` and `Content-Type: text/plain` (the shape a no-preflight cross-origin `fetch` produces) must get 401.
- Chaos: 2 — `test_malformed_authorization_header_returns_401` covers `Bearer`, `Bearer `, `Basic xyz`, and a 10 KB header value without raising; `test_unreadable_token_file_falls_back_to_generated` covers a config dir that exists but is not readable.
- E2E: 1 — `test_reindex_roundtrip_with_token` starts the handler, POSTs `/api/reindex` with a valid token against a stubbed `index_vault`, asserts 200 plus one invocation. Closes the path opened by iterations 1–2.
- Performance: N/A.
- TDD Parity: 100% — two new public symbols (`resolve_dashboard_token`, `_require_auth`), both directly tested.
- Coverage: +4%.

**Acceptance criteria (binary):**
- [ ] Unauthenticated POST to each of the four endpoints returns 401.
- [ ] POST with a valid bearer token returns the pre-existing status code for that endpoint.
- [ ] `grep -n 'compare_digest' src/dashboard.py` returns a match.
- [ ] The token file is created mode `0600` and reused on the next start.
- [ ] `DASHBOARD_TOKEN` overrides the file and writes nothing.
- [ ] No token material is written anywhere under the repo root.
- [ ] Every `method:'POST'` in `HTML_PAGE` carries an `Authorization` header.
- [ ] GET `/api/stats` still returns 200 with no `Authorization` header.
- [ ] Full suite exits 0.

**Estimated effort:** M

**Blocked by:** Iteration 1

---

#### Iteration 3 — The watchdog thread stops dying

**Goal:** A database outage during a file delete or move no longer kills the watchdog observer and silently halts all indexing.

**Shippable on its own?** Yes — self-contained resilience fix.

**Source references:**
- `src/server.py:820` — `on_deleted` calls `delete_note(event.src_path)` unguarded.
- `src/server.py:825` — `on_moved` calls `delete_note(event.src_path)` unguarded.
- `src/server.py:664-673` — `delete_note` body; opens `db_conn()` and raises `psycopg2.OperationalError` when Postgres is unreachable.
- `src/server.py:795-808` — `_handle_upsert`, the **correct** pattern to copy. Note its ordering bug: `except FileNotFoundError: delete_note(path)` sits *before* the broad `except Exception`, so a DB failure inside that recovery escapes the guard. Fix that ordering here too — same defect class.

**Files touched:**
- `src/server.py` (modified)
- `tests/test_watchdog_resilience.py` (new)
- `CHANGELOG.md` (modified)

**Commit message:**
`fix(watcher): guard delete_note so a DB outage cannot kill the observer thread`

**TDD cycle:**
- RED:
  - `tests/test_watchdog_resilience.py::test_on_deleted_survives_db_error` — patches `server.delete_note` to raise `psycopg2.OperationalError`, fires `on_deleted`, asserts no exception propagates.
  - `tests/test_watchdog_resilience.py::test_on_moved_survives_db_error_on_source` — same for `on_moved`, and asserts `_schedule(event.dest_path)` is still called so a source-side failure does not swallow destination indexing.
  - `tests/test_watchdog_resilience.py::test_handle_upsert_survives_db_error_in_filenotfound_recovery` — `Path.read_text` raises `FileNotFoundError` and `delete_note` raises `OperationalError`; asserts nothing propagates. Fails today because of the `:805-808` ordering.
  - `tests/test_watchdog_resilience.py::test_delete_note_failure_is_logged` — asserts a `log.warning` naming the path is emitted, so failures are visible rather than silent.
- GREEN:
  - Add `_safe_delete_note(path: str) -> None` to `src/server.py` wrapping `delete_note` in `try/except Exception` with a warning log. Call it from `on_deleted` and `on_moved`.
  - Reorder `_handle_upsert` so the `FileNotFoundError` recovery calls `_safe_delete_note`.
- REFACTOR:
  - None. The helper is the refactor.

**Side-effect fence:** repo tree only. All tests use monkeypatched `delete_note`; none open a real DB connection or touch a real vault.

**Test pyramid for this iteration:**
- Smoke: `VaultEventHandler` instantiates and dispatches a synthetic event without raising.
- Unit: 4 — the four RED tests.
- Integration: 1 — `test_observer_still_dispatches_after_delete_failure` fires a failing `on_deleted` then a succeeding `on_created` against one handler instance, asserting the second is processed. This is the actual bug: not that one call fails, but that the thread stops serving.
- State machine: N/A.
- Contract: N/A — no schema or config surface.
- Regression: 1 — `test_handle_upsert_survives_db_error_in_filenotfound_recovery` guards the `src/server.py:805-808` ordering defect.
- Chaos: 1 — `test_repeated_db_failures_do_not_exhaust_handler` fires 50 consecutive failing deletes and asserts a subsequent success is still processed.
- E2E: N/A — no user-facing path changes.
- Performance: N/A.
- TDD Parity: 100% — one new public symbol (`_safe_delete_note`), directly tested.
- Coverage: +2%.

**Acceptance criteria (binary):**
- [ ] `on_deleted` and `on_moved` raise nothing when `delete_note` raises `psycopg2.OperationalError`.
- [ ] `on_moved` still schedules `event.dest_path` after a source-side failure.
- [ ] No unguarded `delete_note(` call remains inside `VaultEventHandler`.
- [ ] A warning naming the failed path is logged on each failure.
- [ ] Full suite exits 0.

**Estimated effort:** S

**Blocked by:** Iteration 0 (independent of 1–2; may run in parallel)

---

#### Iteration 4 — Remove the phantom dependencies

**Goal:** `starlette` and `uvicorn` leave `pyproject.toml`, deleting four CVEs and a false statement in `CLAUDE.md`.

**Shippable on its own?** Yes — pure subtraction.

**Source references:**
- `pyproject.toml:28-29` — the two pinned dependencies.
- `src/dashboard.py:13` — `import http.server`, the actual implementation. **Re-verify before removing:** run `grep -rn 'starlette\|uvicorn' src/ osm_init.py tests/` and confirm zero hits. The audit measured zero on 2026-07-20; any hit now invalidates this iteration and it must be re-planned.
- `CLAUDE.md` — the line describing `src/dashboard.py` as a "Starlette monitoring dashboard". **`CLAUDE.md` is a protected instruction file**: this edit is explicitly authorized by this plan because the line is factually wrong, and it is the only line in that file this iteration may touch.

**Files touched:**
- `pyproject.toml` (modified)
- `uv.lock` (modified, regenerated)
- `CLAUDE.md` (modified — one line only)
- `tests/test_dependency_contract.py` (new)
- `CHANGELOG.md` (modified)

**Commit message:**
`chore(deps): drop unused starlette and uvicorn pins`

**TDD cycle:**
- RED:
  - `tests/test_dependency_contract.py::test_no_unimported_runtime_dependencies` — parses `[project] dependencies` from `pyproject.toml` and asserts each distribution's import name appears under `src/`. Fails today on `starlette` and `uvicorn`. Use an explicit `DIST_TO_MODULE` mapping for known mismatches (`psycopg2-binary` → `psycopg2`, `pyjwt` → `jwt`, `python-dotenv` → `dotenv`, `pyyaml` → `yaml`) rather than guessing at runtime.
- GREEN:
  - Delete lines 28-29 from `pyproject.toml`.
  - `uv lock` to regenerate.
  - Fix the `CLAUDE.md` line.
- REFACTOR:
  - None.

**Side-effect fence:** repo tree only. `uv lock` writes `uv.lock`; expected and intended. Do **not** run `uv sync --upgrade` — this iteration removes packages, it does not bump them (Stage 4 work, out of scope).

**Test pyramid for this iteration:**
- Smoke: `python3 -c "import sys; sys.path.insert(0,'src'); import dashboard, server"` succeeds after removal.
- Unit: 1 — `test_no_unimported_runtime_dependencies`.
- Integration: 1 — `test_lockfile_has_no_starlette_or_uvicorn` greps `uv.lock`, catching a regenerate that kept them as transitives of something else.
- State machine: N/A.
- Contract: 1 — the dependency contract permanently blocks adding an unimported runtime dep.
- Regression: N/A — no bug fixed, a dependency removed.
- Chaos: N/A.
- E2E: N/A.
- Performance: N/A.
- TDD Parity: 100%.
- Coverage: +0% (no source lines).

**Acceptance criteria (binary):**
- [ ] `grep -n 'starlette\|uvicorn' pyproject.toml` returns no match.
- [ ] `grep -c 'starlette' uv.lock` returns 0.
- [ ] `uv sync` succeeds and `pytest -q` exits 0.
- [ ] `grep -n 'Starlette' CLAUDE.md` returns no match.

**Estimated effort:** S

**Blocked by:** Iteration 0

---

#### Iteration 5 — Postgres advisory locks, and a database in CI

**Goal:** Re-index mutual exclusion works across processes and containers, and the tests that prove it run in CI rather than only on the author's machine.

**Shippable on its own?** Yes — replaces a lock that does not work with one that does; no API change.

**Source references:**
- `src/dashboard.py:53` — `_reindex_lock = threading.Lock()`, the process-local lock being replaced.
- `src/dashboard.py:825-844` — the `do_POST` acquire/release around the re-index thread, including `DELETE FROM notes;` at `:837`.
- `src/dashboard.py:787-790` — `/api/reindex/status` reads the same lock. **Both call sites must change together**; updating only `do_POST` leaves the status endpoint permanently reporting `busy: false`.
- `src/server.py:1508-1525` — the `reindex_vault` MCP tool, which spawns a re-index thread with **no** lock at all. It must take the same lock and return busy when it cannot.
- `src/server.py:243-266` — `db_conn()`. The lock helper uses this context manager, never a bare `psycopg2.connect()` (project guardrail).
- `.github/workflows/tests.yml:20-41` — the `unit-tests` job. **Read it before editing**; the `postgres` service and the `pg` step are added here, and the file is SHA-pinned throughout — any new `uses:` must be SHA-pinned to match.

**Design decisions (resolved):**
- **Session-level** `pg_try_advisory_lock(key)` / `pg_advisory_unlock(key)`, not `pg_advisory_xact_lock` — the lock must outlive the short transaction and span the whole re-index.
- The lock is held on a **dedicated connection** checked out for its lifetime, because session-level advisory locks are bound to the session. Taking it on a pooled connection that is then returned would release it unpredictably.
- Key: module-level `REINDEX_LOCK_KEY = 8474927` in `src/config.py` so both modules import one value. Any fixed 64-bit int works; it is namespaced by the database.
- Non-blocking everywhere. A blocked caller returns 409, matching today's dashboard behavior.
- The pool is `ThreadedConnectionPool(1, 5)`. Holding one connection for a re-index leaves 4. Acceptable — **note it in the code comment** so a future reader does not raise the pool minimum without understanding why.
- **The `pg` tests run in CI.** These are the tests that would have caught both HIGH coordination bugs; a mocked `threading.Lock` passes every unit test perfectly, which is why the bug shipped. Skipping them in CI rebuilds the original failure mode. Add a `postgres` service (pgvector image, matching the compose pin) to `tests.yml`. `-m "not pg"` remains the local escape hatch for machines with no database.
- **`pg` fixture safety guard:** the fixture prefers `PYTEST_DATABASE_URL` over `DATABASE_URL` and **hard-fails unless the target database name ends in `_test`**. This guard is written and tested *first*, before any `pg` test runs.

**Files touched:**
- `src/config.py` (modified)
- `src/dashboard.py` (modified)
- `src/server.py` (modified)
- `tests/conftest.py` (modified)
- `tests/test_advisory_lock.py` (new)
- `.github/workflows/tests.yml` (modified)
- `CHANGELOG.md` (modified)

**Commit message:**
`fix(reindex): use a Postgres advisory lock so mutual exclusion spans processes`

**TDD cycle:**
- RED:
  - `tests/test_advisory_lock.py::test_pg_fixture_refuses_non_test_database` — unit; asserts the fixture raises when pointed at a database whose name does not end in `_test`. **Write and pass this before any other `pg` test.**
  - `tests/test_advisory_lock.py::test_try_acquire_returns_true_when_free` (`@pytest.mark.pg`)
  - `tests/test_advisory_lock.py::test_second_acquire_from_other_connection_returns_false` (`@pytest.mark.pg`) — the test that would have caught the original bug.
  - `tests/test_advisory_lock.py::test_release_allows_reacquire` (`@pytest.mark.pg`)
  - `tests/test_advisory_lock.py::test_lock_released_when_context_exits_on_exception` (`@pytest.mark.pg`)
  - `tests/test_advisory_lock.py::test_reindex_tool_returns_busy_when_lock_held` — unit, mocked.
  - `tests/test_advisory_lock.py::test_reindex_status_reflects_advisory_lock` — unit, mocked; asserts `/api/reindex/status` reads the advisory lock, not `threading.Lock`.
- GREEN:
  - Add `reindex_lock()` context manager to `src/server.py`: checks out a dedicated connection, runs `SELECT pg_try_advisory_lock(%s)`, yields the boolean, unconditionally runs `pg_advisory_unlock` plus connection release in `finally`.
  - Replace both `_reindex_lock` call sites in `src/dashboard.py`.
  - Wrap the `reindex_vault` tool's `_reindex_all` thread in it; return a busy `TextContent` when not acquired.
  - Delete `_reindex_lock` from `src/dashboard.py:53`.
  - Add the `pg` marker and its guarded fixture to `tests/conftest.py`; register the marker in `pyproject.toml` so `--strict-markers` stays clean.
  - Add the `postgres` service to `.github/workflows/tests.yml` and set `PYTEST_DATABASE_URL` for the test step.
- REFACTOR:
  - Collapse the duplicated busy-response construction in `do_POST` into one helper.

**Side-effect fence:** **This iteration reaches a live Postgres.** `pg` tests take advisory locks only — they must **not** write to `notes` or `note_links` and must **not** run `DELETE`. Advisory locks are ephemeral and vanish on disconnect, so no rollback is needed. The `_test`-suffix guard is the hard boundary; the executor must confirm `test_pg_fixture_refuses_non_test_database` passes before running any other `pg` test. Non-`pg` tests stay fully mocked.

**Test pyramid for this iteration:**
- Smoke: `from server import reindex_lock` imports; entering and exiting with no DB yields `False` rather than raising.
- Unit: 3 — the fixture guard plus the two mocked tests.
- Integration: 4 — the four `pg` tests exercising the real Postgres lock boundary.
- State machine: 1 — `test_lock_state_transitions` enumerates free → held → released → re-held → held-by-other-rejected.
- Contract: 1 — `test_reindex_lock_key_is_shared_constant` asserts `dashboard` and `server` both resolve to `config.REINDEX_LOCK_KEY`, guarding against a copy-pasted literal drifting.
- Regression: 1 — `test_second_acquire_from_other_connection_returns_false` guards the Stage 6 HIGH finding.
- Chaos: 1 — `test_lock_released_when_context_exits_on_exception` injects a raise inside the context body and asserts the lock is free afterward.
- E2E: N/A — no user-facing path changes; correct behavior is invisible.
- Performance: N/A.
- TDD Parity: 100% — two new public symbols (`reindex_lock`, `REINDEX_LOCK_KEY`), both directly tested.
- Coverage: +3%. Now counted in CI too, since `pg` tests run there.

**Acceptance criteria (binary):**
- [ ] `grep -n '_reindex_lock' src/dashboard.py` returns no match.
- [ ] Two independent connections cannot both hold the lock.
- [ ] `reindex_vault` returns a busy message when the lock is held.
- [ ] `/api/reindex/status` reports `busy: true` while another connection holds the lock.
- [ ] The `pg` fixture raises when pointed at a database not named `*_test`.
- [ ] `.github/workflows/tests.yml` runs a `postgres` service and the `pg` tests execute in CI (visible in the run log, not skipped).
- [ ] `pytest -q -m "not pg"` exits 0 with no database present.
- [ ] `pytest -q` exits 0 with a database present.

**Estimated effort:** M

**Blocked by:** Iteration 2

---

#### Iteration 6 — Persist indexing state in the database

**Goal:** Indexing progress and failures live in Postgres, so the dashboard's rebuild-failure panel reports real data across container boundaries instead of being structurally always empty.

**Shippable on its own?** Yes — the panel starts working; nothing else changes.

**Source references:**
- `src/server.py:129-147` — `_LAST_REBUILD_FAILED`, `_LAST_REBUILD_FAILED_LOCK`, `get_last_rebuild_failures()`, `_set_last_rebuild_failures()`. The module-global state being replaced. The comment at `:125` claims cross-process visibility that does not exist; correct it.
- `src/server.py:123` — `_INDEXING_IN_PROGRESS = threading.Event()`, read by the search gate.
- `src/server.py:1424` — the search-path gate reading `_INDEXING_IN_PROGRESS`. **Verify this line number before editing**; `server.py` is 1825 lines and this reference came from a subagent's read, not a direct one.
- `src/server.py:755` — where `_set_last_rebuild_failures` is called at the end of an index pass.
- `src/dashboard.py:334-335` — where the dashboard imports and calls `get_last_rebuild_failures()` in its own process, which is why the panel is always empty.
- `src/server.py:920-933` — `background_init`, which sets and clears the flag and swallows every exception.

**Design decisions (resolved):**
- New table `index_state`, one row per vault. `vault_id TEXT PRIMARY KEY`, `status TEXT NOT NULL` (`idle` | `indexing` | `failed`), `started_at TIMESTAMP`, `finished_at TIMESTAMP`, `failed_paths TEXT[]`, `error TEXT`.
- `_INDEXING_IN_PROGRESS` stays as a fast in-process short-circuit but is no longer the source of truth; the DB is. Keeping it avoids a round-trip on every search.
- Table creation goes in `init_db` here and is **moved into a versioned migration in iteration 7**. Deliberate ordering: ship the table first so 7a is about the mechanism, not a schema change and a mechanism at once.

**Files touched:**
- `src/server.py` (modified)
- `src/dashboard.py` (modified)
- `tests/test_index_state.py` (new)
- `CHANGELOG.md` (modified)

**Commit message:**
`feat(observability): persist indexing state so failures survive process boundaries`

**TDD cycle:**
- RED:
  - `tests/test_index_state.py::test_set_indexing_writes_status_row` (`@pytest.mark.pg`)
  - `tests/test_index_state.py::test_failed_paths_round_trip_through_db` (`@pytest.mark.pg`) — write from one connection, read from another.
  - `tests/test_index_state.py::test_status_transitions_to_failed_on_exception` (`@pytest.mark.pg`)
  - `tests/test_index_state.py::test_dashboard_reads_failures_written_by_other_process` (`@pytest.mark.pg`) — simulates the two-container topology with two connections.
  - `tests/test_index_state.py::test_get_last_rebuild_failures_falls_back_when_db_unavailable` — unit; a DB error yields an empty list rather than raising into the stats endpoint.
- GREEN:
  - Add the `index_state` DDL to `init_db`.
  - Add `set_index_state(vault_id, status, *, failed_paths=None, error=None)` and `get_index_state(vault_id=None)` to `src/server.py`, both via `db_conn()`.
  - Rewrite `get_last_rebuild_failures()` to read from the table, keeping the signature so `src/dashboard.py:334-335` needs no change.
  - Call `set_index_state` at the start, end, and exception path of `background_init` and `index_vault`.
  - Correct the misleading comment at `src/server.py:125`.
- REFACTOR:
  - Remove `_LAST_REBUILD_FAILED` and `_LAST_REBUILD_FAILED_LOCK` once nothing reads them.

**Side-effect fence:** **Reaches a live Postgres and writes rows** — unlike iteration 5. Every `pg` test operates on a synthetic `vault_id` prefixed `pytest-` and deletes its own rows in teardown. No test may touch a row whose `vault_id` does not start with `pytest-`. The `_test`-database guard from iteration 5 still applies and is the outer boundary.

**Test pyramid for this iteration:**
- Smoke: `init_db` runs against a fresh database and `index_state` exists.
- Unit: 1 — the fallback test.
- Integration: 4 — the four `pg` tests, including the cross-connection read that is the whole point.
- State machine: 1 — `test_index_state_transitions` covers idle → indexing → idle, idle → indexing → failed, failed → indexing → idle.
- Contract: 1 — `test_index_state_schema_matches_expected_columns` asserts column names and types via `information_schema.columns`, so a later hand-edit to the DDL breaks loudly.
- Regression: 1 — `test_dashboard_reads_failures_written_by_other_process` guards the Stage 6 HIGH dead-observability finding.
- Chaos: 1 — `test_index_state_write_failure_does_not_abort_indexing` makes `set_index_state` raise mid-pass and asserts indexing completes. Observability must never break the thing it observes.
- E2E: 1 — `test_stats_endpoint_surfaces_persisted_failures` drives `/api/stats` and asserts a failure written by a separate connection appears in the response.
- Performance: N/A.
- TDD Parity: 100% — two new public symbols (`set_index_state`, `get_index_state`), both directly tested.
- Coverage: +4%.

**Acceptance criteria (binary):**
- [ ] `index_state` exists after `init_db`.
- [ ] A failure written on connection A is readable on connection B.
- [ ] `grep -n '_LAST_REBUILD_FAILED' src/server.py` returns no match.
- [ ] `/api/stats` surfaces a failure written by a different connection.
- [ ] An exception inside `set_index_state` does not abort an in-flight index pass.
- [ ] No `pg` test leaves a row whose `vault_id` lacks the `pytest-` prefix.
- [ ] `pytest -q -m "not pg"` exits 0 with no database.

**Estimated effort:** M

**Blocked by:** Iteration 5

---

#### Iteration 7 — Versioned schema migrations

**Goal:** Schema changes run through an ordered, versioned, idempotent migration list instead of `CREATE TABLE IF NOT EXISTS` plus ad-hoc `ALTER`.

**Shippable on its own?** Yes — mechanism only, no behavior change for an existing install.

**Source references:**
- `src/server.py:270-355` — the whole of `init_db`: `CREATE TABLE IF NOT EXISTS`, two `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, the `vault_id` backfill `UPDATE`, three `CREATE INDEX IF NOT EXISTS`, the `note_links` table, the dimension check, the IVFFlat auto-tune. **Read all 85 lines before touching any of it.**
- `src/server.py:346-355` — the IVFFlat auto-tune, which must run **after** migrations.

**Design decisions (resolved):**
- Ordered list of `(version: int, name: str, sql_or_callable)` in a new `src/migrations.py`. **No Alembic** — the dependency cost is not justified at this table count, and the finding is "no versioning", not "no framework".
- `schema_version` table: `version INTEGER PRIMARY KEY`, `name TEXT NOT NULL`, `applied_at TIMESTAMP DEFAULT NOW()`. One row per applied migration, so partial application is visible.
- Migration 1 is a **baseline** that stamps the current schema as version 1 without executing DDL when the tables already exist. This is what makes the change safe for existing installs: an established database is stamped, not rebuilt.
- Migration 2 is `index_state`, moved out of iteration 6's inline DDL.
- Migrations run inside `init_db`, before the auto-tune, each in its own transaction.

**Files touched:**
- `src/migrations.py` (new)
- `src/server.py` (modified)
- `tests/test_migrations.py` (new)
- `docs/ARCHITECTURE.md` (modified)
- `CHANGELOG.md` (modified)

**Commit message:**
`feat(schema): add a versioned, idempotent migration mechanism`

**TDD cycle:**
- RED:
  - `tests/test_migrations.py::test_schema_version_table_created_on_first_run` (`@pytest.mark.pg`)
  - `tests/test_migrations.py::test_baseline_migration_stamps_existing_schema_without_ddl` (`@pytest.mark.pg`) — the safety property for existing installs.
  - `tests/test_migrations.py::test_migrations_are_idempotent` (`@pytest.mark.pg`) — run `init_db` twice, assert one row per migration.
  - `tests/test_migrations.py::test_migrations_apply_in_version_order` — unit; asserts the list is sorted and versions are unique and contiguous.
  - `tests/test_migrations.py::test_partial_failure_leaves_earlier_migrations_applied` (`@pytest.mark.pg`) — inject a failing migration, assert prior versions stay recorded.
- GREEN:
  - Create `src/migrations.py` with `MIGRATIONS: list[Migration]`, `current_version(conn) -> int`, `apply_pending(conn) -> list[int]`.
  - Migration 1 baseline; migration 2 `index_state`.
  - Rewrite `init_db` to call `apply_pending` and keep only the IVFFlat auto-tune inline.
- REFACTOR:
  - Move the `note_links` DDL and the three `CREATE INDEX` statements into migration 1 so `init_db` shrinks to migration invocation plus auto-tune.

**Side-effect fence:** `pg` tests run against a `*_test` database only, per the iteration 5 guard. Migrations here are additive (a new table and a version stamp); none drop or rewrite existing columns. The executor may **not** run migrations against a real database — that is the operator's decision after review.

**Test pyramid for this iteration:**
- Smoke: `from migrations import MIGRATIONS, apply_pending` imports; `init_db` on an empty test database exits 0.
- Unit: 1 — `test_migrations_apply_in_version_order`.
- Integration: 4 — the four `pg` tests.
- State machine: 1 — `test_migration_application_states` covers unstamped → baseline-stamped → fully-applied, plus partially-applied → resumed.
- Contract: 1 — `test_schema_version_matches_migration_list` asserts the highest applied version equals `max(m.version for m in MIGRATIONS)` after `init_db`.
- Regression: 1 — `test_baseline_migration_stamps_existing_schema_without_ddl` guards the "no migration system" finding and the upgrade path for existing users.
- Chaos: 1 — `test_partial_failure_leaves_earlier_migrations_applied`.
- E2E: N/A — closes in 7b.
- Performance: 1 — `test_migration_completes_within_budget` asserts `apply_pending` on a 1000-row synthetic table finishes under 5 seconds. Startup migrations that hang look identical to a crashed server, so a ceiling is an acceptance criterion.
- TDD Parity: 100% — three new public symbols (`MIGRATIONS`, `current_version`, `apply_pending`), all directly tested.
- Coverage: +3%.

**Acceptance criteria (binary):**
- [ ] `schema_version` exists and holds one row per applied migration.
- [ ] Running `init_db` twice applies each migration exactly once.
- [ ] An existing populated database is stamped at baseline without losing rows.
- [ ] A failing migration leaves earlier versions recorded.
- [ ] `apply_pending` on 1000 rows completes under 5 seconds.
- [ ] `pytest -q -m "not pg"` exits 0 with no database.

**Estimated effort:** M

**Blocked by:** Iteration 6

---

#### Iteration 8 — Operator-triggered, non-destructive dimension change

**Goal:** An embedding-model change stops being a `docker compose down -v` data-loss event, and never starts itself.

**Shippable on its own?** Yes, and it is the last iteration — the plan is complete when it lands.

**Source references:**
- `src/server.py:334-345` — the dimension-mismatch branch that currently only logs "Run `docker compose down -v`". The behavior being replaced.
- `src/server.py:346-355` — the IVFFlat auto-tune, which must run against the correct column after any dimension change.
- `osm_init.py` — the `osm` CLI's subcommand dispatch. **Read how an existing subcommand (`status`, `rebuild`, `tunnel`) is registered and wire `migrate` the same way.** Do not invent a parallel dispatch path. Search for the subcommand table rather than assuming its name.
- `docs/audits/2026-07-20-job-ready.md` — Stage 6 schema findings, for the rationale behind the non-destructive design.

**Design decisions (resolved):**
- The dimension path lives in the migrations module iteration 7 creates, and reuses its `apply_pending` transaction discipline — but it is **not** a numbered migration, because it is data movement rather than schema versioning. That module does not exist on disk until iteration 7 lands; read it then, not before.
- **Boot detects, it does not migrate.** On mismatch, `init_db` records `status='dimension_mismatch'` in `index_state`, logs a warning naming the exact command to run, and **keeps serving on the existing column**. A container restart can never begin hours of re-embedding.
- Migration is **operator-triggered**: `osm migrate --embedding-dim`. Rationale: on CPU-only Ollama a full vault re-embed is hours, and a silent automatic start is indistinguishable from a hung server. Editing an env var should not read as "rebuild my index".
- **Add a column, never mutate the old one.** `embedding_<newdim> vector(N)` is added and populated; the old column is dropped only after the new one is fully populated and search has been cut over.
- If re-embedding cannot complete (Ollama unreachable), the old column and old model stay authoritative. Fail toward the working state.
- The dashboard surfaces `dimension_mismatch` so the operator finds out without reading logs.

**Files touched:**
- `src/server.py` (modified)
- `src/migrations.py` (modified)
- `osm_init.py` (modified)
- `src/dashboard.py` (modified)
- `tests/test_dimension_migration.py` (new)
- `docs/RUNBOOK.md` (modified — the operator procedure)
- `CHANGELOG.md` (modified)

**Commit message:**
`feat(schema): add osm migrate for non-destructive embedding-dimension changes`

**TDD cycle:**
- RED:
  - `tests/test_dimension_migration.py::test_boot_detects_mismatch_without_migrating` (`@pytest.mark.pg`) — the central safety property: `init_db` on a mismatched database adds no column and re-embeds nothing.
  - `tests/test_dimension_migration.py::test_boot_records_mismatch_in_index_state` (`@pytest.mark.pg`)
  - `tests/test_dimension_migration.py::test_search_still_works_under_unmigrated_mismatch` (`@pytest.mark.pg`) — the user keeps a working index while deciding.
  - `tests/test_dimension_migration.py::test_migrate_adds_new_column_without_dropping_old` (`@pytest.mark.pg`)
  - `tests/test_dimension_migration.py::test_old_column_survives_failed_reembed` (`@pytest.mark.pg`) — with embedding stubbed to raise, the original column and data are intact.
  - `tests/test_dimension_migration.py::test_search_works_throughout_migration` (`@pytest.mark.pg`)
  - `tests/test_dimension_migration.py::test_cutover_drops_old_column_only_when_new_is_complete` (`@pytest.mark.pg`)
  - `tests/test_dimension_migration.py::test_osm_migrate_subcommand_is_registered` — unit; asserts `osm migrate` is dispatchable and `--embedding-dim` is accepted.
- GREEN:
  - Replace the warning at `src/server.py:334-345` with detection that writes `index_state` and logs the command.
  - Add `migrate_embedding_dimension(old_dim, new_dim, *, dry_run=False)` to `src/migrations.py`.
  - Register the `migrate` subcommand in `osm_init.py` following the existing pattern.
  - Surface `dimension_mismatch` in the dashboard stats payload.
- REFACTOR:
  - Extract the add-column / backfill / cutover steps into three named functions so each is separately testable and separately resumable.

**Side-effect fence:** **The tightest fence in the plan — this is the only code that can destroy user data.** All `pg` tests run against the `*_test` database enforced by the iteration 5 guard. The executor may **not** run `osm migrate` against any real database, and may not run it at all outside the test fixture. `--dry-run` must be implemented and tested before the real path is wired. The cutover step (dropping the old column) must be a separate function with its own test proving it refuses to run when the new column has any NULL row.

**Test pyramid for this iteration:**
- Smoke: `osm migrate --help` exits 0; `from migrations import migrate_embedding_dimension` imports.
- Unit: 1 — `test_osm_migrate_subcommand_is_registered`.
- Integration: 7 — the seven `pg` tests.
- State machine: 1 — `test_dimension_migration_state_transitions` covers stable → mismatch-detected → new-column-added → re-embedding → cutover → stable, plus the re-embedding → failed → stable-on-old-column branch.
- Contract: 1 — `test_index_state_exposes_dimension_mismatch_status` asserts the status string the dashboard reads matches the one the server writes.
- Regression: 1 — `test_old_column_survives_failed_reembed` guards the Stage 6 HIGH data-loss finding.
- Chaos: 2 — `test_migration_survives_ollama_outage` kills embedding mid-migration and asserts the old column stays authoritative; `test_cutover_refuses_when_new_column_has_nulls`.
- E2E: 1 — `test_full_dimension_change_end_to_end` indexes at dim A, switches the stubbed model to dim B, runs `osm migrate`, and asserts search returns correct results before, during, and after. This is the demo script in §5.
- Performance: 1 — `test_migration_reports_progress_within_interval` asserts progress is written to `index_state` at least every 100 notes, so an hours-long run is observable rather than opaque.
- TDD Parity: 100% — two new public symbols (`migrate_embedding_dimension`, the `migrate` subcommand), both directly tested.
- Coverage: +4%.

**Acceptance criteria (binary):**
- [ ] `init_db` on a dimension-mismatched database adds no column and re-embeds nothing.
- [ ] The mismatch is recorded in `index_state` and surfaced by `/api/stats`.
- [ ] Search returns results while a mismatch is detected but unmigrated.
- [ ] `osm migrate --embedding-dim` adds a new column and leaves the old one populated until cutover.
- [ ] With embedding stubbed to fail, the original column and all its data survive.
- [ ] Cutover refuses to drop the old column while any new-column row is NULL.
- [ ] `osm migrate --dry-run` reports the plan and writes nothing.
- [ ] Search returns results at every point during a migration.
- [ ] `pytest -q -m "not pg"` exits 0 with no database.

**Estimated effort:** L

**Blocked by:** Iteration 7

---

## 4. Test inventory summary

| Iter | Smoke | Unit | Integration | State machine | Contract | Regression | Chaos | E2E | Performance | TDD Parity | Coverage Δ |
|------|-------|------|-------------|---------------|----------|------------|-------|-----|-------------|------------|------------|
| 0 | 1 | 1 | 0 | 0 | 1 | 0–6 | 0 | 0 | 0 | 100% | +0% (19 dormant tests activated) |
| 1 | 1 | 3 | 1 | 0 | 1 | 1 | 0 | 0 | 0 | 100% | +1% |
| 2 | 1 | 12 | 2 | 1 | 1 | 1 | 2 | 1 | 0 | 100% | +4% |
| 3 | 1 | 4 | 1 | 0 | 0 | 1 | 1 | 0 | 0 | 100% | +2% |
| 4 | 1 | 1 | 1 | 0 | 1 | 0 | 0 | 0 | 0 | 100% | +0% |
| 5 | 1 | 3 | 4 | 1 | 1 | 1 | 1 | 0 | 0 | 100% | +3% |
| 6 | 1 | 1 | 4 | 1 | 1 | 1 | 1 | 1 | 0 | 100% | +4% |
| 7 | 1 | 1 | 4 | 1 | 1 | 1 | 1 | 0 | 1 | 100% | +3% |
| 8 | 1 | 1 | 7 | 1 | 1 | 1 | 2 | 1 | 1 | 100% | +4% |

Baseline coverage is **unmeasured** — CI runs `pytest` with no `--cov` and no `.coveragerc` exists (Stage 7 finding). **Iteration 0 records the real baseline** and the executor corrects this table's absolutes from that measurement. The deltas are the meaningful figure; there are no absolutes in this table on purpose.

## 5. End-to-end definition of done

**Deduplicated acceptance criteria:**

1. Every file in `tests/` is collected by a bare `pytest` run (≥351 tests).
2. The dashboard binds `127.0.0.1` unless `DASHBOARD_BIND` says otherwise, and Docker sets it explicitly.
3. No Postgres password appears in dashboard startup output.
4. All four mutating dashboard endpoints return 401 without a valid bearer token, compared with `hmac.compare_digest`.
5. The token persists at `~/.config/obsidian-semantic-mcp/dashboard_token` mode `0600`, is overridable by `DASHBOARD_TOKEN`, and no token material is ever written under the repo root.
6. GET endpoints remain unauthenticated and functional; the UI sends the token on every POST.
7. A DB outage during a file delete or move cannot kill the watchdog observer, and the failure is logged.
8. `starlette` and `uvicorn` are gone from `pyproject.toml` and `uv.lock`; `CLAUDE.md` no longer calls the dashboard Starlette.
9. Re-index mutual exclusion holds across two independent connections; `_reindex_lock` no longer exists.
10. `reindex_vault` returns busy rather than starting a concurrent pass.
11. The `pg` tests run in CI against a real `postgres` service, and the `pg` fixture refuses any database not named `*_test`.
12. Indexing failures written by one process are readable by another and surface at `/api/stats`.
13. `schema_version` records one row per applied migration; `init_db` is idempotent; an existing database is stamped, not rebuilt.
14. A dimension mismatch is detected at boot but never migrated automatically; `osm migrate --embedding-dim` performs it non-destructively and survives an Ollama outage without data loss.
15. `pytest -q -m "not pg"` exits 0 on a machine with no database.

**Demo script (the single manual test that proves the whole feature):**

1. `createdb obsidian_brain_test`; point `PYTEST_DATABASE_URL` at it.
2. Start the dashboard natively: `OBSIDIAN_VAULT=<vault> python3 src/dashboard.py`. Confirm the banner shows `127.0.0.1`, shows **no** password, and that `~/.config/obsidian-semantic-mcp/dashboard_token` now exists mode `0600`.
3. From another machine on the LAN, `curl http://<host>:8484/api/stats` — must fail to connect.
4. Locally, `curl -X POST http://127.0.0.1:8484/api/reindex` with no header — must return 401.
5. Repeat with `-H "Authorization: Bearer $(cat ~/.config/obsidian-semantic-mcp/dashboard_token)"` — must return 200.
6. While that re-index runs, call the `reindex_vault` MCP tool — must return busy, not start a second pass.
7. Restart the dashboard. The same token still works: it persisted.
8. `kill -STOP` the Postgres container, delete a note from the vault, `kill -CONT`. Touch another note. Confirm it still indexes: the watcher survived.
9. `/api/stats` shows the failure recorded during the outage.
10. Point `EMBEDDING_MODEL` at a model with a different dimension and restart. Confirm the server **keeps serving on the old index** and reports `dimension_mismatch` rather than re-embedding.
11. Run `osm migrate --embedding-dim --dry-run`, then for real. Search throughout — results must never go empty.

**Green command at the end** (every file this plan creates or modifies, explicit for `/plan-implement`):

```
uv run pytest -q \
  tests/test_collection_contract.py \
  tests/test_dashboard_security.py \
  tests/test_watchdog_resilience.py \
  tests/test_dependency_contract.py \
  tests/test_advisory_lock.py \
  tests/test_index_state.py \
  tests/test_migrations.py \
  tests/test_dimension_migration.py \
  tests/test_unit.py \
  tests/test_dashboard_smoke.py \
  tests/test_osm_init.py \
  tests/test_osm_commands.py \
  tests/test_e2e.py \
  tests/test_launcher.py \
  tests/test_portable_invocation.py \
  tests/test_setup.py \
  tests/test_stdin_pipe_response.py \
  tests/test_v096_config.py
```

Requires `PYTEST_DATABASE_URL` pointing at a `*_test` database. Without one, append `-m "not pg"`.

## 6. Out of scope

| Deferred | Reason |
|----------|--------|
| README badges, dashboard GIF, community files | Stage 1–3 scope. The sibling plan, and arguably the higher-priority one for job hunting — a reviewer reaches this code only if the README earns it. |
| Splitting `osm_init.py` (2878 lines, 93 functions, 0 classes) | Large refactor, no behavior change, 74 KB of tests to keep passing. Real debt that loses to security work. |
| Multi-vault `_resolve_vault_path` bug (`src/server.py:1041-1047`) | Fails closed, so it is a broken feature rather than a vulnerability. Needs its own plan with the multi-vault story. |
| Dashboard `ThreadingHTTPServer` (self-DoS via slow embed) | Real, but after loopback binding the attacker is the user themselves. Cheap to add later. |
| Dependency CVE bumps, `.env.example`, dependabot | Stage 4 scope. |
| Branch protection, CI coverage flags, environment gates | Stage 7 scope; configuration rather than code. Note iteration 5 adds a CI *service*, not a governance control. |
| Docker digest pinning, `read_only`, `cap_drop` | Stage 5 MEDIUM docker findings; a compose-hardening pass of its own. |
| Query-param validation (`?limit=abc` → uncaught `ValueError`) | Stage 5 LOW. Fold into the next dashboard touch. |
| Default Postgres password `"obsidian"` | Needs an `osm_init` UX decision, not a code fix. The token-file pattern from iteration 2 is the obvious template when it gets planned. |
| Removing `_INDEXING_IN_PROGRESS` entirely | Deliberately kept as an in-process fast path. Revisit only if the DB round-trip proves cheap. |

## 7. Open questions

None. The three questions raised at drafting were resolved into the plan: token persistence (iteration 2 — `~/.config/`, not `.env`), `pg` tests in CI (iteration 5 — yes, with a `postgres` service and a `*_test` database guard), and dimension-migration triggering (iteration 8 — operator-run `osm migrate`, never automatic on boot).

---

## Build outcome — 2026-07-20

**Shipped:** all 9 iterations (0-8), on branch `feat/security-correctness`, in 3 commits:
- `92f69ff` test(collection) — iteration 0, plus the conftest config-dir fence and the `pg_dsn` fix
- `e2a5304` chore(deps) — iteration 4
- `86b98b7` feat(security) — iterations 1-3, 5-8

Suite: **408 passed, 30 skipped** (bare `pytest`, which is what the pre-commit hook runs). Coverage 48% → 51%.

**Deviations from plan:**
- **Not 9 commits.** `src/server.py` carries iterations 3, 5, 6, 7 and 8; `src/config.py`/`src/dashboard.py` carry 1, 2 and parts of 5, 6, 8. Per-iteration commits would have required patch-level staging and produced intermediate commits with failing tests. Collapsed to 3 coherent commits, each green on its own.
- **`CLAUDE.md` line not fixed.** Guardrail-blocked (protected instruction file). Still outstanding — and note the correction below means the accurate wording is `http.server`, not Starlette.
- **Token resolution made lazy**, which the plan did not specify. As written it resolved at import, so importing the module wrote a secret to the real config dir; test collection triggered exactly that during the build.
- **`pg_dsn()` reads `PYTEST_DATABASE_URL` only.** The plan said "prefers" over `DATABASE_URL`; the implemented fallback contradicted its own docstring and would have connected to real data on any host whose database name ended in `_test`. It also broke the pre-commit hook by picking up another test module's placeholder DSN.
- **Iteration 7 DDL split:** `notes`' own `CREATE TABLE` stayed inline in `init_db()`, since a static migration list cannot parameterise `vector(N)` with a dimension only known after probing Ollama at runtime. Migration 1 covers `note_links` + the two indexes; migration 2 covers `index_state`.

**Learned:**
- **The starlette/uvicorn finding in the audit was wrong.** Both are unconditional requirements of the `mcp` SDK. Removing our pins closes zero CVEs. Corrected in `docs/audits/2026-07-20-job-ready.md`; the real fix is `mcp` 1.26.0 → 1.28.1.
- **The test suite had been writing to the real `~/.config/obsidian-semantic-mcp/` on every run**, long before this work: `PROJECT_ROOT_FILE` is derived from `OSM_CONFIG_DIR` at import, so repointing the directory alone does not move it. Content happened to be identical each time, which is why nobody noticed. A test crashing mid-`_with_root()` could have left the real `osm` launcher pointing at a deleted tmp_path.
- **`test_stdin_pipe_response.py` opened a real TCP connection to the live Postgres container at *collection* time** — its `skipif` probe was a decorator argument, which evaluates on import. Any `skipif` doing I/O has this property.
- **A safety guard that raises is worth more than one that skips.** The `*_test` database guard is what surfaced the `DATABASE_URL` fallback bug; had it skipped quietly, the fallback would have shipped.
