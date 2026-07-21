# Security Policy

## Supported versions

Security fixes land on the latest `0.x` release. There is no long-term support branch yet; upgrade to the newest release to receive fixes.

| Version | Supported |
|---------|-----------|
| latest `0.x` | ✅ |
| older | ❌ |

## Reporting a vulnerability

Report privately — do not open a public issue for a security problem.

- Use GitHub's [private vulnerability reporting](https://github.com/artificemachine/obsidian-semantic-mcp/security/advisories/new) (Security → Advisories → Report a vulnerability), or
- email the maintainer at the address on the GitHub profile.

Please include: affected version, a description, and the smallest steps that reproduce the issue. Expect an acknowledgement within a few days.

## Security model

This project runs entirely on the operator's machine or LAN. It has no cloud component and stores no data off-host. The relevant trust boundaries:

- **The dashboard** (`src/dashboard.py`) binds `127.0.0.1` by default (`DASHBOARD_BIND`). Its mutating endpoints (`/api/reindex`, `/api/reindex/full`, `/api/prune`, `/api/ollama/start`) require a bearer token (`DASHBOARD_TOKEN`, or a `0600` token file under `~/.config/obsidian-semantic-mcp/`). Read-only endpoints are unauthenticated by design and are reachable only from loopback unless the operator explicitly rebinds.
- **The database** holds vault content and embeddings. Credentials come from environment variables or `~/.config/obsidian-semantic-mcp/`; never commit a real `.env`. The bundled `.env.example` is placeholders only.
- **SQL** goes through parameterized queries or `psycopg2.sql` composition; dynamic identifiers are never string-interpolated. A SAST scan (`shipguard`) runs in CI and blocks the build on findings.

## Dependencies

Known vulnerabilities are tracked with `pip-audit` against the pinned lockfile. The project ships no runtime dependency with an open HIGH or CRITICAL advisory at release time; residual advisories, when present, are transitive and awaiting upstream releases.
