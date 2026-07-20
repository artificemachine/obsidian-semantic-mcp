"""tests/test_migrations.py — Security & Correctness plan, iteration 7.

Replaces server.py's `CREATE TABLE IF NOT EXISTS` + ad-hoc `ALTER TABLE`
approach with an ordered, versioned, idempotent migration list
(src/migrations.py) — see that module's docstring for the full design.

Side-effect fence: pg-marked tests reach a live Postgres and create/drop the
`notes`, `note_links`, `index_state`, and `schema_version` tables via the
`migrations_on_pg` fixture below. This is deliberate and matches the plan's
fence for this iteration ("migrations here are additive... the executor may
not run migrations against a real database — that is the operator's
decision after review"): DDL-level setup/teardown against a `*_test`
database (never a real one — enforced by the `pg` fixture's
`_require_test_database_name` guard) is expected for schema-mechanism
tests. The fixture drops only the four tables this test file's migrations
create, in teardown, so each test starts from a clean slate.

PER THE ORCHESTRATOR'S EXPLICIT INSTRUCTION: every `pg`-marked test in this
file was written to the same standard as the rest of the suite but NEVER
EXECUTED against a real database by the implementing agent.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

os.environ.setdefault("OBSIDIAN_VAULT", "/tmp/test_vault")
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/test")
os.environ.setdefault("OLLAMA_URL", "http://localhost:11434")

import migrations  # noqa: E402


@pytest.fixture
def migrations_on_pg(pg):
    """Create a minimal `notes` table (migration 1's indexes reference its
    `vault_id`/`content_tsv` columns, mirroring what server.init_db creates
    inline before calling apply_pending). Drops every table this file's
    migrations can create in teardown, so each test starts unstamped."""
    with pg.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS notes (
                id          SERIAL PRIMARY KEY,
                path        TEXT UNIQUE NOT NULL,
                content     TEXT NOT NULL,
                hash        TEXT NOT NULL,
                embedding   vector(8),
                content_tsv tsvector,
                vault_id    TEXT,
                indexed_at  TIMESTAMP DEFAULT NOW()
            );
            """
        )
    pg.commit()

    yield

    with pg.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS schema_version CASCADE;")
        cur.execute("DROP TABLE IF EXISTS index_state CASCADE;")
        cur.execute("DROP TABLE IF EXISTS note_links CASCADE;")
        cur.execute("DROP TABLE IF EXISTS notes CASCADE;")
    pg.commit()


# ── Smoke ─────────────────────────────────────────────────────────────────

def test_smoke_migrations_module_imports():
    from migrations import MIGRATIONS, apply_pending, current_version

    assert MIGRATIONS
    assert callable(apply_pending)
    assert callable(current_version)


# ── Unit ──────────────────────────────────────────────────────────────────

def test_migrations_apply_in_version_order():
    versions = [m.version for m in migrations.MIGRATIONS]
    assert versions == sorted(versions), "MIGRATIONS must be listed in ascending version order"
    assert len(versions) == len(set(versions)), "migration versions must be unique"
    assert versions == list(range(1, len(versions) + 1)), (
        "migration versions must be contiguous starting at 1"
    )


# ── Integration (pg) ──────────────────────────────────────────────────────

@pytest.mark.pg
def test_schema_version_table_created_on_first_run(pg, migrations_on_pg):
    migrations.apply_pending(pg)
    with pg.cursor() as cur:
        cur.execute("SELECT to_regclass('public.schema_version')")
        assert cur.fetchone()[0] is not None


