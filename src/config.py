"""
config.py — shared runtime configuration for server.py and dashboard.py.

Centralises DSN construction so both modules read from the same source of truth
and cannot silently diverge.
"""
import os

# Mandatory frontmatter keys every note written via write_file must carry.
# `None` means "no default value" -- the key still gets added, just empty
# (list types) or left for the caller to fill in (string types). `created`
# and `updated` are computed at write time (see server.py's
# _ensure_frontmatter), not listed here.
REQUIRED_FRONTMATTER_DEFAULTS: dict[str, object] = {
    "aliases": [],
    "tags": [],
    "category": "",
    "session": "",
    "nas-path": "",
    "related": [],
}


def build_dsn() -> str:
    """Build a psycopg2-compatible connection string from environment variables.

    Prefers DATABASE_URL when set (native installs).  Falls back to individual
    POSTGRES_* vars in libpq keyword format so no credential URL ever appears
    in a committed source file (Docker installs set these separately).
    """
    if url := os.environ.get("DATABASE_URL"):
        return url
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db   = os.environ.get("POSTGRES_DB",   "obsidian_brain")
    user = os.environ.get("POSTGRES_USER", "obsidian")
    pw   = os.environ.get("POSTGRES_PASSWORD", "")
    if not pw:
        raise RuntimeError("POSTGRES_PASSWORD environment variable must be set and non-empty")
    return f"host={host} port={port} dbname={db} user={user} password={pw}"
