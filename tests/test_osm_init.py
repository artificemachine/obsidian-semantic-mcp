"""
Unit tests for osm_init.py — no subprocess calls, no real filesystem writes
(except write_env real-write tests which use tmp_path).
"""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import osm_init
from conftest import _reset  # noqa: E402


# ── _parse_flags ──────────────────────────────────────────────────────────────

class TestParseFlags:
    def setup_method(self):
        _reset()

    def teardown_method(self):
        _reset()

    def test_dry_run_sets_global(self):
        """--dry-run must flip the DRY_RUN global and be stripped from remaining args."""
        remaining, _ = osm_init._parse_flags(["init", "--dry-run"])
        assert osm_init.DRY_RUN is True
        assert remaining == ["init"]

    def test_key_equals_value(self):
        """--vault=/tmp/vault syntax."""
        _, params = osm_init._parse_flags(["--vault=/tmp/vault"])
        assert params["vault"] == "/tmp/vault"

    def test_key_space_value(self):
        """--vault /tmp/vault syntax."""
        _, params = osm_init._parse_flags(["--vault", "/tmp/vault"])
        assert params["vault"] == "/tmp/vault"

    def test_persistent_stores_y(self):
        _, params = osm_init._parse_flags(["--persistent"])
        assert params["persistent"] == "y"

    def test_no_persistent_stores_n(self):
        _, params = osm_init._parse_flags(["--no-persistent"])
        assert params["persistent"] == "n"

    def test_mode_flag(self):
        _, params = osm_init._parse_flags(["init", "--mode", "3"])
        assert params["mode"] == "3"

    def test_pg_password_flag(self):
        _, params = osm_init._parse_flags(["--pg-password", "secret"])
        assert params["pg_password"] == "secret"

    def test_ssh_flags(self):
        _, params = osm_init._parse_flags([
            "--ssh-host", "203.0.113.5",
            "--ssh-user", "ubuntu",
            "--ssh-port", "11434",
            "--ssh-key",  "/path/to/key",
        ])
        assert params["ssh_host"] == "203.0.113.5"
        assert params["ssh_user"] == "ubuntu"
        assert params["ssh_port"] == "11434"
        assert params["ssh_key"]  == "/path/to/key"

    def test_unknown_flag_passes_through(self):
        """Unrecognised flags are left in remaining args — not silently swallowed."""
        remaining, params = osm_init._parse_flags(["init", "--unknown-flag"])
        assert "--unknown-flag" in remaining
        assert params == {}

    def test_multiple_flags_combined(self):
        _, params = osm_init._parse_flags([
            "init",
            "--mode=3",
            "--vault", "/tmp/vault",
            "--pg-password", "pw",
            "--persistent",
        ])
        assert params["mode"]        == "3"
        assert params["vault"]       == "/tmp/vault"
        assert params["pg_password"] == "pw"
        assert params["persistent"]  == "y"

    def test_non_flag_args_preserved(self):
        """Positional args must survive flag stripping unchanged."""
        remaining, _ = osm_init._parse_flags(["init", "--mode", "3", "extra"])
        assert "init"  in remaining
        assert "extra" in remaining

    def test_data_dir_flag(self):
        _, params = osm_init._parse_flags(["--data-dir", "/data/pg"])
        assert params["data_dir"] == "/data/pg"

    def test_vault_remote_flag(self):
        _, params = osm_init._parse_flags(["--vault-remote", "/remote/vault"])
        assert params["vault_remote"] == "/remote/vault"


# ── _read_env ─────────────────────────────────────────────────────────────────

class TestReadEnv:
    def _with_root(self, tmp_path, content, fn):
        (tmp_path / ".env").write_text(content)
        original = osm_init.PROJECT_ROOT
        osm_init.PROJECT_ROOT = tmp_path
        try:
            return fn()
        finally:
            osm_init.PROJECT_ROOT = original

    def test_parses_key_value(self, tmp_path):
        result = self._with_root(tmp_path, "FOO=bar\nBAZ=qux\n", osm_init._read_env)
        assert result["FOO"] == "bar"
        assert result["BAZ"] == "qux"

    def test_ignores_comment_lines(self, tmp_path):
        result = self._with_root(tmp_path, "# comment\nKEY=value\n", osm_init._read_env)
        assert "# comment" not in result
        assert result["KEY"] == "value"

    def test_ignores_blank_lines(self, tmp_path):
        result = self._with_root(tmp_path, "\nKEY=value\n\n", osm_init._read_env)
        assert "" not in result
        assert result["KEY"] == "value"

    def test_returns_empty_when_file_missing(self, tmp_path):
        original = osm_init.PROJECT_ROOT
        osm_init.PROJECT_ROOT = tmp_path
        try:
            result = osm_init._read_env()
        finally:
            osm_init.PROJECT_ROOT = original
        assert result == {}

    def test_value_with_equals_sign(self, tmp_path):
        """Values containing '=' must be preserved correctly."""
        result = self._with_root(tmp_path, "URL=http://host/path?a=1\n", osm_init._read_env)
        assert result["URL"] == "http://host/path?a=1"


