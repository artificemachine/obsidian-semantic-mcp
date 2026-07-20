"""
migrations.py — versioned, idempotent schema migrations for obsidian-semantic-mcp.

Replaces the previous `CREATE TABLE IF NOT EXISTS` + ad-hoc `ALTER TABLE`
approach in server.py's init_db with an ordered, versioned migration list.
The Stage 6 finding this fixes is "no versioning," not "no framework" — no
Alembic; the dependency cost isn't justified at this table count.

Design:
  - `schema_version` table: one row per applied migration (version, name,
    applied_at), so partial application is visible.
  - Migration 1 is a BASELINE: creates `note_links` and the indexes that
    predate this mechanism, using `IF NOT EXISTS` throughout. On an
    established database these objects already exist, so this migration is
    a safe no-op that only stamps the version row — an existing install is
    stamped, not rebuilt. On a fresh database it creates them for real.
    The `notes` table itself is deliberately NOT part of this migration —
    its `embedding` column dimension is a runtime value (probed from the
    configured Ollama model), which a static migration list can't
    parameterize; it stays inline in server.py's init_db, created before
    apply_pending() runs (migration 1's indexes reference notes' columns).
  - Migration 2 adds `index_state` (see iteration 6 of
    docs/PLAN-security-correctness.md) — moved here from the inline DDL
    iteration 6 added directly to init_db, now that the mechanism exists.
  - Each migration commits in its own transaction, immediately followed by
    its own schema_version stamp in that SAME transaction — so a failing
    migration leaves every earlier migration's row (and effects) intact
    rather than rolling back the whole batch.
"""
from __future__ import annotations

import logging
import re

from psycopg2 import sql
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable  # Callable[[psycopg2 connection], None] — runs its own cursor(s)


def _migration_1_baseline(conn) -> None:
    """Create note_links and the pre-existing indexes, IF NOT EXISTS
    throughout. Safe to run against both a fresh database and an
    established one that already has these objects."""
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS note_links (
                source_path TEXT NOT NULL,
                target_name TEXT NOT NULL,
                target_path TEXT,
                PRIMARY KEY (source_path, target_name)
            );
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS note_links_target_idx ON note_links (target_path);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS notes_vault_idx ON notes (vault_id);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS notes_tsv_idx ON notes USING GIN (content_tsv);"
        )


def _migration_2_index_state(conn) -> None:
    """Create index_state — one row per vault, tracking indexing status and
    the paths that failed to embed on the most recent pass (see
    server.set_index_state / get_index_state)."""
    with conn.cursor() as cur:
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


MIGRATIONS: list[Migration] = [
    Migration(1, "baseline", _migration_1_baseline),
    Migration(2, "index_state", _migration_2_index_state),
]


def _assert_versions_contiguous() -> None:
    versions = sorted(m.version for m in MIGRATIONS)
    expected = list(range(1, len(versions) + 1))
    if versions != expected:
        raise AssertionError(
            f"MIGRATIONS versions must be contiguous starting at 1, unique, "
            f"and sorted: got {versions}, expected {expected}"
        )


def _ensure_schema_version_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_version (
                version    INTEGER PRIMARY KEY,
                name       TEXT NOT NULL,
                applied_at TIMESTAMP DEFAULT NOW()
            );
            """
        )


def current_version(conn) -> int:
    """Return the highest applied migration version, or 0 if none have been
    applied yet (including when schema_version itself does not exist)."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.schema_version')")
        if cur.fetchone()[0] is None:
            return 0
        cur.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version")
        return cur.fetchone()[0]


