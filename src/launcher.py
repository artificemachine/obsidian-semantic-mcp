"""
launcher.py — path-agnostic entry point for obsidian-semantic-mcp.

Behaviour:
  - Default (no OSM_DOCKER): validate env, then run the MCP server in-process.
  - OSM_DOCKER=1: poll up to OSM_DOCKER_WAIT seconds for the compose container,
    exec into it if found, fall through to in-process server on timeout.

Environment variables:
  OSM_DOCKER          set to "1" to enable Docker mode
  OSM_PROJECT_ROOT    path to the docker-compose project dir (required in Docker mode)
  OSM_DOCKER_WAIT     seconds to poll for the container (default: 30)
  OBSIDIAN_VAULT      vault path (required unless OBSIDIAN_VAULTS is set)
  OBSIDIAN_VAULTS     comma-separated vault paths
  DATABASE_URL        postgres connection string (or set POSTGRES_PASSWORD)
  POSTGRES_PASSWORD   postgres password
"""
from __future__ import annotations

import os
import sys
import subprocess
import time
from pathlib import Path


def _docker_bin() -> str:
    return os.environ.get("DOCKER_BIN", "docker")


def _project_root() -> Path:
    root = os.environ.get("OSM_PROJECT_ROOT")
    if root:
        return Path(root)
    # When running from a dev checkout, __file__ is src/launcher.py → parent is src/ → parent is repo root.
    return Path(__file__).resolve().parent.parent


def _validate_env() -> None:
    if not os.environ.get("OBSIDIAN_VAULTS") and not os.environ.get("OBSIDIAN_VAULT"):
        print("obsidian-semantic-mcp: missing OBSIDIAN_VAULTS or OBSIDIAN_VAULT", file=sys.stderr)
        sys.exit(1)
    if not os.environ.get("DATABASE_URL") and not os.environ.get("POSTGRES_PASSWORD"):
        print("obsidian-semantic-mcp: missing DATABASE_URL or POSTGRES_PASSWORD", file=sys.stderr)
        sys.exit(1)


def _docker_info_ok() -> bool:
    docker = _docker_bin()
    try:
        result = subprocess.run(
            [docker, "info"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def _container_id(project_root: Path) -> str:
    docker = _docker_bin()
    try:
        result = subprocess.run(
            [docker, "compose", "--project-directory", str(project_root),
             "ps", "--status", "running", "-q", "mcp-server"],
            capture_output=True,
            timeout=10,
        )
        return result.stdout.decode().strip()
    except Exception:
        return ""


def _exec_into_container(project_root: Path) -> None:
    docker = _docker_bin()
    args = [docker, "compose", "--project-directory", str(project_root),
            "exec", "-T", "mcp-server", "python3", "/app/src/server.py"] + sys.argv[1:]
    os.execvp(docker, args)


def _run_server() -> None:
    from src.server import run_server
    run_server()


def main() -> None:
    _validate_env()

    if os.environ.get("OSM_DOCKER") == "1":
        wait = int(os.environ.get("OSM_DOCKER_WAIT", "30"))
        project_root = _project_root()

        if wait > 0 and _docker_info_ok():
            for _ in range(wait):
                cid = _container_id(project_root)
                if cid:
                    _exec_into_container(project_root)
                    return  # exec replaces process; this line is never reached
                time.sleep(1)

    _run_server()


if __name__ == "__main__":
    main()
