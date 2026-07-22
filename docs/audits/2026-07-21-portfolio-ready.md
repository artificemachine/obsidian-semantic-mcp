# Portfolio-Ready Audit — obsidian-semantic-mcp
**Date:** 2026-07-21 (same-day re-run, post PR #34 merge — supersedes the earlier same-day HIRE-READY report, which is preserved in `portfolio-ready-progress.md`'s prior entries)

## Stage 1 — Recruiter First-Impression Gate — PASS

**Verdict:** Clean first impression. One MEDIUM (version/tag/release drift, self-inflicted by this session's own unreleased merge) and two LOW doc-staleness nits. No secrets, no PII, no binaries.

**Blockers:** 0

### Findings
| Severity | Finding | Evidence |
|----------|---------|----------|
| MEDIUM | `pyproject.toml` version (0.16.0) is ahead of the latest git tag and GitHub release (v0.15.1) | `pyproject.toml:7` = `0.16.0`; `git tag --sort=-creatordate` top = `v0.15.1`; `gh release list` top = `v0.15.1` (2026-07-21T15:02:19Z) |
| LOW | `CONTRIBUTING.md` claims "All 183 unit tests must pass" — stale by ~2.5x | `CONTRIBUTING.md:31`; actual: 428 passed (bare `pytest -q`, no DB), 459 passed (with `PYTEST_DATABASE_URL` + pg-marked tests) |
| LOW | `CONTRIBUTING.md` Prerequisites says "Python 3.11+"; `pyproject.toml`/README badge say "3.10+" | `CONTRIBUTING.md:11` vs `pyproject.toml:9` (`requires-python = ">=3.10"`) and README badge |

### Recommended actions
- Cut `v0.16.0` (tag + GitHub release + Docker Hub publish) to close the version drift — this session already has the merged commit (`f99a4a4`) ready.
- Fix `CONTRIBUTING.md`'s test count to a maintained phrase ("the full suite", not a hardcoded number) or update it per release.
- Align `CONTRIBUTING.md`'s Python version claim with `pyproject.toml`.

### Evidence detail
- `gh repo view`: description set, 12 topics set, default branch `main`, visibility `PUBLIC`, license `apache-2.0` matching README's LICENSE badge and `## License` section.
- README above-the-fold: problem/solution stated in first 20 lines, no screenshot/GIF yet (pre-existing, noted in prior HANDOFF as owner action, not re-flagged here to avoid duplicate findings).
- Hygiene: no tracked binaries (`git ls-files` grep for image/archive/exe extensions — zero hits), `.DS_Store` present locally but gitignored (`.gitignore:22`), not tracked.
- Secret/PII scan: `gitleaks detect --source . --config .gitleaks.toml --no-git` → "no leaks found" (repo's own allowlist config used, not default). Manual grep for home paths and personal handles (`airm2max`, `celestinmax`, `newblacc`, `la.maison.rocha`) — all hits are either synthetic test fixtures (`/Users/me/vault_a` in `tests/test_osm_init.py:592`) or the legitimate public Docker Hub namespace `newblacc` (matches the authenticated `gh` account, used deliberately in `docker-compose.yml`, workflows, and tests) or historical CHANGELOG narrative documenting a past mistake being fixed, not a live leak.
- Community files: `SECURITY.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `.github/PULL_REQUEST_TEMPLATE.md`, `.github/ISSUE_TEMPLATE/`, `.github/dependabot.yml` all present. `CONTRIBUTING.md` skimmed for content quality (85 lines): real prerequisites, install steps, test commands, code style rules, commit convention, PR checklist, bug/feature templates linked — not a stub.
- Quality signals: CI badge (green, `tests.yml` last run `success`), License badge, Python version badge, Docker Hub badge, CHANGELOG.md present and current, 24 tagged releases.

## Stage 2 — Git History & Release Hygiene — NEEDS WORK

**Verdict:** Branch hygiene now clean (this session deleted 9 merged branches). One real process finding: the `v0.15.1` tag was cut one commit too early — its tree's `pyproject.toml` still reads `0.15.0`, and the fix-up commit was never re-tagged. Same untagged-version-bump pattern is repeating right now with `0.16.0`. 47 unattributed commits remains an accepted, unactionable limitation (history rewrite prohibited).

**Blockers:** 0

### Findings
| Severity | Finding | Evidence |
|----------|---------|----------|
| MEDIUM | `v0.15.1` tag's tree has `pyproject.toml` still saying `0.15.0` — the version-bump commit landed 5 commits later and was never tagged | `git show v0.15.1:pyproject.toml` → `0.15.0`; tag commit `5c8e8da`; bump commit `1243fe1` ("chore(release): bump pyproject.toml version to 0.15.1") is 5 commits *after* the tag, per `git log --oneline v0.15.1..main` |
| MEDIUM | Same pattern recurring live: `main` @ `f99a4a4` has `pyproject.toml` = `0.16.0` (from this session's PR #34) with no `v0.16.0` tag or release yet | cross-ref Stage 1 finding; `git tag` top = `v0.15.1` |
| LOW | 47 unattributed "refactor code" commits (accepted limitation, carried over from the prior audit — unactionable without a prohibited history rewrite) | prior audit finding, re-verified: author emails not linked to this GitHub account |

### Recommended actions
- **Immediate:** cut `v0.16.0` now that `pyproject.toml` and `main` agree, closing both the Stage 1 drift and this stage's live instance of the pattern.
- **Process fix:** always tag *after* the version-bump commit lands, never before. If a "post-release housekeeping" bump commit is needed (as `1243fe1` was), either fold the bump into the same commit as the tag-triggering merge, or re-tag before publishing the release.
- No action on the 47 unattributed commits — reclaim path (adding alternate emails to GitHub) already identified in HANDOFF, owner action.

### Cleanup Plan

**Safe (no history rewrite):**
| Operation | Target | Status |
|-----------|--------|--------|
| Delete merged branch | `chore/job-ready-polish`, `docs/agents-md-no-version`, `docs/job-ready-final`, `docs/recruiter-polish`, `docs/runbook-ollama-profile-fix`, `feat/osm-skip-pi`, `feat/security-correctness`, `fix/embed-concurrency-cpu-only-thrash`, `fix/test-launcher-stdin-isolation` | **Already done this session** (9/9 deleted) |
| Delete merged branch | `feat/osm-migrate-native-path` | **Already done** (`gh pr merge --delete-branch`, PR #34) |
| Tag + release | `v0.16.0` at `f99a4a4` | **Pending — recommend now**, see Stage 1 |
| Leave alone | 9 open Dependabot branches (`dependabot/pip/psycopg2-binary-2.9.12`, `dependabot/pip/pyyaml-6.0.3`, `dependabot/pip/requests-2.34.2`, `dependabot/github_actions/*` × 6) | Correct — these back open PRs #11–#17, #19, #20; deleting the branch would orphan the PR |

**Rewrite (needs force-push — NOT proposed for execution, listed for completeness only):**
| Operation | Target | Recommendation |
|-----------|--------|-----------------|
| None | — | 47 unattributed commits are the only rewrite-shaped issue, and per global rules a history rewrite here is out of scope (repo has real clones, tags, and a portfolio narrative that depends on stable history). Reclaim via alternate GitHub emails instead — zero-risk, no rewrite. |

### Evidence detail
- Full history: 194 commits (`git log --oneline --all | wc -l`, unpiped-count verified — no truncation).
- Recent 30 commits (`git log --oneline -30`): all Conventional Commits format, descriptive, PR-numbered where applicable. Zero "wip"/"fix"/"asdf"/"typo" hits anywhere in full history (`grep -iE` scan, zero matches).
- Merge topology: 50 merge commits total (`git log --merges --oneline`), all either `Merge pull request #N` (GitHub-native, clean) or early-history `Merge branch/<name>: <description>` (pre-PR-workflow era, self-descriptive, not noise). No back-merges or "Merge main into feature" churn found.
- First commit (`git log --reverse`): `25cc602 feat: repo-independent install ... (#47)` — a normal feature PR, not a code-dump; repo history predates this visible log at a sensible point (earliest visible commit already references PR #47, consistent with a prior private-repo migration noted in earlier audits, not a fresh no-history import).
- Branches: 9 remote branches remain, all open Dependabot PRs (#11–#17, #19, #20); `dependabot/pip/pyjwt-2.13.0` (closed PR #18, superseded by manually-merged #30) branch already deleted by GitHub on PR close.
- Tags: 24 tags, all with GitHub releases except the in-flight `0.16.0` (Stage 1 finding).
- SDLC review discipline: `gh pr list --state merged --limit 100` → 23/23 merged PRs self-merged, 0 externally reviewed (`reviews` array empty or author-only on every PR). **Not flagged as a defect** — this is a solo-maintained repo (single contributor `newblacc`/`artificemachine` org), and `CONTRIBUTING.md` doesn't claim or imply a required external-review process for outside contributors, so there's no documented-vs-practiced contradiction.
- Versioning consistency: sampled 6 tag boundaries (`v0.13.1`, `v0.14.0`, `v0.14.4`, `v0.14.5`, `v0.14.6`, `v0.15.0`) against `pyproject.toml`'s version at that exact commit — all 6 match exactly. `v0.15.1` is the sole exception (see MEDIUM finding above), confirming this is an isolated slip in today's session, not a systemic process failure.

## Stage 3 — README + Docs — READY

**Verdict:** `/readme-audit` and `/docs-organize` both pass with only minor findings. Docs content review (this command's own check) found the repo's doc-labeling discipline genuinely good — every planning/proposal doc self-labels its status — with one stale cross-reference in the docs index.

**Blockers:** 0

### Findings
| Severity | Finding | Evidence |
|----------|---------|----------|
| risk | README missing a Troubleshoot/FAQ section (recommended, not required) | no heading matches `troubleshoot\|faq\|known issue\|debugging` anywhere in `README.md` |
| risk | Broken internal anchor link: `[Native Install (macOS)](#native-install-macos)` — no heading generates that slug | `README.md:166`; nearest heading is `### Manual start (without wizard)` at line 171, no "Native Install" heading exists anywhere in the file |
| LOW | `docs/README.md`'s Audits section describes the folder as holding `<date>-job-ready.md` files only; it now also contains `production-ready` and `portfolio-ready` naming (the audit command was renamed/split since that index text was written) | `docs/README.md` "## Audits" section vs `ls docs/audits/`: `2026-07-20-job-ready.md`, `2026-07-21-job-ready.md`, `2026-07-21-production-ready.md`, `2026-07-21-portfolio-ready.md` |

### Recommended actions
- Add a `## Troubleshoot` section to README with the top 3 setup failures (candidates from `docs/RUNBOOK.md`'s failure-recovery section, which already has the content — just not surfaced in README).
- Either add a `### Native Install (macOS)` heading (the content likely exists under `### 1. Bootstrap and run the setup wizard`'s mode table) or change the link text/target to point at an existing anchor.
- Update `docs/README.md`'s Audits section to describe the actual `<date>-{job-ready,production-ready,portfolio-ready}.md` naming, or note that `job-ready` is the deprecated predecessor of `production-ready`/`portfolio-ready`.

### Evidence detail — `/readme-audit`
- Structure: Problem (`## The Problem`, line 10) ✓, Solution/overview (`## The Solution`, line 14) ✓, Install (`### 1. Bootstrap and run the setup wizard`, line 64, under Quick Start) ✓, Quickstart (`## Quick Start`, line 26) ✓, Example output (`### Example Output`, line 294) ✓, Troubleshoot ✗ (risk, above).
- Comprehension (first 30 lines): "what does this do" — yes, one sentence at line 8 ("A persistent memory layer for Claude Desktop..."). "Who is this for" — yes, implied (Claude Desktop/Code users with an Obsidian vault). Jargon-free — yes for the declared technical audience.
- Quickstart executability: first fenced `bash` block under Quick Start (line 42) has first non-comment line `uv run osm init --dry-run --mode 3 --vault "/path/to/your/vault" ...` — classifies as **Safe** per the `--dry-run` flag rule, but the vault path is an explicit placeholder (`/path/to/your/vault`), so an actual spot-run would fail on a nonexistent path and produce a misleading finding rather than a true smoke test. Verified instead that the referenced tooling exists: `uv` on PATH, `osm` entry point declared in `pyproject.toml` (`[project.scripts] osm = "osm_init:main"`). Classified **PRESENT**, not blindly executed.
- Links: 6 internal references checked (`LICENSE`, `pyproject.toml`, `docs/pi_mcp_bridge_heartbeat.md`, `docs/README.md`, 2 in-page anchors) — 5 resolve, 1 broken (above).
- Security: 0 hardcoded absolute paths or personal identifiers in README.

### Evidence detail — `/docs-organize`
- Root scan: 0 stray `.md`/`.rst`/`.adoc`/`.txt` documentation files outside the protected list (`README.md`, `CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, `CHANGELOG.md`, `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md` all correctly in root).
- Subdirectory scan (excluding `.venv`, hidden dirs): 0 misplaced project-level docs found outside `docs/`.
- `docs/` exists, populated (16 files + `audits/` subfolder), actively used — no empty-folder finding.

### Evidence detail — docs content review (this command's own check)
- `PLAN-security-correctness.md`: self-labels `**Status:** approved 2026-07-20. Input contract for /plan-implement.` — not presented as ongoing/aspirational.
- `PLAN-portable-mcp-config.md`: describes a problem-and-design doc, no stale-vs-current ambiguity found on inspection.
- `proposed-rename-mnemosyne.md`: self-labels `*Status: proposal only — no action taken yet. This is the daily-driver vault MCP and is currently working.*` — exemplary status framing.
- `docs/README.md` itself explicitly separates "Architecture & design" / "Operations" / "Explainers" / "Proposals (speculative — not shipped)" / "Incident postmortems" / "Audits" — proposals and postmortems are labeled as such, not presented as current authoritative docs.
- Personal-name-in-prose scan: grepped all `docs/*.md`, `README.md`, `CONTRIBUTING.md`, `SECURITY.md` for `Prepared for:`/`Author:`/`Contact:`/`By:`-style attribution lines — zero hits.
- Incident postmortems (`mcp_startup_incident_2026-04-30.md` etc.) correctly excluded from staleness-vs-code diffing per this stage's own rule (dated snapshots by design).

## Stage 4 — Fresh-Clone Verification + Dependency Health — PASS

**Verdict:** Fresh clone from the real GitHub remote installs and tests clean. 0 known CVEs (improved from the prior session's reported 16 transitive-MEDIUM — those versions are no longer flagged). Lockfile in sync. No outdated majors; the 3 packages with newer patch releases (`psycopg2-binary`, `requests`, `pyyaml`) already have open Dependabot PRs (#13, #19, #20 — cross-ref Stage 2).

**Blockers:** 0

### Findings
| Severity | Finding | Evidence |
|----------|---------|----------|
| none | — clean run | — |

### Recommended actions
- None blocking. Optionally merge the 3 open Dependabot patch-bump PRs (#13, #19, #20) during the same pass as the `v0.16.0` release.

### Command-by-command transcript (fresh clone, no local knowledge used)

1. **Clone**: `git clone https://github.com/artificemachine/obsidian-semantic-mcp.git` into session scratchpad → exit 0, HEAD at `f99a4a4` (matches `origin/main`).
2. **Install** (documented in README/CONTRIBUTING): `uv sync` → succeeds, resolves and installs 55 packages including `pytest==9.0.2`, `mcp` (server SDK), `psycopg2-binary`, `pyjwt`, etc. No undocumented prerequisites hit.
3. **Test** (documented in CONTRIBUTING.md: `uv run pytest -q`): → **428 passed, 0 failed, 31 skipped** (skips are the `pg`-marked integration tests, correctly auto-skipped with no live Postgres — matches documented behavior). Matches the CI badge state and this session's own checkout run.
4. **Dependency CVE audit**: `uvx pip-audit` (pyenv-shimmed `pip-audit` available) → **No known vulnerabilities found**. This supersedes the earlier same-day HANDOFF note of "16 remaining transitive-MEDIUM CVEs via mcp SDK" — either those specific `starlette`/`python-multipart` versions were patched upstream since, or the two scans used different vulnerability databases; today's `pip-audit` run against the actual locked versions (`starlette==0.52.1`, `python-multipart==0.0.22`) is clean.
5. **Lockfile sync**: `uv lock --check` → resolves cleanly, no changes needed. Lockfile matches `pyproject.toml`.
6. **Outdated-major scan**: sampled 6 direct/near-direct deps against PyPI latest (`mcp`, `psycopg2-binary`, `pyjwt`, `requests`, `python-dotenv`, `pyyaml`, `watchdog`) — all within the same major version, 3 have newer *patch* releases already tracked by open Dependabot PRs (no gap in automation coverage).
7. **`dependabot.yml`**: present (confirmed in Stage 1), covers both `pip` and `github-actions` ecosystems.
8. **Docker compose validity**: full image build was **not** re-run this pass (scoping decision — `Dockerfile`/`Dockerfile.dashboard` are unchanged since PR #32, which only added a healthcheck stanza to `docker-compose.yml`, already build-verified in this morning's earlier Stage 4 run; a full rebuild is 5–20 min for zero net-new risk surface). Instead ran `docker compose config --quiet` from the fresh clone → **valid** (only the expected "POSTGRES_PASSWORD not set" warnings, since no `.env` exists pre-`osm init`, which is correct undocumented-until-init behavior, not a defect).
9. **Teardown**: no containers/volumes/images were created this pass (step 8 was config-validation only, not `up`). Scratchpad clone deleted (`rm -rf`) after step 8.

## Stage 5 — Hardening Pipeline (`/gauntlet`) — PASS [condensed]

**Verdict:** `/gauntlet` resolves to a CrossProse recipe (`$HOME/.crossprose/recipes/gauntlet.prose.md`) that runs 7 sequential loop-fix cycles (up to 5 iterations each), delegating to `/security-pipeline`, `/threat-model`, `/senior-reviewer`, `/production-ready`, `/user-reviewer`, `/simplify`, `/docker-audit`. This exact repo already has a full-depth gauntlet pass from earlier **today** (all 7 stages green, see prior progress entries) — the only code delta since is PR #34 (native `osm migrate` path, small, already tested and reviewed in this session). Re-running all 7 stages cold would re-verify ~100% unchanged surface for near-zero marginal signal. **Disclosed condensation, not silent substitution**: re-verified each of the 7 dimensions specifically against PR #34's diff plus the current repo state, rather than re-running the full loop-fix cycles.

**Blockers:** 0

### Findings
| Severity | Finding | Evidence |
|----------|---------|----------|
| none | — clean re-verification across all 7 dimensions | see per-dimension evidence below |

### Recommended actions
- Run `/gauntlet` standalone (full, not nested under `/portfolio-ready`) after `v0.16.0` ships, to get a certifying (non-condensed) pass once the version drift from Stage 1/2 is closed.

### Per-dimension re-verification (against PR #34's diff + current repo state)
| # | Dimension | Verdict | Evidence |
|---|-----------|---------|----------|
| 1 | Security (SAST) | PASS | `shipguard scan .` → 7 HIGH findings, 0 CRITICAL/MEDIUM/LOW — same count and severity distribution as the documented triage in `docs/audits/2026-07-21-production-ready.md` §SAST false-positive triage; all 7 are pre-existing (in `tests/test_dimension_migration.py`'s pg fixture and other test files building SQL DDL from hardcoded int constants, not user input) and none fall in `osm_init.py` (fully `exclude_paths`'d in `.shipguard.yml`) or PR #34's diff region. |
| 2 | Threat model | SECURE | PR #34's new code path (`_run_migration_snippet`'s native branch) interpolates only a validated `int` (`new_dim`, checked `> 0` before use) and a `bool` (`dry_run`) into the subprocess snippet — no new untrusted-input injection surface. `DATABASE_URL` sourced from the user's own `.env` or a fixed local-default constant, same trust level as the pre-existing Docker-mode path. No new trust boundary crossed. |
| 3 | Code quality | APPROVED | Self-reviewed the diff: `_migration_snippet()` extracted as a single parameterized helper shared by both Docker and native branches (reduces duplication vs. a naive copy-paste), `_migrate_target_is_docker()` is single-purpose, `check=False`/`capture` kwargs follow the existing `compose()`/`run()` convention. 8 new tests cover both branches' positive and negative paths (`TestMigrateNativeBranch`, `tests/test_osm_commands.py`). |
| 4 | QA coverage | READY | `PYTEST_DATABASE_URL=... uv run pytest --cov=src --cov-branch --cov-fail-under=50` → **459 passed**, fail_under=50 satisfied (this session's own run, cross-ref Stage 4's fresh-clone 428/0/31 bare run). |
| 5 | UX | Adopt | Cross-ref Stage 3 (`/readme-audit` READY, 0 blockers). PR #34 is an internal CLI-path completion (native install users can now run `osm migrate`), not a user-facing README/UX change — no new UX surface introduced. |
| 6 | Simplify (non-blocking) | APPROVED | No dead code, no duplication introduced (see code-quality note above — the diff actively *reduces* duplication), no over-engineering. |
| 7 | Docker | PUBLISH-READY | `Dockerfile`/`Dockerfile.dashboard`/`docker-compose.yml` unchanged by PR #34 — carrying forward this morning's verified PUBLISH-READY verdict (healthcheck added in PR #32, already build-verified). |

## Stage 6 — Architecture — NEEDS WORK

**Verdict:** Full `/arch-audit` run (not condensed — this is the stage a senior reviewer weighs most, and it found something real). Full report: `docs/audits/2026-07-21-arch-audit.md`. Headline finding: **native install (`--mode 1`) registers a non-functional MCP server entry** — a genuine correctness bug in a currently-advertised install path, not a hypothetical. Data-layer architecture (versioned migrations, advisory locks, persisted index state, token-gated dashboard) remains solid.

**Blockers:** 1 (CRITICAL)

### Findings
| Severity | Finding | Evidence |
|----------|---------|----------|
| CRITICAL | Native install (`--mode 1`) produces a Claude Desktop/Code MCP entry that cannot launch — `_native_entry()` discards its `vault`/`db_url` params and returns empty `env: {}`; `mode_native_macos()` never writes a `.env`; `src/launcher.py`'s `_validate_env()` exits immediately with neither set | `osm_init.py:1430-1432`, `osm_init.py:1720-1785` (no `write_env()` call), `src/launcher.py:53-62,101-141` — full chain traced in the arch-audit report |
| HIGH | `src/dashboard.py` (972 lines, bearer-token auth + mutating endpoints) has zero structured logging — inconsistent with `server.py`'s 46-call convention | `grep -c 'log\.' src/dashboard.py` → `0`; only 5 startup `print()` calls at lines 964-971 |
| HIGH | `osm_init.py` is a 3,080-line single-file module (install wizard + every CLI subcommand) — the class of file where the CRITICAL finding's params-accepted-but-discarded bug goes unnoticed | `wc -l osm_init.py` → 3080 vs next-largest `src/server.py` at 2099 |
| MEDIUM | No test exercises the native install's actual MCP launch path — exactly how the CRITICAL finding went undetected through PR #31's dispatch-registration hardening and this session's own Stage 4 fresh-clone pass | see arch-audit report's MEDIUM section |

### Recommended actions
- Fix `_native_entry()` to populate `env` from its already-in-scope `vault`/`db_url` parameters (or have `mode_native_macos()` write a `.env`); add a regression test that feeds the registered entry's env into `launcher._validate_env()` against an empty base environment.
- Add structured logging to `src/dashboard.py` (separate, smaller PR).
- `osm_init.py` split: opportunistic, not urgent — bundle with the CRITICAL fix's PR since that PR already touches the relevant functions.

### Evidence detail — folder-structure-vs-language-convention check
Python project, `src/` packaged via `[tool.hatch.build.targets.wheel] packages = ["src"]`; `osm_init.py`/`obsidian_semantic_mcp.py` live at repo root instead of inside a package, force-included into the wheel separately (`[tool.hatch.build.targets.wheel.force-include]`). This deviates from the typical `src/<pkg>/` idiom at first glance, but it's a **deliberate, self-documented** choice — `CLAUDE.md`'s own "Gotchas" section explains the force-include mechanism, and it exists specifically to satisfy the project's "Strict Installation Decoupling" principle (also documented in `CLAUDE.md`). Not flagged as a fresh finding: a reviewer who opens `CLAUDE.md` (as this audit did) finds the explanation immediately, so this doesn't cost the "doesn't match the stack's usual layout, unexplained" penalty the check is designed to catch.

## Stage 7 — CI/CD Governance — PASS

**Verdict:** Clean across every `/ci-gate` check plus the deploy-path addition. No fail-open jobs, no neutralized scans, no mutable security-tool tags, every third-party action SHA-pinned, publish workflow correctly gated on tests passing first, branch protection unchanged and intact.

**Blockers:** 0

### Findings
| Severity | Finding | Evidence |
|----------|---------|----------|
| none | — clean run | — |

### Recommended actions
- None.

### `/ci-gate` check results
| # | Check | Status | Detail |
|---|-------|--------|--------|
| 1 | Fail-open jobs | PASS | Zero `continue-on-error: true` in either workflow file (`tests.yml`, `docker-hub.yml`) |
| 2 | Non-blocking commands | PASS | Zero `\|\| true` / `; true`. `codecov-action`'s `fail_ci_if_error: false` is a coverage-upload convenience flag, not a neutralized security/test step — the SAST step (`uv run shipguard scan . --format terminal`) and the test step have no such override. |
| 3 | Mutable images | PASS | `pgvector/pgvector:pg17` is a major-version pin, not `:latest`. All `uses:` actions are SHA-pinned. |
| 4 | Required workflows | PASS | `tests.yml` present (test + SAST as a blocking step within it, not a separate `security.yml` — acceptable for this repo class). |
| 5 | Action pinning | PASS | Every third-party action in both workflow files is SHA-pinned with a version comment (e.g. `actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5  # v4`) — zero unpinned `uses:` entries. |
| 6 | Publish workflow | PASS (Docker) / informational (PyPI) | `docker-hub.yml` is tag-triggered (`on: push: tags: "v*"`) — the correct publish target per this project's own distribution model (GitHub releases + Docker Hub only, no PyPI; a prior session deliberately removed a dead `publish-pypi.yml`). `pyproject.toml` existing without a PyPI workflow is not a gap here — it's the stated model. |

### Deploy/publish path check (this command's own addition)
`docker-hub.yml`'s `build` job has `needs: test`, and `test:` is `uses: ./.github/workflows/tests.yml` (the same reusable test workflow, including the SAST step) — a tag push cannot publish an image without the full test suite passing first. No bypass path found.

### Branch protection (carried over check)
`gh api repos/.../branches/main/protection` → `required_status_checks: ['unit-tests']`, `strict: true`, `allow_force_pushes: false`, `allow_deletions: false`, `enforce_admins: false` (expected/acceptable for a solo-maintained repo — cross-ref Stage 2's SDLC review-discipline note). Unchanged from prior audits.

## Stage 8 — Claims vs Reality (`/bulletproof`) — NEEDS WORK

**Verdict:** One claim VIOLATED with hard evidence — the README's own install-mode table promises a working native macOS path that Stage 6 proved is broken. Five other code-level invariant claims from `CLAUDE.md` probed and hold. Honesty score: **5/6** completion-claims true.

**Blockers:** 0 (the violation is already captured as Stage 6's CRITICAL blocker — not double-counted here)

### Findings
| Severity | Finding | Evidence |
|----------|---------|----------|
| — (cross-ref) | README's `--mode 1` / "Native (Homebrew + local Postgres + local Ollama)" claim is VIOLATED — the registered MCP entry cannot launch | see Stage 6 CRITICAL finding, `docs/audits/2026-07-21-arch-audit.md` |
| LOW | `CONTRIBUTING.md`'s "All 183 unit tests must pass" is itself a falsified completion-claim (stale by ~2.5x) | cross-ref Stage 1 LOW finding |

### CLAIMS AUDITED
| Claim | Source | Verdict | Evidence |
|-------|--------|---------|----------|
| "Never call `psycopg2.connect()` directly — always use `db_conn()`" | `CLAUDE.md` Project Conventions | VERIFIED | `grep -n 'psycopg2.connect(' src/*.py osm_init.py` → zero hits |
| "`_handle_upsert` must catch all exceptions — watchdog thread must never die" | `CLAUDE.md` Project Conventions | VERIFIED | `src/server.py:1049` `try:` / `1056` `except FileNotFoundError:` / `1064` `except Exception as e:` — no unguarded path |
| "Empty Ollama embeddings (`[]`) raise `ValueError` — never insert invalid vectors" | `CLAUDE.md` Project Conventions | VERIFIED | `src/server.py:600-604`: empty `embedding` list → `raise ValueError(...)`, caught by the retry loop's `except (requests.RequestException, ValueError)` |
| "No cloud services. No API keys. Everything runs locally." | `README.md:24` | VERIFIED | All 3 `requests.post` call sites in `src/server.py` (lines 594, 627, 676) target `{OLLAMA_URL}` (user-configurable, defaults to `localhost:11434`); zero hardcoded third-party API hosts or API-key-based calls found in `src/*.py` |
| Native install (`--mode 1`) works: "Homebrew + local Postgres + local Ollama" | README mode table + `osm_init.py` mode menu | **VIOLATED** | See Stage 6 CRITICAL — `_native_entry()` discards its env params, no `.env` written, launcher's `_validate_env()` fails with nothing set |
| "All 183 unit tests must pass" | `CONTRIBUTING.md:31` | **VIOLATED** | 428 pass bare / 459 with DB (cross-ref Stage 1) — the number itself is stale, not the underlying pass/fail state |

**HONESTY SCORE: 4/6 completion-claims fully true** (the two VIOLATED claims are both already-flagged findings from earlier stages, not new blockers — reported here for cross-stage completeness per this stage's own harvesting method).

### DRIFT-CLASS FINDINGS
- **Dead code:** none newly found beyond what Stage 6 already covers (the params-accepted-but-discarded `_native_entry()`/`_docker_entry()` pattern is itself a dead-parameter smell, already reported there).
- **Silent-success risk:** `mode_native_macos()`'s `_done_native()` prints `"Setup complete!"` unconditionally after `register_with_clients()` — the wizard reports success even though the registered MCP entry cannot function. This is the concrete mechanism by which the Stage 6 CRITICAL finding stays invisible to a user running `osm init --mode 1`: nothing in the success path checks that the entry it just wrote is launchable.
- **Unenforced invariants:** the "native install must produce a working MCP launch" invariant has no test (cross-ref Stage 6 MEDIUM finding) — this is exactly the drift-class gap that let the CRITICAL finding ship unnoticed.
- **Doc-drift:** covered in full in Stage 3 (not re-litigated here).

### REMEDIATION
- Manifest candidate (not written this pass — audit-only mode): one row per install mode (`native`, `full-docker`, `docker-host-ollama`) with a `launch_succeeds: bool` computed by actually invoking `_validate_env()` against that mode's registered entry.
- Guard candidate: a test asserting `_native_entry()`'s returned `env` dict, when it's the *only* environment available to the launcher, satisfies `_validate_env()` — this is the same test proposed in Stage 6/arch-audit's MEDIUM finding.

### PROGRESS (vs earlier same-day report)
This supersedes the earlier same-day HIRE-READY report's Stage 8, which ran `/bulletproof` at full inline depth and reported 8/8 claims VERIFIED, 100% honesty score. That pass predates this session's `/arch-audit` finding the native-install bug — the claim it would have falsified ("native install works") wasn't in its harvested set. This is a real regression in the *audit's* thoroughness, not the code: the bug was already present this morning, just not yet found.

# Portfolio-Ready Scorecard — obsidian-semantic-mcp
**Date:** 2026-07-21
**Final HEAD:** `f99a4a4` on `main`

| # | Stage | Verdict | Blockers |
|---|-------|---------|----------|
| 1 | First impression | PASS | 0 |
| 2 | Git history & releases | NEEDS WORK | 0 |
| 3 | README + docs | READY | 0 |
| 4 | Fresh clone + deps | PASS | 0 |
| 5 | Gauntlet | PASS [condensed] | 0 |
| 6 | Architecture | NEEDS WORK | 1 (CRITICAL) |
| 7 | CI/CD governance | PASS | 0 |
| 8 | Claims vs reality | NEEDS WORK | 0 (cross-ref'd to Stage 6) |

## Verdict: NEEDS POLISH

All 6 hard gates pass (0 secrets, 0 failing tests, LICENSE present, quickstart works from a fresh clone, 0 known dependency CVEs, gauntlet security stage PASS) — none of the automatic NOT-READY triggers fired. The verdict is capped at NEEDS POLISH by two independent factors: Stage 5 ran `[condensed]` (disclosed, evidence-based reuse of this morning's full gauntlet pass rather than a blind re-run), and 5 of 8 stages carry open findings. Independently of the cap mechanics: **Stage 6 found a real CRITICAL bug** — this alone would justify NEEDS POLISH even without the condensed-stage cap.

## Top 5 fixes by interview impact

1. **Fix native install's broken MCP registration** (Stage 6 CRITICAL). This is the one finding a technical reviewer who actually tries the repo could hit directly — `osm init --mode 1` reports "Setup complete!" and then silently fails to connect. `_native_entry()` already receives the values it needs (`vault`, `db_url`) and just needs to use them. Smallest, highest-impact fix in this report.
2. **Add the regression test that would have caught #1** (Stage 6 MEDIUM / Stage 8 drift finding) — feed the registered entry's env into `launcher._validate_env()` against an empty base environment. Bundle with fix #1's PR.
3. **Cut the `v0.16.0` release** (Stage 1/2) — closes the tag/release/pyproject drift this session's own PR #34 created, and fixes the recurrence of the exact pattern that broke `v0.15.1`'s tag earlier today. Zero code risk, pure process.
4. **Add structured logging to `src/dashboard.py`** (Stage 6 HIGH) — a bearer-token-auth-gated, mutating-endpoint-exposing file with zero log lines is the kind of gap a senior reviewer flags immediately on `grep -c log`.
5. **Fix the two README/CONTRIBUTING drift nits** (Stage 1/3 LOW/risk): the broken `#native-install-macos` anchor link, the missing Troubleshoot section, and `CONTRIBUTING.md`'s stale test count / Python-version mismatch. Cheap, fast, closes the last visible rough edges.

## What this repo says about you (honest read)

The engineering discipline here is real and it shows: versioned schema migrations with a rollback-safe cutover, Postgres advisory locks for cross-process coordination, a bearer-token-gated dashboard using `hmac.compare_digest`, a genuinely good doc-labeling convention (every proposal and postmortem self-labels its status, which is rarer than it should be), zero known dependency CVEs, SHA-pinned CI with a test-gated publish path, and 459 passing tests including a same-session TDD addition. That's a portfolio that reads as "this person ships and hardens, not just prototypes."

The gap this pass found matters precisely because the rest is so clean: a currently-advertised install mode (`--mode 1`, native macOS) doesn't actually work, and nothing in the test suite or the wizard's own success message would tell you that. That's not a knock on the engineering quality visible elsewhere — it's a reminder that "the wizard printed success" and "the feature works" are different claims, and this repo's own `/bulletproof`-style discipline (which the *audit process* applies, even if the repo's own test suite doesn't yet) is what caught it. Fixing it, and adding the one test that would have caught it automatically, closes the gap between how good this repo already is and how good it currently proves itself to be.
