"""tests/test_advisory_lock.py — Security & Correctness plan, iteration 5.

Replaces the process-local `threading.Lock` re-index guard with a Postgres
advisory lock (`server.reindex_lock()`) so mutual exclusion holds across
processes and containers, not just threads within one process — the actual
Stage 6 HIGH finding: a mocked `threading.Lock` passes every unit test
perfectly, which is why the original coordination bug shipped undetected.

Side-effect fence: THIS FILE REACHES A LIVE POSTGRES for every test marked
`@pytest.mark.pg`, via the `pg` fixture in tests/conftest.py. Those tests
take advisory locks only — none write to `notes` or `note_links`, none run
DELETE. Advisory locks are ephemeral (released on disconnect), so no
teardown/rollback is needed beyond closing connections. The `_test`-suffix
guard in conftest.py (`_require_test_database_name`) is the hard boundary;
`test_pg_fixture_refuses_non_test_database` proves that guard without ever
opening a real connection, and must be the first `pg`-adjacent test trusted.

PER THE ORCHESTRATOR'S EXPLICIT INSTRUCTION: every `pg`-marked test in this
file is written to the same standard as the rest of the suite but was NEVER
EXECUTED against a real database by the implementing agent. `pytest -q -m
"not pg"` is the only invocation run locally; running `pytest -m pg` against
a real `*_test` database is the operator's call after review.
"""
from __future__ import annotations

import http.server
import os
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

os.environ.setdefault("OBSIDIAN_VAULT", "/tmp/test_vault")
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/test")
os.environ.setdefault("OLLAMA_URL", "http://localhost:11434")
os.environ.setdefault("DASHBOARD_TOKEN", "test-fixture-token-not-a-secret")

import server  # noqa: E402
import config  # noqa: E402
import dashboard  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from conftest import _dbname_from_dsn, _require_test_database_name, pg_dsn  # noqa: E402


# ── Unit: the fixture safety guard itself — no real connection attempt ──────
# Per the plan: write and pass this BEFORE any other pg test runs.

def test_pg_fixture_refuses_non_test_database():
    with pytest.raises(RuntimeError, match="_test"):
        _require_test_database_name("postgresql://user:pw@host:5432/obsidian_brain")
    with pytest.raises(RuntimeError, match="_test"):
        _require_test_database_name("host=localhost dbname=production port=5432")

    # Must NOT raise for a correctly-suffixed database name (proves the
    # guard is a real check, not one that rejects everything).
    _require_test_database_name("postgresql://user:pw@host:5432/obsidian_brain_test")
    _require_test_database_name("host=localhost dbname=obsidian_brain_test")


def test_dbname_extraction_handles_both_dsn_shapes():
    assert _dbname_from_dsn("postgresql://u:p@host:5432/mydb") == "mydb"
    assert _dbname_from_dsn("postgresql://u:p@host:5432/mydb?sslmode=require") == "mydb"
    assert _dbname_from_dsn("host=localhost port=5432 dbname=mydb user=u") == "mydb"


# ── Smoke ─────────────────────────────────────────────────────────────────

def test_smoke_reindex_lock_imports_and_no_db_yields_false(monkeypatch):
    """Importing reindex_lock must not raise. Entering and exiting it with
    no reachable DB must yield False rather than raising."""
    monkeypatch.setattr(
        server, "_get_pool", MagicMock(side_effect=Exception("no db reachable"))
    )
    with server.reindex_lock() as acquired:
        assert acquired is False


# ── Contract ──────────────────────────────────────────────────────────────

def test_reindex_lock_key_is_shared_constant():
    """Guards against a copy-pasted literal drifting: dashboard must never
    reimplement the advisory-lock SQL with its own key — it must always go
    through server.reindex_lock(), which resolves config.REINDEX_LOCK_KEY."""
    assert server.REINDEX_LOCK_KEY == config.REINDEX_LOCK_KEY

    dashboard_src = (Path(__file__).parent.parent / "src" / "dashboard.py").read_text()
    assert "pg_try_advisory_lock" not in dashboard_src, (
        "dashboard.py must not call pg_try_advisory_lock directly — route "
        "through server.reindex_lock() so both modules share one key"
    )
    assert "reindex_lock" in dashboard_src


# ── Unit (mocked): reindex_vault MCP tool ────────────────────────────────

