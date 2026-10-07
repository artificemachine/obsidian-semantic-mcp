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

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

OSM_CONFIG_DIR = Path.home() / ".config" / "obsidian-semantic-mcp"
PROJECT_ROOT_FILE = OSM_CONFIG_DIR / "project_root"
_RETRIEVAL_RUNTIME_VARIABLES = {
    "OSM_RETRIEVAL_BACKEND",
    "CAASIOPEIA_BASE_URL",
    "CAASIOPEIA_SOURCE_MAP",
    "CAASIOPEIA_SOURCE_ROOTS",
    "CAASIOPEIA_TOKEN_BUDGET",
}


def _validate_native_runtime(data):
    if not isinstance(data, dict) or set(data) != {"version", "env"}:
        raise ValueError("invalid native runtime schema")
    if type(data["version"]) is not int or data["version"] != 1 or not isinstance(data["env"], dict):
        raise ValueError("invalid native runtime schema")
    env = data["env"]
    vault_keys = set(env) & {"OBSIDIAN_VAULT", "OBSIDIAN_VAULTS"}
    allowed_env = {"OSM_DOCKER", "DATABASE_URL"} | vault_keys | _RETRIEVAL_RUNTIME_VARIABLES
    required_env = {"OSM_DOCKER", "DATABASE_URL"} | vault_keys
    if len(vault_keys) != 1 or not required_env <= set(env) or set(env) - allowed_env:
        raise ValueError("invalid native runtime variables")
    if any(not isinstance(value, str) or not value or "\x00" in value for value in env.values()):
        raise ValueError("invalid native runtime values")
    if any(any(character in env[name] for character in "\r\n")
           for name in set(env) & _RETRIEVAL_RUNTIME_VARIABLES):
        raise ValueError("invalid native retrieval settings")
    if env["OSM_DOCKER"] != "0" or not env["DATABASE_URL"].startswith(("postgresql://", "postgres://")):
        raise ValueError("invalid native runtime values")
    vaults = env[next(iter(vault_keys))].split(",") if "OBSIDIAN_VAULTS" in vault_keys else [env["OBSIDIAN_VAULT"]]
    if any(not Path(vault).is_absolute() for vault in vaults):
        raise ValueError("invalid native vault paths")
    if set(env) & _RETRIEVAL_RUNTIME_VARIABLES:
        _validate_native_retrieval_settings(env, vaults)
    return env


def _read_native_runtime(path):
    """Read an owner-only regular file without following links."""
    if os.name != "posix":
        if path.exists() or path.is_symlink():
            raise ValueError("native runtime permission validation requires POSIX")
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError("unsafe native runtime file") from exc
    with os.fdopen(fd, "r", encoding="utf-8") as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.geteuid() or info.st_nlink != 1 or info.st_size > 65536):
            raise ValueError("unsafe native runtime file")
        return _validate_native_runtime(json.loads(source.read(65537)))


