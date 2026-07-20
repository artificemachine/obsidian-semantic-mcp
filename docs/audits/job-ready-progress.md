# Job-Ready Progress — obsidian-semantic-mcp

Resume state for `/job-ready continue`. Report: `docs/audits/2026-07-20-job-ready.md`

## Stage 1 — Recruiter First-Impression Gate: FAIL (2026-07-20)
- verdict: gitleaks clean (no live secret, pipeline continues); LICENSE + metadata + SHA-pinned CI solid; README has zero badges and zero visuals; 4 community files missing; personal home path in 2 tracked docs
- blockers: 2
- evidence: `README.md` (0 badge/image matches); `ls SECURITY.md CODE_OF_CONDUCT.md .github/PULL_REQUEST_TEMPLATE.md .github/dependabot.yml` all missing; `docs/HANDOFF.md:346`; `docs/mcp_failures_2026-04-30.md:25`; `gitleaks detect` 123 commits, no leaks

## Stage 2 — Git History & Release Hygiene: FAIL (2026-07-20)
- verdict: commit messages excellent (0 junk / 149 of 150 conventional, clean --first-parent), but 47 commits on main have author email `refactor code` and are unattributable on GitHub; 14 of 22 tags have no release; 4 merged branches still open. Cleanup plan in report; nothing executed.
- blockers: 1
- evidence: `git log --pretty='%ae' main | grep -c 'refactor code'` → 47; `git tag -l` 22 vs `gh release list` 8; `git rev-list --count origin/main..origin/<b>` → 0 for all 4 branches

## Stage 3 — README + Docs: NEEDS WORK (2026-07-20)
- verdict: quickstart hands a stranger `--mode 1`, which means a different install on each OS and is not the mode the README itself recommends; docs/ is 19 flat files with no index and 1 inbound link
- blockers: 2
- evidence: `README.md:31,109`; `README.md:446` claims 230 tests vs 332 actual; `docs/` 19 files, `README.md:126` sole reference

## Stage 4 — Fresh Clone + Dependency Health: FAIL (2026-07-20)
- verdict: quickstart dry-run and 332 tests both succeed from a fresh clone of the remote, but `.env.example` never ships and the lockfile carries 38 CVEs in 12 packages incl. a HIGH
- blockers: 2
- evidence: `git check-ignore -v .env.example` → `.gitignore:27`; `pip-audit` → 38 vulns/12 pkgs; OSV `GHSA-537c-gmf6-5ccf` → HIGH; `uv tree --outdated` starlette 0.52.1 vs 1.3.1
- note: real (non-dry-run) `osm init` deliberately NOT executed — mutates global MCP config, `~/.local/bin`, and creates a database

## Stage 5 — Hardening Pipeline: FAIL [condensed] (2026-07-20)
- verdict: documented invariants (db_conn, empty-embedding, path traversal, no SQL/shell injection) all hold, but the dashboard has zero auth on a destructive DELETE endpoint bound to 0.0.0.0, and two unguarded delete_note() calls can kill the watchdog thread
- blockers: 3
- evidence: `src/dashboard.py:807-855,837,863`; `src/server.py:820,825`; `grep -rn 'starlette|uvicorn' src/` → 0 hits vs `pyproject.toml:28-29`
- condensed because: `/gauntlet` recipe has no audit-only mode (unconditionally calls loop-fix); aborted and all 7 dimensions run inline

## Stage 6 — Architecture: NEEDS WORK (2026-07-20)
- verdict: careful in-process code but a multi-process system modeled as single-process — re-index lock, indexing flag, and rebuild-failure panel are all process-local state serving separate containers; no schema migration story
- blockers: 3
- evidence: `src/dashboard.py:825,837`; `src/server.py:1516-1520,277-303,334-345,125-147`

## Stage 7 — CI Governance: FAIL (2026-07-20)
- verdict: workflows are well-built (fully SHA-pinned, no fail-open, blocking SAST, publish gated on tests) but `main` has zero branch protection so nothing is actually gated
- blockers: 1
- evidence: `gh api .../branches/main/protection` → 404; `.github/workflows/tests.yml:30,37-41` (codecov step with no --cov); `docker-hub.yml:3-7`

## Stage 8 — Claims vs Reality: FAIL [condensed] (2026-07-20)
- verdict: architectural claims honest, but `--dry-run` fabricates success messages, cross-platform claims have Linux-only CI, and 4 categories of stale doc drift. Honesty 6/10.
- blockers: 2
- evidence: `osm_init.py:1686,1966,2102,2113` unguarded `ok()` under DRY_RUN; `README.md:446` 230 vs 332; 8 stale `celestinmax/` refs; `CLAUDE.md:59` 0.12.2 vs 0.14.6

## Stage 9 — Final Scorecard: COMPLETE (2026-07-20)
- verdict: **NOT READY** — 3 hard gates (HIGH dependency CVE; security stage FAIL; quickstart broken from fresh clone). 2 stages condensed, which independently caps at NEEDS POLISH.
- blockers: 16 total across 8 stages
- evidence: full report at `docs/audits/2026-07-20-job-ready.md`

_Pipeline complete. Nothing was committed, pushed, fixed, or rewritten._
