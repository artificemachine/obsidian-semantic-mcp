"""tests/test_dimension_migration.py — Security & Correctness plan, iteration 8.

The only code in the whole plan that can destroy user data. An embedding-
model change stops being a `docker compose down -v` data-loss event: boot
DETECTS a dimension mismatch and records it in index_state, but never
migrates automatically — a container restart can never begin an hours-long
re-embed. `osm migrate --embedding-dim` performs the migration
non-destructively: add a new column, backfill it, cut over only when fully
populated. If re-embedding cannot complete (Ollama unreachable), the old
column and old model stay authoritative.

Side-effect fence: THE TIGHTEST IN THE PLAN. pg-marked tests here create and
DROP the `notes`/`index_state` tables in a `*_test` database via the
`dimension_on_pg` fixture (same DDL-level rationale as
tests/test_migrations.py's `migrations_on_pg`). No test calls `osm migrate`
as a subprocess or CLI invocation — only the underlying `migrations.py`
functions are called directly, in-process, against the `pg` fixture's
connection. `osm_init.cmd_migrate` itself is exercised only for
registration/dispatch (test_osm_migrate_subcommand_is_registered), never
invoked.

PER THE ORCHESTRATOR'S EXPLICIT INSTRUCTION: every `pg`-marked test in this
file was written to the same standard as the rest of the suite but NEVER
EXECUTED against a real database by the implementing agent. `osm migrate`
was never run — against a real database, a *_test database, or at all.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("OBSIDIAN_VAULT", "/tmp/test_vault")
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/test")
os.environ.setdefault("OLLAMA_URL", "http://localhost:11434")

import server  # noqa: E402
import migrations  # noqa: E402
import osm_init  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from conftest import pg_dsn  # noqa: E402

OLD_DIM = 8
NEW_DIM = 16


@pytest.fixture
def dimension_on_pg(pg, monkeypatch):
    """A `notes` table at OLD_DIM, seeded with 5 rows, plus `index_state`.
    Points server's pool at the same *_test database the `pg` fixture's
    connection uses. Drops everything this file's tests create in
    teardown."""
    monkeypatch.setattr(server, "DATABASE_URL", pg_dsn())
    monkeypatch.setattr(server, "_pool", None)

    vec = "[" + ",".join(["0.1"] * OLD_DIM) + "]"
    with pg.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS notes (
                id          SERIAL PRIMARY KEY,
                path        TEXT UNIQUE NOT NULL,
                content     TEXT NOT NULL,
                hash        TEXT NOT NULL,
                embedding   vector({OLD_DIM}),
                content_tsv tsvector,
                vault_id    TEXT,
                indexed_at  TIMESTAMP DEFAULT NOW()
            );
            """
        )
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
        for i in range(5):
            cur.execute(
                "INSERT INTO notes (path, content, hash, embedding) VALUES (%s, %s, %s, %s::vector)",
                (f"pytest-dim-note-{i}.md", f"content {i}", f"hash{i}", vec),
            )
    pg.commit()

    yield

    with pg.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS index_state CASCADE;")
        cur.execute("DROP TABLE IF EXISTS notes CASCADE;")
    pg.commit()
    if server._pool is not None:
        server._pool.closeall()
        server._pool = None


# ── Smoke ─────────────────────────────────────────────────────────────────

def test_smoke_migrate_help_and_imports():
    assert "migrate" in osm_init.COMMANDS
    from migrations import migrate_embedding_dimension  # noqa: F401 — must import cleanly


# ── Unit ──────────────────────────────────────────────────────────────────

def test_osm_migrate_subcommand_is_registered():
    assert "migrate" in osm_init.COMMANDS
    assert osm_init.COMMANDS["migrate"][0] is osm_init.cmd_migrate
    assert osm_init._FLAG_MAP.get("embedding-dim") == "embedding_dim"


# ── Contract ──────────────────────────────────────────────────────────────

def test_index_state_exposes_dimension_mismatch_status():
    """The status string the dashboard reads must match the one the server
    writes — guards against a copy-pasted literal drifting."""
    server_src = (REPO_ROOT / "src" / "server.py").read_text()
    dashboard_src = (REPO_ROOT / "src" / "dashboard.py").read_text()
    assert '"dimension_mismatch"' in server_src
    assert '"dimension_mismatch"' in dashboard_src


# ── Integration (pg) ──────────────────────────────────────────────────────

@pytest.mark.pg
def test_boot_detects_mismatch_without_migrating(pg, dimension_on_pg):
    server.init_db(NEW_DIM)

    with pg.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'notes'"
        )
        columns = {row[0] for row in cur.fetchall()}
    assert f"embedding_{NEW_DIM}" not in columns, "boot must not add a new column"

    with pg.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM notes WHERE embedding IS NULL")
        assert cur.fetchone()[0] == 0, "boot must not touch/clear existing embeddings"