def apply_pending(conn) -> list[int]:
    """Apply every migration with version > current_version(), in ascending
    order. Returns the list of versions actually applied this call (empty
    if the database was already fully up to date).

    Each migration's DDL and its schema_version stamp commit together, in
    one transaction, immediately after that migration succeeds — not
    batched with any other migration. A migration that raises rolls back
    only its own transaction; every earlier migration in this call (and
    any from a previous call) stays committed and recorded.
    """
    _assert_versions_contiguous()

    with conn:
        _ensure_schema_version_table(conn)

    applied: list[int] = []
    already_at = current_version(conn)
    for migration in sorted(MIGRATIONS, key=lambda m: m.version):
        if migration.version <= already_at:
            continue
        with conn:
            migration.apply(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO schema_version (version, name) VALUES (%s, %s) "
                    "ON CONFLICT (version) DO NOTHING",
                    (migration.version, migration.name),
                )
        log.info("Applied migration %d (%s)", migration.version, migration.name)
        applied.append(migration.version)
    return applied


# ─────────────────── Embedding-dimension migration (iteration 8) ────────────
#
# NOT a numbered/versioned migration in MIGRATIONS above — this is data
# movement (a full vault re-embed), not schema versioning, and it is
# strictly operator-triggered via `osm migrate --embedding-dim` (see
# osm_init.py's cmd_migrate). init_db() only *detects* a dimension mismatch
# and records it in index_state (see server.py); it never calls anything
# in this section automatically. A container restart can never begin an
# hours-long re-embed.
#
# Add a column, never mutate the old one. The old column and old model
# stay authoritative until cutover explicitly (and irreversibly) succeeds,
# so a failed or interrupted migration leaves the server fully functional
# on the original dimension.


def _new_column_name(new_dim: int) -> str:
    return f"embedding_{int(new_dim)}"


# ALTER TABLE takes ACCESS EXCLUSIVE on `notes`. Without a bound, it waits
# indefinitely behind any open reader transaction — and while it waits, every
# subsequent reader queues behind IT, so a migration that merely blocks takes
# the whole table down with it. That is the opposite of this migration's
# stated guarantee that search stays available throughout.
#
# Observed 2026-07-20 running the pg suite against a real Postgres: a test
# holding an idle-in-transaction SELECT on `notes` stalled ADD COLUMN until
# the run was killed 10 minutes later. Mocks cannot reproduce this; only a
# real lock manager can.
#
# 5s is chosen to be longer than any healthy search transaction and far
# shorter than a human's patience. On timeout the DDL raises LockNotAvailable
# and the migration aborts cleanly, leaving the old column authoritative.
DDL_LOCK_TIMEOUT = "5s"


def _execute_ddl_with_lock_timeout(cur, statements: list[str]) -> None:
    """Run DDL under a bounded lock wait, converting a timeout into a clear,
    actionable error rather than an unbounded stall."""
    import psycopg2.errors

    # DDL_LOCK_TIMEOUT is a module constant ('5s'), never request input.
    # lock_timeout cannot be %s-parameterized. Not a SQLi vector.
    cur.execute(
        sql.SQL("SET LOCAL lock_timeout = {}").format(sql.Literal(DDL_LOCK_TIMEOUT))
    )
    try:
        for stmt in statements:
            cur.execute(stmt)
    except psycopg2.errors.LockNotAvailable as e:
        raise RuntimeError(
            f"Could not acquire the table lock within {DDL_LOCK_TIMEOUT} — another "
            "connection is holding an open transaction on `notes` (a long-running "
            "search, or a session left idle in transaction). No changes were made "
            "and the existing embedding column is untouched. Stop the dashboard "
            "and MCP server, or wait for the reader to finish, then re-run."
        ) from e


