"""tests/test_index_state.py — Security & Correctness plan, iteration 6.

Persists indexing progress and failures in Postgres (`index_state` table)
instead of a module-global list, so the dashboard's rebuild-failure panel
reports real data across process/container boundaries — the module-global
list this replaces was only ever readable by the process that set it, which
is why the panel was structurally always empty (dashboard.py and server.py
run in separate containers in the documented Docker topology).

Side-effect fence: pg-marked tests reach a live Postgres and WRITE rows,
unlike iteration 5's advisory-lock-only tests. Every pg test here operates
on a synthetic vault_id prefixed `pytest-` and the `index_state_on_pg`
fixture deletes only `pytest-%` rows in teardown — no test may touch a row
whose vault_id lacks that prefix. The `_test`-database guard from iteration
5 (tests/conftest.py's `pg` fixture) is the outer boundary.

PER THE ORCHESTRATOR'S EXPLICIT INSTRUCTION: every `pg`-marked test in this
file was written to the same standard as the rest of the suite but NEVER
EXECUTED against a real database by the implementing agent.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

os.environ.setdefault("OBSIDIAN_VAULT", "/tmp/test_vault")
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/test")
os.environ.setdefault("OLLAMA_URL", "http://localhost:11434")
os.environ.setdefault("DASHBOARD_TOKEN", "test-fixture-token-not-a-secret")

import server  # noqa: E402
import dashboard  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from conftest import pg_dsn  # noqa: E402


@pytest.fixture
def index_state_on_pg(pg, monkeypatch):
    """Point server's connection pool at the SAME *_test database the `pg`
    fixture's raw connection uses (server.DATABASE_URL is normally resolved
    once at import time from the ordinary DATABASE_URL/POSTGRES_* env, not
    guaranteed to be PYTEST_DATABASE_URL). Ensures index_state exists — pg
    tests should not assume a full init_db() already ran against this
    database. Deletes only pytest-prefixed rows on teardown."""
    monkeypatch.setattr(server, "DATABASE_URL", pg_dsn())
    monkeypatch.setattr(server, "_pool", None)

    with pg.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS index_state (
                vault_id     TEXT PRIMARY KEY,
                status       TEXT NOT NULL DEFAULT 'idle',
                started_at   TIMESTAMP,
                finished_at  TIMESTAMP,
                failed_paths TEXT[] NOT NULL DEFAULT '{}',
                error        TEXT
            );
            """
        )
    pg.commit()

    yield

    with pg.cursor() as cur:
        cur.execute("DELETE FROM index_state WHERE vault_id LIKE 'pytest-%'")
    pg.commit()
    if server._pool is not None:
        server._pool.closeall()
        server._pool = None


# ── Smoke ─────────────────────────────────────────────────────────────────

def test_smoke_index_state_ddl_wired_into_migrations():
    """index_state's CREATE TABLE must be present in migrations.py and
    invoked by init_db() via apply_pending() — proves the DDL is wired in
    without needing a live database to run init_db() itself.

    Updated by iteration 7: the inline CREATE TABLE this test originally
    checked for in init_db's own source was deliberately moved into
    migrations.py's migration 2 (see docs/PLAN-security-correctness.md
    iteration 7 — "Table creation goes in init_db here [iteration 6] and
    is moved into a versioned migration in iteration 7"). init_db no longer
    contains this DDL directly; it now delegates to apply_pending()."""
    migrations_src = (Path(__file__).parent.parent / "src" / "migrations.py").read_text()
    assert "CREATE TABLE IF NOT EXISTS index_state" in migrations_src

    server_src = (Path(__file__).parent.parent / "src" / "server.py").read_text()
    start = server_src.index("def init_db(")
    end = server_src.index("def _row_to_index_state_dict")
    init_db_src = server_src[start:end]
    assert "apply_pending" in init_db_src
    assert "CREATE TABLE IF NOT EXISTS index_state" not in init_db_src


# ── Unit ──────────────────────────────────────────────────────────────────

def test_get_last_rebuild_failures_falls_back_when_db_unavailable(monkeypatch):
    monkeypatch.setattr(
        server, "get_index_state", MagicMock(side_effect=Exception("db down"))
    )
    assert server.get_last_rebuild_failures() == []


def test_index_state_write_failure_does_not_abort_indexing(monkeypatch):
    """Observability must never break the thing it observes: set_index_state
    swallows its own DB errors rather than propagating them."""
    monkeypatch.setattr(server, "db_conn", MagicMock(side_effect=Exception("db down")))

    # Must not raise, despite db_conn() raising immediately when called.
    server.set_index_state("pytest-chaos-vault", "indexing")
    server.set_index_state(
        "pytest-chaos-vault", "failed", failed_paths=["a.md"], error="boom"
    )


# ── Integration (pg) ──────────────────────────────────────────────────────

