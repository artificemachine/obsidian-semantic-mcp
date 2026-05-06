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

from dotenv import load_dotenv

OSM_CONFIG_DIR = Path.home() / ".config" / "obsidian-semantic-mcp"
PROJECT_ROOT_FILE = OSM_CONFIG_DIR / "project_root"


def _docker_bin() -> str:
    return os.environ.get("DOCKER_BIN", "docker")


def _project_root() -> Path | None:
    root = os.environ.get("OSM_PROJECT_ROOT")
    if root:
        return Path(root)

    # Try global config file (v0.9.6+)
    if PROJECT_ROOT_FILE.exists():
        try:
            return Path(PROJECT_ROOT_FILE.read_text(encoding="utf-8").strip())
        except Exception:
            pass

    # Fallback: dev checkout detection (src/launcher.py is in src/)
    dev_root = Path(__file__).resolve().parent.parent
    if (dev_root / "docker-compose.yml").exists():
        return dev_root

    return None


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
    docker_mode = os.environ.get("OSM_DOCKER")
    project_root = _project_root()

    # If we found a project root, load its .env file to hydrate local environment
    if project_root:
        env_file = project_root / ".env"
        if env_file.exists():
            load_dotenv(env_file)

    # Opt-in logic:
    # 1. OSM_DOCKER="1" always enables it (fails if project_root not found)
    # 2. If OSM_DOCKER is unset, enable it if project_root was found via config/dev
    use_docker = False
    if docker_mode == "1":
        use_docker = True
    elif docker_mode != "0" and project_root:
        use_docker = True

    if use_docker:
        if not project_root:
            if docker_mode == "1":
                print("obsidian-semantic-mcp: OSM_DOCKER=1 but project root not found", file=sys.stderr)
                sys.exit(1)
        else:
            wait = int(os.environ.get("OSM_DOCKER_WAIT", "30"))
            if wait > 0 and _docker_info_ok():
                for _ in range(wait):
                    cid = _container_id(project_root)
                    if cid:
                        _exec_into_container(project_root)
                        return
                    time.sleep(1)

    # Local fallback or native mode
    _validate_env()
    _run_server()


if __name__ == "__main__":
    main()
