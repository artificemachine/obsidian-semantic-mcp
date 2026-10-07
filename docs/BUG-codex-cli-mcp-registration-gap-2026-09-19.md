# BUG — `osm init` omits Codex CLI from MCP registration

**Date:** 2026-09-19  
**Status:** implemented in 0.19.0; shipping and installed validation tracked in the registration plan.  
**Severity:** medium — a supported local MCP server is not discoverable in one
of the four standard coding harnesses after installation.

## Observed behavior

`osm init` registers the server for Claude Desktop, Claude Code CLI, OpenCode
and pi. Codex CLI is neither configured nor mentioned in the installation
outcome.

## Evidence

- `osm_init.py::register_with_clients` calls `update_claude_config`,
  `register_claude_cli`, `update_opencode_config` and `register_pi_agent`.
  It makes no Codex registration call.
- Its docstring says adding Codex CLI is a future one-line change.
- README installation instructions list the same four configured clients and
  omit Codex.
- `tests/test_osm_commands.py` tests the existing fan-out calls only. There is
  no test for Codex detection, idempotent merge, malformed configuration, or
  server validation.

This was established by static inspection only. No user configuration, MCP
registration, or installed server was modified.

## Expected behavior

When Codex CLI is installed, `osm init` should detect it and idempotently
register the same portable executable entry without storing credentials in the
client configuration. It should leave all unrelated entries unchanged.

## Proposed remediation

1. Establish the supported Codex CLI configuration schema from its current
   official interface before writing an entry.
2. Add a dedicated `register_codex_cli` path and call it from the fan-out.
3. Add RED/GREEN tests for detection, first registration, idempotent rerun,
   preservation of unrelated configuration, malformed configuration, and the
   absence of secret values in written output.
4. Update the README client list and validation instructions.

Acceptance: a fixture containing each supported harness configuration registers
exactly one `obsidian-semantic` server, a second run creates no diff, and an
unavailable Codex CLI remains a no-op.

## Implementation — 2026-10-07

The initialization fan-out now registers the installed launcher in shared Codex configuration, preserves existing entries and unrelated TOML, and refuses malformed or unsafe files. Native settings are stored in an owner-only OSM runtime file. Tests exercise first registration, repeat registration, absent CLI/desktop configuration, malformed configuration, atomic failure, and targeted removal using only synthetic files.