# ── _status_ollama_url ───────────────────────────────────────────────────────

class TestStatusOllamaUrl:
    def test_defaults_to_localhost(self):
        assert osm_init._status_ollama_url({}) == "http://localhost:11434"

    def test_maps_full_docker_container_url_to_host_port(self):
        env = {"OLLAMA_URL": "http://ollama:11434"}
        assert osm_init._status_ollama_url(env) == "http://localhost:11435"

    def test_maps_docker_host_bridge_url_to_localhost(self):
        env = {"OLLAMA_URL": "http://host.docker.internal:11434"}
        assert osm_init._status_ollama_url(env) == "http://localhost:11434"

    def test_prefers_ssh_tunnel_port_when_present(self):
        env = {
            "OLLAMA_URL": "http://host.docker.internal:11434",
            "OSM_SSH_LOCAL_PORT": "11435",
        }
        assert osm_init._status_ollama_url(env) == "http://localhost:11435"


# ── write_env ─────────────────────────────────────────────────────────────────

class TestWriteEnv:
    def setup_method(self):
        _reset()

    def teardown_method(self):
        _reset()

    def _call(self, tmp_path, **kwargs):
        original = osm_init.PROJECT_ROOT
        osm_init.PROJECT_ROOT = tmp_path
        try:
            osm_init.write_env("/vault", "pw", "http://ollama:11434", **kwargs)
        finally:
            osm_init.PROJECT_ROOT = original

    def test_writes_file(self, tmp_path):
        self._call(tmp_path)
        content = (tmp_path / ".env").read_text()
        assert "OBSIDIAN_VAULT=/vault" in content
        assert "POSTGRES_PASSWORD=pw"  in content
        assert "OLLAMA_URL=http://ollama:11434" in content

    def test_includes_pgdata_path(self, tmp_path):
        self._call(tmp_path, pgdata_path="/data/pgdata")
        assert "PGDATA_PATH=/data/pgdata" in (tmp_path / ".env").read_text()

    def test_includes_ollama_data_path(self, tmp_path):
        self._call(tmp_path, ollama_data_path="/data/ollama")
        assert "OLLAMA_DATA_PATH=/data/ollama" in (tmp_path / ".env").read_text()

    def test_includes_compose_profiles(self, tmp_path):
        self._call(tmp_path, compose_profiles="full-docker")
        assert "COMPOSE_PROFILES=full-docker" in (tmp_path / ".env").read_text()

    def test_omits_compose_profiles_by_default(self, tmp_path):
        self._call(tmp_path)
        assert "COMPOSE_PROFILES" not in (tmp_path / ".env").read_text()

    def test_includes_ssh_params(self, tmp_path):
        self._call(tmp_path, ssh_params={
            "user": "bob", "host": "myserver",
            "remote_port": 11434, "local_port": 11435,
        })
        content = (tmp_path / ".env").read_text()
        assert "OSM_SSH_USER=bob"    in content
        assert "OSM_SSH_HOST=myserver" in content
        assert "OSM_SSH_REMOTE_PORT=11434" in content
        assert "OSM_SSH_LOCAL_PORT=11435"  in content

    def test_ssh_key_written_when_present(self, tmp_path):
        self._call(tmp_path, ssh_params={
            "user": "u", "host": "h",
            "remote_port": 11434, "local_port": 11435,
            "key_path": "/path/to/key",
        })
        assert "OSM_SSH_KEY=/path/to/key" in (tmp_path / ".env").read_text()

    def test_dry_run_does_not_write(self, tmp_path):
        """In dry-run mode the .env file must not be created."""
        osm_init.DRY_RUN = True
        self._call(tmp_path)
        assert not (tmp_path / ".env").exists()

    def test_dry_run_records_action(self, tmp_path):
        osm_init.DRY_RUN = True
        self._call(tmp_path)
        assert any(".env" in a for a in osm_init._DRY_ACTIONS)


