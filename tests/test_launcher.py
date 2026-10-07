"""
tests/test_launcher.py — unit tests for src/launcher.py

All Docker and server calls are mocked — no real Docker or Postgres required.
"""
import sys
import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def isolate_runtime(monkeypatch, tmp_path):
    from src import launcher
    monkeypatch.setattr(launcher, "OSM_CONFIG_DIR", tmp_path / "osm-config")



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

# ---------------------------------------------------------------------------
# 8. Native MCP entries load private OSM runtime settings before validation.
# ---------------------------------------------------------------------------

def test_native_entry_env_is_launchable(monkeypatch, tmp_path):
    import osm_init
    from src import launcher

    entry = osm_init._native_entry(["/path/to/vault"], "postgresql://localhost/obsidian_brain")

    monkeypatch.setattr("os.environ", entry["env"])
    launcher.write_native_runtime(["/path/to/vault"], "postgresql://localhost/obsidian_brain")
    launcher._load_native_runtime()
    launcher._validate_env()  # must not raise/exit
    assert os.environ["OSM_DOCKER"] == "0"
    assert (launcher.OSM_CONFIG_DIR / "native_runtime.json").stat().st_mode & 0o777 == 0o600


def test_native_entry_multi_vault_uses_obsidian_vaults():
    import osm_init
    from src import launcher

    entry = osm_init._native_entry(
        ["/path/a", "/path/b"], "postgresql://localhost/obsidian_brain"
    )

    assert entry["env"] == {}
    launcher.write_native_runtime(["/path/a", "/path/b"], "postgresql://localhost/obsidian_brain")
    env = json.loads((launcher.OSM_CONFIG_DIR / "native_runtime.json").read_text())["env"]
    assert env["OBSIDIAN_VAULTS"] == "/path/a,/path/b"
    assert "OBSIDIAN_VAULT" not in env


def test_native_entry_single_vault_uses_obsidian_vault():
    import osm_init
    from src import launcher

    entry = osm_init._native_entry(["/path/a"], "postgresql://localhost/obsidian_brain")

    assert entry["env"] == {}
    launcher.write_native_runtime(["/path/a"], "postgresql://localhost/obsidian_brain")
    env = json.loads((launcher.OSM_CONFIG_DIR / "native_runtime.json").read_text())["env"]
    assert env["OBSIDIAN_VAULT"] == "/path/a"
    assert "OBSIDIAN_VAULTS" not in env


def test_native_runtime_skips_recorded_docker_root(monkeypatch):
    from src import launcher
    launcher.write_native_runtime(["/synthetic/vault"], "postgresql://localhost/synthetic")
    monkeypatch.setattr(os, "environ", {})
    with patch.object(launcher, "_project_root", side_effect=AssertionError("must not inspect checkout")), patch.object(launcher, "_run_server") as server:
        launcher.main()
        server.assert_called_once()


def test_explicit_native_mode_without_private_runtime_loads_project_dotenv(monkeypatch, tmp_path):
    from src import launcher
    (tmp_path / ".env").write_text(
        "OBSIDIAN_VAULT=/legacy/vault\nDATABASE_URL=postgresql://localhost/legacy\n"
    )
    monkeypatch.setattr(os, "environ", {"OSM_DOCKER": "0"})
    with patch.object(launcher, "_project_root", return_value=tmp_path), patch.object(launcher, "_run_server") as server:
        launcher.main()
        server.assert_called_once()
    assert os.environ["OBSIDIAN_VAULT"] == "/legacy/vault"
    assert os.environ["DATABASE_URL"] == "postgresql://localhost/legacy"


def test_private_native_runtime_excludes_discovered_docker_dotenv(monkeypatch, tmp_path):
    from src import launcher
    launcher.write_native_runtime(["/native/vault"], "postgresql://localhost/native")
    (tmp_path / ".env").write_text("OLLAMA_URL=http://synthetic-docker.invalid:11434\n")
    monkeypatch.setattr(os, "environ", {})
    with patch.object(launcher, "_project_root", return_value=tmp_path) as root, patch.object(launcher, "_run_server") as server:
        launcher.main()
        root.assert_not_called()
        server.assert_called_once()
    assert "OLLAMA_URL" not in os.environ


def test_explicit_environment_wins_over_private_runtime(monkeypatch):
    from src import launcher
    launcher.write_native_runtime(["/file/vault"], "postgresql://localhost/file")
    monkeypatch.setattr(os, "environ", {"OBSIDIAN_VAULT": "/explicit/vault", "DATABASE_URL": "postgresql://localhost/explicit", "OSM_DOCKER": "1"})
    launcher._load_native_runtime()
    assert os.environ == {"OBSIDIAN_VAULT": "/explicit/vault", "DATABASE_URL": "postgresql://localhost/explicit", "OSM_DOCKER": "1"}


