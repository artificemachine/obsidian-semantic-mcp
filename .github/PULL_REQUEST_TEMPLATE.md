<!-- Keep this short. Delete sections that don't apply. -->

## What and why

<!-- One or two sentences: what this changes and the reason. -->

## Type

- [ ] `feat` — new behavior (minor bump)
- [ ] `fix` / `security` / `chore` — patch bump
- [ ] `BREAKING CHANGE` — major bump (explain the migration below)
- [ ] docs / test / ci only (no bump)

## Checklist

- [ ] Tests pass: `uv run pytest -q` (append `-m "not pg"` without a database)
- [ ] `CHANGELOG.md` has an entry for this change
- [ ] Version bumped in `pyproject.toml` if this is a `feat`/`fix`/`BREAKING`
- [ ] No secret, real path, or credential added to a tracked file
- [ ] SAST clean: `uv run shipguard scan .`

## Notes for the reviewer

<!-- Anything non-obvious: a design decision, a deferred follow-up, a risk. -->