def write_native_runtime(vaults, db_url, config_dir=None, retrieval_settings=None):
    """Atomically persist native settings outside every MCP client's config."""
    vaults = [vaults] if isinstance(vaults, str) else list(vaults)
    env = {"OSM_DOCKER": "0", "DATABASE_URL": db_url}
    if not vaults:
        raise ValueError("native runtime requires a vault")
    env["OBSIDIAN_VAULTS" if len(vaults) > 1 else "OBSIDIAN_VAULT"] = ",".join(vaults)
    if retrieval_settings is not None:
        if not isinstance(retrieval_settings, dict) or set(retrieval_settings) - _RETRIEVAL_RUNTIME_VARIABLES:
            raise ValueError("invalid native retrieval settings")
        if any(not isinstance(value, str) or not value or any(character in value for character in "\x00\r\n") for value in retrieval_settings.values()):
            raise ValueError("invalid native retrieval settings")
        env.update(retrieval_settings)
    data = {"version": 1, "env": env}
    _validate_native_runtime(data)
    encoded = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if len(encoded.encode("utf-8")) > 65536 or os.name != "posix":
        raise ValueError("unsupported native runtime configuration")
    directory = OSM_CONFIG_DIR if config_dir is None else Path(config_dir)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o022):
        raise ValueError("unsafe native runtime directory")
    path = directory / "native_runtime.json"
    _read_native_runtime(path)
    fd, temporary = tempfile.mkstemp(prefix=".native-runtime-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            os.fchmod(target.fileno(), 0o600)
            target.write(encoded)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _validate_native_retrieval_settings(env, vaults):
    from src.config import (
        MAX_CAASIOPEIA_TOKEN_BUDGET,
        _parse_source_map,
        _parse_source_roots,
        resolve_retrieval_backend,
    )

    try:
        backend = resolve_retrieval_backend(env)
        if backend == "caasiopeia" and not {
            "CAASIOPEIA_BASE_URL", "CAASIOPEIA_SOURCE_MAP"
        } <= set(env):
            raise ValueError
        if "CAASIOPEIA_BASE_URL" in env:
            parts = urlsplit(env["CAASIOPEIA_BASE_URL"])
            if (parts.scheme not in ("http", "https") or not parts.hostname
                    or parts.username is not None or parts.password is not None
                    or parts.path not in ("", "/") or parts.query or parts.fragment):
                raise ValueError
            _ = parts.port
        source_ids = None
        if "CAASIOPEIA_SOURCE_MAP" in env:
            source_ids = _parse_source_map(env["CAASIOPEIA_SOURCE_MAP"], vaults)
        if "CAASIOPEIA_SOURCE_ROOTS" in env:
            if source_ids is None:
                raise ValueError
            _parse_source_roots(env["CAASIOPEIA_SOURCE_ROOTS"], list(source_ids))
        if "CAASIOPEIA_TOKEN_BUDGET" in env:
            budget = int(env["CAASIOPEIA_TOKEN_BUDGET"])
            if not 1 <= budget <= MAX_CAASIOPEIA_TOKEN_BUDGET:
                raise ValueError
    except (ValueError, TypeError, RuntimeError) as exc:
        raise ValueError("invalid native retrieval settings") from exc


def _load_native_runtime() -> bool:
    if os.environ.get("OSM_DOCKER") == "1":
        return False
    env = _read_native_runtime(OSM_CONFIG_DIR / "native_runtime.json")
    if env is None:
        return False
    explicit_backend = os.environ.get("OSM_RETRIEVAL_BACKEND")
    incompatible_backend = (
        explicit_backend is not None
        and explicit_backend.strip().lower() != env.get("OSM_RETRIEVAL_BACKEND", "local").strip().lower()
    )
    for name, value in env.items():
        if incompatible_backend and name in _RETRIEVAL_RUNTIME_VARIABLES:
            continue
        if name in ("OBSIDIAN_VAULT", "OBSIDIAN_VAULTS") and (
                "OBSIDIAN_VAULT" in os.environ or "OBSIDIAN_VAULTS" in os.environ):
            continue
        if name == "DATABASE_URL" and ("DATABASE_URL" in os.environ or "POSTGRES_PASSWORD" in os.environ):
            continue
        os.environ.setdefault(name, value)
    return True


def remove_native_runtime(config_dir=None):
    """Remove only the native runtime file, leaving other OSM configuration intact."""
    directory = OSM_CONFIG_DIR if config_dir is None else Path(config_dir)
    try:
        info = directory.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode) or (os.name == "posix" and info.st_uid != os.geteuid()):
        raise ValueError("unsafe native runtime directory")
    path = directory / "native_runtime.json"
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if os.name == "posix" and info.st_uid != os.geteuid():
        raise ValueError("unsafe native runtime file")
    path.unlink()


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


def _validate_retrieval_env() -> None:
    from src.config import (
        RETRIEVAL_BACKEND_CAASIOPEIA,
        ConfigError,
        load_caasiopeia_settings,
        resolve_retrieval_backend,
    )

    try:
        backend = resolve_retrieval_backend(os.environ)
        if backend == RETRIEVAL_BACKEND_CAASIOPEIA:
            vaults = os.environ.get("OBSIDIAN_VAULTS", "").strip()
            vault_paths = vaults.split(",") if vaults else [os.environ["OBSIDIAN_VAULT"]]
            load_caasiopeia_settings(vault_paths, os.environ)
    except (ConfigError, KeyError) as exc:
        message = str(exc) if isinstance(exc, ConfigError) else "OBSIDIAN_VAULT is required"
        print(f"obsidian-semantic-mcp: invalid retrieval configuration ({message})", file=sys.stderr)
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
    try:
        native_runtime_loaded = _load_native_runtime()
    except (OSError, ValueError, TypeError) as exc:
        print(f"obsidian-semantic-mcp: invalid native runtime configuration ({type(exc).__name__})", file=sys.stderr)
        sys.exit(1)
    docker_mode = os.environ.get("OSM_DOCKER")
    project_root = None if native_runtime_loaded and docker_mode == "0" else _project_root()

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
    _validate_retrieval_env()
    _run_server()


if __name__ == "__main__":
    main()
