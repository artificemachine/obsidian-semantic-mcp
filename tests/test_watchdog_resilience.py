"""tests/test_watchdog_resilience.py — Security & Correctness plan, iteration 3.

A database outage during a file delete or move must not kill the watchdog
observer thread. watchdog dispatches event-handler callbacks directly on the
observer thread with no supervising try/except of its own — an unguarded
exception inside on_deleted/on_moved/_handle_upsert silently ends all further
event dispatch on that observer. From the outside this looks identical to
"indexing just stopped," not a crash, which is what made the original bug
hard to diagnose.

Side-effect fence: repo tree only. Every test here monkeypatches
server.delete_note (or lower); none open a real DB connection or touch a
real vault.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import psycopg2

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

os.environ.setdefault("OBSIDIAN_VAULT", "/tmp/test_vault")
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/test")
os.environ.setdefault("OLLAMA_URL", "http://localhost:11434")

import server  # noqa: E402


def _event(*, is_directory: bool = False, src_path: str = "", dest_path: str = ""):
    """Build a minimal object matching the subset of watchdog's event API
    VaultEventHandler actually reads (is_directory, src_path, dest_path)."""
    return type(
        "FakeEvent", (), {"is_directory": is_directory, "src_path": src_path, "dest_path": dest_path}
    )()


# ── Smoke ─────────────────────────────────────────────────────────────────

def test_smoke_handler_dispatches_synthetic_event_without_raising(monkeypatch):
    delete_mock = MagicMock()
    monkeypatch.setattr(server, "delete_note", delete_mock)
    monkeypatch.setattr(server, "VAULT_PATH", "/vault")
    monkeypatch.setattr(server, "_VAULT_LIST", ["/vault"])
    handler = server.VaultEventHandler()
    handler.on_deleted(_event(src_path="/vault/note.md"))
    delete_mock.assert_called_once_with("/vault/note.md")


# ── Unit ──────────────────────────────────────────────────────────────────

def test_on_deleted_survives_db_error(monkeypatch):
    monkeypatch.setattr(
        server, "delete_note", MagicMock(side_effect=psycopg2.OperationalError("db down"))
    )
    monkeypatch.setattr(server, "VAULT_PATH", "/vault")
    monkeypatch.setattr(server, "_VAULT_LIST", ["/vault"])
    handler = server.VaultEventHandler()

    # Must not raise.
    handler.on_deleted(_event(src_path="/vault/note.md"))


def test_on_moved_survives_db_error_on_source(monkeypatch):
    monkeypatch.setattr(
        server, "delete_note", MagicMock(side_effect=psycopg2.OperationalError("db down"))
    )
    monkeypatch.setattr(server, "VAULT_PATH", "/vault")
    monkeypatch.setattr(server, "_VAULT_LIST", ["/vault"])
    handler = server.VaultEventHandler()
    schedule_mock = MagicMock()
    monkeypatch.setattr(handler, "_schedule", schedule_mock)

    # Must not raise, AND must still schedule the destination path — a
    # source-side delete failure must not swallow destination indexing.
    handler.on_moved(_event(src_path="/vault/old.md", dest_path="/vault/new.md"))

    schedule_mock.assert_called_once_with("/vault/new.md")


def test_handle_upsert_survives_db_error_in_filenotfound_recovery(monkeypatch):
    """Fails today (pre-fix) because of the on_deleted-ordering defect class:
    _handle_upsert's `except FileNotFoundError: delete_note(path)` recovery
    branch called delete_note() unguarded, so a DB failure *inside the
    recovery itself* escaped the outer `except Exception`."""
    monkeypatch.setattr(
        server, "delete_note", MagicMock(side_effect=psycopg2.OperationalError("db down"))
    )
    handler = server.VaultEventHandler()

    # A path that does not exist on disk triggers the FileNotFoundError
    # recovery branch inside _handle_upsert.
    missing_path = "/nonexistent/path/that/does/not/exist/note.md"
    handler._handle_upsert(missing_path)  # must not raise


def test_delete_note_failure_is_logged(monkeypatch, caplog):
    monkeypatch.setattr(
        server, "delete_note", MagicMock(side_effect=psycopg2.OperationalError("db down"))
    )
    with caplog.at_level(logging.WARNING, logger="server"):
        server._safe_delete_note("/vault/note.md")

    assert any(
        "/vault/note.md" in record.getMessage() for record in caplog.records
    ), f"expected a warning naming the failed path; got: {[r.getMessage() for r in caplog.records]}"


# ── Integration ───────────────────────────────────────────────────────────

def test_observer_still_dispatches_after_delete_failure(monkeypatch, tmp_path):
    """The actual bug this iteration fixes: not that one call fails, but
    that the observer thread stops serving subsequent events afterward.

    Calls _handle_upsert directly rather than on_created -> _schedule: the
    real _schedule() creates a threading.Timer while holding handler._lock
    and the Timer callback (_handle_upsert) re-acquires that same
    (non-reentrant) lock on its own thread once the delay elapses — that's
    safe with a real, separate-thread Timer, but calling _handle_upsert
    synchronously in-test exercises exactly the code path this iteration
    changed (the try/except around index_note / delete_note) without
    depending on real wall-clock debounce timing.
    """
    monkeypatch.setattr(
        server, "delete_note", MagicMock(side_effect=psycopg2.OperationalError("db down"))
    )
    index_note_mock = MagicMock()
    monkeypatch.setattr(server, "index_note", index_note_mock)
    monkeypatch.setattr(server, "VAULT_PATH", str(tmp_path))
    monkeypatch.setattr(server, "_VAULT_LIST", [str(tmp_path)])

    live_note = tmp_path / "note.md"
    live_note.write_text("# live", encoding="utf-8")

    handler = server.VaultEventHandler()

    # First: a delete that fails.
    handler.on_deleted(_event(src_path=str(tmp_path / "gone.md")))

    # Second: on the SAME handler instance, an upsert must still be
    # processed — proves the handler survived the first failure rather than
    # having propagated an exception that would have killed the observer
    # thread before reaching this line in real usage.
    handler._handle_upsert(str(live_note))

    index_note_mock.assert_called_once()


# ── Chaos ─────────────────────────────────────────────────────────────────

def test_repeated_db_failures_do_not_exhaust_handler(monkeypatch):
    fail_then_succeed = MagicMock(side_effect=psycopg2.OperationalError("db down"))
    monkeypatch.setattr(server, "delete_note", fail_then_succeed)
    monkeypatch.setattr(server, "VAULT_PATH", "/vault")
    monkeypatch.setattr(server, "_VAULT_LIST", ["/vault"])
    handler = server.VaultEventHandler()

    for i in range(50):
        handler.on_deleted(_event(src_path=f"/vault/note-{i}.md"))  # must not raise

    # A subsequent success must still be processed normally afterward.
    fail_then_succeed.side_effect = None
    fail_then_succeed.reset_mock()
    handler.on_deleted(_event(src_path="/vault/note-final.md"))
    fail_then_succeed.assert_called_once_with("/vault/note-final.md")


# ── Regression (grep-equivalent) ─────────────────────────────────────────

def test_no_unguarded_delete_note_call_inside_handler():
    """grep-equivalent: on_deleted and on_moved must route through
    _safe_delete_note, never call delete_note( directly."""
    src = (Path(__file__).parent.parent / "src" / "server.py").read_text()
    start = src.index("class VaultEventHandler")
    end = src.index("def _needs_polling")
    handler_src = src[start:end]
    assert "delete_note(" not in handler_src.replace("_safe_delete_note(", ""), (
        "found a call to delete_note( inside VaultEventHandler that isn't "
        "routed through _safe_delete_note("
    )