@pytest.mark.pg
def test_boot_records_mismatch_in_index_state(pg, dimension_on_pg, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATHS", ["pytest-dim-vault"])
    server.init_db(NEW_DIM)

    state = server.get_index_state("pytest-dim-vault")
    assert state is not None
    assert state["status"] == "dimension_mismatch"
    assert str(OLD_DIM) in state["error"]
    assert str(NEW_DIM) in state["error"]


@pytest.mark.pg
def test_search_still_works_under_unmigrated_mismatch(pg, dimension_on_pg):
    server.init_db(NEW_DIM)

    query_vec = "[" + ",".join(["0.1"] * OLD_DIM) + "]"
    with pg.cursor() as cur:
        cur.execute(
            "SELECT path FROM notes ORDER BY embedding <=> %s::vector LIMIT 5",
            (query_vec,),
        )
        assert len(cur.fetchall()) == 5


@pytest.mark.pg
def test_migrate_adds_new_column_without_dropping_old(pg, dimension_on_pg):
    migrations.add_embedding_column(pg, NEW_DIM)

    with pg.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'notes'"
        )
        columns = {row[0] for row in cur.fetchall()}
    assert "embedding" in columns
    assert f"embedding_{NEW_DIM}" in columns


@pytest.mark.pg
def test_old_column_survives_failed_reembed(pg, dimension_on_pg):
    def failing_embed(text):
        raise RuntimeError("ollama unreachable")

    with pytest.raises(RuntimeError):
        migrations.migrate_embedding_dimension(pg, OLD_DIM, NEW_DIM, failing_embed)

    with pg.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM notes WHERE embedding IS NOT NULL")
        assert cur.fetchone()[0] == 5, "original column and data must survive intact"
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'notes' AND column_name = 'embedding'"
        )
        assert cur.fetchone() is not None


@pytest.mark.pg
def test_search_works_throughout_migration(pg, dimension_on_pg):
    def stub_embed(text):
        return [0.2] * NEW_DIM

    migrations.add_embedding_column(pg, NEW_DIM)
    migrations.backfill_embedding_column(pg, NEW_DIM, stub_embed)

    # Mid-migration (backfilled but not cut over): old column still serves search.
    old_query = "[" + ",".join(["0.1"] * OLD_DIM) + "]"
    with pg.cursor() as cur:
        cur.execute(
            "SELECT path FROM notes ORDER BY embedding <=> %s::vector LIMIT 5",
            (old_query,),
        )
        assert len(cur.fetchall()) == 5

    migrations.cutover_embedding_column(pg, NEW_DIM)

    # After cutover: `embedding` now holds the NEW dimension's values.
    new_query = "[" + ",".join(["0.2"] * NEW_DIM) + "]"
    with pg.cursor() as cur:
        cur.execute(
            "SELECT path FROM notes ORDER BY embedding <=> %s::vector LIMIT 5",
            (new_query,),
        )
        assert len(cur.fetchall()) == 5


@pytest.mark.pg
def test_cutover_drops_old_column_only_when_new_is_complete(pg, dimension_on_pg):
    migrations.add_embedding_column(pg, NEW_DIM)

    with pg.cursor() as cur:
        cur.execute("SELECT id FROM notes ORDER BY id LIMIT 1")
        first_id = cur.fetchone()[0]
    vec = "[" + ",".join(["0.2"] * NEW_DIM) + "]"
    with pg.cursor() as cur:
        cur.execute(
            f"UPDATE notes SET embedding_{NEW_DIM} = %s::vector WHERE id != %s",
            (vec, first_id),
        )
    pg.commit()

    with pytest.raises(RuntimeError, match="cutover refused"):
        migrations.cutover_embedding_column(pg, NEW_DIM)

    with pg.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'notes' AND column_name = 'embedding'"
        )
        assert cur.fetchone() is not None, "old column must still exist after refused cutover"


# ── State machine (pg) ──────────────────────────────────────────────────

@pytest.mark.pg
def test_dimension_migration_state_transitions(pg, dimension_on_pg, monkeypatch):
    """stable -> mismatch-detected -> new-column-added -> re-embedding ->
    cutover -> stable. The re-embedding -> failed -> stable-on-old-column
    branch is covered separately by test_old_column_survives_failed_reembed."""
    monkeypatch.setattr(server, "VAULT_PATHS", ["pytest-dim-vault"])

    server.init_db(NEW_DIM)
    assert server.get_index_state("pytest-dim-vault")["status"] == "dimension_mismatch"

    def stub_embed(text):
        return [0.3] * NEW_DIM

    result = migrations.migrate_embedding_dimension(pg, OLD_DIM, NEW_DIM, stub_embed)
    assert result["status"] == "complete"

    with pg.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'notes' AND column_name = %s",
            (f"embedding_{NEW_DIM}",),
        )
        assert cur.fetchone() is None, "temp column is renamed away after cutover"


# ── Chaos (pg) ────────────────────────────────────────────────────────────