def test_missing_native_runtime_is_optional(monkeypatch):
    from src import launcher
    monkeypatch.setattr(os, "environ", {"OBSIDIAN_VAULT": "/explicit/vault"})
    launcher._load_native_runtime()
    assert os.environ == {"OBSIDIAN_VAULT": "/explicit/vault"}


@pytest.mark.parametrize("payload", ["{invalid", "[]", '{"version":1,"env":{"DATABASE_URL":"synthetic-private"}}', '{"version":1,"env":{"PYTHONPATH":"synthetic-private"}}'])
def test_invalid_private_runtime_fails_safely_without_overwriting(monkeypatch, capsys, payload):
    from src import launcher
    launcher.OSM_CONFIG_DIR.mkdir(exist_ok=True)
    path = launcher.OSM_CONFIG_DIR / "native_runtime.json"
    path.write_text(payload)
    path.chmod(0o600)
    with pytest.raises(SystemExit):
        launcher.main()
    assert "synthetic-private" not in capsys.readouterr().err
    with pytest.raises(ValueError):
        launcher.write_native_runtime(["/vault"], "postgresql://localhost/db")
    assert path.read_text() == payload


@pytest.mark.parametrize("unsafe", ["mode", "symlink", "hardlink"])
def test_private_runtime_rejects_unsafe_file(monkeypatch, tmp_path, unsafe):
    from src import launcher
    launcher.write_native_runtime(["/vault"], "postgresql://localhost/db")
    path = launcher.OSM_CONFIG_DIR / "native_runtime.json"
    if unsafe == "mode":
        path.chmod(0o644)
    elif unsafe == "hardlink":
        os.link(path, tmp_path / "second-link")
    else:
        original = tmp_path / "original.json"
        path.rename(original)
        path.symlink_to(original)
    with pytest.raises(ValueError):
        launcher._load_native_runtime()


def test_docker_entry_overrides_retained_native_runtime(monkeypatch, tmp_path):
    import osm_init
    from src import launcher
    launcher.write_native_runtime(["/native/vault"], "postgresql://localhost/native")
    monkeypatch.setattr(os, "environ", osm_init._docker_entry()["env"])
    with patch.object(launcher, "_project_root", return_value=tmp_path), patch.object(launcher, "_docker_info_ok", return_value=True), patch.object(launcher, "_container_id", return_value="synthetic-container"), patch.object(launcher, "_exec_into_container") as execute, patch.object(launcher, "_run_server") as server:
        launcher.main()
        execute.assert_called_once_with(tmp_path)
        server.assert_not_called()


def test_explicit_docker_mode_uses_project_env_without_native_hydration(monkeypatch, tmp_path):
    from src import launcher
    launcher.write_native_runtime(["/native/vault"], "postgresql://localhost/native")
    (tmp_path / ".env").write_text(
        "OBSIDIAN_VAULT=/docker/vault\nDATABASE_URL=postgresql://localhost/docker\n"
    )
    monkeypatch.setattr(os, "environ", {"OSM_DOCKER": "1", "OSM_DOCKER_WAIT": "0"})
    with patch.object(launcher, "_project_root", return_value=tmp_path), patch.object(launcher, "_run_server") as server:
        launcher.main()
        server.assert_called_once()
    assert os.environ["OBSIDIAN_VAULT"] == "/docker/vault"
    assert os.environ["DATABASE_URL"] == "postgresql://localhost/docker"


def test_explicit_docker_mode_does_not_read_invalid_native_runtime(monkeypatch):
    from src import launcher
    (launcher.OSM_CONFIG_DIR / "native_runtime.json").write_text("malformed")
    monkeypatch.setattr(os, "environ", {"OSM_DOCKER": "1"})
    launcher._load_native_runtime()
    assert os.environ == {"OSM_DOCKER": "1"}


def test_native_runtime_remove_preserves_unrelated_configuration():
    from src import launcher
    launcher.write_native_runtime(["/vault"], "postgresql://localhost/db")
    unrelated = launcher.OSM_CONFIG_DIR / "unrelated.json"
    unrelated.write_text("unchanged")
    launcher.remove_native_runtime()
    assert not (launcher.OSM_CONFIG_DIR / "native_runtime.json").exists()
    assert unrelated.read_text() == "unchanged"


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