# ── _resolve_project_root ─────────────────────────────────────────────────────

class TestResolveProjectRoot:
    def test_env_override_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OSM_PROJECT_ROOT", str(tmp_path))
        assert osm_init._resolve_project_root() == tmp_path

    def test_config_file_used(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OSM_PROJECT_ROOT", raising=False)
        deploy = tmp_path / "deploy"
        deploy.mkdir()
        cfg = tmp_path / "project_root"
        cfg.write_text(str(deploy), encoding="utf-8")
        monkeypatch.setattr(osm_init, "PROJECT_ROOT_FILE", cfg)
        assert osm_init._resolve_project_root() == deploy

    def test_co_located_compose(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OSM_PROJECT_ROOT", raising=False)
        monkeypatch.setattr(osm_init, "PROJECT_ROOT_FILE", tmp_path / "absent")
        code = tmp_path / "code"
        code.mkdir()
        (code / "docker-compose.yml").write_text("services: {}\n")
        monkeypatch.setattr(osm_init, "_CODE_DIR", code)
        assert osm_init._resolve_project_root() == code

    def test_default_deploy_dir(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OSM_PROJECT_ROOT", raising=False)
        monkeypatch.delenv("OSM_DATA_DIR", raising=False)
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        monkeypatch.setattr(osm_init, "PROJECT_ROOT_FILE", tmp_path / "absent")
        empty = tmp_path / "no_compose"
        empty.mkdir()
        monkeypatch.setattr(osm_init, "_CODE_DIR", empty)
        assert osm_init._resolve_project_root() == tmp_path / "obsidian-semantic-mcp"


# ── _ensure_deploy_dir ────────────────────────────────────────────────────────

class TestEnsureDeployDir:
    def test_noop_when_compose_present(self, tmp_path, monkeypatch):
        monkeypatch.setattr(osm_init, "DRY_RUN", False)
        root = tmp_path / "deploy"
        root.mkdir()
        (root / "docker-compose.yml").write_text("services: {}\n")
        monkeypatch.setattr(osm_init, "PROJECT_ROOT", root)
        code = tmp_path / "code"
        code.mkdir()
        (code / "docker-compose.yml").write_text("PACKAGED\n")
        monkeypatch.setattr(osm_init, "_CODE_DIR", code)
        osm_init._ensure_deploy_dir()
        assert (root / "docker-compose.yml").read_text() == "services: {}\n"

    def test_provisions_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(osm_init, "DRY_RUN", False)
        root = tmp_path / "deploy"
        monkeypatch.setattr(osm_init, "PROJECT_ROOT", root)
        code = tmp_path / "code"
        code.mkdir()
        (code / "docker-compose.yml").write_text("PACKAGED\n")
        monkeypatch.setattr(osm_init, "_CODE_DIR", code)
        osm_init._ensure_deploy_dir()
        assert (root / "docker-compose.yml").read_text() == "PACKAGED\n"

    def test_dry_run_does_not_copy(self, tmp_path, monkeypatch):
        monkeypatch.setattr(osm_init, "DRY_RUN", True)
        root = tmp_path / "deploy"
        monkeypatch.setattr(osm_init, "PROJECT_ROOT", root)
        code = tmp_path / "code"
        code.mkdir()
        (code / "docker-compose.yml").write_text("PACKAGED\n")
        monkeypatch.setattr(osm_init, "_CODE_DIR", code)
        osm_init._ensure_deploy_dir()
        assert not root.exists()


# ── _default_ssh_key ──────────────────────────────────────────────────────────

class TestDefaultSshKey:
    def test_returns_empty_when_no_keys_exist(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        assert osm_init._default_ssh_key() == ""

    def test_returns_first_existing_key(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        key = ssh_dir / "id_ed25519"
        key.touch()
        assert osm_init._default_ssh_key() == str(key)

    def test_prefers_ed25519_over_rsa(self, tmp_path, monkeypatch):
        """id_ed25519 appears first in the candidate list and must win."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        (ssh_dir / "id_ed25519").touch()
        (ssh_dir / "id_rsa").touch()
        assert "id_ed25519" in osm_init._default_ssh_key()

    def test_falls_back_to_rsa_when_ed25519_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        (ssh_dir / "id_rsa").touch()
        assert "id_rsa" in osm_init._default_ssh_key()


# ── Launcher platform parity ──────────────────────────────────────────────────

class TestOsmLauncherPath:
    """_osm_launcher_path() must return platform-correct path."""

    def test_unix_returns_bin_osm(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        result = osm_init._osm_launcher_path()
        assert result == Path.home() / ".local" / "bin" / "osm"

    def test_linux_returns_bin_osm(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Linux")
        result = osm_init._osm_launcher_path()
        assert result == Path.home() / ".local" / "bin" / "osm"

    def test_windows_returns_osm_cmd(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Windows")
        result = osm_init._osm_launcher_path()
        assert result == Path.home() / ".local" / "bin" / "osm.cmd"


# ── Windows launcher script ──────────────────────────────────────────────────

class TestWindowsLauncherScript:
    def test_uses_python_not_python3(self):
        script = (Path(__file__).parent.parent / "scripts" / "osm.ps1").read_text()
        assert "uv run --project $ProjectRoot python $Wizard @args" in script
        assert "& python $Wizard @args" in script
        assert "uv run --project $ProjectRoot python3 $Wizard @args" not in script
        assert "& python3 $Wizard @args" not in script


class TestUnixInstallerScript:
    def test_reattaches_tty_before_wizard(self):
        script = (Path(__file__).parent.parent / "install.sh").read_text()
        assert "[ -t 0 ]" in script
        assert "[ -r /dev/tty ]" in script
        assert 'exec "$INSTALL_DIR/scripts/osm" init "$@" < /dev/tty' in script
        assert "No interactive terminal available for the setup wizard" in script


class TestLinkOsmToPath:
    """_link_osm_to_path() must write platform-appropriate launcher content."""

    def setup_method(self):
        _reset()

    def teardown_method(self):
        _reset()

    def test_unix_launcher_is_bash_script(self, tmp_path, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        launcher = tmp_path / ".local" / "bin" / "osm"
        with patch("osm_init.ok"), patch("osm_init.warn"), patch("osm_init.info"):
            osm_init._link_osm_to_path()
        assert launcher.exists()
        content = launcher.read_text()
        assert content.startswith("#!/usr/bin/env bash")
        assert oct(launcher.stat().st_mode)[-3:] == "755"

    def test_windows_launcher_is_cmd_batch(self, tmp_path, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Windows")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        launcher = tmp_path / ".local" / "bin" / "osm.cmd"
        with patch("osm_init.ok"), patch("osm_init.warn"), patch("osm_init.info"):
            osm_init._link_osm_to_path()
        assert launcher.exists()
        content = launcher.read_text()
        assert "@echo off" in content
        assert "osm.ps1" in content

    def test_dry_run_does_not_write_unix(self, tmp_path, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        osm_init.DRY_RUN = True
        with patch("osm_init.ok"), patch("osm_init.warn"), patch("osm_init.info"):
            osm_init._link_osm_to_path()
        assert not (tmp_path / ".local" / "bin" / "osm").exists()
        assert any("osm" in a for a in osm_init._DRY_ACTIONS)

    def test_dry_run_does_not_write_windows(self, tmp_path, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Windows")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        osm_init.DRY_RUN = True
        with patch("osm_init.ok"), patch("osm_init.warn"), patch("osm_init.info"):
            osm_init._link_osm_to_path()
        assert not (tmp_path / ".local" / "bin" / "osm.cmd").exists()
        assert any("osm" in a for a in osm_init._DRY_ACTIONS)


class TestCmdRemoveLauncherParity:
    """cmd_remove() must delete the platform-correct launcher file."""

    def setup_method(self):
        _reset()

    def teardown_method(self):
        _reset()

    def _stub_remove_env(self, tmp_path):
        """Point PROJECT_ROOT at tmp_path so .env check doesn't crash."""
        osm_init.PROJECT_ROOT = tmp_path

    def test_removes_unix_launcher(self, tmp_path, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        launcher = tmp_path / ".local" / "bin" / "osm"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/usr/bin/env bash\n")
        self._stub_remove_env(tmp_path)
        osm_init._PARAMS["yes"] = "y"
        with (
            patch("osm_init.ok"), patch("osm_init.warn"), patch("osm_init.info"),
            patch("osm_init.header"),
            patch("osm_init.run", return_value=type("R", (), {"stdout": "", "returncode": 0})()),
            patch("osm_init.cmd_exists", return_value=False),
            patch("osm_init._claude_cfg_path", return_value=None),
        ):
            osm_init.cmd_remove()
        assert not launcher.exists()

    def test_removes_windows_launcher(self, tmp_path, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Windows")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        launcher = tmp_path / ".local" / "bin" / "osm.cmd"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("@echo off\n")
        self._stub_remove_env(tmp_path)
        osm_init._PARAMS["yes"] = "y"
        with (
            patch("osm_init.ok"), patch("osm_init.warn"), patch("osm_init.info"),
            patch("osm_init.header"),
            patch("osm_init.run", return_value=type("R", (), {"stdout": "", "returncode": 0})()),
            patch("osm_init.cmd_exists", return_value=False),
            patch("osm_init._claude_cfg_path", return_value=None),
        ):
            osm_init.cmd_remove()
        assert not launcher.exists()


# ── compose_up fail-fast ──────────────────────────────────────────────────────

class TestComposeUpFailFast:
    """compose_up must surface docker errors immediately on a non-zero exit
    instead of letting wait_for_postgres time out 90s later. On Windows, a
    bind-mount UNC path failure must point the user at the WSL2 workaround."""

    def setup_method(self):
        _reset()

    def teardown_method(self):
        _reset()

    def _wire(self, monkeypatch, returncode, output):
        from io import StringIO

        class FakeProc:
            def __init__(self):
                self.stdout = StringIO(output)
                self.returncode = returncode

            def wait(self):
                return self.returncode

        monkeypatch.setattr(osm_init.subprocess, "Popen", lambda *a, **k: FakeProc())
        monkeypatch.setattr(osm_init, "DRY_RUN", False)

        diag = {"ps": False, "logs": False}

        def fake_run(cmd, **kw):
            if "ps" in cmd:
                diag["ps"] = True
                return type("R", (), {"stdout": "mcp-server  Created  Exit 1\n", "returncode": 0})()
            if "logs" in cmd:
                diag["logs"] = True
                return type("R", (), {"stdout": "fake log line\n", "returncode": 0})()
            return type("R", (), {"stdout": "", "returncode": 0})()

        monkeypatch.setattr(osm_init, "run", fake_run)
        return diag

    def test_success_skips_diagnostics(self, monkeypatch):
        diag = self._wire(monkeypatch, returncode=0, output="Started\n")
        osm_init.compose_up()
        assert diag["ps"] is False
        assert diag["logs"] is False

    def test_failure_runs_diagnostics_and_exits(self, monkeypatch):
        diag = self._wire(monkeypatch, returncode=1, output="Error: invalid mount\n")
        with pytest.raises(SystemExit):
            osm_init.compose_up()
        assert diag["ps"] is True
        assert diag["logs"] is True

    def test_unc_path_error_surfaces_wsl_hint(self, monkeypatch, capsys):
        unc_err = "Error response from daemon: \\\\10.0.0.1\\share is not a valid Windows path\n"
        self._wire(monkeypatch, returncode=1, output=unc_err)
        with pytest.raises(SystemExit):
            osm_init.compose_up()
        out = capsys.readouterr().out
        assert "WSL2" in out or "wsl2" in out.lower(), (
            "Windows UNC path failure must hint at the WSL2 workaround"
        )


# ── --vault-fs flag (nfs/cifs override generation) ───────────────────────────

class TestVaultFsFlag:
    """--vault-fs <auto|local|nfs|cifs> controls whether
    docker-compose.override.yml uses bind mounts (default) or named volumes
    backed by NFS / CIFS driver_opts. Resolves a real-world Windows + NAS
    install failure where bind-mounts of network drives silently mount empty
    directories."""

    def setup_method(self):
        _reset()

    def teardown_method(self):
        _reset()

    def test_vault_fs_local_keeps_bind_mount_behavior(self, tmp_path, monkeypatch):
        monkeypatch.setattr(osm_init, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(osm_init, "DRY_RUN", False)
        osm_init._PARAMS["vault_fs"] = "local"
        osm_init._write_compose_override(["/Users/me/vault_a", "/Users/me/vault_b"])
        out = (tmp_path / "docker-compose.override.yml").read_text()
        assert "/Users/me/vault_a:/vault_a" in out, "local mode must use bind mount"
        assert "driver: local" not in out or "type: nfs" not in out, (
            "local mode must not emit nfs driver_opts"
        )

    def test_vault_fs_nfs_emits_named_volume_with_driver_opts(self, tmp_path, monkeypatch):
        monkeypatch.setattr(osm_init, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(osm_init, "DRY_RUN", False)
        osm_init._PARAMS["vault_fs"] = "nfs"
        # NFS-style entries: host:/export/path
        osm_init._write_compose_override([
            "10.0.0.1:/exports/coredev",
            "10.0.0.1:/exports/yjjoe",
        ])
        out = (tmp_path / "docker-compose.override.yml").read_text()
        assert "driver: local" in out, "NFS mode must declare a named volume"
        assert "type: nfs" in out, "NFS mode must set driver type to nfs"
        assert "addr=10.0.0.1" in out, "NFS host must be in driver_opts"
        assert "/exports/coredev" in out and "/exports/yjjoe" in out
        assert "obsidian_vault_coredev" in out and "obsidian_vault_yjjoe" in out, (
            "named volume per vault basename"
        )

    def test_vault_fs_cifs_emits_named_volume_with_driver_opts(self, tmp_path, monkeypatch):
        monkeypatch.setattr(osm_init, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(osm_init, "DRY_RUN", False)
        osm_init._PARAMS["vault_fs"] = "cifs"
        osm_init._PARAMS["vault_cifs_user"] = "alice"
        osm_init._PARAMS["vault_cifs_pass"] = "secret"
        osm_init._write_compose_override(["//nas.local/share/coredev"])
        out = (tmp_path / "docker-compose.override.yml").read_text()
        assert "type: cifs" in out
        assert "username=alice" in out
        assert "password=secret" in out
        assert "//nas.local/share/coredev" in out


class TestRemoveCleansNamedVolumes:
    """If the generated override declared NFS / CIFS named volumes, osm remove
    must drop those volumes — otherwise the network mount lingers as a Docker
    volume reference after teardown and silently leaks into the next install."""

    def setup_method(self):
        _reset()

    def teardown_method(self):
        _reset()

    def test_remove_runs_docker_volume_rm_for_obsidian_volumes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(osm_init, "PROJECT_ROOT", tmp_path)
        override = tmp_path / "docker-compose.override.yml"
        override.write_text(
            "services:\n"
            "  mcp-server:\n"
            "    volumes:\n"
            "      - obsidian_vault_coredev:/coredev\n"
            "volumes:\n"
            "  obsidian_vault_coredev:\n"
            "    driver: local\n"
            "    driver_opts:\n"
            "      type: nfs\n"
        )

        rm_calls: list[list[str]] = []

        def fake_run(cmd, **kw):
            if "volume" in cmd and "rm" in cmd:
                rm_calls.append(cmd)
            return type("R", (), {"stdout": "", "returncode": 0})()

        monkeypatch.setattr(osm_init, "run", fake_run)
        osm_init._remove_named_volumes_from_override()

        flat = " ".join(c for cmd in rm_calls for c in cmd)
        assert "obsidian_vault_coredev" in flat, (
            "must run docker volume rm on the named volume from the override"
        )



# ── pi client opt-out (OSM_SKIP_PI) ───────────────────────────────────────────

class TestPiEnabled:
    """The niche `pi` MCP client is configured only when the pi binary is on
    PATH and the user has not opted out. OSM_SKIP_PI lets a pi user run/record
    a clean setup matching what the pi-less majority sees."""

    def test_enabled_when_pi_present_and_no_optout(self, monkeypatch):
        monkeypatch.delenv("OSM_SKIP_PI", raising=False)
        with patch.object(osm_init, "cmd_exists", return_value=True):
            assert osm_init._pi_enabled() is True

    def test_disabled_when_pi_absent(self, monkeypatch):
        monkeypatch.delenv("OSM_SKIP_PI", raising=False)
        with patch.object(osm_init, "cmd_exists", return_value=False):
            assert osm_init._pi_enabled() is False

    def test_optout_wins_even_when_pi_present(self, monkeypatch):
        with patch.object(osm_init, "cmd_exists", return_value=True):
            for val in ("1", "true", "TRUE", "yes", " 1 "):
                monkeypatch.setenv("OSM_SKIP_PI", val)
                assert osm_init._pi_enabled() is False, f"OSM_SKIP_PI={val!r} should disable"

    def test_empty_optout_does_not_disable(self, monkeypatch):
        monkeypatch.setenv("OSM_SKIP_PI", "")
        with patch.object(osm_init, "cmd_exists", return_value=True):
            assert osm_init._pi_enabled() is True

    def test_label_names_pi_only_when_enabled(self, monkeypatch):
        monkeypatch.delenv("OSM_SKIP_PI", raising=False)
        with patch.object(osm_init, "cmd_exists", return_value=True):
            assert osm_init._mcp_clients_label().endswith(", pi")
        monkeypatch.setenv("OSM_SKIP_PI", "1")
        with patch.object(osm_init, "cmd_exists", return_value=True):
            assert "pi" not in osm_init._mcp_clients_label().split(", ")