@pytest.mark.pg
def test_migration_survives_ollama_outage(pg, dimension_on_pg):
    """Kills embedding mid-migration (some notes succeed before the
    outage) — the old column must stay fully populated and authoritative."""
    calls = {"n": 0}

    def flaky_embed(text):
        calls["n"] += 1
        if calls["n"] > 2:
            raise RuntimeError("ollama unreachable")
        return [0.4] * NEW_DIM

    with pytest.raises(RuntimeError):
        migrations.migrate_embedding_dimension(pg, OLD_DIM, NEW_DIM, flaky_embed)

    with pg.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM notes WHERE embedding IS NOT NULL")
        assert cur.fetchone()[0] == 5


# test_cutover_refuses_when_new_column_has_nulls is the same property as
# test_cutover_drops_old_column_only_when_new_is_complete above — one test,
# two names in the plan's pyramid (integration + chaos).


# ── E2E (pg) ──────────────────────────────────────────────────────────────

@pytest.mark.pg
def test_full_dimension_change_end_to_end(pg, dimension_on_pg, monkeypatch):
    """Indexes at OLD_DIM (fixture), switches the stubbed model to NEW_DIM,
    runs the migration, and asserts search returns correct results before,
    during (unmigrated-but-detected), and after."""
    monkeypatch.setattr(server, "VAULT_PATHS", ["pytest-dim-vault"])

    old_query = "[" + ",".join(["0.1"] * OLD_DIM) + "]"
    with pg.cursor() as cur:
        cur.execute(
            "SELECT path FROM notes ORDER BY embedding <=> %s::vector LIMIT 5",
            (old_query,),
        )
        assert len(cur.fetchall()) == 5

    server.init_db(NEW_DIM)
    assert server.get_index_state("pytest-dim-vault")["status"] == "dimension_mismatch"
    with pg.cursor() as cur:
        cur.execute(
            "SELECT path FROM notes ORDER BY embedding <=> %s::vector LIMIT 5",
            (old_query,),
        )
        assert len(cur.fetchall()) == 5, "search still works, unmigrated"

    def stub_embed(text):
        return [0.5] * NEW_DIM

    result = migrations.migrate_embedding_dimension(pg, OLD_DIM, NEW_DIM, stub_embed)
    assert result["status"] == "complete"

    new_query = "[" + ",".join(["0.5"] * NEW_DIM) + "]"
    with pg.cursor() as cur:
        cur.execute(
            "SELECT path FROM notes ORDER BY embedding <=> %s::vector LIMIT 5",
            (new_query,),
        )
        assert len(cur.fetchall()) == 5


# ── Performance (pg) ──────────────────────────────────────────────────────

@pytest.mark.pg
def test_migration_reports_progress_within_interval(pg, dimension_on_pg):
    """Progress must be written at least every 100 notes, so an
    hours-long run is observable rather than opaque."""
    vec = "[" + ",".join(["0.1"] * OLD_DIM) + "]"
    with pg.cursor() as cur:
        cur.executemany(
            "INSERT INTO notes (path, content, hash, embedding) VALUES (%s, %s, %s, %s::vector)",
            [(f"pytest-dim-bulk-{i}.md", "x", f"h{i}", vec) for i in range(120)],
        )
    pg.commit()

    progress_calls: list[tuple[int, int]] = []

    def record_progress(done, total):
        progress_calls.append((done, total))

    def stub_embed(text):
        return [0.6] * NEW_DIM

    migrations.add_embedding_column(pg, NEW_DIM)
    migrations.backfill_embedding_column(
        pg, NEW_DIM, stub_embed, batch_size=50, progress_callback=record_progress
    )

    assert progress_calls, "progress_callback must be invoked at least once"
    prev = 0
    for done, total in progress_calls:
        assert done - prev <= 100, f"gap of {done - prev} notes between progress reports"
        prev = done
    assert prev == 125  # 5 seeded in dimension_on_pg + 120 bulk-inserted here


@pytest.mark.pg
def test_add_column_times_out_instead_of_stalling_behind_a_reader(pg, dimension_on_pg):
    """Regression: ALTER TABLE needs ACCESS EXCLUSIVE, so an open reader
    transaction blocks it — and every later reader then queues behind the
    blocked ALTER, taking the table down. Found 2026-07-20 by this suite
    deadlocking for 10 minutes against a real Postgres.

    A second connection holds an idle-in-transaction SELECT on `notes`;
    add_embedding_column must give up within DDL_LOCK_TIMEOUT and raise a
    clear error, not wait forever.
    """
    import psycopg2
    from conftest import pg_dsn

    blocker = psycopg2.connect(pg_dsn())
    try:
        with blocker.cursor() as bcur:
            bcur.execute("SELECT COUNT(*) FROM notes")  # opens a txn, holds ACCESS SHARE
            bcur.fetchone()

        started = time.monotonic()
        with pytest.raises(RuntimeError, match="Could not acquire the table lock"):
            migrations.add_embedding_column(pg, NEW_DIM)
        elapsed = time.monotonic() - started

        assert elapsed < 30, f"gave up after {elapsed:.1f}s — should bound at DDL_LOCK_TIMEOUT"

        # The old column and its data must be untouched by the failed attempt.
        with pg.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM notes WHERE embedding IS NOT NULL")
            assert cur.fetchone()[0] > 0
    finally:
        blocker.rollback()
        blocker.close()
