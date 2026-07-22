"""
Unit tests for server.py — pytest-compatible, no sys.exit, no real DB/Ollama needed.
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure src/ is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# Minimal env so server.py imports without crashing
os.environ.setdefault("OBSIDIAN_VAULT", "/tmp/test_vault")
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/test")
os.environ.setdefault("OLLAMA_URL", "http://localhost:11434")


def _make_mock_conn():
    """Return a (fake_db_conn contextmanager, mock_cur) pair for search_vault tests."""
    from contextlib import contextmanager

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_cur.__enter__ = lambda s: s
    mock_cur.__exit__ = MagicMock(return_value=False)
    mock_cur.fetchall.return_value = []
    mock_conn.__enter__ = lambda s: s
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value = mock_cur

    @contextmanager
    def fake_db_conn():
        yield mock_conn

    return fake_db_conn, mock_cur


# ── embed() ──────────────────────────────────────────────────────────────────

class TestEmbed:
    def test_raises_on_empty_embedding(self, monkeypatch):
        """Ollama returning [] must raise ValueError — not silently produce a bad vector."""
        import requests
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"embedding": []}
        mock_resp.raise_for_status = lambda: None
        monkeypatch.setattr(requests, "post", lambda *a, **kw: mock_resp)

        import server
        with pytest.raises(ValueError, match="Empty embedding"):
            server.embed("some content")

    def test_returns_vector_on_success(self, monkeypatch):
        """Valid Ollama response returns the embedding list."""
        import requests
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"embedding": [0.1, 0.2, 0.3]}
        mock_resp.raise_for_status = lambda: None
        monkeypatch.setattr(requests, "post", lambda *a, **kw: mock_resp)

        import server
        result = server.embed("some content")
        assert result == [0.1, 0.2, 0.3]

    def test_truncates_to_max_chars(self, monkeypatch):
        """Input longer than MAX_EMBED_CHARS is truncated before sending to Ollama."""
        import requests
        captured = {}

        def fake_post(url, json=None, **kw):
            captured["prompt"] = json.get("prompt", "")
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"embedding": [0.1]}
            mock_resp.raise_for_status = lambda: None
            return mock_resp

        monkeypatch.setattr(requests, "post", fake_post)

        import server
        long_text = "x" * 5000
        server.embed(long_text)
        assert len(captured["prompt"]) <= server.MAX_EMBED_CHARS


# ── embed concurrency defaults ────────────────────────────────────────────────

class TestEmbedConcurrencyDefaults:
    """Regression: EMBED_WORKERS default must not outrun Ollama's actual serving
    capacity. A CPU-only Ollama (no GPU passthrough) negotiates a single request
    slot (`-np 1`) under normal container memory limits. With EMBED_WORKERS=4 and
    EMBED_TIMEOUT=15, 3 of 4 concurrent embed calls always queue behind the 1
    active slot, exceed the 15s timeout, and the exponential-backoff retry
    resubmits at the same concurrency — a self-reinforcing thrash loop observed
    live: the ollama container pegged at 949-1223% CPU for 14h40m during a full
    vault reindex (see the reindex design notes). Defaults must be conservative enough for
    a single-slot CPU server; env overrides remain available for GPU/multi-slot
    deployments.
    """

    def test_embed_workers_default_matches_single_slot_capacity(self):
        import server
        assert server.EMBED_WORKERS == 1

    def test_embed_timeout_default_has_cpu_only_headroom(self):
        import server
        assert server.EMBED_TIMEOUT == 30

    def test_embed_workers_still_overridable_via_env(self, monkeypatch):
        """GPU/multi-slot deployments must still be able to raise concurrency."""
        import importlib
        import server

        monkeypatch.setenv("EMBED_WORKERS", "8")
        monkeypatch.setenv("EMBED_TIMEOUT", "5")
        try:
            importlib.reload(server)
            assert server.EMBED_WORKERS == 8
            assert server.EMBED_TIMEOUT == 5
        finally:
            monkeypatch.delenv("EMBED_WORKERS", raising=False)
            monkeypatch.delenv("EMBED_TIMEOUT", raising=False)
            importlib.reload(server)


# ── VaultEventHandler._handle_upsert ─────────────────────────────────────────

class TestWatchdogHandler:
    def test_db_exception_does_not_kill_thread(self, monkeypatch):
        """DataException from index_note must be caught — watcher thread must survive."""
        import psycopg2
        import server

        def boom(path, content):
            raise psycopg2.errors.DataException("vector must have at least 1 dimension")

        monkeypatch.setattr(server, "index_note", boom)

        with tempfile.NamedTemporaryFile(suffix=".md") as f:
            f.write(b"# test note\n")
            f.flush()
            handler = server.VaultEventHandler()
            # Must not raise — exception must be caught internally
            handler._handle_upsert(f.name)

    def test_generic_exception_does_not_kill_thread(self, monkeypatch):
        """Any exception from index_note must be caught — watcher thread must survive."""
        import server

        def boom(*a):
            raise RuntimeError("boom")
        monkeypatch.setattr(server, "index_note", boom)

        with tempfile.NamedTemporaryFile(suffix=".md") as f:
            f.write(b"# test\n")
            f.flush()
            handler = server.VaultEventHandler()
            handler._handle_upsert(f.name)  # must not raise

    def test_dimension_mismatch_write_failure_recorded_in_index_state(self, monkeypatch):
        """A watcher write that fails with a pgvector dimension error must be
        recorded in index_state (dashboard-visible), not just logged — the
        boot-time dimension_mismatch check only runs once at startup, so an
        edit made afterward that fails the same way was previously silent
        beyond a per-file log.warning."""
        import psycopg2
        import server

        def boom(path, content, vault_id=""):
            raise psycopg2.errors.DataException("expected 768 dimensions, not 1024")
        monkeypatch.setattr(server, "index_note", boom)

        record_mock = MagicMock()
        monkeypatch.setattr(server, "_record_watcher_dimension_failure", record_mock)

        with tempfile.NamedTemporaryFile(suffix=".md") as f:
            f.write(b"# test\n")
            f.flush()
            handler = server.VaultEventHandler(vault_id="myvault")
            handler._handle_upsert(f.name)  # must not raise

        record_mock.assert_called_once()
        args, _ = record_mock.call_args
        assert args[0] == "myvault"
        assert args[1] == f.name
        assert "dimensions" in args[2]

    def test_empty_embedding_data_exception_does_not_call_recorder(self, monkeypatch):
        """A DataException for a genuinely empty embedding ('vector must have
        at least 1 dimension' — a different bug class, an empty embed()
        result) must NOT be misrouted into the column-dimension-mismatch
        recorder. Only the specific 'expected N dimensions, not M' pgvector
        column-mismatch phrasing should trigger it — a bare 'dimension'
        substring match would wrongly conflate the two."""
        import psycopg2
        import server

        def boom(path, content, vault_id=""):
            raise psycopg2.errors.DataException("vector must have at least 1 dimension")
        monkeypatch.setattr(server, "index_note", boom)

        record_mock = MagicMock()
        monkeypatch.setattr(server, "_record_watcher_dimension_failure", record_mock)

        with tempfile.NamedTemporaryFile(suffix=".md") as f:
            f.write(b"# test\n")
            f.flush()
            handler = server.VaultEventHandler()
            handler._handle_upsert(f.name)  # must not raise

        record_mock.assert_not_called()

    def test_concurrent_dimension_failures_do_not_lose_a_path(self, monkeypatch):
        """Two watcher threads recording a dimension-mismatch failure for the
        SAME vault_id but different paths, at the same time, must not lose
        either path — the read-merge-write in
        _record_watcher_dimension_failure must be serialized (a plain
        read-then-write with no lock loses whichever write finishes first)."""
        import threading
        import server

        state = {"failed_paths": []}
        state_lock = threading.Lock()  # guards the fake DB only, not the code under test
        release_first_write = threading.Event()
        first_reader_is_waiting = threading.Event()

        call_count = {"n": 0}

        def fake_get_index_state(vault_id):
            with state_lock:
                return {"failed_paths": list(state["failed_paths"])}

        def fake_set_index_state(vault_id, status, *, failed_paths=None, error=None):
            call_count["n"] += 1
            # Force the SECOND call (the second thread's write) to interleave
            # between the first thread's read and write, reproducing the race
            # a lock would prevent.
            if call_count["n"] == 1:
                first_reader_is_waiting.set()
                release_first_write.wait(timeout=5)
            with state_lock:
                state["failed_paths"] = list(failed_paths or [])

        monkeypatch.setattr(server, "get_index_state", fake_get_index_state)
        monkeypatch.setattr(server, "set_index_state", fake_set_index_state)

        def thread_a():
            server._record_watcher_dimension_failure("vault1", "/a.md", "expected 768 dimensions, not 1024")

        def thread_b():
            first_reader_is_waiting.wait(timeout=5)
            server._record_watcher_dimension_failure("vault1", "/b.md", "expected 768 dimensions, not 1024")
            release_first_write.set()

        t1 = threading.Thread(target=thread_a)
        t2 = threading.Thread(target=thread_b)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert set(state["failed_paths"]) == {"/a.md", "/b.md"}, (
            f"lost an update under concurrent access: {state['failed_paths']}"
        )

    def test_archive_modified_event_is_not_scheduled(self, tmp_path, monkeypatch):
        """Archive changes must be ignored before debounce scheduling."""
        import server

        archive_note = tmp_path / "archive" / "nested" / "note.md"
        archive_note.parent.mkdir(parents=True)
        archive_note.write_text("# archived", encoding="utf-8")

        timer_calls: list[str] = []

        class FakeTimer:
            def __init__(self, delay, func, args=()):
                timer_calls.append("timer_init")
            def cancel(self):
                timer_calls.append("cancel")
            def start(self):
                timer_calls.append("start")

        monkeypatch.setattr(server.threading, "Timer", FakeTimer)
        monkeypatch.setattr(server, "VAULT_PATH", str(tmp_path))
        monkeypatch.setattr(server, "_VAULT_LIST", [str(tmp_path)])

        handler = server.VaultEventHandler()

        event = type("Event", (), {"is_directory": False, "src_path": str(archive_note)})()
        handler.on_modified(event)

        assert timer_calls == []

    def test_live_modified_event_is_scheduled(self, tmp_path, monkeypatch):
        """Live markdown changes must still flow through the watcher debounce path."""
        import server

        live_note = tmp_path / "notes" / "note.md"
        live_note.parent.mkdir(parents=True)
        live_note.write_text("# live", encoding="utf-8")

        timer_calls: list[str] = []

        class FakeTimer:
            def __init__(self, delay, func, args=()):
                timer_calls.append("timer_init")
                self.delay = delay
                self.func = func
                self.args = args
            def cancel(self):
                timer_calls.append("cancel")
            def start(self):
                timer_calls.append("start")

        monkeypatch.setattr(server.threading, "Timer", FakeTimer)
        monkeypatch.setattr(server, "VAULT_PATH", str(tmp_path))
        monkeypatch.setattr(server, "_VAULT_LIST", [str(tmp_path)])

        handler = server.VaultEventHandler()

        event = type("Event", (), {"is_directory": False, "src_path": str(live_note)})()
        handler.on_modified(event)

        assert timer_calls == ["timer_init", "start"]


# ── index_note connection safety ──────────────────────────────────────────────

class TestIndexNoteConnectionSafety:
    """Regression: index_note must NOT hold a DB connection while calling embed().
    The embed() call can block for up to EMBED_TIMEOUT (15s). With a pool of 5,
    concurrent file saves would exhaust the pool and starve search/hash queries.
    """

    def test_hash_check_connection_released_before_embed_and_upsert(self, monkeypatch):
        """The hash-check DB connection must be fully released before _embed_and_upsert
        is invoked. The embed() call (inside _embed_and_upsert) can block for up to
        EMBED_TIMEOUT seconds; holding the hash-check connection across it exhausts the
        pool under concurrent file-save activity.
        """
        import server
        from contextlib import contextmanager

        call_order: list[str] = []

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.__enter__ = lambda s: s
        mock_cur.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchone.return_value = None  # no existing hash → proceeds to upsert
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur

        @contextmanager
        def fake_db_conn():
            call_order.append("db_open")
            try:
                yield mock_conn
            finally:
                call_order.append("db_close")

        def fake_embed_and_upsert(path, content, h, vault_id=""):
            call_order.append("embed_and_upsert")

        monkeypatch.setattr(server, "db_conn", fake_db_conn)
        monkeypatch.setattr(server, "_embed_and_upsert", fake_embed_and_upsert)
        monkeypatch.setattr(server, "embed_batch", lambda texts: (_ for _ in ()).throw(RuntimeError("force fallback")))
        # Prevent Ollama calls in the broken pre-fix state (current code calls embed
        # directly inside the db_conn block; after the fix it delegates to _embed_and_upsert)
        monkeypatch.setattr(server, "embed", lambda text: [0.1, 0.2])

        server.index_note("/vault/note.md", "# Note content", "vault")

        assert "embed_and_upsert" in call_order, "_embed_and_upsert was never called"
        close_idx = call_order.index("db_close")
        upsert_idx = call_order.index("embed_and_upsert")
        assert close_idx < upsert_idx, (
            f"Hash-check DB connection was not released before _embed_and_upsert: {call_order}"
        )


# ── _is_system_path ───────────────────────────────────────────────────────────

class TestIsSystemPath:
    def test_skips_obsidian_dir(self, tmp_path):
        """Files inside .obsidian should be skipped."""
        import server
        with patch.object(server, "VAULT_PATH", str(tmp_path)), \
             patch.object(server, "_VAULT_LIST", [str(tmp_path)]):
            p = tmp_path / ".obsidian" / "config.json"
            assert server._is_system_path(p) is True

    def test_skips_trash(self, tmp_path):
        """Files inside .trash should be skipped."""
        import server
        with patch.object(server, "VAULT_PATH", str(tmp_path)), \
             patch.object(server, "_VAULT_LIST", [str(tmp_path)]):
            p = tmp_path / ".trash" / "deleted.md"
            assert server._is_system_path(p) is True

    def test_does_not_skip_normal_note(self, tmp_path):
        """Regular notes should not be skipped."""
        import server
        with patch.object(server, "VAULT_PATH", str(tmp_path)), \
             patch.object(server, "_VAULT_LIST", [str(tmp_path)]):
            p = tmp_path / "notes" / "my_note.md"
            assert server._is_system_path(p) is False

    def test_vault_inside_hidden_dir_not_skipped(self, tmp_path):
        """Notes in a vault that itself lives inside a hidden parent dir must NOT be skipped."""
        hidden_vault = tmp_path / ".vaults" / "my_vault"
        hidden_vault.mkdir(parents=True)
        import server
        with patch.object(server, "VAULT_PATH", str(hidden_vault)), \
             patch.object(server, "_VAULT_LIST", [str(hidden_vault)]):
            p = hidden_vault / "notes" / "note.md"
            assert server._is_system_path(p) is False


# ── ignore path override ─────────────────────────────────────────────────────

class TestIgnorePathOverride:
    def test_default_skips_archive(self, tmp_path):
        """archive/ should remain excluded when no override is set."""
        import server
        with patch.object(server, "VAULT_PATH", str(tmp_path)), \
             patch.object(server, "_VAULT_LIST", [str(tmp_path)]):
            p = tmp_path / "archive" / "nested" / "note.md"
            assert server._is_system_path(p) is True

    def test_empty_ignore_paths_allows_archive_indexing(self, tmp_path, monkeypatch):
        """Setting OBSIDIAN_IGNORE_PATHS empty should allow archive/ to be indexed."""
        import server

        live = tmp_path / "notes" / "live.md"
        archived = tmp_path / "archive" / "nested" / "note.md"
        live.parent.mkdir(parents=True)
        archived.parent.mkdir(parents=True)
        live.write_text("# Live\nKeep me", encoding="utf-8")
        archived.write_text("# Archived\nKeep me too", encoding="utf-8")

        indexed: list[str] = []

        def fake_embed_and_upsert(path, content, hash_, vault):
            indexed.append(path)

        monkeypatch.setenv("OBSIDIAN_IGNORE_PATHS", "")
        monkeypatch.setattr(server, "VAULT_PATH", str(tmp_path))
        monkeypatch.setattr(server, "_VAULT_LIST", [str(tmp_path)])
        monkeypatch.setattr(server, "_bulk_load_hashes", lambda paths: {})
        monkeypatch.setattr(server, "_embed_and_upsert", fake_embed_and_upsert)
        monkeypatch.setattr(server, "embed_batch", lambda texts: (_ for _ in ()).throw(RuntimeError("force fallback")))

        server.index_vault(str(tmp_path))

        assert str(archived) in indexed
        assert str(live) in indexed


# ── indexing_in_progress flag ────────────────────────────────────────────────

class TestIndexingFlag:
    def test_search_returns_indexing_message_when_in_progress(self, monkeypatch):
        """search_vault with empty DB during indexing must say indexing is in progress, not 'try reindex_vault'."""
        import asyncio
        import server
        import threading
        evt = threading.Event()
        evt.set()
        monkeypatch.setattr(server, "_INDEXING_IN_PROGRESS", evt)

        fake_db_conn, _ = _make_mock_conn()
        monkeypatch.setattr(server, "db_conn", fake_db_conn)
        monkeypatch.setattr(server, "embed", lambda q: [0.1, 0.2])

        result = asyncio.run(server.call_tool("search_vault", {"query": "anything"}))
        text = result[0].text
        assert "indexing" in text.lower()
        assert "reindex_vault" not in text


# ── db_conn pool safety ───────────────────────────────────────────────────────

class TestDbConnPoolSafety:
    def test_connection_discarded_on_exception(self, monkeypatch):
        """When the body of db_conn() raises, putconn must be called with close=True
        so the pool discards the connection rather than recycling a broken one."""
        import server

        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_pool.getconn.return_value = mock_conn
        monkeypatch.setattr(server, "_pool", mock_pool)

        with pytest.raises(RuntimeError):
            with server.db_conn():
                raise RuntimeError("simulated mid-transaction failure")

        mock_pool.putconn.assert_called_once_with(mock_conn, close=True)

    def test_connection_returned_normally_on_success(self, monkeypatch):
        """On clean exit putconn must be called without close=True."""
        import server

        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_pool.getconn.return_value = mock_conn
        monkeypatch.setattr(server, "_pool", mock_pool)

        with server.db_conn():
            pass

        mock_pool.putconn.assert_called_once_with(mock_conn)


# ── input validation — limit / context_length ─────────────────────────────────

class TestSearchInputValidation:
    def test_negative_limit_clamped_to_one(self, monkeypatch):
        """search_vault must not pass a negative LIMIT to PostgreSQL."""
        import asyncio
        import server

        fake_db_conn, mock_cur = _make_mock_conn()
        monkeypatch.setattr(server, "db_conn", fake_db_conn)
        monkeypatch.setattr(server, "embed", lambda q: [0.1])
        monkeypatch.setattr(server, "_INDEXING_IN_PROGRESS", False)

        asyncio.run(server.call_tool("search_vault", {"query": "x", "limit": -99}))

        # SQL uses parameterized queries (%s), so the clamped value is in the
        # params tuple — not the SQL string. Third param is the LIMIT value.
        params = mock_cur.execute.call_args[0][1]
        assert params[-1] >= 1, f"LIMIT must be clamped to ≥1, got {params[-1]}"


# ── _vec_to_str ───────────────────────────────────────────────────────────────

class TestVecToStr:
    def test_formats_correctly(self):
        import server
        result = server._vec_to_str([0.1, 0.2, 0.3])
        assert result == "[0.1,0.2,0.3]"

    def test_empty_raises(self):
        import server
        with pytest.raises(ValueError):
            server._vec_to_str([])


# ── _build_dsn ────────────────────────────────────────────────────────────────

class TestBuildDsn:
    def test_prefers_database_url(self, monkeypatch):
        """DATABASE_URL env var takes priority over POSTGRES_* vars."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://custom/db")
        import config
        assert config.build_dsn() == "postgresql://custom/db"

    def test_falls_back_to_postgres_vars(self, monkeypatch):
        """When DATABASE_URL is absent, assembles DSN from POSTGRES_* vars."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setenv("POSTGRES_HOST", "myhost")
        monkeypatch.setenv("POSTGRES_PORT", "5433")
        monkeypatch.setenv("POSTGRES_DB",   "mydb")
        monkeypatch.setenv("POSTGRES_USER", "myuser")
        monkeypatch.setenv("POSTGRES_PASSWORD", "mypass")
        import config
        dsn = config.build_dsn()
        assert "host=myhost" in dsn
        assert "port=5433" in dsn
        assert "dbname=mydb" in dsn
        assert "user=myuser" in dsn
        assert "password=mypass" in dsn

    def test_fallback_dsn_has_no_credential_url(self, monkeypatch):
        """The libpq keyword format must never produce a postgresql://user:pass@host URL."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setenv("POSTGRES_PASSWORD", "testpass")
        import config
        assert "://" not in config.build_dsn()

    def test_fallback_dsn_raises_on_empty_password(self, monkeypatch):
        """build_dsn() must raise when POSTGRES_PASSWORD is unset to prevent silent no-auth connections."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
        import config
        with pytest.raises(RuntimeError, match="POSTGRES_PASSWORD"):
            config.build_dsn()


# ── _resolve_vault_path ───────────────────────────────────────────────────────

class TestResolveVaultPath:
    def test_allows_nested_path(self, tmp_path):
        import server
        with patch.object(server, "VAULT_PATH", str(tmp_path)):
            result = server._resolve_vault_path("notes/note.md")
            assert Path(result).is_relative_to(tmp_path.resolve())

    def test_blocks_dotdot_traversal(self, tmp_path):
        """../../etc/passwd must raise ValueError."""
        import server
        with patch.object(server, "VAULT_PATH", str(tmp_path)):
            with pytest.raises(ValueError, match="escapes vault"):
                server._resolve_vault_path("../../etc/passwd")

    def test_blocks_absolute_path(self, tmp_path):
        """/etc/passwd must raise ValueError — absolute paths escape the vault."""
        import server
        with patch.object(server, "VAULT_PATH", str(tmp_path)):
            with pytest.raises(ValueError, match="escapes vault"):
                server._resolve_vault_path("/etc/passwd")

    def test_vault_root_itself_is_allowed(self, tmp_path):
        import server
        with patch.object(server, "VAULT_PATH", str(tmp_path)):
            result = server._resolve_vault_path(".")
            assert result == tmp_path.resolve()


# ── file_hash ─────────────────────────────────────────────────────────────────

class TestFileHash:
    def test_deterministic(self):
        import server
        assert server.file_hash("hello world") == server.file_hash("hello world")

    def test_different_inputs_differ(self):
        import server
        assert server.file_hash("hello") != server.file_hash("world")

    def test_returns_string(self):
        import server
        assert isinstance(server.file_hash("x"), str)

    def test_sha256_not_md5(self):
        """Regression: index_vault must use file_hash (SHA-256), not hashlib.md5().
        If both hash the same content, they must produce the same value — otherwise
        every file appears 'changed' on every reindex.
        """
        import hashlib
        import server

        content = "# Test Note\nSome content"
        sha256_hex = hashlib.sha256(content.encode()).hexdigest()
        md5_hex = hashlib.md5(content.encode()).hexdigest()

        result = server.file_hash(content)
        assert result == sha256_hex, "file_hash must use SHA-256"
        assert result != md5_hex, "file_hash must NOT use MD5 (would cause reindex on every run)"


# ── index_vault hash consistency ─────────────────────────────────────────────

class TestIndexVaultHashConsistency:
    """Regression test: index_vault must use file_hash() for change detection,
    not a different algorithm (e.g. hashlib.md5). A mismatch would cause every
    file to appear 'changed' on every reindex.
    """

    def test_index_vault_skips_unchanged_file(self, tmp_path, monkeypatch):
        """A file already indexed with file_hash() must be skipped on reindex."""
        import server

        content = "# Note\nUnchanged content"
        note = tmp_path / "note.md"
        note.write_text(content, encoding="utf-8")

        expected_hash = server.file_hash(content)

        # Simulate DB already having this file with the correct SHA-256 hash
        fake_db_conn, mock_cur = _make_mock_conn()
        mock_cur.fetchall.return_value = [(str(note), expected_hash)]

        embed_calls: list[str] = []

        def fake_embed_and_upsert(path, content, hash_, vault):
            embed_calls.append(path)

        monkeypatch.setattr(server, "_bulk_load_hashes", lambda paths: {str(note): expected_hash})
        monkeypatch.setattr(server, "_embed_and_upsert", fake_embed_and_upsert)
        monkeypatch.setattr(server, "embed_batch", lambda texts: (_ for _ in ()).throw(RuntimeError("force fallback")))
        monkeypatch.setattr(server, "_should_skip_path", lambda p: False)

        server.index_vault(str(tmp_path))

        assert embed_calls == [], (
            "Unchanged file was re-embedded — hash algorithm mismatch between "
            "index_vault() and file_hash()"
        )

    def test_index_vault_reindexes_changed_file(self, tmp_path, monkeypatch):
        """A file whose content changed must be re-embedded."""
        import server

        content = "# Note\nNew content"
        note = tmp_path / "note.md"
        note.write_text(content, encoding="utf-8")

        stale_hash = server.file_hash("# Note\nOld content")  # different from current

        embed_calls: list[str] = []

        def fake_embed_and_upsert(path, content, hash_, vault):
            embed_calls.append(path)

        monkeypatch.setattr(server, "_bulk_load_hashes", lambda paths: {str(note): stale_hash})
        monkeypatch.setattr(server, "_embed_and_upsert", fake_embed_and_upsert)
        monkeypatch.setattr(server, "embed_batch", lambda texts: (_ for _ in ()).throw(RuntimeError("force fallback")))
        monkeypatch.setattr(server, "_should_skip_path", lambda p: False)

        server.index_vault(str(tmp_path))

        assert str(note) in embed_calls, "Changed file must be re-embedded"


# ── index_vault archive exclusion ────────────────────────────────────────────

class TestIndexVaultArchiveExclusion:
    """Regression: archive/ content must stay out of the default index."""

    def test_skips_archive_notes_during_indexing(self, tmp_path, monkeypatch):
        import server

        live = tmp_path / "notes" / "live.md"
        archived = tmp_path / "archive" / "nested" / "note.md"
        live.parent.mkdir(parents=True)
        archived.parent.mkdir(parents=True)
        live.write_text("# Live\nKeep me", encoding="utf-8")
        archived.write_text("# Archived\nSkip me", encoding="utf-8")

        indexed: list[str] = []

        def fake_embed_and_upsert(path, content, hash_, vault):
            indexed.append(path)

        monkeypatch.setattr(server, "VAULT_PATH", str(tmp_path))
        monkeypatch.setattr(server, "_VAULT_LIST", [str(tmp_path)])
        monkeypatch.setattr(server, "_bulk_load_hashes", lambda paths: {})
        monkeypatch.setattr(server, "_embed_and_upsert", fake_embed_and_upsert)
        monkeypatch.setattr(server, "embed_batch", lambda texts: (_ for _ in ()).throw(RuntimeError("force fallback")))

        server.index_vault(str(tmp_path))

        assert indexed == [str(live)]

    def test_indexes_normal_live_notes(self, tmp_path, monkeypatch):
        import server

        live = tmp_path / "notes" / "live.md"
        live.parent.mkdir(parents=True)
        live.write_text("# Live\nKeep me", encoding="utf-8")

        indexed: list[str] = []

        def fake_embed_and_upsert(path, content, hash_, vault):
            indexed.append(path)

        monkeypatch.setattr(server, "VAULT_PATH", str(tmp_path))
        monkeypatch.setattr(server, "_VAULT_LIST", [str(tmp_path)])
        monkeypatch.setattr(server, "_bulk_load_hashes", lambda paths: {})
        monkeypatch.setattr(server, "_embed_and_upsert", fake_embed_and_upsert)
        monkeypatch.setattr(server, "embed_batch", lambda texts: (_ for _ in ()).throw(RuntimeError("force fallback")))

        server.index_vault(str(tmp_path))

        assert indexed == [str(live)]


# ── dashboard search_notes connection safety ──────────────────────────────────

class TestDashboardSearchConnectionSafety:
    """Regression: dashboard search_notes must NOT call embed() while holding a DB connection.
    Holding the connection during an Ollama HTTP call (up to 15s) starves the pool.
    """

    def test_embed_called_before_db_connection_opened(self, monkeypatch):
        """embed() must be called and complete before any db_conn is acquired."""
        from contextlib import contextmanager

        # dashboard imports server, so it's already in sys.modules after test_unit imports
        import dashboard
        import server

        call_order: list[str] = []

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.__enter__ = lambda s: s
        mock_cur.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchall.return_value = []
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur

        @contextmanager
        def fake_db_conn():
            call_order.append("db_open")
            try:
                yield mock_conn
            finally:
                call_order.append("db_close")

        def fake_embed(text):
            call_order.append("embed")
            return [0.1, 0.2, 0.3]

        monkeypatch.setattr(server, "db_conn", fake_db_conn)
        monkeypatch.setattr(dashboard, "db_conn", fake_db_conn)
        monkeypatch.setattr(server, "embed", fake_embed)
        monkeypatch.setattr(dashboard, "embed", fake_embed)

        dashboard.search_notes("test query", mode="hybrid")

        assert "embed" in call_order, "embed() was never called"
        embed_idx = call_order.index("embed")
        db_open_idx = call_order.index("db_open")
        assert embed_idx < db_open_idx, (
            f"embed() was called after db_conn was opened — pool starvation risk: {call_order}"
        )


# ── dashboard _get_vault_stats — archive exclusion and multi-vault ────────────

class TestDashboardVaultStats:
    """
    _get_vault_stats() must:
    - use _should_skip_path() (not a hand-rolled dotfile filter)
    - exclude archive/ by default
    - include archive/ when OBSIDIAN_IGNORE_PATHS=""
    - sum across all VAULT_PATHS in multi-vault mode
    """

    def _run(self, tmp_path, vault_paths, monkeypatch, env_override=None):
        import server
        import dashboard

        monkeypatch.setattr(server, "_VAULT_LIST", [str(p) for p in vault_paths])
        monkeypatch.setattr(dashboard, "VAULT_PATHS", [str(p) for p in vault_paths])
        monkeypatch.setattr(dashboard, "VAULT_PATH", str(vault_paths[0]) if vault_paths else "")

        if env_override is not None:
            monkeypatch.setenv("OBSIDIAN_IGNORE_PATHS", env_override)
        else:
            monkeypatch.delenv("OBSIDIAN_IGNORE_PATHS", raising=False)

        stats = {"indexed_count": 0}
        dashboard._get_vault_stats(stats)
        return stats

    def test_archive_excluded_by_default(self, tmp_path, monkeypatch):
        vault = tmp_path / "vault"
        (vault / "notes").mkdir(parents=True)
        (vault / "archive" / "old").mkdir(parents=True)
        (vault / "notes" / "live.md").write_text("# live")
        (vault / "archive" / "old" / "gone.md").write_text("# archived")

        stats = self._run(tmp_path, [vault], monkeypatch)
        assert stats["vault_file_count"] == 1, (
            f"archive/ should be excluded by default, got {stats['vault_file_count']}"
        )

    def test_archive_included_when_ignore_paths_empty(self, tmp_path, monkeypatch):
        vault = tmp_path / "vault"
        (vault / "notes").mkdir(parents=True)
        (vault / "archive").mkdir(parents=True)
        (vault / "notes" / "live.md").write_text("# live")
        (vault / "archive" / "gone.md").write_text("# archived")

        stats = self._run(tmp_path, [vault], monkeypatch, env_override="")
        assert stats["vault_file_count"] == 2, (
            f"OBSIDIAN_IGNORE_PATHS='' should include archive/, got {stats['vault_file_count']}"
        )

    def test_multi_vault_sums_all_vaults(self, tmp_path, monkeypatch):
        vault_a = tmp_path / "vault_a"
        vault_b = tmp_path / "vault_b"
        vault_a.mkdir()
        vault_b.mkdir()
        (vault_a / "a1.md").write_text("# a1")
        (vault_a / "a2.md").write_text("# a2")
        (vault_b / "b1.md").write_text("# b1")

        stats = self._run(tmp_path, [vault_a, vault_b], monkeypatch)
        assert stats["vault_file_count"] == 3, (
            f"multi-vault should sum all vaults, got {stats['vault_file_count']}"
        )

    def test_dotfile_dirs_still_excluded(self, tmp_path, monkeypatch):
        vault = tmp_path / "vault"
        (vault / ".obsidian").mkdir(parents=True)
        (vault / "notes").mkdir()
        (vault / ".obsidian" / "config.md").write_text("# internal")
        (vault / "notes" / "real.md").write_text("# real")

        stats = self._run(tmp_path, [vault], monkeypatch)
        assert stats["vault_file_count"] == 1, (
            f".obsidian/ must still be excluded, got {stats['vault_file_count']}"
        )

    def test_unindexed_count_excludes_archive(self, tmp_path, monkeypatch):
        vault = tmp_path / "vault"
        (vault / "notes").mkdir(parents=True)
        (vault / "archive").mkdir(parents=True)
        (vault / "notes" / "live.md").write_text("# live")
        (vault / "archive" / "old.md").write_text("# archived")

        # pretend 0 notes are indexed
        stats = self._run(tmp_path, [vault], monkeypatch)
        # vault_file_count = 1 (archive excluded), indexed_count = 0 → unindexed = 1
        assert stats["unindexed_count"] == 1


# ── dashboard _get_db_stats — recent-notes multi-vault relativization ─────────

class TestDashboardRecentNotesRelativization:
    """
    Recent notes paths must be rendered relative to the correct vault root.
    In multi-vault mode, a note from vault_b should not fail with a ValueError
    when relativized against vault_a — it should use vault_b's root instead.
    """

    def _make_fake_db_conn(self, rows_notes, rows_paths):
        from contextlib import contextmanager

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.__enter__ = lambda s: s
        mock_cur.__exit__ = MagicMock(return_value=False)

        call_count = {"n": 0}

        def side_effect_fetchone():
            call_count["n"] += 1
            n = call_count["n"]
            if n == 1:
                return ("PostgreSQL 16",)
            if n == 2:
                return ("0.7.0",)
            if n == 3:
                return (len(rows_notes), None, None)
            if n == 4:
                return (1024,)
            return None

        mock_cur.fetchone.side_effect = side_effect_fetchone
        mock_cur.fetchall.side_effect = [rows_notes, rows_paths]
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur

        @contextmanager
        def fake_db_conn():
            yield mock_conn

        return fake_db_conn

    def test_recent_note_relativized_to_correct_vault(self, tmp_path, monkeypatch):
        import server
        import dashboard

        vault_a = tmp_path / "vault_a"
        vault_b = tmp_path / "vault_b"
        vault_a.mkdir()
        vault_b.mkdir()

        note_in_b = str(vault_b / "note_b.md")

        from datetime import datetime, timezone
        ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        rows_notes = [(note_in_b, ts)]
        rows_paths = [(note_in_b,)]

        fake_db = self._make_fake_db_conn(rows_notes, rows_paths)

        monkeypatch.setattr(server, "_VAULT_LIST", [str(vault_a), str(vault_b)])
        monkeypatch.setattr(dashboard, "VAULT_PATHS", [str(vault_a), str(vault_b)])
        monkeypatch.setattr(dashboard, "VAULT_PATH", str(vault_a))
        monkeypatch.setattr(dashboard, "db_conn", fake_db)

        stats = {"recent_notes": []}
        dashboard._get_db_stats(stats)

        assert len(stats["recent_notes"]) == 1
        rendered_path = stats["recent_notes"][0]["path"]
        # Must be relative to vault_b, not fail with ValueError
        assert "note_b.md" in rendered_path
        # Must NOT contain the full tmp_path prefix (i.e. it was relativized)
        assert str(vault_b) not in rendered_path


# ── test_e2e.py — subprocess env validation ───────────────────────────────────

class TestE2eHarnessEnv:
    """
    _build_server_env() must:
    - Exit before spawning the server when no DB config is present
    - Pass DATABASE_URL through when set
    - Pass POSTGRES_PASSWORD through when set
    - Always include OBSIDIAN_VAULT
    """

    @pytest.fixture(autouse=True)
    def _insert_tests_dir(self):
        tests_dir = str(Path(__file__).parent)
        if tests_dir not in sys.path:
            sys.path.insert(0, tests_dir)

    def _import_build_server_env(self):
        # Import fresh each time so monkeypatching os.environ is respected
        import importlib
        import test_e2e
        importlib.reload(test_e2e)
        return test_e2e._build_server_env

    def test_exits_when_no_db_config(self, monkeypatch):
        """Harness must fail before spawning when neither DATABASE_URL nor POSTGRES_PASSWORD is set."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
        fn = self._import_build_server_env()
        with pytest.raises(SystemExit):
            fn("/vault")

    def test_passes_database_url_through(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/test_db")
        monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
        fn = self._import_build_server_env()
        env = fn("/vault")
        assert env.get("DATABASE_URL") == "postgresql://localhost/test_db"
        assert env.get("OBSIDIAN_VAULT") == "/vault"

    def test_passes_postgres_password_through(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setenv("POSTGRES_PASSWORD", "secret")
        fn = self._import_build_server_env()
        env = fn("/vault")
        assert env.get("POSTGRES_PASSWORD") == "secret"
        assert env.get("OBSIDIAN_VAULT") == "/vault"

    def test_vault_always_overrides_env(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/test_db")
        monkeypatch.setenv("OBSIDIAN_VAULT", "/original")
        fn = self._import_build_server_env()
        env = fn("/new_vault")
        assert env["OBSIDIAN_VAULT"] == "/new_vault"


# ── CI workflow — security tool pinning ───────────────────────────────────────

class TestCIWorkflowPinning:
    """
    Static assertion: shipguard must be declared as a pinned dev dependency in
    pyproject.toml so it is installed by `uv sync` and the pin is managed
    alongside all other deps rather than buried in the workflow.
    """

    @pytest.fixture(scope="class")
    def pyproject_text(self):
        p = Path(__file__).parent.parent / "pyproject.toml"
        assert p.exists(), f"pyproject.toml not found: {p}"
        return p.read_text()

    @pytest.fixture(scope="class")
    def workflow_text(self):
        wf = Path(__file__).parent.parent / ".github" / "workflows" / "tests.yml"
        assert wf.exists(), f"Workflow file not found: {wf}"
        return wf.read_text()

    def test_shipguard_in_dev_deps(self, pyproject_text):
        """shipguard must be declared in [dependency-groups] dev.
        The exact version is pinned in uv.lock; pyproject.toml carries the constraint."""
        assert "shipguard" in pyproject_text, (
            "shipguard must appear as a dev dependency in pyproject.toml — "
            "exact version is pinned via uv.lock, not the workflow"
        )

    def test_workflow_does_not_install_shipguard_separately(self, workflow_text):
        """CI must not install shipguard via a separate pip install step —
        uv sync handles it through pyproject.toml."""
        assert "pip install shipguard" not in workflow_text, (
            "Found 'pip install shipguard' in the workflow — "
            "remove it and manage the version via pyproject.toml dev dependencies"
        )


class TestDockerComposeOllamaModelPull:
    def test_ollama_pull_service_exists(self):
        compose = (Path(__file__).parent.parent / "docker-compose.yml").read_text()
        assert "ollama-pull:" in compose
        assert 'entrypoint: ["ollama", "pull", "nomic-embed-text"]' in compose
        assert "OLLAMA_HOST: http://ollama:11434" in compose


# ── index_vault failure tracking + retry ─────────────────────────────────────

def _fake_index_state_store():
    """In-memory stand-in for the index_state table (see iteration 6 of
    docs/PLAN-security-correctness.md), returned as a (set_fn, get_fn) pair
    with the same signatures as server.set_index_state/get_index_state.

    get_last_rebuild_failures() now reads from Postgres via
    server.get_index_state() instead of an in-process module-global list —
    without this fake, these tests would exercise the real DB-unreachable
    fallback path (an empty list, always) rather than the failure-tracking
    behavior they're named for.
    """
    store: dict[str, dict] = {}

    def fake_set(vault_id, status, *, failed_paths=None, error=None):
        store[vault_id] = {
            "vault_id": vault_id,
            "status": status,
            "failed_paths": list(failed_paths or []),
            "error": error,
        }

    def fake_get(vault_id=None):
        if vault_id is not None:
            return store.get(vault_id)
        return list(store.values())

    return fake_set, fake_get


class TestIndexVaultFailureTracking:
    """index_vault must retry transient embed failures once and surface
    persistent failures via get_last_rebuild_failures(), so a wedged Ollama
    can no longer silently drop notes from a full rebuild."""

    def test_retries_failed_paths_once_and_succeeds(self, tmp_path, monkeypatch):
        import server

        note = tmp_path / "notes" / "flaky.md"
        note.parent.mkdir(parents=True)
        note.write_text("# Flaky\nbody", encoding="utf-8")

        attempts: dict[str, int] = {}

        def flaky_embed(path, content, hash_, vault):
            attempts[path] = attempts.get(path, 0) + 1
            if attempts[path] == 1:
                raise RuntimeError("simulated ollama timeout")

        fake_set, fake_get = _fake_index_state_store()
        monkeypatch.setattr(server, "set_index_state", fake_set)
        monkeypatch.setattr(server, "get_index_state", fake_get)
        monkeypatch.setattr(server, "VAULT_PATH", str(tmp_path))
        monkeypatch.setattr(server, "_VAULT_LIST", [str(tmp_path)])
        monkeypatch.setattr(server, "_bulk_load_hashes", lambda paths: {})
        monkeypatch.setattr(server, "_embed_and_upsert", flaky_embed)
        # Force the batched path to always fall through to per-item _embed_and_upsert,
        # so this test exercises retry semantics independently of /api/embed availability.
        monkeypatch.setattr(server, "embed_batch", lambda texts: (_ for _ in ()).throw(RuntimeError("force fallback")))

        server.index_vault(str(tmp_path))

        assert attempts[str(note)] == 2, "first failure must trigger one retry"
        assert server.get_last_rebuild_failures() == [], (
            "successful retry must clear the failure list"
        )

    def test_persistent_failures_recorded(self, tmp_path, monkeypatch):
        import server

        note = tmp_path / "notes" / "broken.md"
        note.parent.mkdir(parents=True)
        note.write_text("# Broken\nbody", encoding="utf-8")

        def always_fail(path, content, hash_, vault):
            raise RuntimeError("ollama unreachable")

        fake_set, fake_get = _fake_index_state_store()
        monkeypatch.setattr(server, "set_index_state", fake_set)
        monkeypatch.setattr(server, "get_index_state", fake_get)
        monkeypatch.setattr(server, "VAULT_PATH", str(tmp_path))
        monkeypatch.setattr(server, "_VAULT_LIST", [str(tmp_path)])
        monkeypatch.setattr(server, "_bulk_load_hashes", lambda paths: {})
        monkeypatch.setattr(server, "_embed_and_upsert", always_fail)
        monkeypatch.setattr(server, "embed_batch", lambda texts: (_ for _ in ()).throw(RuntimeError("force fallback")))

        server.index_vault(str(tmp_path))

        assert str(note) in server.get_last_rebuild_failures(), (
            "paths that fail both attempts must be surfaced via "
            "get_last_rebuild_failures() so /api/stats can warn the operator"
        )


# ── batch embeddings ─────────────────────────────────────────────────────────

class TestEmbedBatch:
    """embed_batch must use Ollama's /api/embed endpoint with input=[...] so a
    full rebuild doesn't pay a per-note request round-trip. Falls back to
    single embed when the batch endpoint is unavailable so older Ollama
    versions keep working."""

    def test_batch_calls_api_embed_with_input_array(self, monkeypatch):
        import server

        captured: dict = {}

        class FakeResp:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                pass

            def json(self):
                return self._payload

        def fake_post(url, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            return FakeResp({"embeddings": [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]]})

        monkeypatch.setattr(server.requests, "post", fake_post)
        vecs = server.embed_batch(["a", "b", "c"])

        assert captured["url"].endswith("/api/embed"), (
            "batch path must hit /api/embed, not /api/embeddings (singular)"
        )
        assert captured["json"]["input"] == ["a", "b", "c"], (
            "input must be the list, not concatenated"
        )
        assert vecs == [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]]

    def test_batch_returns_vectors_in_input_order(self, monkeypatch):
        import server

        class FakeResp:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                pass

            def json(self):
                return self._payload

        monkeypatch.setattr(
            server.requests, "post",
            lambda url, json=None, timeout=None: FakeResp(
                {"embeddings": [[float(i)] for i in range(len(json["input"]))]}
            ),
        )
        vecs = server.embed_batch(["x", "y", "z"])
        assert vecs == [[0.0], [1.0], [2.0]], "order must match input"

    def test_index_vault_uses_batch_path(self, tmp_path, monkeypatch):
        """index_vault should call embed_batch with chunks of files, not embed
        once per file."""
        import server

        for i in range(5):
            p = tmp_path / "notes" / f"n{i}.md"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"# Note {i}\nbody", encoding="utf-8")

        batch_calls: list[int] = []
        single_calls: list[str] = []

        def fake_batch(texts):
            batch_calls.append(len(texts))
            return [[0.1, 0.2] for _ in texts]

        def fake_single(text):
            single_calls.append(text[:10])
            return [0.1, 0.2]

        # Stub upsert SQL — we only care about call shape
        upserts: list[str] = []

        def fake_upsert_one(path, content, h, vec, vault):
            upserts.append(path)

        monkeypatch.setattr(server, "VAULT_PATH", str(tmp_path))
        monkeypatch.setattr(server, "_VAULT_LIST", [str(tmp_path)])
        monkeypatch.setattr(server, "_bulk_load_hashes", lambda paths: {})
        monkeypatch.setattr(server, "embed_batch", fake_batch)
        monkeypatch.setattr(server, "embed", fake_single)
        monkeypatch.setattr(server, "_upsert_note", fake_upsert_one)

        server.index_vault(str(tmp_path))

        total_batched = sum(batch_calls)
        assert total_batched == 5, (
            f"all 5 files must go through embed_batch, got {batch_calls=} {single_calls=}"
        )
        assert len(upserts) == 5


# ── orphan prune ─────────────────────────────────────────────────────────────

class TestPruneOrphans:
    """prune_orphans() deletes DB rows whose path no longer exists on disk —
    eliminates the slow drift between indexed_count and vault_file_count
    that builds up when files are deleted, vault paths change, or
    OBSIDIAN_IGNORE_PATHS is updated."""

    def test_prune_deletes_only_missing_paths(self, tmp_path, monkeypatch):
        import server

        live = tmp_path / "live.md"
        live.write_text("# live", encoding="utf-8")
        gone = tmp_path / "gone.md"  # never created on disk

        deleted: list[str] = []
        rows_in_db = [(str(live),), (str(gone),)]

        class FakeCursor:
            def __init__(self):
                self._next = None
                self.executes = []

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=None):
                self.executes.append((sql, params))
                if "SELECT path" in sql:
                    self._next = list(rows_in_db)
                elif "DELETE" in sql:
                    if params and isinstance(params[0], list):
                        deleted.extend(params[0])

            def fetchall(self):
                return self._next or []

        class FakeConn:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def cursor(self):
                return FakeCursor()

        from contextlib import contextmanager

        @contextmanager
        def fake_db_conn():
            yield FakeConn()

        monkeypatch.setattr(server, "db_conn", fake_db_conn)

        n = server.prune_orphans()
        assert str(gone) in deleted, "missing file must be deleted from DB"
        assert str(live) not in deleted, "live file must be kept"
        assert n == 1, f"prune must return count of deleted rows, got {n}"

    def test_prune_no_op_when_all_paths_exist(self, tmp_path, monkeypatch):
        import server

        a = tmp_path / "a.md"
        b = tmp_path / "b.md"
        a.write_text("a", encoding="utf-8")
        b.write_text("b", encoding="utf-8")

        deleted: list[str] = []

        class FakeCursor:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=None):
                if "SELECT path" in sql:
                    self._rows = [(str(a),), (str(b),)]
                elif "DELETE" in sql and params and isinstance(params[0], list):
                    deleted.extend(params[0])

            def fetchall(self):
                return getattr(self, "_rows", [])

        class FakeConn:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def cursor(self):
                return FakeCursor()

        from contextlib import contextmanager

        @contextmanager
        def fake_db_conn():
            yield FakeConn()

        monkeypatch.setattr(server, "db_conn", fake_db_conn)

        n = server.prune_orphans()
        assert deleted == []
        assert n == 0



