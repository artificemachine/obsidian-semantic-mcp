# Contributing to Obsidian Semantic MCP

Thank you for your interest in contributing! This document covers everything you need to get started.

## Development Setup

Terminology: `osm` means the Obsidian Semantic MCP CLI (`osm init`, `osm dashboard`, etc.), not OpenStreetMap.

When using chat-based coding agents (Claude Code, Codex, OpenCode), ask for explicit command execution (for example, `Run osm init`) so the agent executes shell commands instead of treating `osm` as plain text.

### Prerequisites

- Python 3.10+
- [uv](https://github.com/astral-sh/uv) — fast Python package manager
- Docker Desktop (for integration tests) — `osm init` can install it for you, or install manually
- Ollama running locally (`ollama serve`)

### Install

```bash
git clone https://github.com/artificemachine/obsidian-semantic-mcp.git
cd obsidian-semantic-mcp
uv sync
```

### Run the test suite

```bash
uv run pytest -q                   # unit tests (no DB or Ollama needed)
OBSIDIAN_VAULT=/path/to/vault uv run python3 tests/test_setup.py   # prereq check
OBSIDIAN_VAULT=/path/to/vault uv run python3 tests/test_e2e.py      # end-to-end
```

The full test suite must pass before submitting a PR (the [Tests badge](README.md) shows pass/fail status; see CI logs for the exact count).

## Code Style

- **Python:** Follow PEP 8. Line length: 100 chars.
- **Logging:** Use `%s` lazy format — no f-strings in log calls (matches project convention).
- **DB access:** Always use the `db_conn()` context manager — never call `psycopg2.connect()` directly.
- **Path safety:** Always use `_resolve_vault_path()` for user-supplied paths — no raw string joins.
- **Error handling:** `_handle_upsert` must catch all exceptions — the watchdog thread must never die.

## Commit Messages

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add search result caching
fix: handle empty Ollama response gracefully
docs: update setup instructions for Linux
test: add concurrent search stress test
chore: pin dependency versions
refactor: extract EmbeddingBackend interface
```

## Pull Request Checklist

Before opening a PR:

- [ ] All unit tests pass (`uv run pytest -q`)
- [ ] New behaviour is covered by tests
- [ ] No hardcoded paths or usernames
- [ ] No secrets or credentials committed
- [ ] CHANGELOG.md updated under `[Unreleased]`
- [ ] Code follows project conventions above

## Reporting Bugs

Use the [bug report template](.github/ISSUE_TEMPLATE/bug_report.md). Include:
- OS and installation mode (native/Docker)
- Vault size (approximate note count)
- Ollama version (`ollama --version`)
- Relevant logs (`docker compose logs mcp-server`)

## Feature Requests

Open a [feature request](.github/ISSUE_TEMPLATE/feature_request.md) with:
- The problem you're trying to solve
- Your proposed solution
- Alternatives you've considered

## License

By contributing, you agree that your contributions will be licensed under the [Apache 2.0 License](LICENSE).
