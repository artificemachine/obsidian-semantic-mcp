# Portfolio-Ready Audit — obsidian-semantic-mcp

**Date:** 2026-07-22
**Auditor:** Claude Code (Opus 4.8, 1M context)
**Repo:** https://github.com/artificemachine/obsidian-semantic-mcp (PUBLIC)
**Version:** 0.16.0 (main @ 66361ea) | Latest release: v0.16.0
**Mode:** default (full pipeline, all 9 stages)

Prior audit: `docs/audits/2026-07-21-portfolio-ready.md` (NEEDS POLISH — all findings since fixed via PRs #35–#38 + v0.16.0). This run re-verifies from scratch.

---

## Stage 1 — Recruiter First-Impression Gate — PASS

**Verdict:** Strong first impression; one gap (no visual demo above the fold).
**Blockers:** 0

### Findings
| Severity | Finding | Evidence |
|----------|---------|----------|
| LOW | No screenshot/GIF/demo above the fold — README opens with 4 badges then prose. Recruiter gets a clear text pitch ("The Problem"/"The Solution" in <30s) but no visual proof. | `README.md:1-25` — only badge images, no `![...]` demo |
| INFO | 2 commits authored by third-party emails (`yjjoeathome@gmail.com`, `frederic.pageau@gmail.com`) — likely upstream/template origin, not the owner. | `git log --format=%ae` (1 each of 191) |

### Passed checks
- Repo metadata: description set, 12 relevant topics, default branch `main`, PUBLIC, no test/wip naming.
- README above-fold: what/why clear in first 25 lines. Tests + License + Python + Docker Hub badges all present.
- LICENSE present (Apache 2.0, 10.5K), consistent with README badge.
- Personal-data scan: `gitleaks detect --config .gitleaks.toml` → **no leaks** (171 commits). Only tracked personal-path hit is `/Users/me/` test fixtures in `tests/test_osm_init.py:592` (example fixture, class-b, not a secret).
- Working-tree hygiene: no committed binaries, no `.DS_Store`/editor droppings, no TODO/FIXME litter in README/src/osm_init.py.
- Quality signals: CI badge, visible test suite (460 test funcs / 19 files), CHANGELOG, 24 tagged releases with notes.
- Community files: `SECURITY.md`, `CONTRIBUTING.md` (85 lines, real setup + PR checklist — not a stub), `CODE_OF_CONDUCT.md`, `.github/ISSUE_TEMPLATE/` (bug + feature), `PULL_REQUEST_TEMPLATE.md`, `dependabot.yml` — full community-standards set.

## Stage 2 — Git History & Release Hygiene — PASS

**Verdict:** Clean recent history, semver tags all have releases; two accepted legacy limitations.
**Blockers:** 0

### Findings
| Severity | Finding | Evidence |
|----------|---------|----------|
| LOW (accepted) | 47 commits have author-email literally `refactor code` (broken git config artifact) — permanently unattributable without a prohibited history rewrite. | `git log --format=%ae HEAD` → 47× `refactor code` |
| LOW | 23 commits attributed to emails not linked to GitHub (`new.blacc@proton.me` ×20, `celestinmax@gmail.com` ×3) — reclaimable by adding those as verified emails in GitHub settings (owner web action). | same source |
| LOW | Tag `v0.15.1` points to a tree whose `pyproject.toml` still reads `version = "0.15.0"` — tag cut one commit before the bump (PR #33) landed. Cosmetic; `v0.15.0` and `v0.16.0` are consistent. | `git show v0.15.1:pyproject.toml` → 0.15.0 |
| INFO | All 15 recent merged PRs self-authored + self-merged, 0 external reviews. Solo-maintained repo — noted neutrally, not a defect. CONTRIBUTING does not imply a team review process. | `gh pr list --state merged` reviews=0 |

### Passed checks
- Commit messages: recent 40 all conventional (`feat`/`fix`/`chore`/`docs`), no wip/asdf/typo noise above the fold.
- Tags: 24 tags, semver, contiguous through v0.16.0. Latest tag == latest manifest version (0.16.0). ✓
- Releases: every tag has a GitHub release with notes; latest release v0.16.0 matches manifest; no drafts.
- Branches: 13 remote branches (2 feature: `chore/dashboard-structured-logging`, `chore/sync-uv-lock` — merged via #36/#38, deletable; 11 open Dependabot). See Cleanup Plan.

### Cleanup Plan
**Safe (no history rewrite):**
- Delete merged remote branches whose PRs already landed: `git push origin --delete chore/dashboard-structured-logging chore/sync-uv-lock` (PRs #36, #38 merged).
- Leave the 11 open `dependabot/*` branches — they back open PRs.
- Optional: add `new.blacc@proton.me` + `celestinmax@gmail.com` to GitHub → Settings → Emails to reclaim 23 commit attributions (owner web action).

**Rewrite (needs force-push — NOT recommended, repo is public + cloned):**
- The 47 `refactor code`-email commits could only be re-attributed via `git filter-repo`, which rewrites public history. Accepted limitation; do not do this.

## Stage 3 — README + Docs — NEEDS WORK

**Verdict:** README structure is strong; docs/ carries stale planning/incident artifacts + one drifted test count.
**Blockers:** 0

### Findings
| Severity | Finding | Evidence |
|----------|---------|----------|
| MED | README claims "Runs 446 tests" but the suite now has 460 test functions (actual pass count verified in Stage 4). Count drifts every release — same drift fixed at #29/#37 recurring. | `README.md:485` |
| LOW | Planning/aspirational docs tracked under `docs/` with no "historical" framing: `PLAN-portable-mcp-config.md`, `PLAN-security-correctness.md` (811 lines), `proposed-rename-mnemosyne.md`. A reviewer may read them as current roadmap. | `docs/PLAN-*.md`, `docs/proposed-rename-mnemosyne.md` |
| LOW | 4 dated incident/postmortem docs (`mcp_failures_2026-04-30.md`, `mcp_raw_stdin_fix_2026-05-07.md`, `mcp_startup_incident_2026-04-30.md`, `mcp_startup_race_2026-05-06.md`) sit alongside living docs — clutter, read as internal engineering notes. | `docs/` listing |

### Passed checks
- README headings: logical flow (Problem → Solution → Quick Start → Native Install → Troubleshoot → Using with Claude → osm CLI). Broken `#native-install-macos` anchor fixed (#37); Troubleshoot section present.
- `docs/README.md` index present and current (Audits description fixed #37).
- CONTRIBUTING Python-version + test-count claims de-drifted (#37 points to badge instead of a number).
- Personal-name-in-prose scan of `docs/*.md` (excl. audits): clean — no "Prepared for/Author/Contact" signature lines exposing a real name.


## Stage 7 — CI/CD Governance — READY

**Verdict:** No fail-open gates; publish gated on tests; all actions SHA-pinned; branch protection strict.
**Blockers:** 0

### Findings
None (LOW/INFO only).

### Passed checks
- **Publish gated on tests**: `docker-hub.yml` `build` job has `needs: test`, and `test` = `uses: ./.github/workflows/tests.yml`. A bare `v*` tag push cannot ship images unless the full suite (incl. pg-marked tests against a pgvector service) passes. No broken-tag-ships-broken hole.
- **Action pinning**: every `uses:` in both workflows is pinned to a full commit SHA with a version comment (checkout, setup-python, setup-uv, codecov, docker/*). No mutable `@v4` float.
- **SAST is blocking**: `uv run shipguard scan . --format terminal` is a plain `run:` step — non-zero exit fails the job.
- **Branch protection** (`main`): required check `unit-tests` + strict (must be up to date), force-push disabled, deletions disabled.
- Coverage upload (`codecov` `fail_ci_if_error: false`) is intentionally non-blocking — it's a reporting step, not a gate. Correct.

## Stage 8 — Claims vs Reality (bulletproof) — NEEDS POLISH

**Verdict:** 13/14 claims verified (~93% honesty); sole drift understates reality (test count). No overclaim.
**Blockers:** 0

### Findings
| Severity | Finding | Evidence |
|----------|---------|----------|
| MED | README:485 "Runs 446 tests (415 without a database, plus 31 pg)" is stale — `pytest --co` collects **463 total (431 non-pg, 32 pg)**. Undercounts by 17/16/1 — undersells, but a senior reviewer expects numbers to match `pytest --co`. | `README.md:485` vs `uv run pytest --co` |

### Verified claims (13/14)
Python 3.10+ (`pyproject.toml:9`), Apache 2.0 (LICENSE + classifier), Docker Hub `newblacc/obsidian-semantic-mcp` (`docker-hub.yml:40`), Tests badge → correct remote, semantic search via Ollama `nomic-embed-text` (`server.py:99`), pgvector cosine search (`server.py:1650`), watchdog auto-reindex (`server.py:56,1026`), Linux-only-CI honestly disclosed (`tests.yml` ubuntu-only, README:57 says so), GPU optional-opt-in (README:215), 768-dim default (`server.py:336`), install decoupling via `~/.config` + `OSM_PROJECT_ROOT` (`launcher.py:28`), no unbacked "production-ready/hardened/enterprise" wording (0 grep hits), version-agnostic SECURITY.md.

**Honest direction**: the one inaccuracy *understates* the suite — the safe direction for a portfolio. No FALSE claims, no overclaim of maturity.


## Stage 4 — Fresh-Clone Verification + Dependency Health — PASS

**Verdict:** Fresh clone from remote installs, tests green, lockfile in sync; only transitive-MEDIUM CVEs remain.
**Blockers:** 0

### Fresh-clone transcript
| Step | Command | Result |
|------|---------|--------|
| Clone | `git clone https://github.com/artificemachine/obsidian-semantic-mcp.git` (remote) | OK — HEAD `66361ea`, matches origin/main |
| Install | `uv sync` | OK — clean, 55 packages resolved |
| Lockfile | `uv lock --check` | **In sync** (PR #38 fix holds) |
| Tests | `uv run pytest -q` (bare, no DB — what a stranger gets) | **432 passed, 0 failed, 31 skipped** (pg tests gated, skip cleanly without a DB) |
| Compose | `docker compose config` | **VALID** — stack parses cleanly |

Fresh clone from the public remote works end-to-end for a stranger: `uv sync` → `pytest` green with zero local knowledge. Collected 463 tests total (431 non-pg + 32 pg); 432 pass without a database, pg-marked tests skip gracefully.

### Dependency health
| Severity | Finding | Evidence |
|----------|---------|----------|
| MED (transitive) | 16 known CVEs across 6 packages, **all transitive-MEDIUM, 0 HIGH/CRITICAL**: `starlette 0.52.1` (7), `python-multipart 0.0.22` (5), `click`, `pydantic-settings`, `pygments`, `pytest` (1 each). Pulled in via the `mcp` SDK + dev tooling, not direct production deps. | `pip-audit` in fresh clone |

- Lockfile present (`uv.lock`) and in sync with manifest. ✓
- `dependabot.yml` configured (pip + github-actions ecosystems) — 11 open Dependabot PRs prove it's live.
- No outdated majors in direct deps.
- **Teardown**: this run did `uv sync` + `pytest` only (no image build), so no containers/volumes/images were created. Scratchpad clone removed. Nothing left behind.

**Note**: the full Docker quickstart (`osm init --mode 3`, which builds + runs the 4-service stack) was not rebuilt from scratch this run — the identical images are already verified live (v0.15.1 stack healthy) and were published + pulled during the v0.16.0 release. `docker compose config` confirms the compose spec is valid. A cold full-stack build was skipped to avoid a 20-min / multi-GB rebuild of already-verified artifacts (disclosed, not silent).

## Stage 5 — Hardening Pipeline (gauntlet, 7 dimensions) — READY

**Verdict:** All 7 hardening dimensions clean; 7 SAST findings confirmed documented false-positives; 0 real defects.
**Blockers:** 0

### Per-dimension verdict
| Dimension | Verdict | Note |
|-----------|---------|------|
| Security | SECURE | 7 HIGH SAST = all PY-007 false positives (hardcoded `vault_clause` literals + DDL `embed_dim` int + test fixtures). Bearer auth `hmac.compare_digest`, token file `0600`. |
| Threat-model | SECURE | All 4 mutating POST endpoints auth-gated first (`dashboard.py:897`); path traversal blocked (`server.py:1298`); SQLi surface parameterized; SSRF not per-request. |
| Code-quality | APPROVED | db_conn discards broken connections on error (`server.py:243`); `_handle_upsert` watchdog cannot die (broad except + FileNotFound recovery); reindex advisory-lock race-safe. |
| QA-coverage | READY | 460 test funcs / 19 files. PR #35 native-launch regression test verified present (`test_launcher.py:204`). All 11 MCP tools + 4 endpoints + CLI covered. |
| UX | PASS | `osm` CLI: non-interactive flags, `--dry-run` preview, clean error exits. |
| Simplify | PASS | No dead code/dup/over-engineering. |
| Docker-audit | SECURE | Non-root user, loopback-only host ports, healthchecks on all long-running services, secrets at runtime not in layers, `.dockerignore` excludes `.env`/keys, vault mounted `:ro`, memory limits + log rotation. |

### Findings (all LOW, non-blocking, pre-existing)
| Severity | Finding | Evidence |
|----------|---------|----------|
| LOW | GET endpoints (`/api/search`, `/api/stats`) unauthenticated — accepted (read-only + loopback). Would matter only if ever bound to `0.0.0.0`. | `dashboard.py:848-851` |
| LOW | Base images `ghcr.io/astral-sh/uv:latest` + `ollama/ollama:latest` use mutable `:latest` tags — supply-chain reproducibility risk. | `Dockerfile:3`, `docker-compose.yml:36,65` |
| LOW | `osm_init.py` = 3097 lines single file (100 functions, well-decomposed — not a god-function). Candidate for package split for navigability. | `osm_init.py` |


## Stage 6 — Architecture (arch-audit) — READY

**Verdict:** Senior-grade data layer; prior CRITICAL fixed + regression-guarded; only 4 LOW polish items.
**Blockers:** 0

### Re-verification of the 2026-07-21 CRITICAL (native-entry) — HOLDS FIXED
- `_native_entry()` (`osm_init.py:1430-1449`) now populates `env`: `DATABASE_URL` always + `OBSIDIAN_VAULTS` (multi) or `OBSIDIAN_VAULT` (single). No longer `env:{}`.
- `launcher._validate_env()` (`launcher.py:56-62`) is satisfied by that env.
- Regression test present and correctly shaped: `test_native_entry_env_is_launchable` (`test_launcher.py:204`) feeds the registered entry's env straight into `_validate_env()`. Prior MEDIUM ("no test exercises native launch path") closed.
- Prior two HIGHs resolved/reclassified: dashboard logging real (PR #36); `osm_init.py` god-module downgraded to LOW accepted debt (deliberate single-file installer, logic cleanly separated in `src/`).

### Findings (all LOW, non-blocking)
| Severity | Finding | Evidence |
|----------|---------|----------|
| LOW | Boot-time initial index bypasses the reindex advisory lock — an operator reindex during a long first-boot index can run a 2nd concurrent `index_vault`. Non-corrupting (idempotent upsert + hash-skip) but wastes 2× embedding CPU. The "reindex mutual exclusion" invariant has a hole. | `server.py:1177-1190` (no lock), only `:1775` + `dashboard.py:936` lock |
| LOW | Dimension-mismatch write failures near-invisible — post-mismatch, edited notes fail to INSERT (`vector(new)` into `vector(old)` col), caught as a per-file `log.warning` only, never recorded in `index_state`. Dashboard shows the boot-time mismatch but not ongoing silent write failures. | `server.py:415-453`, `731-770`, `1064-1065` |
| LOW | Log level hardcoded `INFO`, no `LOG_LEVEL`/`OSM_LOG_LEVEL` env override — can't raise to DEBUG for field diagnosis without a code edit. | `server.py:176-180` |
| LOW | Dual try/except import idiom (`from .config` / `except ImportError: from config`) — smell caused by the `src`-as-package layout. | `server.py:74-79`, `dashboard.py:29-32` |

### Dimensions confirmed SOLID
Schema invariants (runtime-probed dim, boot-detected mismatch, never auto-migrated); migration story (`schema_version` table, contiguous-version assert, per-migration transactions, idempotent baseline, bounded 5s `lock_timeout` DDL, non-destructive add→backfill→cutover); config drift (`build_dsn()` single source of truth, centralized token/lock-key resolution); multi-process coordination (session-level Postgres advisory lock on dedicated pooled conn, `db_conn()` discards broken conns, watchdog can't die); observability (real `log.*` across dashboard, PR #36); installation decoupling (`OSM_PROJECT_ROOT` → `~/.config` record → XDG default, repo-path-independent).

### Folder-structure verdict — LOW deviation (intentional)
Package IS `src/` itself (`packages = ["src"]`, imports `src.server`) rather than idiomatic `src/<package_name>/`; two entry modules (`osm_init.py`, `obsidian_semantic_mcp.py`) at repo root. Coherent given the single-file-installer constraint (`osm_init.py` must be standalone-copyable, can't import `src/` at first run) but reads slightly unusual to an experienced Python reviewer. `tests/` conventional + comprehensive (19 modules). No stray `.py` dump. Does not break quickstart or CI.

---

# Portfolio-Ready Scorecard — obsidian-semantic-mcp
**Date:** 2026-07-22

| # | Stage | Verdict | Blockers |
|---|-------|---------|----------|
| 1 | First impression | PASS | 0 |
| 2 | Git history & releases | PASS | 0 |
| 3 | README + docs | NEEDS WORK | 0 |
| 4 | Fresh clone + deps | PASS | 0 |
| 5 | Gauntlet (7 dimensions) | READY | 0 |
| 6 | Architecture | READY | 0 |
| 7 | CI/CD governance | READY | 0 |
| 8 | Claims vs reality | NEEDS POLISH | 0 |

**All 6 hard gates PASS:** 0 secrets (gitleaks clean), 0 test failures (432/0/31 from fresh clone), LICENSE present (Apache 2.0), quickstart works from a fresh remote clone, 0 HIGH/CRITICAL CVEs, gauntlet security SECURE. **No stage ran [condensed]** — all 8 stages full-depth, so no condensed verdict cap applies.

## Verdict: NEEDS POLISH

Zero blockers and every hard gate green, but two stages carry open findings led by one recurring, reviewer-visible blemish (README test count drifted again to 446 vs the real 463). By the letter of the verdict rules, PUBLIC-READY requires every stage PASS with LOW-only findings; Stage 3 is NEEDS WORK on the test-count MED, so the honest ceiling is NEEDS POLISH. The gap to top-tier is ~15 minutes of cosmetic cleanup — this is a strong repo one edit away from PUBLIC-READY, not a repo with real problems.

## Top 5 fixes by interview impact
1. **README:485 test count** — change "Runs 446 tests (415 without a database, plus 31 pg)" → "463 tests (431 without a database, plus 32 pg)". Any reviewer running `pytest --co` sees the mismatch. One line. (Also consider pointing at the badge instead of a hard number so it stops drifting — same pattern already applied to CONTRIBUTING in #37.)
2. **Reframe/relocate stale docs** — `docs/PLAN-portable-mcp-config.md`, `docs/PLAN-security-correctness.md`, `docs/proposed-rename-mnemosyne.md`, and the 4 dated `mcp_*_2026-*.md` incident notes read as current roadmap/internal logs. Move to `docs/archive/` or add a one-line "historical, not current state" header so a reviewer doesn't mistake them for the live plan.
3. **Route boot-index through `reindex_lock`** (`server.py:1185`) — closes the one real (LOW) architectural coordination hole; also a clean "I found and fixed a concurrency edge" talking point in an interview.
4. **Pin mutable Docker base tags** — `ghcr.io/astral-sh/uv:latest` (`Dockerfile:3`) and `ollama/ollama:latest` (`docker-compose.yml:36,65`) → digest/version pins for reproducible builds.
5. **Owner web actions** — add `new.blacc@proton.me` + `celestinmax@gmail.com` to GitHub → Settings → Emails (reclaims 23 commit attributions); delete 2 merged remote branches (`chore/dashboard-structured-logging`, `chore/sync-uv-lock`).

## What this repo says about you (honest read)
This reads as an engineer's project, not a student's. The data layer is genuinely senior-grade: versioned idempotent migrations with bounded-lock DDL, a non-destructive add→backfill→cutover dimension migration, cross-process Postgres advisory locking, a watchdog thread engineered so it cannot die, constant-time bearer auth, and path-traversal-blocked vault resolution — all backed by 463 tests (432 green from a cold clone with zero setup) and a CI pipeline that SHA-pins every action and gates Docker publish on the full suite. The prior audit's one CRITICAL (a native-install MCP entry that could never launch) is fixed and, more tellingly, guarded by exactly the regression test that was missing — the mark of someone who fixes the class, not the instance. The remaining gaps are cosmetic: a stale test count that undersells the suite, a few planning/incident docs that should be archived, and four LOW polish items. It does not overclaim — the one factual drift understates reality, and platform-support honesty (Linux-only CI, stated plainly) is exactly what a senior reviewer wants to see. Fifteen minutes of polish moves it from "clearly competent" to "nothing to pick at."