# ── extract_wikilinks ─────────────────────────────────────────────────────────

class TestExtractWikilinks:
    def test_simple_link(self):
        import server
        assert server.extract_wikilinks("See [[Resilience Patterns]] for details.") == ["Resilience Patterns"]

    def test_alias_link(self):
        import server
        assert server.extract_wikilinks("[[Resilience Patterns|resilience]]") == ["Resilience Patterns"]

    def test_heading_link(self):
        import server
        # [[note#heading]] — the regex strips the heading fragment; only the note name is captured
        assert server.extract_wikilinks("See [[Resilience Patterns#intro]]") == ["Resilience Patterns"]

    def test_folder_link(self):
        import server
        assert server.extract_wikilinks("[[concepts/Resilience Patterns]]") == ["concepts/Resilience Patterns"]

    def test_multiple_links_deduplicated(self):
        import server
        content = "[[A]] and [[B]] and [[A]] again"
        assert server.extract_wikilinks(content) == ["A", "B"]

    def test_no_links(self):
        import server
        assert server.extract_wikilinks("Plain text with no links.") == []

    def test_empty_string(self):
        import server
        assert server.extract_wikilinks("") == []


# ── _build_link_index ─────────────────────────────────────────────────────────

class TestBuildLinkIndex:
    def test_indexes_md_files(self, tmp_path):
        (tmp_path / "Alpha.md").write_text("# Alpha")
        (tmp_path / "beta note.md").write_text("# Beta")

        import server as srv
        import unittest.mock as mock
        with mock.patch.object(srv, "_should_skip_path", return_value=False):
            idx = srv._build_link_index(str(tmp_path))

        assert "alpha" in idx
        assert "beta note" in idx

    def test_skipped_paths_excluded(self, tmp_path, monkeypatch):
        import server
        (tmp_path / ".obsidian").mkdir()
        (tmp_path / ".obsidian" / "hidden.md").write_text("# hidden")
        (tmp_path / "visible.md").write_text("# visible")

        # _should_skip_path needs _VAULT_LIST to know the vault root
        monkeypatch.setattr(server, "_VAULT_LIST", [str(tmp_path)])
        idx = server._build_link_index(str(tmp_path))
        assert "visible" in idx
        assert "hidden" not in idx


