"""
Tests for osm_init.py commands and every user-facing decision path.

Coverage:
  - run() helper (dry-run vs live)
  - check_docker / check_compose / check_ollama_at
  - open_ssh_tunnel (success / failure / dry-run, with/without key)
  - _claude_cfg_path (Darwin / Linux / other)
  - _docker_entry / _native_entry
  - update_claude_config (new file / merge / bad JSON / dry-run / None path)
  - prompt_vault (from --vault param / env var / interactive retry)
  - prompt_persistent_storage (y/n, with/without ollama, dry-run, default data-dir)
  - wait_for_postgres (immediate / timeout / dry-run)
  - prompt_ssh_credentials (--ssh-key param / auth choice 1 key / auth choice 2 agent)
  - _prompt_vault_location (--vault / --vault-remote / interactive 1 or 2 / sshfs flows)
  - cmd_help
  - cmd_status (docker output / ollama / claude config states)
  - cmd_tunnel (success / failure / missing config / uses key from .env)
  - cmd_rebuild
  - cmd_remove (abort / dry-run / .env / claude config edge cases)
  - cmd_init (macOS modes 1-4 / Linux modes 1-3 / unsupported platform)
  - mode_full_docker (happy path / docker fail / ollama URL / pgdata forwarding)
  - mode_docker_host_ollama (ollama down / macOS vs Linux URL / service list)
  - mode_docker_remote_ollama (tunnel fail+continue / SSH params in .env / OS URL)
  - mode_native_macos (brew missing / uv missing / psql present / DB present / ollama down)
  - _done_dry_run (with actions / empty / hint)
"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import osm_init  # noqa: E402
from conftest import _reset  # noqa: E402


def _docker_mode_defaults(monkeypatch, tmp_path):
    """Patch the 8 functions shared by all Docker-mode happy-path tests.

    Callers can override individual patches after calling this helper.
    Sets vault and pg_password in _PARAMS so the real prompt_* functions
    short-circuit without touching stdin.
    """
    monkeypatch.setattr(osm_init, "check_docker", lambda: True)
    monkeypatch.setattr(osm_init, "check_compose", lambda: True)
    osm_init._PARAMS["vault"] = str(tmp_path)
    osm_init._PARAMS["pg_password"] = "pw"
    monkeypatch.setattr(osm_init, "prompt_persistent_storage", lambda **kw: (None, None))
    monkeypatch.setattr(osm_init, "write_env", lambda *a, **kw: None)
    monkeypatch.setattr(osm_init, "compose_up", lambda *a, **kw: None)
    monkeypatch.setattr(osm_init, "wait_for_postgres", lambda **kw: True)
    monkeypatch.setattr(osm_init, "update_claude_config", lambda *a, **kw: None)


@pytest.fixture(autouse=True)
def reset_state():
    _reset()
    yield
    _reset()


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


# ── run() helper ──────────────────────────────────────────────────────────────

class TestRunHelper:
    def test_dry_run_does_not_execute(self, monkeypatch):
        osm_init.DRY_RUN = True
        called = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: called.append(True))
        result = osm_init.run(["ls"])
        assert result.returncode == 0
        assert not called

    def test_dry_run_records_command(self):
        osm_init.DRY_RUN = True
        osm_init.run(["docker", "compose", "up"])
        assert any("docker" in a for a in osm_init._DRY_ACTIONS)

    def test_live_run_delegates_to_subprocess(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            subprocess, "run",
            lambda cmd, **kw: seen.append(cmd) or _cp(0),
        )
        osm_init.run(["echo", "hi"])
        assert seen == [["echo", "hi"]]

    def test_shell_string_uses_shell_true(self, monkeypatch):
        kwargs_seen = {}
        monkeypatch.setattr(
            subprocess, "run",
            lambda cmd, **kw: kwargs_seen.update(kw) or _cp(0),
        )
        osm_init.run("echo hi")
        assert kwargs_seen.get("shell") is True


# ── check_docker ──────────────────────────────────────────────────────────────

class TestCheckDocker:
    def test_docker_not_installed(self, monkeypatch):
        monkeypatch.setattr(osm_init, "cmd_exists", lambda _: False)
        assert osm_init.check_docker() is False

    def test_daemon_not_running(self, monkeypatch):
        monkeypatch.setattr(osm_init, "cmd_exists", lambda _: True)
        monkeypatch.setattr(osm_init, "run", lambda *a, **kw: _cp(1))
        assert osm_init.check_docker() is False

    def test_docker_running(self, monkeypatch):
        monkeypatch.setattr(osm_init, "cmd_exists", lambda _: True)
        monkeypatch.setattr(osm_init, "run", lambda *a, **kw: _cp(0))
        assert osm_init.check_docker() is True


# ── check_compose ─────────────────────────────────────────────────────────────

class TestCheckCompose:
    def test_not_available(self, monkeypatch):
        monkeypatch.setattr(osm_init, "run", lambda *a, **kw: _cp(1))
        assert osm_init.check_compose() is False

    def test_available(self, monkeypatch):
        monkeypatch.setattr(osm_init, "run", lambda *a, **kw: _cp(0))
        assert osm_init.check_compose() is True


# ── check_ollama_at ───────────────────────────────────────────────────────────

class TestCheckOllamaAt:
    def test_reachable(self, monkeypatch):
        monkeypatch.setattr(osm_init.urllib.request, "urlopen", lambda *a, **kw: MagicMock())
        assert osm_init.check_ollama_at("localhost") is True

    def test_not_reachable(self, monkeypatch):
        def boom(*a, **kw):
            raise OSError("connection refused")
        monkeypatch.setattr(osm_init.urllib.request, "urlopen", boom)
        assert osm_init.check_ollama_at("localhost") is False

    def test_correct_url_formed(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            osm_init.urllib.request, "urlopen",
            lambda url, **kw: seen.append(url) or MagicMock(),
        )
        osm_init.check_ollama_at("myhost", 9999)
        assert seen[0] == "http://myhost:9999/api/tags"


# ── open_ssh_tunnel ───────────────────────────────────────────────────────────

class TestOpenSshTunnel:
    def test_dry_run_returns_true(self):
        osm_init.DRY_RUN = True
        assert osm_init.open_ssh_tunnel("u", "h", 11434, 11435) is True

    def test_dry_run_records_action(self):
        osm_init.DRY_RUN = True
        osm_init.open_ssh_tunnel("u", "h", 11434, 11435)
        assert any("ssh" in a for a in osm_init._DRY_ACTIONS)

    def test_dry_run_includes_key_in_action(self):
        osm_init.DRY_RUN = True
        osm_init.open_ssh_tunnel("u", "h", 11434, 11435, key_path="/my/key")
        assert any("/my/key" in a for a in osm_init._DRY_ACTIONS)

    def test_success(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _cp(0))
        assert osm_init.open_ssh_tunnel("u", "h", 11434, 11435) is True

    def test_failure(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _cp(1))
        assert osm_init.open_ssh_tunnel("u", "h", 11434, 11435) is False

    def test_key_path_added_to_cmd(self, monkeypatch):
        captured = []
        monkeypatch.setattr(
            subprocess, "run",
            lambda cmd, **kw: captured.append(cmd) or _cp(0),
        )
        osm_init.open_ssh_tunnel("u", "h", 11434, 11435, key_path="/k")
        assert "-i" in captured[0]
        assert "/k" in captured[0]

    def test_no_key_no_i_flag(self, monkeypatch):
        captured = []
        monkeypatch.setattr(
            subprocess, "run",
            lambda cmd, **kw: captured.append(cmd) or _cp(0),
        )
        osm_init.open_ssh_tunnel("u", "h", 11434, 11435)
        assert "-i" not in captured[0]


# ── _claude_cfg_path ──────────────────────────────────────────────────────────

class TestClaudeCfgPath:
    def test_darwin(self, monkeypatch):
        monkeypatch.setattr(osm_init.platform, "system", lambda: "Darwin")
        p = osm_init._claude_cfg_path()
        assert "Application Support" in str(p)
        assert p.name == "claude_desktop_config.json"

    def test_linux(self, monkeypatch):
        monkeypatch.setattr(osm_init.platform, "system", lambda: "Linux")
        p = osm_init._claude_cfg_path()
        assert ".config" in str(p)
        assert p.name == "claude_desktop_config.json"

    def test_windows(self, monkeypatch):
        monkeypatch.setattr(osm_init.platform, "system", lambda: "Windows")
        monkeypatch.setenv("APPDATA", "/tmp/fake_appdata")
        p = osm_init._claude_cfg_path()
        assert "fake_appdata" in str(p)
        assert p.name == "claude_desktop_config.json"

    def test_windows_no_appdata_returns_none(self, monkeypatch):
        monkeypatch.setattr(osm_init.platform, "system", lambda: "Windows")
        monkeypatch.delenv("APPDATA", raising=False)
        assert osm_init._claude_cfg_path() is None

    def test_other_returns_none(self, monkeypatch):
        monkeypatch.setattr(osm_init.platform, "system", lambda: "FreeBSD")
        assert osm_init._claude_cfg_path() is None


# ── _docker_entry / _native_entry ─────────────────────────────────────────────

class TestEntries:
    def test_docker_entry_command(self):
        assert osm_init._docker_entry()["command"] == "docker"

    def test_docker_entry_exec_and_server(self):
        args = osm_init._docker_entry()["args"]
        assert "exec" in args
        assert "python3" in args
        assert "src/server.py" in args

    def test_docker_entry_container_contains_project_name(self):
        # args[2] is the container name in: docker exec -i <container> python3 src/server.py
        container = osm_init._docker_entry()["args"][2]
        assert osm_init.PROJECT_ROOT.name in container

    def test_native_entry_command_is_venv_python(self):
        entry = osm_init._native_entry("/vault", "postgresql://localhost/db")
        assert "python3" in entry["command"]
        assert ".venv" in entry["command"]

    def test_native_entry_args_has_server(self):
        entry = osm_init._native_entry("/vault", "postgresql://localhost/db")
        assert any("server.py" in a for a in entry["args"])

    def test_native_entry_env_vars(self):
        entry = osm_init._native_entry("/vault", "postgresql://localhost/db")
        assert entry["env"]["OBSIDIAN_VAULT"] == "/vault"
        assert entry["env"]["DATABASE_URL"] == "postgresql://localhost/db"


# ── update_claude_config ──────────────────────────────────────────────────────

class TestUpdateClaudeConfig:
    def test_none_path_does_not_raise(self, monkeypatch):
        monkeypatch.setattr(osm_init, "_claude_cfg_path", lambda: None)
        osm_init.update_claude_config({"command": "docker"})  # must not raise

    def test_writes_new_file(self, tmp_path, monkeypatch):
        cfg = tmp_path / "cfg.json"
        monkeypatch.setattr(osm_init, "_claude_cfg_path", lambda: cfg)
        entry = {"command": "docker", "args": [], "env": {}}
        osm_init.update_claude_config(entry)
        assert json.loads(cfg.read_text())["mcpServers"]["obsidian-semantic"] == entry

    def test_merges_existing_servers(self, tmp_path, monkeypatch):
        cfg = tmp_path / "cfg.json"
        cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "foo"}}}))
        monkeypatch.setattr(osm_init, "_claude_cfg_path", lambda: cfg)
        osm_init.update_claude_config({"command": "docker"})
        data = json.loads(cfg.read_text())
        assert "other" in data["mcpServers"]
        assert "obsidian-semantic" in data["mcpServers"]

    def test_invalid_json_resets_and_writes(self, tmp_path, monkeypatch):
        cfg = tmp_path / "cfg.json"
        cfg.write_text("not json {")
        monkeypatch.setattr(osm_init, "_claude_cfg_path", lambda: cfg)
        entry = {"command": "docker"}
        osm_init.update_claude_config(entry)
        data = json.loads(cfg.read_text())
        assert data["mcpServers"]["obsidian-semantic"] == entry

    def test_dry_run_does_not_write(self, tmp_path, monkeypatch):
        osm_init.DRY_RUN = True
        cfg = tmp_path / "cfg.json"
        monkeypatch.setattr(osm_init, "_claude_cfg_path", lambda: cfg)
        osm_init.update_claude_config({"command": "docker"})
        assert not cfg.exists()

    def test_dry_run_records_action(self, tmp_path, monkeypatch):
        osm_init.DRY_RUN = True
        cfg = tmp_path / "cfg.json"
        monkeypatch.setattr(osm_init, "_claude_cfg_path", lambda: cfg)
        osm_init.update_claude_config({"command": "docker"})
        assert osm_init._DRY_ACTIONS


# ── prompt_vault ──────────────────────────────────────────────────────────────

class TestPromptVault:
    def test_from_param_existing_dir(self, tmp_path):
        osm_init._PARAMS["vault"] = str(tmp_path)
        assert osm_init.prompt_vault() == str(tmp_path)

    def test_from_param_missing_dir_exits(self, tmp_path):
        osm_init._PARAMS["vault"] = str(tmp_path / "no-such-dir")
        with pytest.raises(SystemExit):
            osm_init.prompt_vault()

    def test_from_env_confirm_yes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OBSIDIAN_VAULT", str(tmp_path))
        monkeypatch.setattr(osm_init, "confirm", lambda *a, **kw: True)
        assert osm_init.prompt_vault() == str(tmp_path)

    def test_from_env_confirm_no_prompts_interactively(self, tmp_path, monkeypatch):
        new_vault = tmp_path / "newvault"
        new_vault.mkdir()
        monkeypatch.setenv("OBSIDIAN_VAULT", "/nonexistent")
        monkeypatch.setattr(osm_init, "confirm", lambda *a, **kw: False)
        monkeypatch.setattr(osm_init, "prompt", lambda *a, **kw: str(new_vault))
        assert osm_init.prompt_vault() == str(new_vault)

    def test_retries_until_valid_dir(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OBSIDIAN_VAULT", raising=False)
        attempts = iter(["/does/not/exist", str(tmp_path)])
        monkeypatch.setattr(osm_init, "prompt", lambda *a, **kw: next(attempts))
        assert osm_init.prompt_vault() == str(tmp_path)


# ── prompt_persistent_storage ─────────────────────────────────────────────────

class TestPromptPersistentStorage:
    def test_no_persistent_returns_nones(self, monkeypatch):
        monkeypatch.setattr(osm_init, "confirm", lambda *a, **kw: False)
        pg, ollama = osm_init.prompt_persistent_storage()
        assert pg is None and ollama is None

    def test_persistent_pgdata_path_name(self, tmp_path, monkeypatch):
        osm_init._PARAMS["persistent"] = "y"
        osm_init._PARAMS["data_dir"] = str(tmp_path / "data")
        monkeypatch.setattr(osm_init, "confirm", lambda *a, **kw: True)
        monkeypatch.setattr(osm_init, "prompt", lambda *a, **kw: str(tmp_path / "data"))
        pg, _ = osm_init.prompt_persistent_storage()
        assert Path(pg).name == "pgdata"

    def test_include_ollama_true_returns_ollama_path(self, tmp_path, monkeypatch):
        osm_init._PARAMS["persistent"] = "y"
        osm_init._PARAMS["data_dir"] = str(tmp_path / "data")
        monkeypatch.setattr(osm_init, "confirm", lambda *a, **kw: True)
        monkeypatch.setattr(osm_init, "prompt", lambda *a, **kw: str(tmp_path / "data"))
        _, ollama = osm_init.prompt_persistent_storage(include_ollama=True)
        assert ollama is not None and Path(ollama).name == "ollama"

    def test_include_ollama_false_returns_none_ollama(self, tmp_path, monkeypatch):
        osm_init._PARAMS["persistent"] = "y"
        osm_init._PARAMS["data_dir"] = str(tmp_path / "data")
        monkeypatch.setattr(osm_init, "confirm", lambda *a, **kw: True)
        monkeypatch.setattr(osm_init, "prompt", lambda *a, **kw: str(tmp_path / "data"))
        _, ollama = osm_init.prompt_persistent_storage(include_ollama=False)
        assert ollama is None

    def test_dry_run_records_mkdir_and_no_actual_dirs(self, tmp_path, monkeypatch):
        osm_init.DRY_RUN = True
        data_dir = tmp_path / "data"
        osm_init._PARAMS["persistent"] = "y"
        osm_init._PARAMS["data_dir"] = str(data_dir)
        monkeypatch.setattr(osm_init, "confirm", lambda *a, **kw: True)
        monkeypatch.setattr(osm_init, "prompt", lambda *a, **kw: str(data_dir))
        osm_init.prompt_persistent_storage()
        assert any("mkdir" in a for a in osm_init._DRY_ACTIONS)
        assert not data_dir.exists()

    def test_persistent_without_data_dir_auto_fills_default(self, tmp_path, monkeypatch):
        """--persistent alone must auto-populate data_dir in _PARAMS."""
        osm_init._PARAMS["persistent"] = "y"
        # do NOT set data_dir — let the function default it
        monkeypatch.setattr(osm_init, "confirm", lambda *a, **kw: True)
        monkeypatch.setattr(osm_init, "prompt", lambda *a, **kw: str(tmp_path / "d"))
        osm_init.prompt_persistent_storage()
        assert "data_dir" in osm_init._PARAMS


# ── wait_for_postgres ─────────────────────────────────────────────────────────

class TestWaitForPostgres:
    def test_succeeds_immediately(self, monkeypatch):
        monkeypatch.setattr(osm_init, "run", lambda *a, **kw: _cp(0))
        assert osm_init.wait_for_postgres(timeout=5) is True

    def test_times_out(self, monkeypatch):
        monkeypatch.setattr(osm_init, "run", lambda *a, **kw: _cp(1))
        monkeypatch.setattr(osm_init.time, "sleep", lambda n: None)
        assert osm_init.wait_for_postgres(timeout=0) is False

    def test_dry_run_fast_path(self):
        """In dry-run mode run() returns returncode=0 → should succeed."""
        osm_init.DRY_RUN = True
        assert osm_init.wait_for_postgres(timeout=5) is True


# ── cmd_help ──────────────────────────────────────────────────────────────────

class TestCmdHelp:
    def test_all_commands_listed(self, capsys):
        osm_init.cmd_help()
        out = capsys.readouterr().out
        for cmd in osm_init.COMMANDS:
            assert cmd in out

    def test_flags_listed(self, capsys):
        osm_init.cmd_help()
        out = capsys.readouterr().out
        for flag in ("--dry-run", "--vault", "--mode", "--persistent", "--ssh-host"):
            assert flag in out

    def test_examples_present(self, capsys):
        osm_init.cmd_help()
        out = capsys.readouterr().out
        assert "Examples" in out


# ── cmd_status ────────────────────────────────────────────────────────────────

class TestCmdStatus:
    def _run(self, monkeypatch, docker_stdout="", docker_rc=0, cfg_path=None, cfg_content=None):
        monkeypatch.setattr(osm_init, "run", lambda *a, **kw: _cp(docker_rc, stdout=docker_stdout))
        monkeypatch.setattr(osm_init, "check_ollama_at", lambda *a, **kw: True)
        monkeypatch.setattr(osm_init, "_claude_cfg_path", lambda: cfg_path)
        if cfg_path is not None and cfg_content is not None:
            cfg_path.write_text(cfg_content)

    def test_no_docker_services_no_exception(self, monkeypatch):
        self._run(monkeypatch, docker_stdout="", docker_rc=0)
        osm_init.cmd_status()  # must not raise

    def test_docker_output_is_printed(self, tmp_path, monkeypatch, capsys):
        self._run(monkeypatch, docker_stdout="NAME\npostgres   running")
        osm_init.cmd_status()
        assert "postgres" in capsys.readouterr().out

    def test_claude_config_has_entry(self, tmp_path, monkeypatch, capsys):
        cfg = tmp_path / "cfg.json"
        self._run(monkeypatch, cfg_path=cfg,
                  cfg_content=json.dumps({"mcpServers": {"obsidian-semantic": {}}}))
        osm_init.cmd_status()
        assert "configured" in capsys.readouterr().out

    def test_claude_config_missing_entry(self, tmp_path, monkeypatch, capsys):
        cfg = tmp_path / "cfg.json"
        self._run(monkeypatch, cfg_path=cfg, cfg_content=json.dumps({"mcpServers": {}}))
        osm_init.cmd_status()
        assert "NOT configured" in capsys.readouterr().out

    def test_claude_config_invalid_json(self, tmp_path, monkeypatch, capsys):
        cfg = tmp_path / "cfg.json"
        self._run(monkeypatch, cfg_path=cfg, cfg_content="{{bad")
        osm_init.cmd_status()
        assert "could not be parsed" in capsys.readouterr().out

    def test_claude_config_file_not_found(self, tmp_path, monkeypatch, capsys):
        missing = tmp_path / "missing.json"
        self._run(monkeypatch, cfg_path=missing)  # file never written → doesn't exist
        osm_init.cmd_status()
        assert "not found" in capsys.readouterr().out

    def test_claude_cfg_path_none(self, monkeypatch, capsys):
        self._run(monkeypatch, cfg_path=None)
        osm_init.cmd_status()
        assert "not found" in capsys.readouterr().out


# ── cmd_tunnel ────────────────────────────────────────────────────────────────

class TestCmdTunnel:
    def _write_env(self, tmp_path, content):
        (tmp_path / ".env").write_text(content)
        osm_init.PROJECT_ROOT = tmp_path

    def test_missing_user_and_host_exits(self, tmp_path):
        self._write_env(tmp_path, "OBSIDIAN_VAULT=/vault\n")
        with pytest.raises(SystemExit):
            osm_init.cmd_tunnel()

    def test_missing_host_exits(self, tmp_path):
        self._write_env(tmp_path, "OSM_SSH_USER=u\n")
        with pytest.raises(SystemExit):
            osm_init.cmd_tunnel()

    def test_tunnel_success_checks_ollama(self, tmp_path, monkeypatch):
        self._write_env(
            tmp_path,
            "OSM_SSH_USER=u\nOSM_SSH_HOST=h\nOSM_SSH_LOCAL_PORT=11435\nOSM_SSH_REMOTE_PORT=11434\n",
        )
        monkeypatch.setattr(osm_init, "open_ssh_tunnel", lambda *a, **kw: True)
        ollama_calls = []
        monkeypatch.setattr(osm_init, "check_ollama_at", lambda h, p: ollama_calls.append((h, p)))
        monkeypatch.setattr(osm_init.time, "sleep", lambda n: None)
        osm_init.cmd_tunnel()
        assert ollama_calls

    def test_tunnel_failure_exits(self, tmp_path, monkeypatch):
        self._write_env(tmp_path, "OSM_SSH_USER=u\nOSM_SSH_HOST=h\n")
        monkeypatch.setattr(osm_init, "open_ssh_tunnel", lambda *a, **kw: False)
        with pytest.raises(SystemExit):
            osm_init.cmd_tunnel()

    def test_uses_key_from_env(self, tmp_path, monkeypatch):
        self._write_env(
            tmp_path,
            "OSM_SSH_USER=u\nOSM_SSH_HOST=h\nOSM_SSH_KEY=/my/key\n"
            "OSM_SSH_LOCAL_PORT=11435\nOSM_SSH_REMOTE_PORT=11434\n",
        )
        captured = {}
        def fake_tunnel(user, host, rport, lport, key_path=None):
            captured["key"] = key_path
            return True
        monkeypatch.setattr(osm_init, "open_ssh_tunnel", fake_tunnel)
        monkeypatch.setattr(osm_init, "check_ollama_at", lambda *a, **kw: None)
        monkeypatch.setattr(osm_init.time, "sleep", lambda n: None)
        osm_init.cmd_tunnel()
        assert captured["key"] == "/my/key"

    def test_default_ports_used_when_absent_from_env(self, tmp_path, monkeypatch):
        self._write_env(tmp_path, "OSM_SSH_USER=u\nOSM_SSH_HOST=h\n")
        captured = {}
        def fake_tunnel(user, host, rport, lport, key_path=None):
            captured.update(rport=rport, lport=lport)
            return True
        monkeypatch.setattr(osm_init, "open_ssh_tunnel", fake_tunnel)
        monkeypatch.setattr(osm_init, "check_ollama_at", lambda *a, **kw: None)
        monkeypatch.setattr(osm_init.time, "sleep", lambda n: None)
        osm_init.cmd_tunnel()
        assert captured["rport"] == 11434
        assert captured["lport"] == 11435


# ── cmd_rebuild ───────────────────────────────────────────────────────────────

class TestCmdRebuild:
    def test_calls_compose_with_build(self, monkeypatch):
        calls = []
        monkeypatch.setattr(osm_init, "compose", lambda args, **kw: calls.append(args))
        osm_init.cmd_rebuild()
        assert calls
        assert "--build" in calls[0]

    def test_rebuilds_mcp_server_and_dashboard(self, monkeypatch):
        calls = []
        monkeypatch.setattr(osm_init, "compose", lambda args, **kw: calls.append(args))
        osm_init.cmd_rebuild()
        assert "mcp-server" in calls[0]
        assert "dashboard" in calls[0]


# ── cmd_remove ────────────────────────────────────────────────────────────────

class TestCmdRemove:
    def _setup(self, monkeypatch, tmp_path, cfg_path=None):
        osm_init.PROJECT_ROOT = tmp_path
        monkeypatch.setattr(
            osm_init, "run",
            lambda *a, **kw: _cp(0, stdout="container-id"),
        )
        monkeypatch.setattr(osm_init, "_claude_cfg_path", lambda: cfg_path)

    def test_user_aborts(self, tmp_path, monkeypatch, capsys):
        self._setup(monkeypatch, tmp_path)
        monkeypatch.setattr(osm_init, "confirm", lambda *a, **kw: False)
        osm_init.cmd_remove()
        assert "Aborted" in capsys.readouterr().out

    def test_dry_run_records_delete_env(self, tmp_path, monkeypatch):
        osm_init.DRY_RUN = True
        self._setup(monkeypatch, tmp_path)
        osm_init.cmd_remove()
        assert any("remove" in a or ".env" in a for a in osm_init._DRY_ACTIONS)

    def test_env_file_deleted(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("KEY=val\n")
        self._setup(monkeypatch, tmp_path)
        monkeypatch.setattr(osm_init, "confirm", lambda *a, **kw: True)
        osm_init.cmd_remove()
        assert not env_file.exists()

    def test_env_file_absent_skips_gracefully(self, tmp_path, monkeypatch, capsys):
        self._setup(monkeypatch, tmp_path)  # no .env written
        monkeypatch.setattr(osm_init, "confirm", lambda *a, **kw: True)
        osm_init.cmd_remove()
        assert "skipping" in capsys.readouterr().out

    def test_removes_obsidian_semantic_from_config(self, tmp_path, monkeypatch):
        cfg = tmp_path / "cfg.json"
        cfg.write_text(json.dumps({"mcpServers": {"obsidian-semantic": {}, "other": {}}}))
        self._setup(monkeypatch, tmp_path, cfg_path=cfg)
        monkeypatch.setattr(osm_init, "confirm", lambda *a, **kw: True)
        osm_init.cmd_remove()
        data = json.loads(cfg.read_text())
        assert "obsidian-semantic" not in data["mcpServers"]
        assert "other" in data["mcpServers"]

    def test_config_entry_absent_skips(self, tmp_path, monkeypatch, capsys):
        cfg = tmp_path / "cfg.json"
        cfg.write_text(json.dumps({"mcpServers": {"other": {}}}))
        self._setup(monkeypatch, tmp_path, cfg_path=cfg)
        monkeypatch.setattr(osm_init, "confirm", lambda *a, **kw: True)
        osm_init.cmd_remove()
        assert "skipping" in capsys.readouterr().out

    def test_config_invalid_json_warns(self, tmp_path, monkeypatch, capsys):
        cfg = tmp_path / "cfg.json"
        cfg.write_text("not json")
        self._setup(monkeypatch, tmp_path, cfg_path=cfg)
        monkeypatch.setattr(osm_init, "confirm", lambda *a, **kw: True)
        osm_init.cmd_remove()
        out = capsys.readouterr().out
        assert "manually" in out or "parse" in out.lower()

    def test_cfg_path_none_warns_manual(self, tmp_path, monkeypatch, capsys):
        self._setup(monkeypatch, tmp_path, cfg_path=None)
        monkeypatch.setattr(osm_init, "confirm", lambda *a, **kw: True)
        osm_init.cmd_remove()
        assert "manually" in capsys.readouterr().out

    def test_dry_run_records_cfg_action(self, tmp_path, monkeypatch):
        osm_init.DRY_RUN = True
        cfg = tmp_path / "cfg.json"
        self._setup(monkeypatch, tmp_path, cfg_path=cfg)
        osm_init.cmd_remove()
        assert any("obsidian-semantic" in a or "cfg" in a for a in osm_init._DRY_ACTIONS)


# ── cmd_init — platform routing ───────────────────────────────────────────────

class TestCmdInit:
    """
    MODES_MACOS / MODES_LINUX store direct function references captured at import
    time, so we patch the dicts themselves rather than the module attributes.
    """

    def _stub_modes(self, monkeypatch, modes_attr, mode_key, handler):
        original = getattr(osm_init, modes_attr)
        name, desc, _ = original[mode_key]
        patched = {**original, mode_key: (name, desc, handler)}
        monkeypatch.setattr(osm_init, modes_attr, patched)

    def _macos_env(self, monkeypatch):
        monkeypatch.setattr(osm_init.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(osm_init.platform, "mac_ver", lambda: ("14.0", "", ""))
        monkeypatch.setattr(osm_init.platform, "machine", lambda: "arm64")

    def test_macos_mode1_native(self, monkeypatch):
        self._macos_env(monkeypatch)
        called = []
        self._stub_modes(monkeypatch, "MODES_MACOS", "1", lambda: called.append(1))
        osm_init._PARAMS["mode"] = "1"
        osm_init.cmd_init()
        assert called

    def test_macos_mode2_docker_host_ollama(self, monkeypatch):
        self._macos_env(monkeypatch)
        called = []
        self._stub_modes(monkeypatch, "MODES_MACOS", "2", lambda: called.append(2))
        osm_init._PARAMS["mode"] = "2"
        osm_init.cmd_init()
        assert called

    def test_macos_mode3_full_docker(self, monkeypatch):
        self._macos_env(monkeypatch)
        called = []
        self._stub_modes(monkeypatch, "MODES_MACOS", "3", lambda: called.append(3))
        osm_init._PARAMS["mode"] = "3"
        osm_init.cmd_init()
        assert called

    def test_macos_mode4_remote_ollama(self, monkeypatch):
        self._macos_env(monkeypatch)
        called = []
        self._stub_modes(monkeypatch, "MODES_MACOS", "4", lambda: called.append(4))
        osm_init._PARAMS["mode"] = "4"
        osm_init.cmd_init()
        assert called

    def test_linux_mode1_docker_host_ollama(self, monkeypatch):
        monkeypatch.setattr(osm_init.platform, "system", lambda: "Linux")
        called = []
        self._stub_modes(monkeypatch, "MODES_LINUX", "1", lambda: called.append(1))
        osm_init._PARAMS["mode"] = "1"
        osm_init.cmd_init()
        assert called

    def test_linux_mode2_full_docker(self, monkeypatch):
        monkeypatch.setattr(osm_init.platform, "system", lambda: "Linux")
        called = []
        self._stub_modes(monkeypatch, "MODES_LINUX", "2", lambda: called.append(2))
        osm_init._PARAMS["mode"] = "2"
        osm_init.cmd_init()
        assert called

    def test_linux_mode3_remote_ollama(self, monkeypatch):
        monkeypatch.setattr(osm_init.platform, "system", lambda: "Linux")
        called = []
        self._stub_modes(monkeypatch, "MODES_LINUX", "3", lambda: called.append(3))
        osm_init._PARAMS["mode"] = "3"
        osm_init.cmd_init()
        assert called

    def test_unsupported_platform_exits(self, monkeypatch):
        monkeypatch.setattr(osm_init.platform, "system", lambda: "FreeBSD")
        with pytest.raises(SystemExit):
            osm_init.cmd_init()

    def test_windows_shows_3_modes(self, monkeypatch, capsys):
        monkeypatch.setattr(osm_init.platform, "system", lambda: "Windows")
        monkeypatch.setattr(osm_init.platform, "version", lambda: "10.0.22631")
        monkeypatch.setattr(osm_init.platform, "machine", lambda: "AMD64")
        called = []
        self._stub_modes(monkeypatch, "MODES_WINDOWS", "1", lambda: called.append(1))
        osm_init._PARAMS["mode"] = "1"
        osm_init.cmd_init()
        out = capsys.readouterr().out
        for key in osm_init.MODES_WINDOWS:
            assert key in out
        assert "WSL2" in out

    def test_macos_shows_4_modes(self, monkeypatch, capsys):
        self._macos_env(monkeypatch)
        called = []
        self._stub_modes(monkeypatch, "MODES_MACOS", "1", lambda: called.append(1))
        osm_init._PARAMS["mode"] = "1"
        osm_init.cmd_init()
        # All 4 mode keys must appear in stdout
        out = capsys.readouterr().out
        for key in osm_init.MODES_MACOS:
            assert key in out

    def test_linux_shows_3_modes(self, monkeypatch, capsys):
        monkeypatch.setattr(osm_init.platform, "system", lambda: "Linux")
        called = []
        self._stub_modes(monkeypatch, "MODES_LINUX", "1", lambda: called.append(1))
        osm_init._PARAMS["mode"] = "1"
        osm_init.cmd_init()
        out = capsys.readouterr().out
        for key in osm_init.MODES_LINUX:
            assert key in out


# ── mode_full_docker ──────────────────────────────────────────────────────────

class TestModeFullDocker:
    def _setup(self, monkeypatch, tmp_path):
        _docker_mode_defaults(monkeypatch, tmp_path)

    def test_docker_check_fails_exits(self, tmp_path, monkeypatch):
        self._setup(monkeypatch, tmp_path)
        monkeypatch.setattr(osm_init, "check_docker", lambda: False)
        with pytest.raises(SystemExit):
            osm_init.mode_full_docker()

    def test_compose_check_fails_exits(self, tmp_path, monkeypatch):
        self._setup(monkeypatch, tmp_path)
        monkeypatch.setattr(osm_init, "check_compose", lambda: False)
        with pytest.raises(SystemExit):
            osm_init.mode_full_docker()

    def test_happy_path(self, tmp_path, monkeypatch):
        self._setup(monkeypatch, tmp_path)
        osm_init.mode_full_docker()  # must not raise

    def test_ollama_url_is_internal_docker_network(self, tmp_path, monkeypatch):
        self._setup(monkeypatch, tmp_path)
        seen = []
        monkeypatch.setattr(osm_init, "write_env", lambda v, pw, url, **kw: seen.append(url))
        osm_init.mode_full_docker()
        assert seen[0] == "http://ollama:11434"

    def test_pgdata_and_ollama_data_forwarded_to_write_env(self, tmp_path, monkeypatch):
        _docker_mode_defaults(monkeypatch, tmp_path)
        monkeypatch.setattr(osm_init, "prompt_persistent_storage",
                            lambda **kw: ("/pgdata", "/ollama"))
        seen = {}
        monkeypatch.setattr(osm_init, "write_env", lambda v, pw, url, **kw: seen.update(kw))
        osm_init.mode_full_docker()
        assert seen.get("pgdata_path") == "/pgdata"
        assert seen.get("ollama_data_path") == "/ollama"

    def test_pgdata_forwarded_to_compose_env(self, tmp_path, monkeypatch):
        self._setup(monkeypatch, tmp_path)
        monkeypatch.setattr(osm_init, "prompt_persistent_storage",
                            lambda **kw: ("/pgdata", None))
        env_seen = {}
        monkeypatch.setattr(osm_init, "compose_up", lambda *a, env=None, **kw: env_seen.update(env or {}))
        osm_init.mode_full_docker()
        assert env_seen.get("PGDATA_PATH") == "/pgdata"


# ── mode_docker_host_ollama ───────────────────────────────────────────────────

class TestModeDockerHostOllama:
    def _setup(self, monkeypatch, tmp_path, ollama_up=True, system="Darwin"):
        _docker_mode_defaults(monkeypatch, tmp_path)
        monkeypatch.setattr(osm_init, "check_ollama_at", lambda *a, **kw: ollama_up)
        monkeypatch.setattr(osm_init.platform, "system", lambda: system)

    def test_ollama_not_running_exits(self, tmp_path, monkeypatch):
        self._setup(monkeypatch, tmp_path, ollama_up=False)
        with pytest.raises(SystemExit):
            osm_init.mode_docker_host_ollama()

    def test_macos_uses_host_docker_internal(self, tmp_path, monkeypatch):
        self._setup(monkeypatch, tmp_path, system="Darwin")
        seen = []
        monkeypatch.setattr(osm_init, "write_env", lambda v, pw, url, **kw: seen.append(url))
        osm_init.mode_docker_host_ollama()
        assert "host.docker.internal" in seen[0]

    def test_linux_uses_bridge_ip(self, tmp_path, monkeypatch):
        self._setup(monkeypatch, tmp_path, system="Linux")
        seen = []
        monkeypatch.setattr(osm_init, "write_env", lambda v, pw, url, **kw: seen.append(url))
        osm_init.mode_docker_host_ollama()
        assert "172.17.0.1" in seen[0]

    def test_only_starts_postgres_mcp_dashboard_not_ollama(self, tmp_path, monkeypatch):
        self._setup(monkeypatch, tmp_path)
        seen = []
        monkeypatch.setattr(osm_init, "compose_up",
                            lambda services=None, **kw: seen.extend(services or []))
        osm_init.mode_docker_host_ollama()
        assert "postgres" in seen and "mcp-server" in seen and "dashboard" in seen
        assert "ollama" not in seen

    def test_happy_path(self, tmp_path, monkeypatch):
        self._setup(monkeypatch, tmp_path)
        osm_init.mode_docker_host_ollama()  # must not raise


# ── mode_docker_remote_ollama ─────────────────────────────────────────────────

class TestModeDockerRemoteOllama:
    def _setup(self, monkeypatch, tmp_path, tunnel_ok=True, system="Darwin",
               continue_on_fail=True):
        _docker_mode_defaults(monkeypatch, tmp_path)
        monkeypatch.setattr(osm_init, "prompt_ssh_credentials",
                            lambda: ("u", "host", 11434, "/key"))
        monkeypatch.setattr(osm_init, "open_ssh_tunnel", lambda *a, **kw: tunnel_ok)
        monkeypatch.setattr(osm_init, "check_ollama_at", lambda *a, **kw: True)
        monkeypatch.setattr(osm_init.time, "sleep", lambda n: None)
        monkeypatch.setattr(osm_init.platform, "system", lambda: system)
        monkeypatch.setattr(osm_init, "confirm", lambda *a, **kw: continue_on_fail)
        monkeypatch.setattr(osm_init, "_prompt_vault_location", lambda *a, **kw: str(tmp_path))
        monkeypatch.setattr(osm_init, "_done_docker_remote", lambda *a, **kw: None)

    def test_tunnel_fails_continue_no_exits(self, tmp_path, monkeypatch):
        self._setup(monkeypatch, tmp_path, tunnel_ok=False, continue_on_fail=False)
        with pytest.raises(SystemExit):
            osm_init.mode_docker_remote_ollama()

    def test_tunnel_fails_continue_yes_proceeds(self, tmp_path, monkeypatch):
        self._setup(monkeypatch, tmp_path, tunnel_ok=False, continue_on_fail=True)
        osm_init.mode_docker_remote_ollama()  # must not raise

    def test_happy_path(self, tmp_path, monkeypatch):
        self._setup(monkeypatch, tmp_path, tunnel_ok=True)
        osm_init.mode_docker_remote_ollama()

    def test_ssh_params_written_to_env(self, tmp_path, monkeypatch):
        self._setup(monkeypatch, tmp_path)
        captured = {}
        monkeypatch.setattr(osm_init, "write_env",
                            lambda v, pw, url, **kw: captured.update(kw))
        osm_init.mode_docker_remote_ollama()
        sp = captured["ssh_params"]
        assert sp["user"] == "u"
        assert sp["host"] == "host"
        assert sp["remote_port"] == 11434

    def test_macos_ollama_url_uses_docker_internal(self, tmp_path, monkeypatch):
        self._setup(monkeypatch, tmp_path, system="Darwin")
        seen = []
        monkeypatch.setattr(osm_init, "write_env", lambda v, pw, url, **kw: seen.append(url))
        osm_init.mode_docker_remote_ollama()
        assert "host.docker.internal" in seen[0]

    def test_linux_ollama_url_uses_bridge_ip(self, tmp_path, monkeypatch):
        self._setup(monkeypatch, tmp_path, system="Linux")
        seen = []
        monkeypatch.setattr(osm_init, "write_env", lambda v, pw, url, **kw: seen.append(url))
        osm_init.mode_docker_remote_ollama()
        assert "172.17.0.1" in seen[0]

    def test_tunnel_port_is_11435(self, tmp_path, monkeypatch):
        self._setup(monkeypatch, tmp_path)
        seen = {}
        monkeypatch.setattr(osm_init, "open_ssh_tunnel",
                            lambda u, h, rp, lp, kp=None: seen.update(lp=lp) or True)
        osm_init.mode_docker_remote_ollama()
        assert seen["lp"] == 11435


# ── mode_native_macos ─────────────────────────────────────────────────────────

class TestModeNativeMacos:
    def _setup(self, monkeypatch, tmp_path,
               has_brew=True, has_psql=True, has_ollama=True, has_uv=True,
               ollama_up=True, db_exists=True):
        def fake_exists(name):
            return {"brew": has_brew, "psql": has_psql,
                    "ollama": has_ollama, "uv": has_uv}.get(name, False)
        monkeypatch.setattr(osm_init, "cmd_exists", fake_exists)
        osm_init._PARAMS["vault"] = str(tmp_path)
        db_out = "obsidian_brain" if db_exists else ""

        def fake_run(cmd, **kw):
            if isinstance(cmd, list) and "psql" in cmd and "-lqt" in cmd:
                return _cp(0, stdout=db_out)
            return _cp(0)

        monkeypatch.setattr(osm_init, "run", fake_run)
        monkeypatch.setattr(osm_init, "check_ollama_at", lambda *a, **kw: ollama_up)
        monkeypatch.setattr(osm_init, "update_claude_config", lambda *a, **kw: None)
        monkeypatch.setattr(osm_init, "_done_native", lambda *a, **kw: None)
        monkeypatch.setattr(osm_init.time, "sleep", lambda n: None)
        # Popen is called directly by mode_native_macos — default to no-op
        monkeypatch.setattr(osm_init.subprocess, "Popen", lambda *a, **kw: MagicMock())

    def test_brew_not_found_exits(self, tmp_path, monkeypatch):
        self._setup(monkeypatch, tmp_path, has_brew=False)
        with pytest.raises(SystemExit):
            osm_init.mode_native_macos()

    def test_uv_not_found_exits(self, tmp_path, monkeypatch):
        self._setup(monkeypatch, tmp_path, has_uv=False)
        with pytest.raises(SystemExit):
            osm_init.mode_native_macos()

    def test_psql_present_skips_brew_install(self, tmp_path, monkeypatch):
        brew_installs = []
        self._setup(monkeypatch, tmp_path, has_psql=True)
        orig = osm_init.run
        def tracking(cmd, **kw):
            if isinstance(cmd, list) and "brew" in cmd and "install" in cmd:
                brew_installs.append(cmd)
            return orig(cmd, **kw)
        monkeypatch.setattr(osm_init, "run", tracking)
        osm_init.mode_native_macos()
        assert not any("postgresql" in str(c) for c in brew_installs)

    def test_psql_absent_installs_via_brew(self, tmp_path, monkeypatch):
        brew_installs = []
        self._setup(monkeypatch, tmp_path, has_psql=False)
        orig = osm_init.run
        def tracking(cmd, **kw):
            if isinstance(cmd, list) and "brew" in cmd and "install" in cmd:
                brew_installs.append(cmd)
            return orig(cmd, **kw)
        monkeypatch.setattr(osm_init, "run", tracking)
        osm_init.mode_native_macos()
        assert any("postgresql@17" in str(c) for c in brew_installs)

    def test_db_exists_skips_createdb(self, tmp_path, monkeypatch):
        createdb_calls = []
        self._setup(monkeypatch, tmp_path, db_exists=True)
        orig = osm_init.run
        def tracking(cmd, **kw):
            if isinstance(cmd, list) and "createdb" in cmd:
                createdb_calls.append(cmd)
            return orig(cmd, **kw)
        monkeypatch.setattr(osm_init, "run", tracking)
        osm_init.mode_native_macos()
        assert not createdb_calls

    def test_db_absent_calls_createdb(self, tmp_path, monkeypatch):
        createdb_calls = []
        self._setup(monkeypatch, tmp_path, db_exists=False)
        orig = osm_init.run
        def tracking(cmd, **kw):
            if isinstance(cmd, list) and "createdb" in cmd:
                createdb_calls.append(cmd)
            return orig(cmd, **kw)
        monkeypatch.setattr(osm_init, "run", tracking)
        osm_init.mode_native_macos()
        assert createdb_calls

    def test_ollama_not_running_starts_serve(self, tmp_path, monkeypatch):
        popen_cmds = []
        self._setup(monkeypatch, tmp_path, ollama_up=False)
        monkeypatch.setattr(osm_init.subprocess, "Popen",
                            lambda cmd, **kw: popen_cmds.append(cmd) or MagicMock())
        osm_init.mode_native_macos()
        assert any("ollama" in str(c) for c in popen_cmds)

    def test_happy_path(self, tmp_path, monkeypatch):
        self._setup(monkeypatch, tmp_path)
        osm_init.mode_native_macos()  # must not raise


# ── _prompt_vault_location ────────────────────────────────────────────────────

class TestPromptVaultLocation:
    def test_vault_param_calls_prompt_vault(self, tmp_path, monkeypatch):
        osm_init._PARAMS["vault"] = str(tmp_path)
        monkeypatch.setattr(osm_init, "prompt_vault", lambda: str(tmp_path))
        assert osm_init._prompt_vault_location("u", "h") == str(tmp_path)

    def test_interactive_choice_1_returns_local_vault(self, tmp_path, monkeypatch):
        # prompt("Choose", ...) is the only prompt call; returning "1" routes to prompt_vault.
        monkeypatch.setattr(osm_init, "prompt", lambda q, **kw: "1")
        monkeypatch.setattr(osm_init, "prompt_vault", lambda: str(tmp_path))
        assert osm_init._prompt_vault_location("u", "h") == str(tmp_path)

    def test_vault_remote_param_skips_menu(self, tmp_path, monkeypatch):
        """--vault-remote bypasses the local/remote menu and goes straight to sshfs."""
        osm_init._PARAMS["vault_remote"] = "/remote/vault"
        monkeypatch.setattr(osm_init, "cmd_exists", lambda n: False)  # sshfs absent
        monkeypatch.setattr(osm_init, "prompt", lambda q, **kw: str(tmp_path))
        monkeypatch.setattr(osm_init, "confirm", lambda *a, **kw: False)  # don't continue
        with pytest.raises(SystemExit):
            osm_init._prompt_vault_location("u", "h")

    def test_sshfs_not_found_continue_no_exits(self, tmp_path, monkeypatch):
        osm_init._PARAMS["vault_remote"] = "/remote/vault"
        monkeypatch.setattr(osm_init, "cmd_exists", lambda n: False)
        monkeypatch.setattr(osm_init, "prompt", lambda q, **kw: str(tmp_path))
        monkeypatch.setattr(osm_init, "confirm", lambda *a, **kw: False)
        with pytest.raises(SystemExit):
            osm_init._prompt_vault_location("u", "h")

    def test_sshfs_not_found_continue_yes_falls_back_to_prompt_vault(self, tmp_path, monkeypatch):
        osm_init._PARAMS["vault_remote"] = "/remote/vault"
        monkeypatch.setattr(osm_init, "cmd_exists", lambda n: False)
        monkeypatch.setattr(osm_init, "prompt", lambda q, **kw: str(tmp_path))
        monkeypatch.setattr(osm_init, "confirm", lambda *a, **kw: True)
        monkeypatch.setattr(osm_init, "prompt_vault", lambda: str(tmp_path))
        assert osm_init._prompt_vault_location("u", "h") == str(tmp_path)

    def test_sshfs_mount_fails_continue_no_exits(self, tmp_path, monkeypatch):
        osm_init._PARAMS["vault_remote"] = "/remote/vault"
        monkeypatch.setattr(osm_init, "cmd_exists", lambda n: True)
        monkeypatch.setattr(osm_init, "prompt", lambda q, **kw: str(tmp_path))
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _cp(1))
        monkeypatch.setattr(osm_init, "confirm", lambda *a, **kw: False)
        with pytest.raises(SystemExit):
            osm_init._prompt_vault_location("u", "h")

    def test_sshfs_mount_fails_continue_yes_falls_back(self, tmp_path, monkeypatch):
        osm_init._PARAMS["vault_remote"] = "/remote/vault"
        monkeypatch.setattr(osm_init, "cmd_exists", lambda n: True)
        monkeypatch.setattr(osm_init, "prompt", lambda q, **kw: str(tmp_path))
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _cp(1))
        monkeypatch.setattr(osm_init, "confirm", lambda *a, **kw: True)
        monkeypatch.setattr(osm_init, "prompt_vault", lambda: str(tmp_path))
        assert osm_init._prompt_vault_location("u", "h") == str(tmp_path)

    def test_sshfs_mount_success_returns_mount_point(self, tmp_path, monkeypatch):
        osm_init._PARAMS["vault_remote"] = "/remote/vault"
        mount = tmp_path / "mount"
        answers = iter(["/remote/vault", str(mount)])
        monkeypatch.setattr(osm_init, "cmd_exists", lambda n: True)
        monkeypatch.setattr(osm_init, "prompt", lambda q, **kw: next(answers))
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _cp(0))
        result = osm_init._prompt_vault_location("u", "h")
        assert result == str(mount.resolve())


# ── prompt_ssh_credentials ────────────────────────────────────────────────────

class TestPromptSshCredentials:
    def _smart_prompt(self, extra_answers=None):
        """Returns a prompt() mock that honours _PARAMS then falls through to extra_answers."""
        seq = iter(extra_answers or [])
        def fn(q, default=None, choices=None, param_key=None):
            if param_key and param_key in osm_init._PARAMS:
                val = osm_init._PARAMS[param_key]
                return val
            return next(seq)
        return fn

    def test_ssh_key_from_param(self, tmp_path, monkeypatch):
        key = tmp_path / "mykey"
        key.touch()
        osm_init._PARAMS.update({
            "ssh_host": "h", "ssh_user": "u", "ssh_port": "11434",
            "ssh_key": str(key),
        })
        monkeypatch.setattr(osm_init, "prompt", self._smart_prompt())
        _, _, _, key_path = osm_init.prompt_ssh_credentials()
        assert key_path == str(key.resolve())

    def test_auth_choice_2_agent_returns_none_key(self, monkeypatch):
        osm_init._PARAMS.update({"ssh_host": "h", "ssh_user": "u", "ssh_port": "11434"})
        # auth choice "2" = agent/password → key_path is None
        monkeypatch.setattr(osm_init, "prompt", self._smart_prompt(extra_answers=["2"]))
        _, _, _, key_path = osm_init.prompt_ssh_credentials()
        assert key_path is None

    def test_auth_choice_1_key_exists(self, tmp_path, monkeypatch):
        key = tmp_path / "key"
        key.touch()
        osm_init._PARAMS.update({"ssh_host": "h", "ssh_user": "u", "ssh_port": "11434"})
        monkeypatch.setattr(osm_init, "_default_ssh_key", lambda: "")
        monkeypatch.setattr(osm_init, "prompt",
                            self._smart_prompt(extra_answers=["1", str(key)]))
        _, _, _, key_path = osm_init.prompt_ssh_credentials()
        assert key_path == str(key.resolve())

    def test_auth_choice_1_key_missing_continue_no_exits(self, tmp_path, monkeypatch):
        osm_init._PARAMS.update({"ssh_host": "h", "ssh_user": "u", "ssh_port": "11434"})
        monkeypatch.setattr(osm_init, "_default_ssh_key", lambda: "")
        monkeypatch.setattr(osm_init, "prompt",
                            self._smart_prompt(extra_answers=["1", "/no/such/key"]))
        monkeypatch.setattr(osm_init, "confirm", lambda *a, **kw: False)
        with pytest.raises(SystemExit):
            osm_init.prompt_ssh_credentials()

    def test_auth_choice_1_key_missing_continue_yes_proceeds(self, tmp_path, monkeypatch):
        osm_init._PARAMS.update({"ssh_host": "h", "ssh_user": "u", "ssh_port": "11434"})
        monkeypatch.setattr(osm_init, "_default_ssh_key", lambda: "")
        monkeypatch.setattr(osm_init, "prompt",
                            self._smart_prompt(extra_answers=["1", "/no/such/key"]))
        monkeypatch.setattr(osm_init, "confirm", lambda *a, **kw: True)
        # Should complete and return the key_path even though it doesn't exist
        _, _, _, key_path = osm_init.prompt_ssh_credentials()
        assert key_path is not None

    def test_port_returned_as_int(self, monkeypatch):
        osm_init._PARAMS.update({"ssh_host": "h", "ssh_user": "u", "ssh_port": "9999"})
        monkeypatch.setattr(osm_init, "prompt", self._smart_prompt(extra_answers=["2"]))
        _, _, port, _ = osm_init.prompt_ssh_credentials()
        assert isinstance(port, int)
        assert port == 9999


# ── _done_dry_run ─────────────────────────────────────────────────────────────

class TestDoneDryRun:
    def test_lists_recorded_actions(self, capsys):
        osm_init._DRY_ACTIONS[:] = ["mkdir /foo", "docker compose up"]
        osm_init._done_dry_run()
        out = capsys.readouterr().out
        assert "mkdir /foo" in out
        assert "docker compose up" in out

    def test_empty_actions_says_none(self, capsys):
        osm_init._DRY_ACTIONS.clear()
        osm_init._done_dry_run()
        assert "no actions" in capsys.readouterr().out

    def test_dry_run_header_present(self, capsys):
        osm_init._done_dry_run()
        assert "DRY RUN" in capsys.readouterr().out

    def test_re_run_hint_present(self, capsys):
        osm_init._done_dry_run()
        assert "--dry-run" in capsys.readouterr().out
