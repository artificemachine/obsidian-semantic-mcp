"""
tests/test_launcher.py — unit tests for src/launcher.py

All Docker and server calls are mocked — no real Docker or Postgres required.
"""
import os
import sys
import subprocess
import importlib
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.launcher  # ensure module is importable before tests run


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ps_result(running: bool) -> MagicMock:
    r = MagicMock()
    r.returncode = 0
    r.stdout = b"abc123\n" if running else b""
    return r


def _base_env(tmp_path, **extra):
    """Minimal valid env for the launcher fallback path."""
    return {
        "OBSIDIAN_VAULT": str(tmp_path / "vault"),
        "POSTGRES_PASSWORD": "secret",
        **extra,
    }


# ---------------------------------------------------------------------------
# 1. Docker daemon absent → run_server() called directly, no exec
# ---------------------------------------------------------------------------

def test_docker_absent_runs_server_directly(tmp_path, monkeypatch):
    monkeypatch.setenv("OBSIDIAN_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret")
    monkeypatch.delenv("OSM_DOCKER", raising=False)

    docker_info_fail = MagicMock(side_effect=FileNotFoundError)

    with patch("subprocess.run", docker_info_fail), \
         patch("src.launcher._run_server") as mock_srv:
        from src import launcher
        launcher.main()
        mock_srv.assert_called_once()


# ---------------------------------------------------------------------------
# 2. OSM_DOCKER=1 + container already running → os.execvp with correct args
# ---------------------------------------------------------------------------

def test_docker_mode_container_running_execs(tmp_path, monkeypatch):
    monkeypatch.setenv("OBSIDIAN_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret")
    monkeypatch.setenv("OSM_DOCKER", "1")
    monkeypatch.setenv("OSM_PROJECT_ROOT", str(tmp_path))

    def fake_run(cmd, **kw):
        if "info" in cmd:
            return MagicMock(returncode=0)
        if "ps" in cmd:
            return _make_ps_result(running=True)
        return MagicMock(returncode=0)

    with patch("subprocess.run", side_effect=fake_run), \
         patch("os.execvp") as mock_exec:
        from src import launcher
        launcher.main()

        mock_exec.assert_called_once()
        args = mock_exec.call_args[0]
        assert args[0] == "docker"
        assert "exec" in args[1]
        assert "-T" in args[1]
        assert "mcp-server" in args[1]


# ---------------------------------------------------------------------------
# 3. OSM_DOCKER=1, container starting → polls, then exec once running
# ---------------------------------------------------------------------------

def test_docker_mode_polls_until_running(tmp_path, monkeypatch):
    monkeypatch.setenv("OBSIDIAN_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret")
    monkeypatch.setenv("OSM_DOCKER", "1")
    monkeypatch.setenv("OSM_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("OSM_DOCKER_WAIT", "5")

    call_count = {"ps": 0}

    def fake_run(cmd, **kw):
        if "info" in cmd:
            return MagicMock(returncode=0)
        if "ps" in cmd:
            call_count["ps"] += 1
            return _make_ps_result(running=call_count["ps"] >= 3)
        return MagicMock(returncode=0)

    with patch("subprocess.run", side_effect=fake_run), \
         patch("time.sleep"), \
         patch("os.execvp") as mock_exec:
        from src import launcher
        launcher.main()

        assert call_count["ps"] == 3
        mock_exec.assert_called_once()


# ---------------------------------------------------------------------------
# 4. OSM_DOCKER=1, container never starts (timeout) → falls through to run_server
# ---------------------------------------------------------------------------

def test_docker_mode_timeout_falls_through(tmp_path, monkeypatch):
    monkeypatch.setenv("OBSIDIAN_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret")
    monkeypatch.setenv("OSM_DOCKER", "1")
    monkeypatch.setenv("OSM_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("OSM_DOCKER_WAIT", "3")

    def fake_run(cmd, **kw):
        if "info" in cmd:
            return MagicMock(returncode=0)
        if "ps" in cmd:
            return _make_ps_result(running=False)
        return MagicMock(returncode=0)

    with patch("subprocess.run", side_effect=fake_run), \
         patch("time.sleep"), \
         patch("os.execvp") as mock_exec, \
         patch("src.launcher._run_server") as mock_srv:
        from src import launcher
        launcher.main()

        mock_exec.assert_not_called()
        mock_srv.assert_called_once()


# ---------------------------------------------------------------------------
# 5. Missing OBSIDIAN_VAULT → sys.exit(1)
# ---------------------------------------------------------------------------

def test_missing_vault_exits(tmp_path, monkeypatch):
    monkeypatch.delenv("OBSIDIAN_VAULT", raising=False)
    monkeypatch.delenv("OBSIDIAN_VAULTS", raising=False)
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret")
    monkeypatch.delenv("OSM_DOCKER", raising=False)

    # Patch _project_root to None so the launcher doesn't load_dotenv() from the
    # repo's .env file (which would re-inject POSTGRES_PASSWORD and bypass the
    # validation we're trying to exercise).
    with patch("subprocess.run", side_effect=FileNotFoundError), \
         patch("src.launcher._project_root", return_value=None), \
         pytest.raises(SystemExit) as exc_info:
        from src import launcher
        launcher.main()

    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# 6. Missing DATABASE_URL and POSTGRES_PASSWORD → sys.exit(1)
# ---------------------------------------------------------------------------

def test_missing_db_config_exits(tmp_path, monkeypatch):
    monkeypatch.setenv("OBSIDIAN_VAULT", str(tmp_path / "vault"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.delenv("OSM_DOCKER", raising=False)

    # Same isolation rationale as test_missing_vault_exits above: skip the
    # .env-file load so the test's deleted env vars stay deleted.
    with patch("subprocess.run", side_effect=FileNotFoundError), \
         patch("src.launcher._project_root", return_value=None), \
         pytest.raises(SystemExit) as exc_info:
        from src import launcher
        launcher.main()

    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# 7. OSM_DOCKER_WAIT=0 → no polling, immediate fallback
# ---------------------------------------------------------------------------

def test_docker_wait_zero_skips_polling(tmp_path, monkeypatch):
    monkeypatch.setenv("OBSIDIAN_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret")
    monkeypatch.setenv("OSM_DOCKER", "1")
    monkeypatch.setenv("OSM_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("OSM_DOCKER_WAIT", "0")

    call_count = {"ps": 0}

    def fake_run(cmd, **kw):
        if "info" in cmd:
            return MagicMock(returncode=0)
        if "ps" in cmd:
            call_count["ps"] += 1
            return _make_ps_result(running=False)
        return MagicMock(returncode=0)

    with patch("subprocess.run", side_effect=fake_run), \
         patch("time.sleep"), \
         patch("os.execvp") as mock_exec, \
         patch("src.launcher._run_server") as mock_srv:
        from src import launcher
        launcher.main()

        assert call_count["ps"] == 0
        mock_exec.assert_not_called()
        mock_srv.assert_called_once()