# ── _resolve_links ────────────────────────────────────────────────────────────

class TestResolveLinks:
    def test_resolves_known_link(self):
        import server
        idx = {"resilience patterns": "/vault/notes/Resilience Patterns.md"}
        result = server._resolve_links(["Resilience Patterns"], idx)
        assert result["Resilience Patterns"] == "/vault/notes/Resilience Patterns.md"

    def test_unknown_link_is_none(self):
        import server
        result = server._resolve_links(["Unknown Note"], {})
        assert result["Unknown Note"] is None

    def test_folder_style_uses_stem(self):
        import server
        idx = {"resilience patterns": "/vault/concepts/Resilience Patterns.md"}
        result = server._resolve_links(["concepts/Resilience Patterns"], idx)
        assert result["concepts/Resilience Patterns"] == "/vault/concepts/Resilience Patterns.md"

    def test_case_insensitive(self):
        import server
        idx = {"resilience patterns": "/vault/Resilience Patterns.md"}
        result = server._resolve_links(["resilience patterns"], idx)
        assert result["resilience patterns"] == "/vault/Resilience Patterns.md"


# ── expand_via_links ──────────────────────────────────────────────────────────

class TestExpandViaLinks:
    def _make_db(self, out_rows=None, in_rows=None):
        """Return a fake db_conn that returns (out_rows, in_rows) for the two queries."""
        from contextlib import contextmanager

        out_rows = out_rows or []
        in_rows = in_rows or []
        call_count = [0]

        class FakeCursor:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, *a): pass
            def fetchall(self_):
                idx = call_count[0]
                call_count[0] += 1
                return out_rows if idx == 0 else in_rows

        class FakeConn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def cursor(self): return FakeCursor()

        @contextmanager
        def fake_db_conn():
            yield FakeConn()

        return fake_db_conn

    def test_returns_outgoing_neighbors(self, monkeypatch):
        import server
        fake = self._make_db(
            out_rows=[("/vault/B.md", "content B", "/vault/A.md")],
        )
        monkeypatch.setattr(server, "db_conn", fake)
        result = server.expand_via_links(["/vault/A.md"], hops=1)
        assert len(result) == 1
        assert result[0][0] == "/vault/B.md"

    def test_returns_incoming_neighbors(self, monkeypatch):
        import server
        fake = self._make_db(
            in_rows=[("/vault/C.md", "content C", "/vault/A.md")],
        )
        monkeypatch.setattr(server, "db_conn", fake)
        result = server.expand_via_links(["/vault/A.md"], hops=1)
        assert len(result) == 1
        assert result[0][0] == "/vault/C.md"

    def test_excludes_seed_paths(self, monkeypatch):
        import server
        fake = self._make_db(
            out_rows=[("/vault/A.md", "content A", "/vault/A.md")],
        )
        monkeypatch.setattr(server, "db_conn", fake)
        result = server.expand_via_links(["/vault/A.md"], hops=1)
        assert result == []

    def test_no_neighbors_returns_empty(self, monkeypatch):
        import server
        fake = self._make_db()
        monkeypatch.setattr(server, "db_conn", fake)
        result = server.expand_via_links(["/vault/A.md"], hops=1)
        assert result == []