def add_embedding_column(conn, new_dim: int) -> None:
    """ADD COLUMN embedding_<new_dim> vector(new_dim), IF NOT EXISTS.

    Purely additive — never touches the existing `embedding` column, so a
    failure here (or anywhere before cutover_embedding_column) leaves
    search fully functional on the original column.
    """
    col = _new_column_name(new_dim)
    # new_dim is an int validated by the caller (osm_init.py's cmd_migrate
    # rejects non-integer/non-positive input before this is ever called),
    # never request input, and pgvector's vector(N) type doesn't accept a
    # %s parameter for its dimension — DDL, not a SQLi vector.
    with conn:
        with conn.cursor() as cur:
            _execute_ddl_with_lock_timeout(cur, [
                # Identifiers are composed with psycopg2.sql.Identifier, which
                # quotes and escapes them at the driver level -- injection-safe by
                # construction rather than by argument. `new_dim` is an int the
                # caller validated; vector(N) is a type, not a value, so it cannot
                # be %s-parameterized.
                sql.SQL("ALTER TABLE notes ADD COLUMN IF NOT EXISTS {} vector({})").format(
                    sql.Identifier(col), sql.SQL(str(int(new_dim)))
                ),
            ])


def backfill_embedding_column(
    conn,
    new_dim: int,
    embed_fn,
    *,
    batch_size: int = 50,
    progress_callback=None,
) -> int:
    """Re-embed every row whose new column is still NULL, in batches of
    `batch_size` (default 50, comfortably under the plan's "at least every
    100 notes" progress-reporting requirement).

    `embed_fn(text: str) -> list[float]` is injected rather than imported
    directly, so callers (and tests) can stub it without touching Ollama.

    `progress_callback(done: int, total: int)`, if given, is called after
    every batch — the caller (typically osm_init.py's cmd_migrate, wiring
    server.set_index_state) uses this to make an hours-long run observable
    rather than opaque.

    Returns the number of rows backfilled. Raises (without ever reaching
    cutover_embedding_column) if embed_fn raises for any row — e.g. Ollama
    becomes unreachable mid-run. The old `embedding` column and all its
    data are untouched by this function regardless of how it exits; the
    old model stays authoritative because cutover never runs.
    """
    col = _new_column_name(new_dim)
    with conn.cursor() as cur:
        # `col` is _new_column_name(new_dim), built from an int the caller
        # validated (osm_init rejects non-integer/non-positive input) — never
        # request input. pgvector's vector(N) and SQL identifiers cannot be
        # %s-parameterized; every real value is. Not a SQLi vector.
        cur.execute(
            sql.SQL("SELECT id, content FROM notes WHERE {} IS NULL ORDER BY id").format(
                sql.Identifier(col)
            )
        )
        rows = cur.fetchall()

    total = len(rows)
    done = 0
    for i in range(0, total, batch_size):
        chunk = rows[i:i + batch_size]
        with conn:
            with conn.cursor() as cur:
                for note_id, content in chunk:
                    vec = embed_fn(content)
                    vec_literal = "[" + ",".join(str(v) for v in vec) + "]"
                    # `col` is _new_column_name(new_dim), built from an int the caller
                    # validated (osm_init rejects non-integer/non-positive input) — never
                    # request input. pgvector's vector(N) and SQL identifiers cannot be
                    # %s-parameterized; every real value is. Not a SQLi vector.
                    cur.execute(
                        sql.SQL("UPDATE notes SET {} = %s::vector WHERE id = %s").format(
                            sql.Identifier(col)
                        ),
                        (vec_literal, note_id),
                    )
        done += len(chunk)
        if progress_callback is not None:
            progress_callback(done, total)
    return done