@pytest.mark.pg
def test_set_indexing_writes_status_row(pg, index_state_on_pg):
    vault_id = "pytest-set-indexing"
    server.set_index_state(vault_id, "indexing")

    with pg.cursor() as cur:
        cur.execute("SELECT status FROM index_state WHERE vault_id = %s", (vault_id,))
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "indexing"


@pytest.mark.pg
def test_failed_paths_round_trip_through_db(pg, index_state_on_pg):
    """Write from one connection (server's pool), read from another (the
    `pg` fixture's own connection)."""
    vault_id = "pytest-failed-paths"
    server.set_index_state(
        vault_id, "failed", failed_paths=["a.md", "b.md"], error="2 failed"
    )

    with pg.cursor() as cur:
        cur.execute(
            "SELECT failed_paths, error FROM index_state WHERE vault_id = %s",
            (vault_id,),
        )
        failed_paths, error = cur.fetchone()
    assert sorted(failed_paths) == ["a.md", "b.md"]
    assert error == "2 failed"


@pytest.mark.pg
def test_status_transitions_to_failed_on_exception(pg, index_state_on_pg):
    """index_vault on a vault path that does not exist on disk raises
    (Path.rglob on a nonexistent directory) — the except branch must record
    status='failed' with the exception message before re-raising."""
    vault_id = "pytest-exc-nonexistent-vault"

    with pytest.raises(Exception):
        server.index_vault(vault_id)

    state = server.get_index_state(vault_id)
    assert state is not None
    assert state["status"] == "failed"
    assert state["error"]


@pytest.mark.pg
def test_dashboard_reads_failures_written_by_other_process(pg, index_state_on_pg):
    """Regression guard for the Stage 6 HIGH dead-observability finding:
    simulates the two-container topology — one connection (server's pool,
    standing in for the mcp-server container) writes, a wholly separate
    connection (the `pg` fixture's, standing in for the dashboard
    container) reads."""
    vault_id = "pytest-cross-process"
    server.set_index_state(vault_id, "failed", failed_paths=["x.md"], error="1 failed")

    with pg.cursor() as cur:
        cur.execute(
            "SELECT failed_paths FROM index_state WHERE vault_id = %s", (vault_id,)
        )
        failed_paths = cur.fetchone()[0]
    assert failed_paths == ["x.md"]


@pytest.mark.pg
def test_index_state_transitions(pg, index_state_on_pg):
    """State machine: idle -> indexing -> idle, idle -> indexing -> failed,
    failed -> indexing -> idle."""
    vault_id = "pytest-state-transitions"

    server.set_index_state(vault_id, "indexing")
    assert server.get_index_state(vault_id)["status"] == "indexing"
    server.set_index_state(vault_id, "idle", failed_paths=[])
    assert server.get_index_state(vault_id)["status"] == "idle"

    server.set_index_state(vault_id, "indexing")
    server.set_index_state(vault_id, "failed", failed_paths=["a.md"], error="boom")
    state = server.get_index_state(vault_id)
    assert state["status"] == "failed"
    assert state["failed_paths"] == ["a.md"]

    server.set_index_state(vault_id, "indexing")
    server.set_index_state(vault_id, "idle", failed_paths=[])
    state = server.get_index_state(vault_id)
    assert state["status"] == "idle"
    assert state["failed_paths"] == []


@pytest.mark.pg
def test_index_state_schema_matches_expected_columns(pg, index_state_on_pg):
    with pg.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = 'index_state'
            ORDER BY ordinal_position
            """
        )
        columns = {row[0]: row[1] for row in cur.fetchall()}

    assert columns.get("vault_id") == "text"
    assert columns.get("status") == "text"
    assert columns.get("started_at") == "timestamp without time zone"
    assert columns.get("finished_at") == "timestamp without time zone"
    assert columns.get("failed_paths") == "ARRAY"
    assert columns.get("error") == "text"


# ── E2E (pg) ──────────────────────────────────────────────────────────────

@pytest.mark.pg
def test_stats_endpoint_surfaces_persisted_failures(pg, index_state_on_pg, monkeypatch):
    """Drives dashboard.gather_stats() (the function backing /api/stats) and
    asserts a failure written by a separate connection appears. Stubs the
    non-index_state stat gatherers (DB size, vault file count, Ollama
    health) since this test is scoped to the index_state <-> /api/stats
    wiring, not a full live-stack integration test."""
    vault_id = "pytest-e2e-stats"
    server.set_index_state(vault_id, "failed", failed_paths=["e2e.md"], error="1 failed")

    monkeypatch.setattr(dashboard, "_get_db_stats", lambda stats: stats.update({"db_ok": True}))
    monkeypatch.setattr(dashboard, "_get_vault_stats", lambda stats: None)
    monkeypatch.setattr(dashboard, "_get_ollama_stats", lambda stats: None)

    stats = dashboard.gather_stats()

    assert "e2e.md" in stats["last_rebuild_failed_sample"]
    assert stats["last_rebuild_failed_count"] >= 1