class _FakeLockCM:
    """Stand-in for reindex_lock()'s return value — __enter__ always
    reports "not acquired", __exit__ is a no-op. Lets tests exercise the
    busy-path branching without a real DB."""

    def __enter__(self):
        return False

    def __exit__(self, *exc_info):
        return False


def test_reindex_tool_returns_busy_when_lock_held(monkeypatch):
    import asyncio

    monkeypatch.setattr(server, "reindex_lock", lambda: _FakeLockCM())
    monkeypatch.setattr(server, "VAULT_PATHS", ["/fake/vault"])
    index_vault_mock = MagicMock()
    monkeypatch.setattr(server, "index_vault", index_vault_mock)

    result = asyncio.run(server.call_tool("reindex_vault", {}))

    text = result[0].text
    assert "progress" in text.lower() or "busy" in text.lower()
    index_vault_mock.assert_not_called()


class _FakeLockCMAcquired:
    """Stand-in for reindex_lock() when the lock is FREE — __enter__ reports
    "acquired", __exit__ is a no-op."""

    def __enter__(self):
        return True

    def __exit__(self, *exc_info):
        return False


def _stub_background_init_deps(monkeypatch):
    """Neutralise background_init's real side effects (sleep, embed probe,
    DB init, watcher start) so the test isolates the lock/index coupling."""
    monkeypatch.setattr(server.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "get_embed_dim", lambda: 768)
    monkeypatch.setattr(server, "init_db", lambda *_a, **_k: None)
    watcher_mock = MagicMock()
    monkeypatch.setattr(server, "start_watcher", watcher_mock)
    return watcher_mock


def test_background_init_skips_boot_index_when_lock_held(monkeypatch):
    """The first-boot full index must go through reindex_lock() — if an
    operator re-index already holds the lock (another process/container),
    background_init must NOT run a second concurrent index_vault, but it
    must still start the watcher so live edits are picked up."""
    watcher_mock = _stub_background_init_deps(monkeypatch)
    monkeypatch.setattr(server, "reindex_lock", lambda: _FakeLockCM())  # held
    index_vault_mock = MagicMock()
    monkeypatch.setattr(server, "index_vault", index_vault_mock)

    server.background_init(["/fake/vault"])

    index_vault_mock.assert_not_called()          # lock held → no double index
    watcher_mock.assert_called_once_with("/fake/vault")  # watcher still starts


def test_background_init_indexes_under_lock_when_free(monkeypatch):
    """When the lock is free, background_init indexes the vault (under the
    lock) and starts the watcher."""
    watcher_mock = _stub_background_init_deps(monkeypatch)
    monkeypatch.setattr(server, "reindex_lock", lambda: _FakeLockCMAcquired())  # free
    index_vault_mock = MagicMock()
    monkeypatch.setattr(server, "index_vault", index_vault_mock)

    server.background_init(["/fake/vault"])

    index_vault_mock.assert_called_once_with("/fake/vault")
    watcher_mock.assert_called_once_with("/fake/vault")


def test_background_init_acquires_lock_independently_per_vault(monkeypatch):
    """Regression guard for a future refactor accidentally hoisting the lock
    acquire outside the per-vault loop: with 3 vaults where the lock is
    free/held/free in sequence, each vault's index_vault call must depend
    ONLY on that vault's own acquire result, not a single acquire() reused
    (or its busy state leaking) across the whole vault list. The watcher
    must still start for every vault regardless of lock outcome."""
    watcher_mock = _stub_background_init_deps(monkeypatch)

    # A fresh CM per call, cycling free/held/free — proves reindex_lock() is
    # invoked once per vault (call_count == 3), not once for the whole loop.
    lock_results = [_FakeLockCMAcquired(), _FakeLockCM(), _FakeLockCMAcquired()]
    reindex_lock_mock = MagicMock(side_effect=lock_results)
    monkeypatch.setattr(server, "reindex_lock", reindex_lock_mock)

    index_vault_mock = MagicMock()
    monkeypatch.setattr(server, "index_vault", index_vault_mock)

    vaults = ["/fake/vault-a", "/fake/vault-b", "/fake/vault-c"]
    server.background_init(vaults)

    assert reindex_lock_mock.call_count == 3
    index_vault_mock.assert_has_calls(
        [call("/fake/vault-a"), call("/fake/vault-c")], any_order=False
    )
    assert index_vault_mock.call_count == 2  # vault-b's held lock skipped it
    assert watcher_mock.call_count == 3
    watcher_mock.assert_has_calls(
        [call("/fake/vault-a"), call("/fake/vault-b"), call("/fake/vault-c")]
    )