@pytest.mark.pg
def test_baseline_migration_stamps_existing_schema_without_ddl(pg, migrations_on_pg):
    """The safety property for existing installs: pre-create note_links
    (with a row) and the two notes_* indexes exactly as the pre-iteration-7
    inline DDL used to, BEFORE ever calling apply_pending — simulating an
    established database. Migration 1 must not drop/rebuild note_links;
    the pre-existing row must survive."""
    with pg.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE note_links (
                source_path TEXT NOT NULL,
                target_name TEXT NOT NULL,
                target_path TEXT,
                PRIMARY KEY (source_path, target_name)
            );
            """
        )
        cur.execute(
            "INSERT INTO note_links (source_path, target_name) VALUES ('a.md', 'b')"
        )
        cur.execute("CREATE INDEX notes_vault_idx ON notes (vault_id);")
        cur.execute("CREATE INDEX notes_tsv_idx ON notes USING GIN (content_tsv);")
    pg.commit()

    applied = migrations.apply_pending(pg)
    assert 1 in applied

    with pg.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM note_links")
        assert cur.fetchone()[0] == 1, "pre-existing row must survive — stamped, not rebuilt"


@pytest.mark.pg
def test_migrations_are_idempotent(pg, migrations_on_pg):
    first = migrations.apply_pending(pg)
    second = migrations.apply_pending(pg)

    assert first == [1, 2]
    assert second == [], "nothing should be left to apply on a second call"

    with pg.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM schema_version")
        assert cur.fetchone()[0] == 2, "one row per migration, not duplicated"


@pytest.mark.pg
def test_partial_failure_leaves_earlier_migrations_applied(pg, migrations_on_pg, monkeypatch):
    def _boom(conn):
        raise RuntimeError("simulated migration failure")

    fake_migrations = [
        migrations.Migration(1, "baseline", migrations._migration_1_baseline),
        migrations.Migration(2, "boom", _boom),
    ]
    monkeypatch.setattr(migrations, "MIGRATIONS", fake_migrations)

    with pytest.raises(RuntimeError):
        migrations.apply_pending(pg)

    with pg.cursor() as cur:
        cur.execute("SELECT version FROM schema_version ORDER BY version")
        versions = [row[0] for row in cur.fetchall()]
    assert versions == [1], "migration 1 must stay recorded despite migration 2 failing"


# ── State machine (pg) ──────────────────────────────────────────────────

@pytest.mark.pg
def test_migration_application_states(pg, migrations_on_pg):
    """unstamped -> baseline-stamped -> fully-applied, plus
    partially-applied -> resumed."""
    assert migrations.current_version(pg) == 0

    applied = migrations.apply_pending(pg)
    assert applied == [1, 2]
    assert migrations.current_version(pg) == 2

    # Simulate a partially-applied database: drop only the version-2 stamp
    # (not the table it created) and confirm apply_pending resumes from
    # there rather than re-running migration 1.
    with pg.cursor() as cur:
        cur.execute("DELETE FROM schema_version WHERE version = 2")
    pg.commit()
    assert migrations.current_version(pg) == 1

    resumed = migrations.apply_pending(pg)
    assert resumed == [2]


# ── Contract (pg) ─────────────────────────────────────────────────────────

@pytest.mark.pg
def test_schema_version_matches_migration_list(pg, migrations_on_pg):
    migrations.apply_pending(pg)
    assert migrations.current_version(pg) == max(m.version for m in migrations.MIGRATIONS)


# ── Performance (pg) ──────────────────────────────────────────────────────

@pytest.mark.pg
def test_migration_completes_within_budget(pg, migrations_on_pg):
    """Startup migrations that hang look identical to a crashed server, so
    a ceiling is an acceptance criterion, not a nice-to-have."""
    vec_literal = "[" + ",".join(["0.1"] * 8) + "]"
    with pg.cursor() as cur:
        cur.executemany(
            "INSERT INTO notes (path, content, hash, embedding) VALUES (%s, %s, %s, %s::vector)",
            [
                (f"pytest-note-{i}.md", "x" * 100, f"hash{i}", vec_literal)
                for i in range(1000)
            ],
        )
    pg.commit()

    start = time.monotonic()
    migrations.apply_pending(pg)
    elapsed = time.monotonic() - start

    assert elapsed < 5.0, f"apply_pending took {elapsed:.2f}s on 1000 rows — budget is 5s"