def cutover_embedding_column(conn, new_dim: int) -> None:
    """Drop the old `embedding` column and rename embedding_<new_dim> to
    `embedding`, in one transaction, then rebuild the IVFFlat index.

    Refuses to run while any row's new column is still NULL — a
    partially-backfilled migration must never cut over. This is the ONLY
    irreversible step in the whole migration; everything before it is
    additive and safe to abandon.
    """
    col = _new_column_name(new_dim)
    with conn.cursor() as cur:
        # `col` is _new_column_name(new_dim), built from an int the caller
        # validated (osm_init rejects non-integer/non-positive input) — never
        # request input. pgvector's vector(N) and SQL identifiers cannot be
        # %s-parameterized; every real value is. Not a SQLi vector.
        cur.execute(
            sql.SQL("SELECT COUNT(*) FROM notes WHERE {} IS NULL").format(
                sql.Identifier(col)
            )
        )
        remaining = cur.fetchone()[0]
    if remaining > 0:
        raise RuntimeError(
            f"cutover refused: {remaining} row(s) still have a NULL {col} — "
            f"backfill is incomplete. Re-run the migration to finish "
            f"backfilling before cutover is attempted again."
        )

    with conn:
        with conn.cursor() as cur:
            # Same ACCESS EXCLUSIVE hazard as add_embedding_column, and worse
            # here: this is the irreversible step, so stalling mid-cutover is
            # the least acceptable place to block every reader.
            _execute_ddl_with_lock_timeout(cur, [
                sql.SQL("ALTER TABLE notes DROP COLUMN embedding"),
                # `col` is _new_column_name(new_dim), built from an int the caller
                # validated (osm_init rejects non-integer/non-positive input) — never
                # request input. pgvector's vector(N) and SQL identifiers cannot be
                # %s-parameterized; every real value is. Not a SQLi vector.
                sql.SQL("ALTER TABLE notes RENAME COLUMN {} TO embedding").format(
                    sql.Identifier(col)
                ),
                sql.SQL("DROP INDEX IF EXISTS notes_embedding_idx"),
                sql.SQL("CREATE INDEX notes_embedding_idx ON notes "
                        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"),
            ])


def migrate_embedding_dimension(
    conn,
    old_dim: int | None,
    new_dim: int,
    embed_fn,
    *,
    dry_run: bool = False,
    progress_callback=None,
) -> dict:
    """Orchestrate a non-destructive embedding-dimension change.

    `old_dim`, if given, is validated against the CURRENT `notes.embedding`
    column's actual dimension before anything else runs — a guard against
    an operator invoking `osm migrate --embedding-dim` with a stale
    assumption about the current dimension. Pass `None` to skip this check
    (the add/backfill/cutover steps below don't otherwise need it — the
    new dimension alone determines what they do).

    `dry_run=True` reports the plan (row count to re-embed, whether a
    dimension mismatch actually exists) and writes nothing.

    Real run: add_embedding_column -> backfill_embedding_column ->
    cutover_embedding_column, in that order. If backfill raises (e.g.
    Ollama becomes unreachable), this function lets the exception
    propagate WITHOUT calling cutover — the old column and old model stay
    authoritative. Search remains available throughout: the old
    `embedding` column is never touched until cutover's single, fast
    (metadata-only DROP + RENAME) transaction.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT format_type(a.atttypid, a.atttypmod)
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            WHERE c.relname = 'notes' AND a.attname = 'embedding'
              AND a.attnum > 0 AND NOT a.attisdropped
            """
        )
        row = cur.fetchone()
    current_dim = None
    if row:
        m = re.search(r'vector\((\d+)\)', row[0])
        if m:
            current_dim = int(m.group(1))

    if old_dim is not None and current_dim is not None and old_dim != current_dim:
        raise RuntimeError(
            f"migrate_embedding_dimension: expected current dimension "
            f"vector({old_dim}) but notes.embedding is actually "
            f"vector({current_dim}) — refusing to proceed with a stale "
            f"assumption. Re-check with `osm migrate --dry-run`."
        )

    plan = {
        "current_dim": current_dim,
        "new_dim": new_dim,
        "dry_run": dry_run,
    }

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM notes")
        plan["notes_to_reembed"] = cur.fetchone()[0]

    if dry_run:
        plan["status"] = "dry_run"
        return plan

    add_embedding_column(conn, new_dim)
    backfilled = backfill_embedding_column(
        conn, new_dim, embed_fn, progress_callback=progress_callback
    )
    cutover_embedding_column(conn, new_dim)

    plan["backfilled"] = backfilled
    plan["status"] = "complete"
    return plan