# ── Unit (mocked): dashboard /api/reindex/status ─────────────────────────

def test_reindex_status_reflects_advisory_lock(monkeypatch):
    """/api/reindex/status must read the advisory lock (via
    dashboard.reindex_lock), not a process-local threading.Lock."""
    monkeypatch.setattr(dashboard, "reindex_lock", lambda: _FakeLockCM())
    monkeypatch.setattr(dashboard, "DASHBOARD_TOKEN", "test-fixture-token-not-a-secret")

    httpd = http.server.HTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        r = requests.get(f"http://127.0.0.1:{httpd.server_port}/api/reindex/status")
        assert r.status_code == 200
        assert r.json() == {"busy": True}
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


# ── Integration (pg) ──────────────────────────────────────────────────────
#
# reindex_lock_on_pg points server's connection pool at the SAME *_test
# database the `pg` fixture's raw connection uses. server.DATABASE_URL is
# normally resolved once at import time from the ordinary
# DATABASE_URL/POSTGRES_* env — not guaranteed to be the *_test database
# PYTEST_DATABASE_URL points at — so every pg test in this section depends
# on both `pg` (for the safety guard + a second independent connection) and
# `reindex_lock_on_pg` (to repoint server's own pool).

@pytest.fixture
def reindex_lock_on_pg(pg, monkeypatch):
    monkeypatch.setattr(server, "DATABASE_URL", pg_dsn())
    monkeypatch.setattr(server, "_pool", None)
    yield
    if server._pool is not None:
        server._pool.closeall()
        server._pool = None


@pytest.mark.pg
def test_try_acquire_returns_true_when_free(pg, reindex_lock_on_pg):
    with server.reindex_lock() as acquired:
        assert acquired is True


@pytest.mark.pg
def test_second_acquire_from_other_connection_returns_false(pg, reindex_lock_on_pg):
    """The test that would have caught the original bug: two independent
    connections (not two threads in one process) must not both hold the
    lock."""
    import psycopg2

    with server.reindex_lock() as first_acquired:
        assert first_acquired is True

        other_conn = psycopg2.connect(pg_dsn())
        try:
            with other_conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (server.REINDEX_LOCK_KEY,))
                second_acquired = cur.fetchone()[0]
            other_conn.commit()
            assert second_acquired is False
        finally:
            # Only unlock if we actually acquired — calling
            # pg_advisory_unlock without holding the lock raises.
            if second_acquired:
                with other_conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (server.REINDEX_LOCK_KEY,))
                other_conn.commit()
            other_conn.close()


@pytest.mark.pg
def test_release_allows_reacquire(pg, reindex_lock_on_pg):
    with server.reindex_lock() as acquired:
        assert acquired is True

    # Context exited -> released. A fresh acquire must succeed.
    with server.reindex_lock() as acquired_again:
        assert acquired_again is True


@pytest.mark.pg
def test_lock_released_when_context_exits_on_exception(pg, reindex_lock_on_pg):
    """Chaos: an exception raised inside the `with` body must not leave the
    lock held."""
    with pytest.raises(RuntimeError):
        with server.reindex_lock() as acquired:
            assert acquired is True
            raise RuntimeError("boom")

    with server.reindex_lock() as acquired_after:
        assert acquired_after is True


@pytest.mark.pg
def test_lock_state_transitions(pg, reindex_lock_on_pg):
    """State machine: free -> held -> released -> re-held ->
    held-by-other-rejected."""
    import psycopg2

    # free -> held
    lock_cm = server.reindex_lock()
    acquired = lock_cm.__enter__()
    assert acquired is True

    # held -> released
    lock_cm.__exit__(None, None, None)

    # released -> re-held
    lock_cm2 = server.reindex_lock()
    acquired2 = lock_cm2.__enter__()
    assert acquired2 is True

    # held-by-other-rejected
    other_conn = psycopg2.connect(pg_dsn())
    try:
        with other_conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (server.REINDEX_LOCK_KEY,))
            other_acquired = cur.fetchone()[0]
        other_conn.commit()
        assert other_acquired is False
    finally:
        other_conn.close()
        lock_cm2.__exit__(None, None, None)