# ── _ensure_frontmatter ───────────────────────────────────────────────────────

class TestEnsureFrontmatter:
    def test_no_existing_frontmatter_adds_all_required_keys(self):
        import server
        import yaml

        result = server._ensure_frontmatter("Just a plain note body.\n")
        assert result.startswith("---\n")
        fm_end = result.find("\n---\n", 4)
        fm = yaml.safe_load(result[4:fm_end])
        for key in ("created", "updated", "aliases", "tags", "category", "session", "nas-path", "related"):
            assert key in fm
        assert fm["aliases"] == []
        assert fm["related"] == []
        assert "Just a plain note body." in result

    def test_preserves_created_and_existing_values(self):
        import server
        import yaml
        from datetime import date

        existing = (
            "---\n"
            "perthor_synced: true\n"
            "source: perthor/docs/audits/AUDIT-x.md\n"
            "branch: maxime_dev\n"
            "tags: [perthor, auto-synced]\n"
            "category: Market Analysis\n"
            "created: 2026-01-01\n"
            "---\n"
            "Body content here.\n"
        )
        result = server._ensure_frontmatter(existing)
        fm_end = result.find("\n---\n", 4)
        fm = yaml.safe_load(result[4:fm_end])

        # created is preserved, not overwritten
        assert fm["created"] == date(2026, 1, 1)
        # already-set values are never silently replaced
        assert fm["category"] == "Market Analysis"
        assert fm["tags"] == ["perthor", "auto-synced"]
        # project-specific extra keys survive untouched
        assert fm["perthor_synced"] is True
        assert fm["source"] == "perthor/docs/audits/AUDIT-x.md"
        assert fm["branch"] == "maxime_dev"
        # missing required keys get filled in with defaults
        assert fm["aliases"] == []
        assert fm["related"] == []
        assert fm["session"] == ""
        assert fm["nas-path"] == ""
        assert "Body content here." in result

    def test_updated_always_refreshes(self):
        import server
        import yaml
        from datetime import date

        existing = (
            "---\n"
            "created: 2026-01-01\n"
            "updated: 2020-01-01\n"
            "---\n"
            "Body.\n"
        )
        result = server._ensure_frontmatter(existing)
        fm_end = result.find("\n---\n", 4)
        fm = yaml.safe_load(result[4:fm_end])
        assert fm["updated"] == date.today()
        assert fm["updated"] != date(2020, 1, 1)

    def test_created_and_updated_are_unquoted_date_scalars(self):
        """Obsidian's Properties panel only shows the date-picker UI for
        real YAML date scalars, not quoted strings -- regression guard."""
        import server

        result = server._ensure_frontmatter("Body.\n")
        fm_end = result.find("\n---\n", 4)
        fm_block = result[4:fm_end]
        for line in fm_block.splitlines():
            if line.startswith("created:") or line.startswith("updated:"):
                assert "'" not in line and '"' not in line


