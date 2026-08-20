"""Contract tests for the isolated Docker stdio session launcher."""

from __future__ import annotations

import fcntl
import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "obsidian-semantic-mcp-session"


def _fake_docker(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "docker.log"
    binary = tmp_path / "docker"
    binary.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_DOCKER_LOG\"\n"
        "case \"${1:-}\" in\n"
        "  ps) [[ -z \"${FAKE_PS_OUTPUT:-}\" ]] || printf '%s\\n' \"$FAKE_PS_OUTPUT\" ;;\n"
        "  inspect) printf '%s\\n' \"${FAKE_LEASE_ID:-}\" ;;\n"
        "  run) while IFS= read -r _line; do :; done ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary, log


def _env(tmp_path: Path, docker: Path, log: Path) -> dict[str, str]:
    env_file = tmp_path / "bridge.env"
    env_file.write_text(
        "POSTGRES_PASSWORD=test-only\n"
        "OSM_BRIDGE_VAULT_SOURCE=test-vault\n"
        "OSM_BRIDGE_VAULT_TYPE=volume\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        {
            "DOCKER_BIN": str(docker),
            "FAKE_DOCKER_LOG": str(log),
            "OSM_BRIDGE_ENV": str(env_file),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
        }
    )
    return env


def test_session_launcher_uses_lease_and_cleans_up_on_eof(tmp_path):
    docker, log = _fake_docker(tmp_path)
    result = subprocess.run(
        [str(SCRIPT)],
        input=b"",
        env=_env(tmp_path, docker, log),
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr.decode(errors="replace")
    calls = log.read_text(encoding="utf-8")
    assert "--label osm.session-bridge=2" in calls
    assert "--label osm.lease-id=" in calls
    assert "-e POSTGRES_PASSWORD" in calls
    assert "test-only" not in calls
    assert "rm -f osm-bridge-" in calls
    assert not list((tmp_path / "cache").rglob("*.lock"))


def test_session_launcher_preserves_unknown_legacy_owners(tmp_path):
    docker, log = _fake_docker(tmp_path)
    env = _env(tmp_path, docker, log)
    env["FAKE_PS_OUTPUT"] = "legacy-container"
    env["FAKE_LEASE_ID"] = ""

    result = subprocess.run(
        [str(SCRIPT)], input=b"", env=env, capture_output=True, timeout=10
    )

    assert result.returncode == 0, result.stderr.decode(errors="replace")
    calls = log.read_text(encoding="utf-8")
    assert "rm -f legacy-container" not in calls


def test_unlocked_lease_is_reaped(tmp_path):
    docker, log = _fake_docker(tmp_path)
    env = _env(tmp_path, docker, log)
    lease_id = "11111111-2222-3333-4444-555555555555"
    lease_dir = tmp_path / "cache" / "obsidian-semantic-mcp" / "leases"
    lease_dir.mkdir(parents=True)
    (lease_dir / f"{lease_id}.lock").touch()
    env["FAKE_PS_OUTPUT"] = "orphan-container"
    env["FAKE_LEASE_ID"] = lease_id

    result = subprocess.run(
        [str(SCRIPT)], input=b"", env=env, capture_output=True, timeout=10
    )

    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert "rm -f orphan-container" in log.read_text(encoding="utf-8")
    assert not (lease_dir / f"{lease_id}.lock").exists()


def test_locked_lease_survives_concurrent_session_start(tmp_path):
    docker, log = _fake_docker(tmp_path)
    env = _env(tmp_path, docker, log)
    lease_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    lease_dir = tmp_path / "cache" / "obsidian-semantic-mcp" / "leases"
    lease_dir.mkdir(parents=True)
    lease_path = lease_dir / f"{lease_id}.lock"
    env["FAKE_PS_OUTPUT"] = "live-container"
    env["FAKE_LEASE_ID"] = lease_id

    with lease_path.open("w") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(
            [str(SCRIPT)], input=b"", env=env, capture_output=True, timeout=10
        )

    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert "rm -f live-container" not in log.read_text(encoding="utf-8")
