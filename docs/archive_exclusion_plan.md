# Archive Exclusion Note

## Status

Implemented.

`obsidian-semantic-mcp` excludes `archive/` content from indexing and watching by default. The default can be overridden by setting `OBSIDIAN_IGNORE_PATHS=""` if you want archived notes included.

## Behavior

- Indexing skips files under `archive/` unless the override is disabled.
- The file watcher uses the same exclusion logic, so archive edits do not retrigger reindexing.
- The default remains safe for vaults that keep duplicate or historical notes under `archive/`.

## Verification

- `archive/` content is excluded by default in `src/server.py`.
- `OBSIDIAN_IGNORE_PATHS=""` restores archive indexing.
- `docs/RUNBOOK.md` documents the operational override.

## Historical Note

This file was originally a plan. It now serves as a short record of the implemented behavior so the docs stay consistent with the codebase.