# ── Perf / stress: bulk vault indexing overhead ──────────────────────────────

class TestIndexVaultPerf:
    """Throughput test for index_vault()'s own orchestration (file-walk, hash
    lookup, link-index build) at a vault size representative of real usage.

    embed()/DB writes are mocked out (_run_embed_pass replaced with a fast
    no-op, _bulk_load_hashes forced to report everything unchanged) so this
    isolates the CPU-bound work index_vault does per file from network/DB
    latency, which would otherwise dominate and make the timing assertion
    meaningless. A regression that makes file-walk/hash/link-build scale
    worse than roughly linear (e.g. an accidentally-quadratic wikilink
    resolution) would blow well past this bound even with I/O removed."""

    @pytest.mark.perf
    def test_index_vault_scales_linearly_with_note_count(self, tmp_path, monkeypatch):
        import time
        import server

        note_count = 300
        for i in range(note_count):
            # Every 5th note links to the previous one, so _build_link_index
            # + _resolve_links do real (if small) wikilink-resolution work
            # rather than a degenerate no-links case.
            body = f"# Note {i}\n"
            if i % 5 == 0 and i > 0:
                body += f"See [[note-{i - 1}]] for context.\n"
            (tmp_path / f"note-{i}.md").write_text(body, encoding="utf-8")

        monkeypatch.setenv("OBSIDIAN_IGNORE_PATHS", "")
        monkeypatch.setattr(server, "VAULT_PATH", str(tmp_path))
        monkeypatch.setattr(server, "_VAULT_LIST", [str(tmp_path)])
        monkeypatch.setattr(server, "_bulk_load_hashes", lambda paths: {})
        monkeypatch.setattr(server, "_run_embed_pass", lambda batch, vault: [])

        start = time.perf_counter()
        server.index_vault(str(tmp_path))
        elapsed = time.perf_counter() - start

        # Generous bound for CI runner variance — this is a regression guard
        # against a scaling blowup, not a strict latency SLA (none is claimed
        # anywhere in README/docs).
        assert elapsed < 10.0, (
            f"index_vault() took {elapsed:.2f}s for {note_count} notes with "
            f"embed/DB mocked out — expected well under 10s for file-walk + "
            f"hash + link-index overhead alone"
        )
