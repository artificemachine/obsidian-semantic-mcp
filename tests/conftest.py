"""
Shared fixtures and helpers for osm_init test suites, plus the `pg` marker's
live-Postgres fixture (Security & Correctness plan, iteration 5).
"""
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import osm_init

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import config

try:
    import dashboard
except Exception:  # pragma: no cover - dashboard needs DB env vars to import
    dashboard = None

_ORIGINAL_PROJECT_ROOT = osm_init.PROJECT_ROOT


@pytest.fixture(autouse=True)
def _isolate_pi_agent(monkeypatch):
    """register_pi_agent writes ~/.pi/agent/mcp.json on hosts where ``pi`` is
    installed; stub it so the suite never mutates the developer's real pi config
    and stays deterministic regardless of host state."""
    monkeypatch.setattr(osm_init, "register_pi_agent", lambda *a, **kw: None)


@pytest.fixture(autouse=True)
def _isolate_osm_config_dir(monkeypatch, tmp_path):
    """Redirect every writer of ~/.config/obsidian-semantic-mcp/ at a tmp_path.

    Two distinct writers reach that directory during a bare `pytest` run:

      1. osm_init's `project_root` pointer. Long-standing: the suite has been
         rewriting the developer's real install pointer on every run. Content
         happened to be identical each time, so it never broke anything and
         never got noticed — but a test that crashed mid-`_with_root()` could
         have left it pointing at a deleted tmp_path and broken the real
         `osm` launcher on the host.
      2. config.resolve_dashboard_token(), which generates and persists a
         bearer token on first call (Security & Correctness plan, iteration 2).

    Autouse and unconditional: a fence that only applies to the tests which
    remembered to ask for it is not a fence.
    """
    fake_config = tmp_path / "osm-config"
    fake_config.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(config, "OSM_CONFIG_DIR", fake_config, raising=False)
    monkeypatch.setattr(osm_init, "OSM_CONFIG_DIR", fake_config, raising=False)
    # PROJECT_ROOT_FILE is derived from OSM_CONFIG_DIR at import time
    # (osm_init.py:311), so repointing the directory alone leaves the
    # already-computed file path aimed at the real config dir. Patch the
    # derived path explicitly — this is the one that actually gets written.
    monkeypatch.setattr(
        osm_init, "PROJECT_ROOT_FILE", fake_config / "project_root", raising=False
    )

    # Force re-resolution against the fake dir rather than reusing whatever a
    # previous test cached.
    if dashboard is not None:
        monkeypatch.setattr(dashboard, "DASHBOARD_TOKEN", None, raising=False)


def _reset():
    """Reset global mutable state in osm_init between tests."""
    osm_init.DRY_RUN = False
    osm_init._DRY_ACTIONS.clear()
    osm_init._PARAMS.clear()
    # Restore PROJECT_ROOT so a test that crashes mid-_with_root() doesn't
    # leave subsequent tests pointing at a cleaned-up tmp_path.
    osm_init.PROJECT_ROOT = _ORIGINAL_PROJECT_ROOT


# ─────────────────────────── `pg` marker fixture ────────────────────────────
#
# Tests marked @pytest.mark.pg need a live PostgreSQL connection. This
# fixture is the ONLY sanctioned way to get one — it is the hard boundary
# between the pg-marked suite and any real database, enforced by
# _require_test_database_name() below.

def pg_dsn() -> str:
    """Resolve the DSN a pg test should connect to — PYTEST_DATABASE_URL ONLY.

    DATABASE_URL is deliberately not consulted, not even as a fallback.
    It is the *production* pointer: on a developer machine it addresses the
    real vault index, and during a test run it is whatever synthetic value
    some other test module last exported. Falling back to it made the pg
    fixture hard-fail on a bare `pytest` (the pre-commit hook's invocation)
    because it picked up another module's placeholder DSN, and would have
    connected to real data on any machine whose database happened to be
    named with a `_test` suffix.

    Opting a database in is therefore explicit and single-purpose: set
    PYTEST_DATABASE_URL. Unset means skip, never guess.
    """
    return os.environ.get("PYTEST_DATABASE_URL", "")


def _dbname_from_dsn(dsn: str) -> str:
    """Extract the target database name from either DSN shape
    config.build_dsn() can produce: a URL
    (postgresql://user:pass@host:port/dbname) or libpq keyword format
    (host=... dbname=... user=... password=...)."""
    if "://" in dsn:
        match = re.search(r'/([^/?]+)(?:\?.*)?$', dsn)
        return match.group(1) if match else ""
    match = re.search(r'\bdbname=(\S+)', dsn)
    return match.group(1) if match else ""


def _require_test_database_name(dsn: str) -> None:
    """Hard-fail unless the target database name ends in `_test`.

    This is the safety guard the plan requires be written and passing
    BEFORE any other pg test runs (test_pg_fixture_refuses_non_test_database
    in tests/test_advisory_lock.py calls this function directly, with no
    real connection attempt, so the guard itself is provable without a DB).
    """
    dbname = _dbname_from_dsn(dsn)
    if not dbname.endswith("_test"):
        raise RuntimeError(
            f"pg fixture refuses to run against database {dbname!r} — the "
            "target database name must end in '_test'. Point "
            "PYTEST_DATABASE_URL at a *_test database before running "
            "`pytest -m pg`."
        )


@pytest.fixture
def pg():
    """Yield a live psycopg2 connection to a `*_test` database.

    Skips (does not fail) when neither PYTEST_DATABASE_URL nor DATABASE_URL
    is set, so `pytest -m "not pg"` — and a bare `pytest` on a machine with
    no database configured at all — both work without this fixture ever
    being instantiated. Raises (does not skip) when a DSN IS configured but
    fails the *_test naming guard: that is a misconfiguration, not "no
    database available," and must be loud.
    """
    import psycopg2  # imported lazily so collection never requires it

    dsn = pg_dsn()
    if not dsn:
        pytest.skip("no PYTEST_DATABASE_URL/DATABASE_URL configured for pg tests")
    _require_test_database_name(dsn)
    conn = psycopg2.connect(dsn)
    try:
        yield conn
    finally:
        conn.close()
