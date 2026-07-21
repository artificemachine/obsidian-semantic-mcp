#!/usr/bin/env python3
"""
osm_init.py — Obsidian Semantic MCP setup wizard.

Usage (after uv sync):
  python3 osm_init.py init      Interactive setup wizard
  python3 osm_init.py status    Check service health
  python3 osm_init.py rebuild   Rebuild Docker images

Or via the scripts/osm wrapper:
  scripts/osm init
"""

from __future__ import annotations

import io
import json
import os
import platform
from importlib.metadata import PackageNotFoundError, version
import tomllib
import requests
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
import secrets

# Ensure stdout/stderr can handle Unicode on Windows (cp1252 etc.)
if sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower().replace("-", "") != "utf8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

def _version_from_pyproject() -> str | None:
    pyproject = Path(__file__).resolve().parent / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        project = data.get("project", {})
        v = project.get("version")
        return str(v) if v else None
    except Exception:
        return None


try:
    APP_VERSION = version("obsidian-semantic-mcp")
except PackageNotFoundError:
    APP_VERSION = _version_from_pyproject() or "0.0.0"


# ── Terminal output ───────────────────────────────────────────────────────────

_TTY = sys.stdout.isatty()


def _c(code, text):
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def ok(msg):
    # Under a dry run, run()/writes are faked (see run()'s DRY_RUN branch), so
    # an unprefixed "✓ Setup complete!" would assert an outcome that never
    # happened. Mark every success line as simulated instead.
    if DRY_RUN:
        print(f"  {_c('93', '○')}  [dry-run] {msg}")
        return
    print(f"  {_c('92', '✓')}  {msg}")


def warn(msg):
    print(f"  {_c('93', '⚠')}  {msg}")


def fail(msg):
    print(f"  {_c('91', '✗')}  {msg}")


def info(msg):
    print(f"  {_c('94', '→')}  {msg}")


def header(msg):
    print(f"\n{_c('1', msg)}")


def hr():
    print("─" * 60)


# ── Dry-run state ─────────────────────────────────────────────────────────────

DRY_RUN = False
_DRY_ACTIONS: list[str] = []  # collects every skipped action for the summary


def _dry(label, detail=""):
    """Print a dry-run notice and record it for the end-of-run summary."""
    line = f"{label}{('  # ' + detail) if detail else ''}"
    print(f"  {_c('90', '[dry-run]')}  {line}")
    _DRY_ACTIONS.append(line)


# ── Subprocess helpers ────────────────────────────────────────────────────────


def run(cmd, check=True, capture=False, env=None):
    if DRY_RUN:
        cmd_str = cmd if isinstance(cmd, str) else " ".join(str(a) for a in cmd)
        _dry(cmd_str)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    kwargs: dict = {"shell": isinstance(cmd, str), "check": check}
    if env:
        kwargs["env"] = env
    if capture:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return subprocess.run(cmd, **kwargs)


def cmd_exists(name):
    return shutil.which(name) is not None


def _stdin_is_tty():
    return bool(getattr(sys.stdin, "isatty", lambda: False)())


# ── Non-interactive params (set by CLI flags, consumed by prompt_*) ───────────

# Keys: vault, pg_password, mode, persistent, data_dir,
#       ssh_host, ssh_user, ssh_port, ssh_key, vault_remote
_PARAMS: dict = {}


# ── Prompts ───────────────────────────────────────────────────────────────────


def prompt(question, default=None, choices=None, param_key=None):
    # Non-interactive: use pre-supplied value from CLI flags
    if param_key and param_key in _PARAMS:
        val = str(_PARAMS[param_key])
        if choices and val not in choices:
            fail(
                f"--{param_key.replace('_', '-')} {val!r} is not one of: {', '.join(choices)}"
            )
            sys.exit(1)
        info(f"{question}: {_c('1', val)}  (from --{param_key.replace('_', '-')})")
        return val

    _EXIT_WORDS = {"q", "quit", "exit", "skip"}
    hint = f" [{default}]" if default else ""
    if choices:
        hint = f" ({'/'.join(choices)}, q to quit)"
    while True:
        try:
            answer = input(f"  {question}{hint}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)
        if answer.lower() in _EXIT_WORDS:
            print()
            sys.exit(0)
        if not answer and default is not None:
            return default
        if choices and answer not in choices:
            print(f"  Please enter one of: {', '.join(choices)}")
            continue
        if answer:
            return answer
        print("  Please enter a value.")


def confirm(question, default="y", param_key=None):
    return (
        prompt(
            question, default=default, choices=["y", "n"], param_key=param_key
        ).lower()
        == "y"
    )


def _prompt_single_vault():
    """Prompt for a single vault path interactively. Returns resolved path string."""
    while True:
        raw = prompt("Absolute path to your Obsidian vault")
        p = Path(raw).expanduser().resolve()
        if p.is_dir():
            valid, msg = _validate_vault(str(p))
            if valid:
                ok(msg)
            else:
                warn(msg)
            return str(p)
        fail(f"Directory not found: {p}")


def prompt_vault():
    """Prompt for one or more vault paths. Returns a list of path strings."""
    # --vault flag: single vault, no interactive multi-vault prompt
    if "vault" in _PARAMS:
        p = Path(_PARAMS["vault"]).expanduser().resolve()
        if not p.is_dir():
            fail(f"Vault not found: {p}")
            sys.exit(1)
        info(f"Vault: {_c('1', str(p))}  (from --vault)")
        valid, msg = _validate_vault(str(p))
        if valid:
            ok(msg)
        else:
            warn(msg)
        return [str(p)]

    # Check existing env vars
    existing_multi = os.environ.get("OBSIDIAN_VAULTS", "")
    existing = os.environ.get("OBSIDIAN_VAULT", "")
    print()

    if existing_multi:
        paths = [v.strip() for v in existing_multi.split(",") if v.strip()]
        info(f"OBSIDIAN_VAULTS is already set: {', '.join(paths)}")
        if confirm("Use these vaults?"):
            for v in paths:
                valid, msg = _validate_vault(v)
                if valid:
                    ok(msg)
                else:
                    warn(msg)
            return paths
    elif existing:
        info(f"OBSIDIAN_VAULT is already set: {existing}")
        if confirm("Use this vault?"):
            valid, msg = _validate_vault(existing)
            if valid:
                ok(msg)
            else:
                warn(msg)
            if confirm("Add more vaults?", default="n"):
                vaults = [existing]
                while True:
                    v = _prompt_single_vault()
                    vaults.append(v)
                    if not confirm("Add another vault?", default="n"):
                        break
                return vaults
            return [existing]

    # Fresh prompt — collect first vault, then offer to add more
    v = _prompt_single_vault()
    vaults = [v]
    if confirm("Add more vaults?", default="n"):
        while True:
            v = _prompt_single_vault()
            vaults.append(v)
            if not confirm("Add another vault?", default="n"):
                break
    return vaults


def prompt_pg_password():
    return prompt(
        "Postgres password (used for the local Docker DB)",
        default="obsidian",
        param_key="pg_password",
    )


def prompt_persistent_storage(include_ollama=False):
    """
    Ask whether to use bind-mount directories for persistent storage.

    Named Docker volumes are wiped by `docker compose down -v`.
    Bind mounts survive it — data lives in a host directory the user controls.

    Returns (pgdata_path, ollama_data_path).  Either may be None (named volume).
    ollama_data_path is only populated when include_ollama=True.
    """
    print()
    print("  Database storage:\n")
    print("    Named volume  — managed by Docker; wiped by  docker compose down -v")
    print(
        "    Bind mount    — lives in a local directory; survives  docker compose down -v"
    )
    print()
    if not confirm(
        "Use a persistent bind mount? (recommended)",
        default="y",
        param_key="persistent",
    ):
        return None, None

    default_dir = str(Path.home() / ".local" / "share" / "obsidian-semantic-mcp")
    # When --persistent is set without --data-dir, use the default silently
    if "persistent" in _PARAMS and "data_dir" not in _PARAMS:
        _PARAMS["data_dir"] = default_dir
    raw = prompt("Local data directory", default=default_dir, param_key="data_dir")
    data_dir = Path(raw).expanduser().resolve()

    pgdata_path = str(data_dir / "pgdata")
    ollama_data_path = str(data_dir / "ollama") if include_ollama else None

    for p in filter(None, [pgdata_path, ollama_data_path]):
        if DRY_RUN:
            _dry(f"mkdir -p {p}")
        else:
            Path(p).mkdir(parents=True, exist_ok=True)
            ok(f"Data directory: {p}")

    return pgdata_path, ollama_data_path


# ── Prerequisite checks ───────────────────────────────────────────────────────

OSM_CONFIG_DIR = Path.home() / ".config" / "obsidian-semantic-mcp"
PROJECT_ROOT_FILE = OSM_CONFIG_DIR / "project_root"

# Directory holding this module. In an editable / dev / install.sh layout the
# compose stack lives here; in a wheel install it does not.
_CODE_DIR = Path(__file__).parent.resolve()


def _default_deploy_dir() -> Path:
    """Stable, code-independent location for the runtime stack."""
    override = os.environ.get("OSM_DATA_DIR")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(xdg) / "obsidian-semantic-mcp"


def _resolve_project_root() -> Path:
    """Locate the docker-compose project dir, independent of where this code lives.

    Order: OSM_PROJECT_ROOT env -> recorded config -> co-located compose
    (dev checkout / install.sh self-contained dir) -> default deploy dir.
    """
    env = os.environ.get("OSM_PROJECT_ROOT")
    if env:
        return Path(env)
    if PROJECT_ROOT_FILE.exists():
        try:
            recorded = PROJECT_ROOT_FILE.read_text(encoding="utf-8").strip()
            if recorded:
                return Path(recorded)
        except OSError:
            pass
    if (_CODE_DIR / "docker-compose.yml").exists():
        return _CODE_DIR
    return _default_deploy_dir()


PROJECT_ROOT = _resolve_project_root()
EMBED_MODEL = "nomic-embed-text"


def _install_docker():
    """Attempt to install Docker Desktop via the platform package manager."""
    system = platform.system()

    if system == "Darwin":
        if not cmd_exists("brew"):
            fail("Homebrew not found — install it first: https://brew.sh")
            return False
        info("Installing Docker Desktop via Homebrew…")
        r = run(["brew", "install", "--cask", "docker"], check=False)
        if r.returncode != 0:
            fail("Docker installation failed")
            return False
        ok("Docker Desktop installed — please launch it from Applications")

    elif system == "Windows":
        if cmd_exists("winget"):
            info("Installing Docker Desktop via winget…")
            r = run(
                [
                    "winget",
                    "install",
                    "-e",
                    "--id",
                    "Docker.DockerDesktop",
                    "--accept-source-agreements",
                    "--accept-package-agreements",
                ],
                check=False,
            )
            if r.returncode != 0:
                fail("Docker installation failed")
                return False
            ok("Docker Desktop installed — please launch it from the Start menu")
        else:
            fail(
                "winget not found — install Docker Desktop manually: "
                "https://docs.docker.com/get-docker/"
            )
            return False

    elif system == "Linux":
        # Prefer apt (Debian/Ubuntu) — most common desktop Linux
        if cmd_exists("apt-get"):
            info("Installing Docker Engine via apt…")
            cmds = [
                "curl -fsSL https://get.docker.com | sh",
            ]
            for c in cmds:
                r = run(c, check=False)
                if r.returncode != 0:
                    fail("Docker installation failed")
                    return False
            ok("Docker Engine installed")
        else:
            fail(
                "No supported Linux installer found — install Docker manually: "
                "https://docs.docker.com/get-docker/"
            )
            return False
    else:
        fail(f"Auto-install not supported on {system}")
        return False

    return True


def _start_docker_daemon():
    """Attempt to start Docker Desktop / daemon."""
    system = platform.system()

    if system == "Darwin":
        info("Starting Docker Desktop…")
        run(["open", "-a", "Docker"], check=False)
    elif system == "Windows":
        docker_paths = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / "Docker"
            / "Docker"
            / "Docker Desktop.exe",
        ]
        for p in docker_paths:
            if p.exists():
                info("Starting Docker Desktop…")
                subprocess.Popen(
                    [str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                break
        else:
            warn("Could not find Docker Desktop executable — start it manually")
            return False
    elif system == "Linux":
        info("Starting Docker daemon…")
        run(["sudo", "systemctl", "start", "docker"], check=False)
    else:
        return False

    # Poll for the daemon to become ready
    info("Waiting for Docker daemon to start…")
    for i in range(30):
        time.sleep(2)
        r = subprocess.run(["docker", "info"], capture_output=True)
        if r.returncode == 0:
            ok("Docker is running")
            return True
    fail("Docker daemon did not start in time — start it manually and re-run osm init")
    return False


def check_docker():
    if not cmd_exists("docker"):
        fail("Docker not found")
        if DRY_RUN:
            _dry("install Docker Desktop")
            return True
        if not _stdin_is_tty():
            fail(
                "Docker is missing and stdin is not interactive — "
                "install Docker manually and re-run osm init"
            )
            return False
        if confirm("Install Docker Desktop now?", default="y"):
            if not _install_docker():
                return False
            # After install, binary may not be on PATH in this session
            if not cmd_exists("docker"):
                warn("Docker was installed but is not on PATH yet")
                info("Please restart your terminal and re-run: osm init")
                return False
        else:
            info("Install Docker Desktop manually: https://docs.docker.com/get-docker/")
            return False

    r = run(["docker", "info"], check=False, capture=True)
    if r.returncode != 0:
        fail("Docker daemon is not running")
        if DRY_RUN:
            _dry("start Docker Desktop")
            return True
        if not _stdin_is_tty():
            fail(
                "Docker daemon is not running and stdin is not interactive — "
                "start Docker Desktop manually and re-run osm init"
            )
            return False
        if confirm("Start Docker Desktop now?", default="y"):
            if not _start_docker_daemon():
                return False
        else:
            info("Start Docker Desktop and re-run: osm init")
            return False

    ok("Docker is running")
    return True


def check_compose():
    r = run(["docker", "compose", "version"], check=False, capture=True)
    if r.returncode != 0:
        fail("docker compose v2 not found — upgrade Docker Desktop")
        return False
    ok("docker compose v2 available")
    return True


def check_ollama_at(host, port=11434):
    url = f"http://{host}:{port}/api/tags"
    try:
        urllib.request.urlopen(url, timeout=4)
        ok(f"Ollama reachable at {host}:{port}")
        return True
    except Exception:
        fail(f"Ollama not reachable at {host}:{port}")
        return False


def _ollama_running_locally(port=11434):
    """Silent probe — returns True if Ollama answers on localhost, no output."""
    try:
        urllib.request.urlopen(f"http://localhost:{port}/api/tags", timeout=2)
        return True
    except Exception:
        return False


def _normalize_ollama_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return "http://localhost:11434"
    if "://" not in url:
        url = f"http://{url}"
    return url.rstrip("/")


def _status_ollama_url(env: dict | None = None) -> str:
    """Return the host-side Ollama URL that `osm status` should probe.

    The runtime `.env` stores the URL from the Docker container's perspective
    (for example `http://ollama:11434` or `http://host.docker.internal:11434`),
    which is not always directly reachable from the host CLI. This helper maps
    those internal URLs back to the host-side probe target.
    """
    env = env or {}

    # Remote Ollama over SSH always reconnects on a localhost tunnel.
    local_port = env.get("OSM_SSH_LOCAL_PORT")
    if local_port:
        return f"http://localhost:{local_port}"

    configured = _normalize_ollama_url(env.get("OLLAMA_URL", "http://localhost:11434"))
    parsed = urllib.parse.urlparse(configured)
    host = parsed.hostname or "localhost"
    port = parsed.port or 11434

    # Full Docker mode exposes the Ollama container on host port 11435.
    if host == "ollama":
        return "http://localhost:11435"

    # Docker + host Ollama stores the container-side bridge hostname in .env.
    if host in {"host.docker.internal", "172.17.0.1"}:
        return f"http://localhost:{port}"

    return f"http://{host}:{port}"


def _ollama_error_detail(resp) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            return str(data["error"])
    except Exception:
        pass
    return (getattr(resp, "text", "") or f"HTTP {resp.status_code}").strip()


def _print_ollama_restart_hint():
    if platform.system() == "Darwin":
        info("Try: brew services restart ollama")
        info("Logs: /opt/homebrew/var/log/ollama.log")
    else:
        info("Restart the Ollama daemon and inspect its logs")


def check_ollama_inference_at(ollama_url: str, model: str = EMBED_MODEL) -> bool:
    """Probe the embeddings endpoint so status catches broken model execution."""
    base = _normalize_ollama_url(ollama_url)
    try:
        resp = requests.post(
            f"{base}/api/embeddings",
            json={"model": model, "prompt": "healthcheck"},
            timeout=30,
        )
    except requests.RequestException as exc:
        fail(f"Ollama embeddings not reachable at {base}")
        info(f"Reason: {exc}")
        return False

    if resp.status_code >= 400:
        detail = _ollama_error_detail(resp)
        fail(f"Ollama embeddings failing at {base}")
        info(f"Reason: {detail}")
        lower = detail.lower()
        if "model failed to load" in lower or "runner process has terminated" in lower:
            _print_ollama_restart_hint()
        return False

    try:
        data = resp.json()
    except ValueError as exc:
        fail(f"Ollama embeddings returned invalid JSON at {base}")
        info(f"Reason: {exc}")
        return False

    if not data.get("embedding"):
        fail(f"Ollama embeddings returned no vector at {base}")
        info(f"Model: {model}")
        return False

    ok(f"Ollama embeddings responding at {base} ({model})")
    return True


# ── SSH tunnel helpers ────────────────────────────────────────────────────────

_SSH_KEY_CANDIDATES = [
    ".ssh/id_ed25519",
    ".ssh/id_rsa",
    ".ssh/id_ecdsa",
    ".ssh/id_ecdsa_sk",
]


def _default_ssh_key():
    """Return the first SSH private key found in $HOME/.ssh, or empty string."""
    for name in _SSH_KEY_CANDIDATES:
        p = Path.home() / name
        if p.exists():
            return str(p)
    return ""


def _test_ssh_connection(host: str, user: str, port: int, key_path: str | None) -> bool:
    """Test SSH connectivity before launching the tunnel."""
    cmd = [
        "ssh",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
    ]
    if key_path:
        cmd += ["-i", key_path]
    if port != 22:
        cmd += ["-p", str(port)]
    cmd += [f"{user}@{host}", "echo", "ok"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def open_ssh_tunnel(user, host, remote_port, local_port, key_path=None):
    """
    Open an SSH port-forward tunnel in the background:
      local_port  ->  host:remote_port

    Uses -o ExitOnForwardFailure so the ssh process exits immediately if
    binding fails, instead of silently hanging.
    """
    # StrictHostKeyChecking=accept-new trusts the host key on first connection
    # and rejects changed keys on subsequent connections. This is vulnerable to
    # MITM on the very first connection. If the remote host key is known, add it
    # to $HOME/.ssh/known_hosts before running osm init to eliminate this window.
    cmd = [
        "ssh",
        "-N",
        "-f",  # background, no remote command
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ExitOnForwardFailure=yes",
        "-L",
        f"{local_port}:localhost:{remote_port}",
        f"{user}@{host}",
    ]
    if key_path:
        cmd += ["-i", key_path]

    if DRY_RUN:
        _dry(" ".join(cmd), f"tunnel localhost:{local_port} → {host}:{remote_port}")
        return True
    r = subprocess.run(cmd, check=False)
    if r.returncode == 0:
        ok(f"SSH tunnel open: localhost:{local_port} → {host}:{remote_port}")
        return True
    fail("SSH tunnel failed — check host, user, and key")
    return False


def prompt_ssh_credentials():
    """
    Interactively collect SSH connection details.
    Returns (user, host, remote_port, key_path_or_None).
    """
    print()
    remote_host = prompt("Remote host (IP address or hostname)", param_key="ssh_host")
    remote_port = prompt("Remote Ollama port", default="11434", param_key="ssh_port")
    ssh_user = prompt(
        "SSH username", default=os.environ.get("USER", "ubuntu"), param_key="ssh_user"
    )

    key_path = None
    if "ssh_key" in _PARAMS:
        key_path = str(Path(_PARAMS["ssh_key"]).expanduser().resolve())
        info(f"SSH key: {_c('1', key_path)}  (from --ssh-key)")
    else:
        print()
        print("  SSH authentication:")
        print("    1)  Private key  (recommended)")
        print("    2)  Password / SSH agent")
        auth = prompt("Choose", choices=["1", "2"])

        if auth == "1":
            default_key = _default_ssh_key()
            default_fallback = str(Path.home() / ".ssh" / "id_ed25519")
            raw = prompt(
                "Path to SSH private key", default=default_key or default_fallback
            )
            key_path = str(Path(raw).expanduser().resolve())
            if not Path(key_path).exists():
                warn(f"Key file not found: {key_path}")
                if not confirm("Continue anyway?", default="n"):
                    sys.exit(0)
        else:
            info("Using SSH agent or password — you may be prompted by ssh")

    if key_path:
        import stat

        kp = Path(key_path)
        if kp.exists():
            key_stat = os.stat(kp)
            if key_stat.st_mode & (stat.S_IRGRP | stat.S_IROTH):
                warn(
                    f"SSH key {key_path} has too-open permissions. Run: chmod 600 {key_path}"
                )

    return ssh_user, remote_host, int(remote_port), key_path


# ── Vault and model helpers ───────────────────────────────────────────────────


def _validate_vault(vault_path: str) -> tuple[bool, str]:
    """Check that vault path is a valid Obsidian vault."""
    p = Path(vault_path)
    if not p.exists():
        return False, f"Path does not exist: {vault_path}"
    if not p.is_dir():
        return False, f"Not a directory: {vault_path}"
    md_files = list(p.rglob("*.md"))
    if len(md_files) == 0:
        return False, f"No .md files found in {vault_path} — is this an Obsidian vault?"
    return True, f"✓ Found {len(md_files)} markdown files"


def _verify_ollama_model(ollama_url: str, model: str) -> bool:
    """Verify the model is available by running a minimal embedding request."""
    try:
        resp = requests.post(
            f"{ollama_url}/api/embeddings",
            json={"model": model, "prompt": "test"},
            timeout=30,
        )
        data = resp.json()
        return bool(data.get("embedding"))
    except Exception:
        return False


def _ensure_ollama_model(ollama_url: str, model: str = EMBED_MODEL) -> None:
    """Pull the embedding model if it is not already available, then verify."""
    header("Embedding model")
    info(f"Checking whether {model} is available…")

    if _verify_ollama_model(ollama_url, model):
        ok(f"{model} already available — embeddings responding")
        return

    # Model not loaded — try pulling via the Ollama API (works for both
    # local and remote Ollama instances reachable over HTTP).
    info(f"Pulling {model} (first run may take a few minutes)…")
    if DRY_RUN:
        _dry(f"POST {ollama_url}/api/pull  model={model}")
        return

    try:
        resp = requests.post(
            f"{ollama_url}/api/pull",
            json={"model": model},
            timeout=600,
            stream=True,
        )
        resp.raise_for_status()
        # Ollama streams JSON status lines — consume them so the pull completes.
        for line in resp.iter_lines():
            pass
    except Exception as exc:
        warn(f"API pull failed ({exc})")
        # Fallback: try the CLI if ollama is installed locally
        if cmd_exists("ollama"):
            info(f"Falling back to:  ollama pull {model}")
            run(["ollama", "pull", model], check=False)
        else:
            warn(
                f"Could not pull {model} — indexing will fail until the model is available"
            )
            info(f"Fix manually:  ollama pull {model}")
            return

    # Verify after pull
    if _verify_ollama_model(ollama_url, model):
        ok(f"{model} verified — embeddings responding")
    else:
        warn(
            f"Model pull completed but verification failed — embeddings did not respond"
        )
        info(f"Try manually:  ollama pull {model}")


# ── .env writer (runtime only — gitignored) ───────────────────────────────────


# Path shared with src/config.py:resolve_dashboard_token() — keep in sync.
# Inlined here because osm_init.py is force-shipped in the wheel and can't
# easily import from the src/ package on a fresh install.
_OSM_CONFIG_DIR = Path.home() / ".config" / "obsidian-semantic-mcp"
_DASHBOARD_TOKEN_FILE = _OSM_CONFIG_DIR / "dashboard_token"


def _generate_dashboard_token() -> str:
    """Generate or reuse the dashboard bearer token that guards the
    mutating endpoints (/api/reindex, /api/reindex/full, /api/prune,
    /api/ollama/start).

    Reuses ~/.config/obsidian-semantic-mcp/dashboard_token if present
    so the token survives `osm init` re-runs and `osm update` /
    `osm rebuild` (no auth flap). On a fresh install, generates a new
    token_urlsafe(32) and persists it mode 0600 — never written under
    the repo checkout.

    Mirrors src/config.py:resolve_dashboard_token() precedence but is
    duplicated here because osm_init.py is the wheel's entrypoint and
    can't import from src/ at first-run time.
    """
    if env_token := os.environ.get("DASHBOARD_TOKEN"):
        return env_token
    if _DASHBOARD_TOKEN_FILE.exists():
        try:
            existing = _DASHBOARD_TOKEN_FILE.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        except OSError:
            pass
    token = secrets.token_urlsafe(32)
    try:
        _OSM_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _DASHBOARD_TOKEN_FILE.write_text(token, encoding="utf-8")
        _DASHBOARD_TOKEN_FILE.chmod(0o600)
    except OSError:
        pass
    return token


def write_env(
    vault,
    pg_password,
    ollama_url,
    ssh_params=None,
    pgdata_path=None,
    ollama_data_path=None,
    compose_profiles=None,
    dashboard_token=None,
):
    """
    Write .env in the project root at runtime. This file is gitignored.

    vault may be a string (single vault) or list of strings (multi-vault).

    ssh_params, if provided, is a dict with keys:
      user, host, remote_port, local_port, key_path (optional)
    These are stored as OSM_SSH_* vars so `osm tunnel` can reconnect.

    pgdata_path / ollama_data_path, if set, are written as PGDATA_PATH /
    OLLAMA_DATA_PATH so Docker Compose uses bind mounts instead of named volumes.

    dashboard_token, if None, is auto-generated via _generate_dashboard_token()
    so Docker users don't get the ephemeral/invisible container-side token.
    Pass an explicit value to override (used by tests).
    """
    env_path = PROJECT_ROOT / ".env"
    vaults = vault if isinstance(vault, list) else [vault]
    if dashboard_token is None:
        dashboard_token = _generate_dashboard_token()
    if len(vaults) > 1:
        lines = [
            f"OBSIDIAN_VAULTS={','.join(vaults)}",
            f"OBSIDIAN_VAULT={vaults[0]}",
            f"POSTGRES_PASSWORD={pg_password}",
            f"OLLAMA_URL={ollama_url}",
            f"DASHBOARD_TOKEN={dashboard_token}",
        ]
    else:
        lines = [
            f"OBSIDIAN_VAULT={vaults[0]}",
            f"POSTGRES_PASSWORD={pg_password}",
            f"OLLAMA_URL={ollama_url}",
            f"DASHBOARD_TOKEN={dashboard_token}",
        ]
    if pgdata_path:
        lines.append(f"PGDATA_PATH={pgdata_path}")
    if ollama_data_path:
        lines.append(f"OLLAMA_DATA_PATH={ollama_data_path}")
    if compose_profiles:
        # Persist the active profile so every `docker compose` in this dir
        # (bare `up -d`, the RUNBOOK `down -v; up -d`, restarts) brings up the
        # embeddings engine. Without it, full-docker installs silently lose Ollama.
        lines.append(f"COMPOSE_PROFILES={compose_profiles}")
    if ssh_params:
        lines += [
            "",
            "# SSH tunnel config — used by: scripts/osm tunnel",
            f"OSM_SSH_USER={ssh_params['user']}",
            f"OSM_SSH_HOST={ssh_params['host']}",
            f"OSM_SSH_REMOTE_PORT={ssh_params['remote_port']}",
            f"OSM_SSH_LOCAL_PORT={ssh_params['local_port']}",
        ]
        if ssh_params.get("key_path"):
            lines.append(f"OSM_SSH_KEY={ssh_params['key_path']}")
    lines.append("")
    if DRY_RUN:
        _dry(f"write {env_path}", "contents shown below")
        print()
        for l in lines:
            print(f"    {_c('90', l)}")
        print()
        return
    env_path.write_text("\n".join(lines))
    env_path.chmod(0o600)  # contains POSTGRES_PASSWORD — owner-only read/write
    ok(f"Wrote {env_path}")

def _ensure_deploy_dir():
    """Ensure the resolved PROJECT_ROOT exists and holds docker-compose.yml.

    For wheel / pip installs the compose file is not co-located with the code,
    so copy the packaged copy into the deploy dir. No-op when it is already
    present (editable / install.sh / dev checkout).
    """
    compose = PROJECT_ROOT / "docker-compose.yml"
    if compose.exists():
        return
    packaged = _CODE_DIR / "docker-compose.yml"
    if DRY_RUN:
        _dry(f"mkdir -p {PROJECT_ROOT}", "deploy dir")
        _dry(f"copy {packaged} -> {compose}")
        return
    if not packaged.exists():
        fail(f"No docker-compose.yml at {PROJECT_ROOT} and none packaged at {_CODE_DIR}")
        sys.exit(1)
    PROJECT_ROOT.mkdir(parents=True, exist_ok=True)
    shutil.copy2(packaged, compose)
    ok(f"Provisioned compose stack into {PROJECT_ROOT}")


def _write_project_root_config():
    """
    Persist the project root to ~/.config/obsidian-semantic-mcp/project_root
    so the launcher can find it automatically.
    """
    if DRY_RUN:
        _dry(f"write {PROJECT_ROOT_FILE}", f"contents: {PROJECT_ROOT}")
        return
    try:
        OSM_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        PROJECT_ROOT_FILE.write_text(str(PROJECT_ROOT), encoding="utf-8")
        ok(f"Registered project root: {PROJECT_ROOT}")
    except Exception as e:
        warn(f"Could not write {PROJECT_ROOT_FILE}: {e}")


# ── Claude Code CLI registration ──────────────────────────────────────────────


def _claude_cli_already_registered() -> bool:
    """Return True if obsidian-semantic is already in the MCP list."""
    r = run(["claude", "mcp", "list"], check=False, capture=True)
    return "obsidian-semantic" in (r.stdout or "")


def register_claude_cli(entry):
    """
    Register the MCP server with Claude Code CLI via `claude mcp add --scope user`.

    This writes to $HOME/.claude.json (separate from claude_desktop_config.json).
    Silently skips if the `claude` CLI is not installed.

    obsidian-semantic is a single global server — shared across all projects.
    If it is already registered, this is a no-op with an informational message.
    """
    if not cmd_exists("claude"):
        return  # Claude Code CLI not installed — Desktop config is enough

    if DRY_RUN:
        cmd_args = entry.get("args", [])
        env_pairs = [f"{k}={v}" for k, v in entry.get("env", {}).items()]
        cli_cmd = ["claude", "mcp", "add", "--scope", "user"]
        for pair in env_pairs:
            cli_cmd += ["-e", pair]
        cli_cmd += ["obsidian-semantic", "--", entry["command"]] + cmd_args
        _dry(" ".join(str(a) for a in cli_cmd))
        return

    if _claude_cli_already_registered():
        ok(
            "Claude Code CLI: obsidian-semantic already registered — global, shared across all projects"
        )
        return

    cmd_args = entry.get("args", [])
    env_pairs = [f"{k}={v}" for k, v in entry.get("env", {}).items()]
    cli_cmd = ["claude", "mcp", "add", "--scope", "user"]
    for pair in env_pairs:
        cli_cmd += ["-e", pair]
    cli_cmd += ["obsidian-semantic", "--", entry["command"]] + cmd_args

    r = run(cli_cmd, check=False)
    if r.returncode == 0:
        ok("Claude Code CLI: obsidian-semantic registered  (claude mcp list to verify)")
    else:
        warn("claude mcp add failed — add manually:")
        info(f"  {' '.join(str(a) for a in cli_cmd)}")


# ── Claude Desktop config ─────────────────────────────────────────────────────


def _claude_cfg_path():
    system = platform.system()
    if system == "Darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude_desktop_config.json"
        )
    if system == "Linux":
        return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"
    if system == "Windows":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            return Path(appdata) / "Claude" / "claude_desktop_config.json"
    return None


def update_claude_config(entry):
    path = _claude_cfg_path()
    if not path:
        warn("Unknown platform — update claude_desktop_config.json manually")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg = {}
    if path.exists():
        try:
            cfg = json.loads(path.read_text())
        except json.JSONDecodeError:
            warn(f"Could not parse {path} — mcpServers section will be reset")
    if cfg.get("mcpServers", {}).get("obsidian-semantic"):
        ok(
            f"Claude Desktop: obsidian-semantic already configured — global, shared across all projects"
        )
        return
    cfg.setdefault("mcpServers", {})["obsidian-semantic"] = entry
    # Show the full merged config so the user can see what other entries are preserved
    pretty = json.dumps(cfg, indent=2)
    if DRY_RUN:
        _dry(f"write {path}", "merged config shown below")
        print()
        for line in pretty.splitlines():
            print(f"    {_c('90', line)}")
        print()
        return
    path.write_text(json.dumps(cfg, indent=2) + "\n")
    ok(f"Updated {path}")
    info("Restart Claude Desktop to pick up the new server")


# ── OpenCode config ───────────────────────────────────────────────────────────


def _opencode_cfg_path() -> Path:
    """OpenCode reads MCP servers from ~/.config/opencode/opencode.json on every platform."""
    return Path.home() / ".config" / "opencode" / "opencode.json"


def update_opencode_config(entry):
    """Merge the obsidian-semantic entry into ~/.config/opencode/opencode.json.

    Converts the standard MCP config entry {command, args, env} into OpenCode's
    native format: command as a flat array, ``type: "local"``, ``enabled: true``.

    Idempotent — preserves other entries, warns on parse errors, and returns
    silently if OpenCode has never been run on this machine (no config file).
    """
    path = _opencode_cfg_path()
    cfg = {}
    if path.exists():
        try:
            cfg = json.loads(path.read_text())
        except json.JSONDecodeError:
            warn(f"Could not parse {path} — mcp section will be reset")
    # Convert standard {command, args, env} to OpenCode's native format
    opencode_entry = {
        "command": [entry["command"]] + entry.get("args", []),
        "enabled": True,
        "type": "local",
    }
    if cfg.setdefault("mcp", {}).get("obsidian-semantic"):
        ok("OpenCode: obsidian-semantic already configured — global, shared across all projects")
        return
    cfg.setdefault("mcp", {})["obsidian-semantic"] = opencode_entry
    pretty = json.dumps(cfg, indent=2)
    if DRY_RUN:
        _dry(f"write {path}", "merged config shown below")
        print()
        for line in pretty.splitlines():
            print(f"    {_c('90', line)}")
        print()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(pretty + "\n")
    ok(f"Updated {path}")
    info("Restart OpenCode to pick up the new server")


def remove_opencode_config():
    """Delete the obsidian-semantic entry from ~/.config/opencode/opencode.json if present."""
    path = _opencode_cfg_path()
    if not path.exists():
        info("OpenCode config not found — skipping")
        return
    if DRY_RUN:
        _dry(f"remove obsidian-semantic entry from {path}")
        return
    try:
        cfg = json.loads(path.read_text())
        servers = cfg.get("mcp", {})
        if "obsidian-semantic" in servers:
            del servers["obsidian-semantic"]
            path.write_text(json.dumps(cfg, indent=2) + "\n")
            ok(f"Removed obsidian-semantic from {path}")
            info("Restart OpenCode to apply")
        else:
            info("obsidian-semantic not found in OpenCode config — skipping")
    except json.JSONDecodeError:
        warn(f"Could not parse {path} — remove entry manually")


# ── pi agent (earendil-works/pi) config ──────────────────────────────────────


def _pi_agent_mcp_path() -> Path:
    """pi agent MCP server config: ~/.pi/agent/mcp.json"""
    return Path.home() / ".pi" / "agent" / "mcp.json"


def _pi_agent_ext_path() -> Path:
    """pi mcp-bridge extension: ~/.pi/agent/extensions/mcp-bridge.ts"""
    return Path.home() / ".pi" / "agent" / "extensions" / "mcp-bridge.ts"


_MCP_BRIDGE_SPAWN_HEARTBEAT_MARKER = "// Start heartbeat immediately at spawn time"


def _patch_pi_mcp_bridge(ext_path: Path):
    """Patch mcp-bridge.ts to start the heartbeat at spawn time rather than
    after the initialize response.

    obsidian-semantic uses a blocking ``for line in sys.stdin.buffer:`` loop
    as its asyncio stdin transport.  That loop freezes the event loop between
    reads, so MCP responses can only be flushed when the next line arrives.
    The heartbeat (periodic \\n writes) must therefore start *before*
    ``initialize`` is sent — otherwise initialize itself deadlocks.

    See docs/pi_mcp_bridge_heartbeat.md for the full explanation.
    """
    if not ext_path.exists():
        warn("pi mcp-bridge extension not found — manual step required:")
        info(f"  Ensure {ext_path} starts heartbeat in spawnServer(), not initializeServer()")
        info("  See docs/pi_mcp_bridge_heartbeat.md for the exact patch")
        return

    src = ext_path.read_text()

    if _MCP_BRIDGE_SPAWN_HEARTBEAT_MARKER in src:
        ok("pi mcp-bridge: heartbeat-at-spawn fix already applied")
        return

    OLD_HB_COMMENT = "// Start heartbeat if configured (keeps stdin-fed event loops alive)"
    if OLD_HB_COMMENT not in src:
        warn("pi mcp-bridge: unrecognized format — skipping auto-patch")
        info("  See docs/pi_mcp_bridge_heartbeat.md for the required change")
        return

    SPAWN_HB_BLOCK = (
        "\n"
        "  // Start heartbeat immediately at spawn time for servers that need it.\n"
        "  // Servers using a blocking `for line in sys.stdin.buffer:` loop freeze\n"
        "  // their asyncio event loop between reads. A periodic \\n yields the loop\n"
        "  // so MCP responses can be written — must start before initialize.\n"
        "  if (config.heartbeat && proc.stdin) {\n"
        "    const heartbeatTimer = setInterval(() => {\n"
        "      try {\n"
        '        proc.stdin?.write("\\n");\n'
        "      } catch {\n"
        "        clearInterval(heartbeatTimer);\n"
        "      }\n"
        "    }, 200);\n"
        '    proc.on("close", () => clearInterval(heartbeatTimer));\n'
        "  }"
    )

    INSERT_AFTER = '    buffer: "",\n  };'
    if INSERT_AFTER not in src:
        warn("pi mcp-bridge: can't find insertion point in spawnServer() — skipping patch")
        info("  See docs/pi_mcp_bridge_heartbeat.md for the required change")
        return

    # Insert spawn-time heartbeat block
    src = src.replace(INSERT_AFTER, INSERT_AFTER + "\n" + SPAWN_HB_BLOCK)

    # Remove the old heartbeat block from initializeServer() and simplify tools/list
    OLD_INIT_HB = (
        "  // Start heartbeat if configured (keeps stdin-fed event loops alive)\n"
        "  let heartbeatTimer: ReturnType<typeof setInterval> | null = null;\n"
        "  if (config.heartbeat && server.process.stdin) {\n"
        "    heartbeatTimer = setInterval(() => {\n"
        "      try {\n"
        '        server.process.stdin?.write("\\n");\n'
        "      } catch {\n"
        "        // stdin closed, stop heartbeat\n"
        "        if (heartbeatTimer) clearInterval(heartbeatTimer);\n"
        "      }\n"
        "    }, 200);\n"
        "  }\n"
        "\n"
        "  try {\n"
        "    // Discover tools\n"
        "    const result = (await sendRequest(server, \"tools/list\")) as { tools: ToolDef[] };\n"
        "    return result.tools ?? [];\n"
        "  } finally {\n"
        "    if (heartbeatTimer) clearInterval(heartbeatTimer);\n"
        "  }"
    )
    NEW_DISCOVER = (
        "  // Discover tools\n"
        "  const result = (await sendRequest(server, \"tools/list\")) as { tools: ToolDef[] };\n"
        "  return result.tools ?? [];"
    )

    if OLD_INIT_HB in src:
        src = src.replace(OLD_INIT_HB, NEW_DISCOVER)
        if DRY_RUN:
            _dry(f"patch {ext_path}", "move heartbeat from initializeServer to spawnServer")
        else:
            ext_path.write_text(src)
            ok(f"pi mcp-bridge: patched — heartbeat now starts at spawn time")
    else:
        if DRY_RUN:
            _dry(f"patch {ext_path}", "move heartbeat from initializeServer to spawnServer")
        else:
            warn("pi mcp-bridge: heartbeat section format not recognized — manual patch needed")
            info("  See docs/pi_mcp_bridge_heartbeat.md")


def _pi_enabled() -> bool:
    """Whether to configure the niche `pi` MCP client during init.

    `pi` is a fourth, optional client alongside Claude Desktop / Code /
    OpenCode. It is configured only when the `pi` binary is on PATH AND the
    user has not opted out with OSM_SKIP_PI. The opt-out lets a `pi` user run
    (or record) a clean setup that matches what the majority — who have no
    `pi` — see, without uninstalling anything.
    """
    if os.environ.get("OSM_SKIP_PI", "").strip().lower() in ("1", "true", "yes"):
        return False
    return cmd_exists("pi")


def _mcp_clients_label() -> str:
    """The client list shown in the 'MCP client configuration' heading —
    names `pi` only when it will actually be configured, so a user who will
    never see the pi step also never sees it advertised."""
    clients = "Claude Desktop, Claude Code CLI, OpenCode"
    return f"{clients}, pi" if _pi_enabled() else clients


def register_pi_agent():
    """Register obsidian-semantic with pi's mcp-bridge extension.

    pi uses its own ~/.pi/agent/mcp.json registry (separate from
    claude_desktop_config.json).  The entry requires ``heartbeat: true``
    because obsidian-semantic's asyncio transport uses a blocking stdin loop
    that freezes the event loop between reads — periodic \\n newlines yield
    the loop so responses can be flushed.

    This function also patches ~/.pi/agent/extensions/mcp-bridge.ts when
    present: the heartbeat must start at spawn time, not after initialize,
    because initialize itself depends on the same yield mechanism.

    Silently skips when the ``pi`` binary is not on PATH, or when the user
    opts out with ``OSM_SKIP_PI`` (see _pi_enabled).
    """
    if not _pi_enabled():
        return

    mcp_path = _pi_agent_mcp_path()
    ext_path = _pi_agent_ext_path()

    new_entry = {
        "name": "obsidian-semantic",
        "command": "docker",
        "args": [
            "compose",
            "--project-directory", str(PROJECT_ROOT),
            "exec", "-T", "mcp-server",
            "python3", "/app/src/server.py",
        ],
        "env": {},
        "heartbeat": True,
    }

    if DRY_RUN:
        _dry(
            f"write {mcp_path}",
            "add obsidian-semantic entry with heartbeat: true",
        )
        _patch_pi_mcp_bridge(ext_path)
        return

    mcp_path.parent.mkdir(parents=True, exist_ok=True)

    # Read or initialise mcp.json
    cfg: dict = {"servers": []}
    if mcp_path.exists():
        try:
            cfg = json.loads(mcp_path.read_text())
        except json.JSONDecodeError:
            warn(f"Could not parse {mcp_path} — servers list will be reset")

    servers: list = cfg.get("servers", [])
    idx = next(
        (i for i, s in enumerate(servers) if s.get("name") == "obsidian-semantic"),
        None,
    )

    if idx is not None:
        if servers[idx] == new_entry:
            ok("pi agent: obsidian-semantic already configured correctly")
        else:
            servers[idx] = new_entry
            cfg["servers"] = servers
            mcp_path.write_text(json.dumps(cfg, indent=2) + "\n")
            ok(f"pi agent: updated obsidian-semantic in {mcp_path}")
    else:
        servers.append(new_entry)
        cfg["servers"] = servers
        mcp_path.write_text(json.dumps(cfg, indent=2) + "\n")
        ok(f"pi agent: registered obsidian-semantic in {mcp_path}")
        info("Use /reload in pi to pick up the new server")

    _patch_pi_mcp_bridge(ext_path)


# ── Cross-client registration fan-out ─────────────────────────────────────────


def register_with_clients(entry):
    """Register the MCP entry with every supported client in one shot.

    Today: Claude Desktop, Claude Code CLI, OpenCode, pi agent. Adding a new
    client (Continue, Cursor, Codex CLI, ...) is a one-line change here.
    """
    update_claude_config(entry)
    register_claude_cli(entry)
    update_opencode_config(entry)
    register_pi_agent()
    _write_project_root_config()


# ── Container name resolution ─────────────────────────────────────────────────


def _docker_entry():
    """MCP client config entry for all Docker-based installs."""
    return {"command": "obsidian-semantic-mcp", "args": [], "env": {}}


def _native_entry(vault, db_url):
    """MCP client config entry for local/native installs.

    Unlike Docker mode (whose entry launches into a container that already
    has its own env from docker-compose), a native install's launcher runs
    the server in-process and has no `.env` file to fall back on — the MCP
    client's own `env` dict is the only environment the process ever sees.

    `vault` is a list of one or more paths (see prompt_vault()); mirrors
    server.py's _parse_vault_paths() convention — OBSIDIAN_VAULTS
    (comma-separated) for multi-vault, OBSIDIAN_VAULT for the single-vault
    case.
    """
    vaults = [vault] if isinstance(vault, str) else list(vault)
    env = {"DATABASE_URL": db_url}
    if len(vaults) > 1:
        env["OBSIDIAN_VAULTS"] = ",".join(vaults)
    else:
        env["OBSIDIAN_VAULT"] = vaults[0]
    return {"command": "obsidian-semantic-mcp", "args": [], "env": env}


# ── Docker compose helpers ────────────────────────────────────────────────────


def compose(args, env=None, check=True, capture=False):
    """Run a `docker compose …` command against PROJECT_ROOT.

    Pass-through kwargs mirror `run()`:
      - check=True (default): raise CalledProcessError on non-zero exit.
      - check=False: return the CompletedProcess without raising (callers
        inspect `.returncode` themselves — used by `osm migrate` to surface
        the failure path without crashing the wizard).
      - capture=False (default): stream stdout/stderr to the terminal.
      - capture=True: capture into `.stdout`/`.stderr` (used by smoke checks).
    """
    return run(
        ["docker", "compose", "--project-directory", str(PROJECT_ROOT)] + args,
        env=env,
        check=check,
        capture=capture,
    )


def _vault_basename(v: str) -> str:
    """Last path component for an entry that may be a local path,
    NFS spec (host:/export/path), or CIFS spec (//host/share/path)."""
    if ":/" in v and not v.startswith("/"):
        # NFS host:/export/path → basename of the export path
        _, _, export = v.partition(":")
        return Path(export).name or "vault"
    if v.startswith("//"):
        # CIFS //host/share/path → last path component
        return Path(v[2:]).name or "vault"
    return Path(v).name


def _write_compose_override(vaults):
    """Generate docker-compose.override.yml for vault mounts.

    Branches on the --vault-fs param:
      auto / local — bind-mount each vault path (current behavior; skipped
                     when there is only one vault, since the base compose
                     handles the single-vault case via OBSIDIAN_VAULT).
      nfs          — emit one named volume per vault with NFS driver_opts.
                     Each entry must be in `host:/export/path` form.
      cifs         — emit one named volume per vault with CIFS driver_opts.
                     Each entry must be in `//host/share/path` form;
                     credentials come from --vault-cifs-user/--vault-cifs-pass.
    """
    vault_fs = (_PARAMS.get("vault_fs") or "auto").lower()
    override_path = PROJECT_ROOT / "docker-compose.override.yml"

    if vault_fs in ("auto", "local"):
        if len(vaults) <= 1:
            if override_path.exists():
                override_path.unlink()
            return
        content = _render_bind_mount_override(vaults)
    elif vault_fs == "nfs":
        content = _render_network_override(vaults, "nfs")
    elif vault_fs == "cifs":
        content = _render_network_override(vaults, "cifs")
    else:
        fail(f"Unknown --vault-fs value: {vault_fs!r} (expected auto|local|nfs|cifs)")
        sys.exit(1)

    if DRY_RUN:
        _dry(f"write {override_path}", "contents shown below")
        print()
        for line in content.splitlines():
            print(f"    {_c('90', line)}")
        print()
        return
    override_path.write_text(content)
    ok(f"Wrote {override_path} ({vault_fs}, {len(vaults)} vault{'s' if len(vaults) != 1 else ''})")


def _render_bind_mount_override(vaults: list) -> str:
    container_paths = []
    vol_lines = []
    for v in vaults:
        container_path = f"/{_vault_basename(v)}"
        container_paths.append(container_path)
        vol_lines.append(f"      - {v}:{container_path}")
    vol_block = "\n".join(vol_lines)
    vol_ro_block = "\n".join(f"{l}:ro" for l in vol_lines)
    vaults_env = ",".join(container_paths)
    return (
        "# Auto-generated by osm init for multi-vault support.\n"
        "# Do not edit manually — re-run osm init to regenerate.\n"
        "services:\n"
        "  mcp-server:\n"
        "    environment:\n"
        f"      OBSIDIAN_VAULTS: {vaults_env}\n"
        "    volumes:\n"
        f"{vol_block}\n"
        "  dashboard:\n"
        "    environment:\n"
        f"      OBSIDIAN_VAULTS: {vaults_env}\n"
        "    volumes:\n"
        f"{vol_ro_block}\n"
    )


def _render_network_override(vaults: list, fs: str) -> str:
    """Render an override that backs each vault with a named Docker volume
    using NFS or CIFS driver_opts. The vault path syntax is fs-specific:
      nfs  — host:/export/path
      cifs — //host/share/path
    """
    container_paths = []
    svc_vol_lines = []
    dash_vol_lines = []
    volumes_block = []
    for v in vaults:
        base = _vault_basename(v)
        named = f"obsidian_vault_{base}"
        container_path = f"/{base}"
        container_paths.append(container_path)
        svc_vol_lines.append(f"      - {named}:{container_path}")
        dash_vol_lines.append(f"      - {named}:{container_path}:ro")

        if fs == "nfs":
            host, _, export = v.partition(":")
            if not host or not export.startswith("/"):
                fail(
                    f"--vault-fs=nfs requires entries in 'host:/export/path' form; "
                    f"got {v!r}"
                )
                sys.exit(1)
            opts = f"addr={host},rw,nolock,soft,vers=4"
            device = f":{export}"
            volumes_block.append(
                f"  {named}:\n"
                f"    driver: local\n"
                f"    driver_opts:\n"
                f"      type: nfs\n"
                f'      o: "{opts}"\n'
                f'      device: "{device}"\n'
            )
        elif fs == "cifs":
            if not v.startswith("//"):
                fail(
                    f"--vault-fs=cifs requires entries in '//host/share/path' form; "
                    f"got {v!r}"
                )
                sys.exit(1)
            user = _PARAMS.get("vault_cifs_user", "")
            pw = _PARAMS.get("vault_cifs_pass", "")
            opts = f"username={user},password={pw},uid=1000,gid=1000,vers=3.0"
            volumes_block.append(
                f"  {named}:\n"
                f"    driver: local\n"
                f"    driver_opts:\n"
                f"      type: cifs\n"
                f'      o: "{opts}"\n'
                f'      device: "{v}"\n'
            )

    vaults_env = ",".join(container_paths)
    return (
        f"# Auto-generated by osm init (--vault-fs={fs}).\n"
        "# Do not edit manually — re-run osm init to regenerate.\n"
        "services:\n"
        "  mcp-server:\n"
        "    environment:\n"
        f"      OBSIDIAN_VAULTS: {vaults_env}\n"
        "    volumes:\n"
        + "\n".join(svc_vol_lines) + "\n"
        "  dashboard:\n"
        "    environment:\n"
        f"      OBSIDIAN_VAULTS: {vaults_env}\n"
        "    volumes:\n"
        + "\n".join(dash_vol_lines) + "\n"
        "volumes:\n"
        + "".join(volumes_block)
    )


def _remove_named_volumes_from_override() -> None:
    """Drop any obsidian_vault_* named volumes referenced by the generated
    override. Called from cmd_remove so a teardown after --vault-fs=nfs|cifs
    doesn't leak Docker volume references that re-attach on next install."""
    override_path = PROJECT_ROOT / "docker-compose.override.yml"
    if not override_path.exists():
        return
    text = override_path.read_text()
    import re

    names = sorted({
        m.group(1)
        for m in re.finditer(r"\b(obsidian_vault_[A-Za-z0-9_-]+)\b", text)
    })
    if not names:
        return
    info(f"Removing named Docker volumes: {', '.join(names)}")
    for name in names:
        run(["docker", "volume", "rm", name], check=False, capture=True)


def compose_up(services=None, env=None):
    args = ["up", "-d"] + (list(services) if services else [])
    cmd = ["docker", "compose", "--project-directory", str(PROJECT_ROOT)] + args
    if DRY_RUN:
        run(cmd, env=env)
        return
    kw: dict = {}
    if env:
        kw["env"] = env
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **kw
    )
    captured = []
    assert proc.stdout is not None  # Popen above sets stdout=PIPE
    for line in proc.stdout:
        print(line, end="", flush=True)
        captured.append(line)
    rc = proc.wait()
    if rc != 0:
        _diagnose_compose_failure("".join(captured))
        sys.exit(1)


def _diagnose_compose_failure(captured_output: str) -> None:
    """Surface docker state and per-container logs immediately so the user
    doesn't have to wait for wait_for_postgres to time out 90s later."""
    fail("docker compose up failed before any container started.")
    base = ["docker", "compose", "--project-directory", str(PROJECT_ROOT)]

    ps = run(base + ["ps", "-a"], check=False, capture=True)
    if ps and (ps.stdout or "").strip():
        info("Container state:")
        for line in (ps.stdout or "").splitlines():
            print(f"    {line}")

    for svc in ("mcp-server", "dashboard", "postgres", "ollama"):
        logs = run(base + ["logs", "--tail", "20", svc], check=False, capture=True)
        out = (logs.stdout or "").strip() if logs else ""
        if out:
            info(f"Last 20 log lines for {svc}:")
            for line in out.splitlines():
                print(f"    {line}")

    if "is not a valid Windows path" in captured_output or "UNC path" in captured_output:
        warn(
            "This usually means the vault path resolves to a UNC or a drive "
            "letter backed by a network filesystem (NFS/SMB) that Docker "
            "Desktop cannot bind-mount. Mount the share inside WSL2 and "
            "re-run osm init with the WSL path — see the README "
            "'Windows + network vault' section."
        )


def wait_for_postgres(timeout=90):
    info("Waiting for postgres to be healthy…")
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = run(
            [
                "docker",
                "compose",
                "--project-directory",
                str(PROJECT_ROOT),
                "exec",
                "-T",
                "postgres",
                "pg_isready",
                "-U",
                "obsidian",
                "-d",
                "obsidian_brain",
            ],
            check=False,
            capture=True,
        )
        if r.returncode == 0:
            ok("Postgres is ready")
            return True
        time.sleep(3)
    fail(f"Postgres did not become ready within {timeout}s")
    return False


# ── Install modes ─────────────────────────────────────────────────────────────


def mode_native_macos():
    header("Native install  (Homebrew + local Postgres + local Ollama)")
    hr()

    if not cmd_exists("brew"):
        fail("Homebrew not found — install from https://brew.sh")
        sys.exit(1)
    ok("Homebrew found")

    vaults = prompt_vault()
    vault = vaults  # pass list to _native_entry

    # ── PostgreSQL ────────────────────────────────────────────────────────────
    header("PostgreSQL + pgvector")
    if not cmd_exists("psql"):
        info("Installing postgresql@17 and pgvector via Homebrew…")
        run(["brew", "install", "postgresql@17", "pgvector"])
        run(["brew", "services", "start", "postgresql@17"])
        time.sleep(3)
    else:
        ok("psql already installed")

    r = run(["psql", "postgres", "-lqt"], check=False, capture=True)
    if "obsidian_brain" not in (r.stdout or ""):
        run(["createdb", "obsidian_brain"])
        run(["psql", "obsidian_brain", "-c", "CREATE EXTENSION IF NOT EXISTS vector;"])
        ok("Created database: obsidian_brain")
    else:
        ok("Database obsidian_brain already exists")

    db_url = "postgresql://localhost/obsidian_brain"

    # ── Ollama ────────────────────────────────────────────────────────────────
    header("Ollama + embedding model")
    if not cmd_exists("ollama"):
        info("Installing ollama via Homebrew…")
        run(["brew", "install", "ollama"])

    if not check_ollama_at("localhost"):
        info("Starting ollama serve in background…")
        if DRY_RUN:
            _dry("ollama serve  (background)")
        else:
            subprocess.Popen(
                ["ollama", "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(2)

    _ensure_ollama_model("http://localhost:11434")

    # ── Python env ────────────────────────────────────────────────────────────
    header("Python environment")
    if not cmd_exists("uv"):
        fail("uv not found — install from https://github.com/astral-sh/uv")
        sys.exit(1)
    run(["uv", "sync", "--project", str(PROJECT_ROOT)])
    ok("Dependencies installed in .venv")

    # ── Claude Desktop + CLI config ───────────────────────────────────────────
    header(f"MCP client configuration  ({_mcp_clients_label()})")
    entry = _native_entry(vault, db_url)
    register_with_clients(entry)

    _done_native(vault)


def mode_full_docker():
    header("Full Docker  (Postgres + Ollama + MCP server all in containers)")
    hr()

    if not check_docker() or not check_compose():
        sys.exit(1)

    _ensure_deploy_dir()

    vaults = prompt_vault()
    pg_pw = prompt_pg_password()
    pgdata_path, ollama_data_path = prompt_persistent_storage(include_ollama=True)
    write_env(
        vaults,
        pg_pw,
        "http://ollama:11434",
        pgdata_path=pgdata_path,
        ollama_data_path=ollama_data_path,
        compose_profiles="full-docker",
    )
    _write_compose_override(vaults)

    header("Starting all services")
    env = {
        **os.environ,
        "OBSIDIAN_VAULT": vaults[0],
        "POSTGRES_PASSWORD": pg_pw,
        "OLLAMA_URL": "http://ollama:11434",
        "COMPOSE_PROFILES": "full-docker",
    }
    if len(vaults) > 1:
        env["OBSIDIAN_VAULTS"] = ",".join(f"/{Path(v).name}" for v in vaults)
    if pgdata_path:
        env["PGDATA_PATH"] = pgdata_path
    if ollama_data_path:
        env["OLLAMA_DATA_PATH"] = ollama_data_path
    compose_up(env=env)
    wait_for_postgres()

    # Ollama container exposes on host port 11435 (avoids conflict with host Ollama).
    # Pull the embedding model so indexing works on first run.
    _ensure_ollama_model("http://localhost:11435")

    header(f"MCP client configuration  ({_mcp_clients_label()})")
    entry = _docker_entry()
    register_with_clients(entry)
    _done_docker()


def mode_docker_host_ollama():
    header(
        "Docker + host Ollama  (Postgres in Docker, Ollama already running on this machine)"
    )
    hr()

    if not check_docker() or not check_compose():
        sys.exit(1)

    _ensure_deploy_dir()

    if not check_ollama_at("localhost"):
        fail("Ollama is not running — start it first:  ollama serve")
        info("Then re-run:  osm init")
        sys.exit(1)

    # Ensure the embedding model is pulled before starting services.
    _ensure_ollama_model("http://localhost:11434")

    # host.docker.internal resolves to the Docker host on macOS and Windows;
    # on Linux you may need to pass --add-host or use the bridge IP.
    system = platform.system()
    ollama_host = (
        "host.docker.internal" if system in ("Darwin", "Windows") else "172.17.0.1"
    )
    ollama_url = f"http://{ollama_host}:11434"

    vaults = prompt_vault()
    pg_pw = prompt_pg_password()
    pgdata_path, _ = prompt_persistent_storage(include_ollama=False)
    write_env(vaults, pg_pw, ollama_url, pgdata_path=pgdata_path)
    _write_compose_override(vaults)

    header("Starting services (postgres, mcp-server, dashboard)")
    env = {
        **os.environ,
        "OBSIDIAN_VAULT": vaults[0],
        "POSTGRES_PASSWORD": pg_pw,
        "OLLAMA_URL": ollama_url,
    }
    if len(vaults) > 1:
        env["OBSIDIAN_VAULTS"] = ",".join(f"/{Path(v).name}" for v in vaults)
    if pgdata_path:
        env["PGDATA_PATH"] = pgdata_path
    compose_up(services=["postgres", "mcp-server", "dashboard"], env=env)
    wait_for_postgres()

    header(f"MCP client configuration  ({_mcp_clients_label()})")
    entry = _docker_entry()
    register_with_clients(entry)
    _done_docker()


def _prompt_vault_location(ssh_user, ssh_host, key_path=None):
    """
    Ask whether the vault lives on this machine or the remote host.
    If remote, offer to mount it via sshfs and return the local mount point.
    Returns the local vault path to pass to Docker.
    """
    # --vault supplied → always local
    if "vault" in _PARAMS:
        vaults = prompt_vault()
        return vaults[0] if len(vaults) == 1 else vaults

    # --vault-remote supplied → skip the menu and go straight to sshfs
    if "vault_remote" not in _PARAMS:
        print()
        print("  Where is your Obsidian vault?\n")
        print("    1)  On this machine  (local path)")
        print("    2)  On the remote machine  (will mount via sshfs)")
        loc = prompt("Choose", choices=["1", "2"])
        if loc == "1":
            vaults = prompt_vault()
            return vaults[0] if len(vaults) == 1 else vaults

    # Remote vault via sshfs
    remote_vault = prompt(
        "Path to vault on remote machine (absolute)", param_key="vault_remote"
    )
    default_mount = str(Path.home() / "obsidian-remote-vault")
    mount_point = prompt("Local mount point", default=default_mount)

    mount_path = Path(mount_point).expanduser().resolve()
    if not DRY_RUN:
        mount_path.mkdir(parents=True, exist_ok=True)

    if not cmd_exists("sshfs"):
        warn("sshfs not found — install it first:")
        if platform.system() == "Darwin":
            info("  brew install --cask macfuse && brew install sshfs")
        else:
            info("  sudo apt install sshfs  (or equivalent)")
        if not confirm("Continue without sshfs mount?", default="n"):
            sys.exit(0)
        # Fall back to asking for a local path
        return prompt_vault()

    header("Mounting remote vault via sshfs")
    sshfs_cmd = ["sshfs", f"{ssh_user}@{ssh_host}:{remote_vault}", str(mount_path)]
    if key_path:
        sshfs_cmd += ["-o", f"IdentityFile={key_path}"]
    sshfs_cmd += ["-o", "StrictHostKeyChecking=accept-new", "-o", "reconnect"]

    r = subprocess.run(sshfs_cmd, check=False)
    if r.returncode == 0:
        ok(f"Mounted {ssh_host}:{remote_vault}  →  {mount_path}")
    else:
        fail("sshfs mount failed — check credentials and remote path")
        if not confirm("Continue with a local vault path instead?", default="n"):
            sys.exit(0)
        return prompt_vault()

    return str(mount_path)


def mode_docker_remote_ollama():
    header(
        "Docker + remote Ollama  (Postgres in Docker, Ollama on another host via SSH)"
    )
    hr()

    if not check_docker() or not check_compose():
        sys.exit(1)

    _ensure_deploy_dir()

    # ── SSH credentials ───────────────────────────────────────────────────────
    header("Remote host & SSH credentials")
    ssh_user, remote_host, remote_port, key_path = prompt_ssh_credentials()

    # ── SSH tunnel for Ollama ─────────────────────────────────────────────────
    # Use a non-standard local port to avoid clashing with a local Ollama.
    local_tunnel_port = 11435

    header("SSH tunnel")
    info(f"Testing SSH connection to {ssh_user}@{remote_host}…")
    if not DRY_RUN and not _test_ssh_connection(remote_host, ssh_user, 22, key_path):
        print()
        fail(f"SSH connection test failed: cannot reach {ssh_user}@{remote_host}:22")
        print(
            f"     Check: SSH key path, host reachability, and that SSH is enabled on the remote."
        )
        print()
        if not confirm("Continue anyway?", default="n"):
            fail("Aborted — fix SSH connectivity and re-run osm init")
            sys.exit(1)
    elif not DRY_RUN:
        ok(f"SSH connection to {ssh_user}@{remote_host} succeeded")

    tunnel_ok = open_ssh_tunnel(
        ssh_user, remote_host, remote_port, local_tunnel_port, key_path
    )
    if tunnel_ok:
        time.sleep(1)
        check_ollama_at("localhost", local_tunnel_port)
        # Ensure the embedding model is pulled on the remote Ollama.
        _ensure_ollama_model(f"http://localhost:{local_tunnel_port}")
    else:
        if not confirm("Tunnel failed — continue anyway?", default="n"):
            sys.exit(0)

    # Docker containers reach the host-side tunnel via host.docker.internal
    # (macOS/Windows Docker Desktop) or the bridge gateway (Linux).
    system = platform.system()
    tunnel_host = (
        "host.docker.internal" if system in ("Darwin", "Windows") else "172.17.0.1"
    )
    ollama_url = f"http://{tunnel_host}:{local_tunnel_port}"

    # ── Vault path ────────────────────────────────────────────────────────────
    header("Obsidian vault")
    vault = _prompt_vault_location(ssh_user, remote_host, key_path)
    vaults = vault if isinstance(vault, list) else [vault]

    # ── Write .env with SSH params for future reconnect ───────────────────────
    pg_pw = prompt_pg_password()
    pgdata_path, _ = prompt_persistent_storage(include_ollama=False)
    ssh_params = {
        "user": ssh_user,
        "host": remote_host,
        "remote_port": remote_port,
        "local_port": local_tunnel_port,
        "key_path": key_path,
    }
    write_env(vaults, pg_pw, ollama_url, ssh_params=ssh_params, pgdata_path=pgdata_path)
    _write_compose_override(vaults)

    # ── Start Docker services ─────────────────────────────────────────────────
    header("Starting services (postgres, mcp-server, dashboard)")
    env = {
        **os.environ,
        "OBSIDIAN_VAULT": vaults[0],
        "POSTGRES_PASSWORD": pg_pw,
        "OLLAMA_URL": ollama_url,
    }
    if len(vaults) > 1:
        env["OBSIDIAN_VAULTS"] = ",".join(f"/{Path(v).name}" for v in vaults)
    if pgdata_path:
        env["PGDATA_PATH"] = pgdata_path
    compose_up(services=["postgres", "mcp-server", "dashboard"], env=env)
    wait_for_postgres()

    header(f"MCP client configuration  ({_mcp_clients_label()})")
    entry = _docker_entry()
    register_with_clients(entry)
    _done_docker_remote(ssh_user, remote_host, remote_port, local_tunnel_port, key_path)


# ── Summary printers ──────────────────────────────────────────────────────────


def _done_docker_remote(ssh_user, ssh_host, remote_port, local_port, key_path):
    _link_osm_to_path()
    key_flag = f" -i {key_path}" if key_path else ""
    tunnel_cmd = (
        f"ssh -N -f -o ExitOnForwardFailure=yes "
        f"-L {local_port}:localhost:{remote_port} "
        f"{ssh_user}@{ssh_host}{key_flag}"
    )
    print()
    hr()
    ok(_c("1", "Setup complete!"))
    print()
    info("Dashboard:  http://localhost:8484")
    info("Logs:       docker compose logs -f mcp-server")
    info("Restart Claude Desktop — server starts automatically")
    print()
    print(f"  {_c('93', '⚠')}  The SSH tunnel must be running for Ollama to work.")
    print(f"     Reconnect with:")
    print(f"\n       {_c('1', tunnel_cmd)}\n")
    info("Or run:  osm tunnel   (reads .env automatically)")
    hr()


def _done_dry_run():
    print()
    hr()
    print(f"  {_c('93', '⚠')}  {_c('1', 'DRY RUN — no changes were made')}")
    print()
    if _DRY_ACTIONS:
        print(f"  {_c('1', 'Actions that would have run:')}\n")
        for i, action in enumerate(_DRY_ACTIONS, 1):
            print(f"  {_c('90', str(i) + '.')}  {action}")
    else:
        print("  (no actions would have run)")
    print()
    info("Re-run without --dry-run to apply")
    hr()


def _osm_launcher_path() -> Path:
    """Return the platform-correct installed osm launcher path."""
    bin_dir = Path.home() / ".local" / "bin"
    if platform.system() == "Windows":
        return bin_dir / "osm.cmd"
    return bin_dir / "osm"


def _link_osm_to_path():
    """
    Write a self-contained launcher to $HOME/.local/bin/ using the
    platform-correct format:
      - Unix (macOS/Linux): bash script at ~/.local/bin/osm
      - Windows: batch wrapper at ~/.local/bin/osm.cmd → scripts/osm.ps1

    Unlike a symlink, the launcher survives the repo being moved or deleted:
    it tries the original install path first, then falls back to the XDG
    standard location, then prints a clear reinstall error.
    """
    bin_dir = Path.home() / ".local" / "bin"
    launcher = _osm_launcher_path()
    xdg_data = Path(
        os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
    )

    if platform.system() == "Windows":
        xdg_ps1 = xdg_data / "obsidian-semantic-mcp" / "scripts" / "osm.ps1"
        primary_ps1 = PROJECT_ROOT / "scripts" / "osm.ps1"
        # Batch wrapper — delegates to PowerShell launcher, mirrors install.ps1 behavior
        script = (
            "@echo off\n"
            "setlocal\n"
            f'set "_PRIMARY={primary_ps1}"\n'
            f'set "_XDG={xdg_ps1}"\n'
            'if exist "%_PRIMARY%" (\n'
            '    powershell -NoProfile -ExecutionPolicy Bypass -File "%_PRIMARY%" %*\n'
            "    exit /b %ERRORLEVEL%\n"
            ")\n"
            'if exist "%_XDG%" (\n'
            '    powershell -NoProfile -ExecutionPolicy Bypass -File "%_XDG%" %*\n'
            "    exit /b %ERRORLEVEL%\n"
            ")\n"
            "echo osm: installation not found 1>&2\n"
            "echo   Looked in: %_PRIMARY% 1>&2\n"
            "echo   And:       %_XDG% 1>&2\n"
            f'echo   Reinstall: powershell -c "irm https://raw.githubusercontent.com/{_GITHUB_REPO}/main/install.ps1 | iex" 1>&2\n'
            "exit /b 1\n"
        )
        if DRY_RUN:
            _dry(f"write {launcher}", "Windows batch launcher (delegates to osm.ps1)")
            return
        bin_dir.mkdir(parents=True, exist_ok=True)
        try:
            launcher.write_text(script, encoding="ascii")
            ok(f"Installed: {launcher}  (osm is now globally available)")
            if str(bin_dir) not in os.environ.get("PATH", ""):
                warn(f"{bin_dir} is not in your PATH — add to your PowerShell profile:")
                info(f'  $env:PATH = "{bin_dir};$env:PATH"')
        except OSError as exc:
            warn(f"Could not install osm launcher: {exc}")
    else:
        xdg_osm = xdg_data / "obsidian-semantic-mcp" / "scripts" / "osm"
        primary = PROJECT_ROOT / "scripts" / "osm"
        script = (
            "#!/usr/bin/env bash\n"
            "# osm launcher — generated by osm init\n"
            f'_PRIMARY="{primary}"\n'
            f'_XDG="{xdg_osm}"\n'
            f'_INSTALL_URL="https://raw.githubusercontent.com/{_GITHUB_REPO}/main/install.sh"\n'
            'if [ -f "$_PRIMARY" ]; then\n'
            '    exec "$_PRIMARY" "$@"\n'
            'elif [ -f "$_XDG" ]; then\n'
            '    exec "$_XDG" "$@"\n'
            "else\n"
            '    echo "osm: installation not found" >&2\n'
            '    echo "  Looked in: $_PRIMARY" >&2\n'
            '    echo "  And:       $_XDG" >&2\n'
            '    echo "  Reinstall: curl -fsSL $_INSTALL_URL | bash" >&2\n'
            "    exit 1\n"
            "fi\n"
        )
        if DRY_RUN:
            _dry(
                f"write {launcher}",
                "self-contained launcher (survives repo move/delete)",
            )
            return
        bin_dir.mkdir(parents=True, exist_ok=True)
        try:
            launcher.unlink(
                missing_ok=True
            )  # remove symlink before writing — write_text follows symlinks
            launcher.write_text(script)
            launcher.chmod(0o755)
            primary.chmod(0o755)
            ok(f"Installed: {launcher}  (osm is now globally available)")
            if str(bin_dir) not in os.environ.get("PATH", ""):
                warn(f"{bin_dir} is not in your PATH — add to your shell profile:")
                info(f'  export PATH="{bin_dir}:$PATH"')
        except OSError as exc:
            warn(f"Could not install osm launcher: {exc}")


def _done_native(vault):
    _link_osm_to_path()
    print()
    hr()
    ok(_c("1", "Setup complete!"))
    print()
    info(f"Vault: {vault}")
    info("Restart Claude Desktop / Claude Code to activate the MCP server")
    hr()


def _done_docker():
    _link_osm_to_path()
    print()
    hr()
    ok(_c("1", "Setup complete!"))
    print()
    info("Dashboard:  http://localhost:8484")
    info("Logs:       docker compose logs -f mcp-server")
    info("Restart Claude Desktop / Claude Code to activate the MCP server")
    hr()


# ── Tunnel command ────────────────────────────────────────────────────────────


def _read_env():
    """Parse .env into a dict (simple KEY=VALUE, ignores comments)."""
    env_path = PROJECT_ROOT / ".env"
    result = {}
    if not env_path.exists():
        return result
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()
    return result


def _update_env_var(key: str, value: str) -> None:
    """Update or append a single KEY=VALUE line in .env, preserving every
    other line and the 0o600 permission (the file holds POSTGRES_PASSWORD)."""
    env_path = PROJECT_ROOT / ".env"
    if DRY_RUN:
        _dry(f"update {env_path}", f"{key}={value}")
        return
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if (
            stripped
            and not stripped.startswith("#")
            and "=" in stripped
            and stripped.split("=", 1)[0].strip() == key
        ):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n")
    env_path.chmod(0o600)


def cmd_tunnel():
    """Re-open the SSH tunnel using credentials stored in .env."""
    header("OSM Tunnel — reconnect SSH tunnel")
    hr()

    env = _read_env()
    user = env.get("OSM_SSH_USER")
    host = env.get("OSM_SSH_HOST")
    remote_port = env.get("OSM_SSH_REMOTE_PORT", "11434")
    local_port = env.get("OSM_SSH_LOCAL_PORT", "11435")
    key_path = env.get("OSM_SSH_KEY")

    if not user or not host:
        fail("No SSH config found in .env — run osm init first")
        sys.exit(1)

    info(
        f"Reconnecting: {user}@{host} (tunnel localhost:{local_port} → {host}:{remote_port})"
    )
    ok_flag = open_ssh_tunnel(user, host, int(remote_port), int(local_port), key_path)
    if ok_flag:
        time.sleep(1)
        check_ollama_at("localhost", int(local_port))
    else:
        sys.exit(1)


# ── Vaults command ────────────────────────────────────────────────────────────


def cmd_vaults():
    """List the Obsidian vault(s) this install is indexing."""
    header("OSM Vaults")
    hr()

    env = _read_env()
    raw_multi = env.get("OBSIDIAN_VAULTS", "")
    vaults = [v.strip() for v in raw_multi.split(",") if v.strip()]
    if not vaults:
        single = env.get("OBSIDIAN_VAULT", "")
        if single:
            vaults = [single]

    if not vaults:
        warn("No vault configured — run osm init")
        return

    for v in vaults:
        p = Path(v)
        if p.exists():
            ok(v)
        else:
            fail(f"{v}  (path not found)")


# ── Status command ────────────────────────────────────────────────────────────


def cmd_status():
    header("OSM Status")
    hr()

    r = run(
        [
            "docker",
            "compose",
            "--project-directory",
            str(PROJECT_ROOT),
            "ps",
            "--format",
            "table",
        ],
        check=False,
        capture=True,
    )
    if r.returncode == 0 and r.stdout.strip():
        print(r.stdout)
    else:
        info("No Docker services running")

    env = _read_env()
    ollama_url = _status_ollama_url(env)
    parsed = urllib.parse.urlparse(ollama_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 11434
    if check_ollama_at(host, port):
        check_ollama_inference_at(ollama_url, env.get("EMBEDDING_MODEL", EMBED_MODEL))

    cfg_path = _claude_cfg_path()
    if cfg_path and cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text())
            if "obsidian-semantic" in cfg.get("mcpServers", {}):
                ok("Claude Desktop: obsidian-semantic configured")
            else:
                warn("Claude Desktop: obsidian-semantic NOT configured — run osm init")
        except json.JSONDecodeError:
            warn("Claude Desktop config could not be parsed")
    else:
        warn("Claude Desktop config not found")

    opencode_cfg = _opencode_cfg_path()
    if opencode_cfg.exists():
        try:
            cfg = json.loads(opencode_cfg.read_text())
            if "obsidian-semantic" in cfg.get("mcp", {}):
                ok("OpenCode: obsidian-semantic configured")
            else:
                info("OpenCode: obsidian-semantic NOT configured — run osm init")
        except json.JSONDecodeError:
            warn("OpenCode config could not be parsed")
    else:
        info("OpenCode config not found — skipping (OpenCode not installed?)")


# ── Rebuild command ───────────────────────────────────────────────────────────


def _compose_image_name(service: str) -> str | None:
    """Extract the `image:` repo name for a service from docker-compose.yml
    (e.g. "newblacc/obsidian-semantic-mcp"), stripping the ${OSM_VERSION:-latest}
    tag expression. Returns None if the service or its image line isn't found."""
    import re

    compose_path = PROJECT_ROOT / "docker-compose.yml"
    if not compose_path.exists():
        return None
    text = compose_path.read_text()
    m = re.search(rf"^  {re.escape(service)}:\n(?:.*\n)*?    image: ([^\s:]+):", text, re.MULTILINE)
    return m.group(1) if m else None


def _build_or_pull_custom_services(pull_base: bool = False) -> None:
    """Refresh mcp-server/dashboard. If a local source checkout is present
    (Dockerfile + Dockerfile.dashboard at PROJECT_ROOT), build directly with
    `docker build` and persist OSM_VERSION=APP_VERSION to .env so the tag
    docker-compose.yml resolves (${OSM_VERSION:-latest}) matches what was
    just built — a plain `compose up -d` then uses it as-is instead of
    attempting a pull. With no local Dockerfile (pip-only/packaged installs,
    which never have a build context to build from), falls back to the
    pull-based compose flow.

    pull_base=True passes --pull to `docker build` so the base image layer
    is refreshed too (used by `osm update`, which is expected to fetch
    upstream changes, not just retag the same base).
    """
    dockerfile = PROJECT_ROOT / "Dockerfile"
    dockerfile_dashboard = PROJECT_ROOT / "Dockerfile.dashboard"

    if dockerfile.exists() and dockerfile_dashboard.exists():
        mcp_repo = _compose_image_name("mcp-server")
        dashboard_repo = _compose_image_name("dashboard")
        if mcp_repo and dashboard_repo:
            _update_env_var("OSM_VERSION", APP_VERSION)
            mcp_image = f"{mcp_repo}:{APP_VERSION}"
            dashboard_image = f"{dashboard_repo}:{APP_VERSION}"
            info(f"Building from local source ({PROJECT_ROOT}) — tag {APP_VERSION}")
            build_prefix = ["docker", "build"] + (["--pull"] if pull_base else [])
            run(build_prefix + ["-f", str(dockerfile), "-t", mcp_image, str(PROJECT_ROOT)])
            run(build_prefix + ["-f", str(dockerfile_dashboard), "-t", dashboard_image, str(PROJECT_ROOT)])
            # Images now match the tag docker-compose.yml resolves to, so a plain
            # `up -d` uses them as-is (Compose's default pull policy only pulls
            # when the tag is missing locally) — no risk of clobbering with a stale pull.
            compose(["up", "-d", "mcp-server", "dashboard"])
            return
        warn("Could not resolve image names from docker-compose.yml — falling back to compose --build")

    warn("No local Dockerfile found — recreating from the already-pulled image, not a source rebuild")
    compose(["up", "-d", "--build", "mcp-server", "dashboard"])


def cmd_rebuild():
    header("Rebuilding Docker images")
    hr()
    _build_or_pull_custom_services()
    ok("Rebuild complete")
    info("Dashboard:  http://localhost:8484")


# ── Migrate command ───────────────────────────────────────────────────────────
#
# Operator-triggered, non-destructive embedding-dimension migration (see
# docs/PLAN-security-correctness.md iteration 8, docs/RUNBOOK.md). This is
# the only command in `osm` that can rewrite `notes.embedding` data — it
# never runs automatically (init_db only *detects* a mismatch and records
# `dimension_mismatch` in index_state; see src/server.py and
# src/migrations.py's migrate_embedding_dimension / add_embedding_column /
# backfill_embedding_column / cutover_embedding_column).


def cmd_migrate():
    header("OSM Migrate — embedding dimension")
    hr()

    new_dim_raw = _PARAMS.get("embedding_dim")
    if not new_dim_raw:
        fail("osm migrate requires --embedding-dim <N> (the new model's output dimension)")
        info("Example: osm migrate --embedding-dim 1024")
        sys.exit(1)
    try:
        new_dim = int(new_dim_raw)
    except ValueError:
        fail(f"--embedding-dim must be an integer, got {new_dim_raw!r}")
        sys.exit(1)
    if new_dim <= 0:
        fail(f"--embedding-dim must be positive, got {new_dim}")
        sys.exit(1)

    info(f"Target dimension: vector({new_dim})")
    info("Adds a new column, backfills it, and only drops the old column")
    info("once every row is backfilled. Search stays available throughout.")
    print()

    if DRY_RUN:
        info("Dry run — reporting the plan only, no changes will be made.")
        _run_migration_snippet(new_dim, dry_run=True)
        return

    if not confirm(
        f"Re-embed every note into vector({new_dim})? This can take a long "
        f"time on CPU-only Ollama for a large vault.",
        default="n",
    ):
        info("Cancelled.")
        return

    result = _run_migration_snippet(new_dim, dry_run=False)
    if result is None or result.returncode != 0:
        fail("Migration failed or could not run — the old embedding column and data are untouched.")
        info("Re-run `osm migrate --embedding-dim N --dry-run` to inspect the plan, or check `osm status`.")
        sys.exit(1)
    ok(f"Migration to vector({new_dim}) complete.")


_NATIVE_DB_URL = "postgresql://localhost/obsidian_brain"


def _migration_snippet(src_path: str, new_dim: int, *, dry_run: bool) -> str:
    """Build the in-process migration snippet, parameterized on where `src/`
    lives (`/app/src` inside the mcp-server container, or PROJECT_ROOT/src
    for a native install running it directly)."""
    return (
        f"import sys; sys.path.insert(0, {src_path!r}); "
        "import server, migrations; "
        "server.init_db(server.get_embed_dim()); "
        "conn = server._get_pool().getconn(); "
        "try:\n"
        f"    result = migrations.migrate_embedding_dimension(conn, None, {new_dim}, server.embed, dry_run={dry_run!r}); "
        "    print(result)\n"
        "finally:\n"
        "    server._get_pool().putconn(conn)\n"
    )


def _migrate_target_is_docker() -> bool:
    """True when an mcp-server container exists for this project (Docker
    install). False means native — the snippet must run locally via `uv
    run` instead of `docker compose exec`."""
    r = compose(["ps", "-q", "mcp-server"], check=False, capture=True)
    return bool(r.returncode == 0 and (r.stdout or "").strip())


def _run_migration_snippet(new_dim: int, *, dry_run: bool):
    """Delegate the actual migration to wherever psycopg2, migrations.py,
    and the configured embed() function are available: the running
    mcp-server container for Docker installs (mirrors the exec pattern used
    elsewhere in this file — see register_pi_agent's docker compose exec -T
    mcp-server call), or a local `uv run` subprocess against PROJECT_ROOT
    for native installs (mode_native_macos never writes a .env, so
    DATABASE_URL falls back to the fixed native default; OLLAMA_URL and
    EMBEDDING_MODEL already default correctly in server.py)."""
    if _migrate_target_is_docker():
        snippet = _migration_snippet("/app/src", new_dim, dry_run=dry_run)
        return compose(
            ["exec", "-T", "mcp-server", "python3", "-c", snippet],
            check=False,
        )

    snippet = _migration_snippet(str(PROJECT_ROOT / "src"), new_dim, dry_run=dry_run)
    env = {**os.environ, "DATABASE_URL": _read_env().get("DATABASE_URL", _NATIVE_DB_URL)}
    return run(
        ["uv", "run", "--project", str(PROJECT_ROOT), "python3", "-c", snippet],
        env=env,
        check=False,
    )


# ── Version command ───────────────────────────────────────────────────────────

_GITHUB_REPO = "artificemachine/obsidian-semantic-mcp"
_LATEST_RELEASE_URL = f"https://api.github.com/repos/{_GITHUB_REPO}/releases/latest"


def _fetch_latest_release_tag(timeout: float = 3.0):
    """Return the latest GitHub release tag (without leading 'v'), or None on failure."""
    try:
        resp = requests.get(_LATEST_RELEASE_URL, timeout=timeout)
        if resp.status_code != 200:
            return None
        tag = (resp.json().get("tag_name") or "").lstrip("v").strip()
        return tag or None
    except Exception:
        return None


def _version_tuple(v: str):
    parts = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            # non-numeric segment (e.g., rc1) — treat as -1 so numeric wins
            parts.append(-1)
    return tuple(parts)


def cmd_version():
    header(f"osm v{APP_VERSION}")
    hr()
    info(f"Installed:  {APP_VERSION}")
    latest = _fetch_latest_release_tag()
    if latest is None:
        warn("Could not reach GitHub to check for updates")
        return
    info(f"Latest:     {latest}")
    if _version_tuple(latest) > _version_tuple(APP_VERSION):
        warn("A newer release is available — run: osm update")
    else:
        ok("You are on the latest release")


# ── Update command ────────────────────────────────────────────────────────────


def cmd_update():
    header("Updating Obsidian Semantic MCP")
    hr()

    installed = APP_VERSION
    latest = _fetch_latest_release_tag()
    if latest:
        info(f"Installed CLI: {installed}    Latest release: {latest}")
    else:
        warn("Could not reach GitHub to check for latest release")

    # mcp-server/dashboard only declare `image:` in docker-compose.yml (no
    # `build:` context) — `compose pull`/`compose build` are no-ops on them.
    # _build_or_pull_custom_services builds directly from source when a
    # local checkout is present (source installs), else falls back to
    # recreating from whatever's already pulled (pip-only/packaged installs).
    info("Pulling latest images for image-based services (postgres, ollama)…")
    compose(["pull", "postgres", "ollama"])

    info("Rebuilding custom services from source with refreshed base images…")
    _build_or_pull_custom_services(pull_base=True)

    ok("Docker services updated")
    print()

    if latest and _version_tuple(latest) > _version_tuple(installed):
        warn(f"The osm CLI itself is outdated ({installed} → {latest})")
        info("Re-run the installer to update the CLI and scripts:")
        print(f"    curl -fsSL {_INSTALL_URL} | bash")
    else:
        ok(f"CLI is up to date (v{installed})")

    # Re-run client registration so any newly installed clients get configured
    # and mcp-bridge patches are applied even if they were installed after osm init.
    print()
    header(f"MCP client configuration  ({_mcp_clients_label()})")
    hr()
    entry = _docker_entry()
    update_claude_config(entry)
    register_claude_cli(entry)
    update_opencode_config(entry)
    register_pi_agent()


# ── Remove command ────────────────────────────────────────────────────────────


def cmd_remove():
    header("OSM Remove — tear down Obsidian Semantic MCP")
    hr()
    print()
    warn("This will:")
    print(
        "    • Stop and remove all Docker containers and volumes  (all indexed embeddings lost)"
    )
    print("    • Delete .env from this project")
    print("    • Remove obsidian-semantic from claude_desktop_config.json")
    print("    • Remove obsidian-semantic from Claude Code CLI  ($HOME/.claude.json)")
    print(f"    • Delete the osm launcher from {_osm_launcher_path()}")
    print()

    force = _PARAMS.get("yes") == "y"
    if not DRY_RUN and not force and not confirm("Continue?", default="n"):
        info("Aborted — nothing changed")
        return

    # ── Docker services + volumes ─────────────────────────────────────────────
    header("Stopping Docker services")
    r = run(
        ["docker", "compose", "--project-directory", str(PROJECT_ROOT), "ps", "-q"],
        check=False,
        capture=True,
    )
    if not DRY_RUN and not (r.stdout or "").strip():
        info("No running Docker services found — skipping")
    else:
        run(
            [
                "docker",
                "compose",
                "--project-directory",
                str(PROJECT_ROOT),
                "down",
                "-v",
            ],
            check=False,
        )
        if not DRY_RUN:
            ok("Docker services stopped and volumes removed")

    # ── Network-vault named volumes ───────────────────────────────────────────
    # If the install used --vault-fs=nfs|cifs we generated obsidian_vault_*
    # named volumes. `docker compose down -v` only removes volumes declared
    # in the compose project; explicit external named volumes need a separate
    # rm pass, otherwise the NFS / CIFS mount lingers as a Docker reference.
    if not DRY_RUN:
        _remove_named_volumes_from_override()

    # ── .env ──────────────────────────────────────────────────────────────────
    header("Removing .env")
    env_path = PROJECT_ROOT / ".env"
    if DRY_RUN:
        _dry(f"remove {env_path}")
    elif env_path.exists():
        env_path.unlink()
        ok(f"Deleted {env_path}")
    else:
        info(".env not found — skipping")

    # ── Claude Desktop config ─────────────────────────────────────────────────
    header("Updating Claude Desktop config")
    cfg_path = _claude_cfg_path()
    if not cfg_path:
        warn(
            "Unknown platform — remove obsidian-semantic from claude_desktop_config.json manually"
        )
    elif DRY_RUN:
        _dry(f"remove obsidian-semantic entry from {cfg_path}")
    elif cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text())
            servers = cfg.get("mcpServers", {})
            if "obsidian-semantic" in servers:
                del servers["obsidian-semantic"]
                cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
                ok(f"Removed obsidian-semantic from {cfg_path}")
                info("Restart Claude Desktop to apply")
            else:
                info("obsidian-semantic not found in config — skipping")
        except json.JSONDecodeError:
            warn(f"Could not parse {cfg_path} — remove entry manually")
    else:
        info("Claude Desktop config not found — skipping")

    # ── Claude Code CLI ───────────────────────────────────────────────────────
    header("Updating Claude Code CLI config")
    if cmd_exists("claude"):
        if DRY_RUN:
            _dry("claude mcp remove obsidian-semantic")
        else:
            r = run(
                ["claude", "mcp", "remove", "--scope", "user", "obsidian-semantic"],
                check=False,
            )
            if r.returncode == 0:
                ok("Removed obsidian-semantic from Claude Code CLI")
            else:
                info("obsidian-semantic not found in Claude Code CLI — skipping")
    else:
        info("claude CLI not found — skipping")

    # ── OpenCode config ───────────────────────────────────────────────────────
    header("Updating OpenCode config")
    remove_opencode_config()

    # ── osm launcher ──────────────────────────────────────────────────────────
    header("Removing osm launcher")
    launcher = _osm_launcher_path()
    if DRY_RUN:
        _dry(f"remove {launcher}")
    elif launcher.exists():
        launcher.unlink()
        ok(f"Deleted {launcher}")
    else:
        info("osm launcher not found — skipping")

    if not DRY_RUN:
        print()
        hr()
        ok(_c("1", "Removed."))
        info("Run  osm init  to reinstall")
        hr()


# ── Install mode tables ───────────────────────────────────────────────────────

MODES_MACOS = {
    "1": ("Native", "Homebrew + local Postgres + local Ollama", mode_native_macos),
    "2": (
        "Docker + host Ollama",
        "Postgres in Docker, Ollama already on this Mac",
        mode_docker_host_ollama,
    ),
    "3": ("Full Docker", "Everything in containers  (recommended)", mode_full_docker),
    "4": (
        "Docker + remote Ollama",
        "Postgres in Docker, Ollama on another machine via SSH",
        mode_docker_remote_ollama,
    ),
}

MODES_LINUX = {
    "1": (
        "Docker + host Ollama",
        "Postgres in Docker, Ollama on this machine",
        mode_docker_host_ollama,
    ),
    "2": ("Full Docker", "Everything in containers  (recommended)", mode_full_docker),
    "3": (
        "Docker + remote Ollama",
        "Postgres in Docker, Ollama on another machine",
        mode_docker_remote_ollama,
    ),
}

MODES_WINDOWS = {
    "1": (
        "Docker + host Ollama",
        "Postgres in Docker, Ollama already on this PC",
        mode_docker_host_ollama,
    ),
    "2": ("Full Docker", "Everything in containers  (recommended)", mode_full_docker),
    "3": (
        "Docker + remote Ollama",
        "Postgres in Docker, Ollama on another machine",
        mode_docker_remote_ollama,
    ),
}


# ── Init command ──────────────────────────────────────────────────────────────


def cmd_init():
    print()
    hr()
    print(_c("1", f"  Obsidian Semantic MCP v{APP_VERSION} — Setup Wizard"))
    hr()

    system = platform.system()
    if system == "Darwin":
        ver = platform.mac_ver()[0]
        arch = platform.machine()
        print(f"\n  Detected: macOS {ver} ({arch})\n")
        modes = MODES_MACOS
    elif system == "Linux":
        distro = "Linux"
        try:
            for line in Path("/etc/os-release").read_text().splitlines():
                if line.startswith("PRETTY_NAME="):
                    distro = line.split("=", 1)[1].strip('"')
        except Exception:
            pass
        print(f"\n  Detected: {distro}\n")
        modes = MODES_LINUX
    elif system == "Windows":
        ver = platform.version()
        arch = platform.machine()
        print(f"\n  Detected: Windows {ver} ({arch})\n")
        info("Requires Docker Desktop with WSL2 backend")
        modes = MODES_WINDOWS
    else:
        fail(f"Platform {system!r} not yet supported by this wizard")
        info("Follow the manual steps in README.md")
        sys.exit(1)

    print("  Installation modes:\n")
    for key, (name, desc, _) in modes.items():
        print(f"    {_c('1', key)})  {_c('1', name)}")
        print(f"         {desc}")
    print()

    if _ollama_running_locally():
        # macOS mode 2 = Docker + host Ollama; Linux/Windows mode 1 = same
        rec_key = "2" if system == "Darwin" else "1"
        rec_name = modes[rec_key][0]
        print(f"  {_c('92', '✓')}  Ollama is already running on this machine.")
        print(
            f"     {_c('93', f'Recommended: {rec_key}) {rec_name}')}"
            f"  — skips the ~3 GB Ollama image pull"
        )
        print()

    choice = prompt("Choose", choices=list(modes.keys()), param_key="mode")
    _, _, handler = modes[choice]
    handler()


# ── Dashboard command ─────────────────────────────────────────────────────────

_DASHBOARD_URL = "http://localhost:8484"


def cmd_dashboard():
    """Open the monitoring dashboard in the default browser."""
    try:
        urllib.request.urlopen(_DASHBOARD_URL, timeout=3)
        ok(f"Dashboard is running")
    except Exception:
        warn(f"Dashboard may not be running — opening anyway")
    info(f"Opening {_DASHBOARD_URL}")
    webbrowser.open(_DASHBOARD_URL)


# ── Help command ──────────────────────────────────────────────────────────────

_INSTALL_URL = f"https://raw.githubusercontent.com/{_GITHUB_REPO}/main/install.sh"


def cmd_help():
    print(f"\n  {_c('1', f'osm v{APP_VERSION}')} — Obsidian Semantic MCP CLI\n")
    print(f"  {_c('1', 'Install (one-liner):')}\n")
    print(f"    curl -fsSL {_INSTALL_URL} | bash\n")
    print(f"  {_c('1', 'Usage:')}  osm <command> [flags]\n")
    print(f"  {_c('1', 'Commands:')}\n")
    for name, (_, desc) in COMMANDS.items():
        print(f"    {_c('1', f'osm {name:<10}')}  {desc}")
    print()
    print(f"  {_c('1', 'Flags:')}\n")
    print(
        f"    {_c('1', '--dry-run')}              Print every action that would run — make no changes"
    )
    print(
        f"    {_c('1', '--yes')}                 Skip confirmation prompts  (use with: osm remove)"
    )
    print()
    print(
        f"  {_c('1', 'init flags')}  (skip interactive prompts — usable from scripts or AI agents)\n"
    )
    print(
        f"    {_c('1', '--mode <1-4>')}          Installation mode (macOS: 1=native 2=docker+host-ollama 3=full-docker 4=remote-ollama)"
    )
    print(f"    {_c('1', '--vault <path>')}         Absolute path to Obsidian vault")
    print(
        f"    {_c('1', '--pg-password <pw>')}     Postgres password  (default: obsidian)"
    )
    print(
        f"    {_c('1', '--persistent')}           Use bind-mount volumes for persistent storage"
    )
    print(
        f"    {_c('1', '--no-persistent')}        Use named Docker volumes (wiped by down -v)"
    )
    print(
        f"    {_c('1', '--data-dir <path>')}      Bind-mount data directory  (implies --persistent)"
    )
    print(f"    {_c('1', '--ssh-host <host>')}      Remote Ollama host  (mode 4)")
    print(f"    {_c('1', '--ssh-user <user>')}      SSH username  (mode 4)")
    print(
        f"    {_c('1', '--ssh-port <port>')}      Remote Ollama port  (default: 11434, mode 4)"
    )
    print(f"    {_c('1', '--ssh-key <path>')}       SSH private key path  (mode 4)")
    print(
        f"    {_c('1', '--vault-remote <path>')}  Vault path on remote machine — mount via sshfs  (mode 4)"
    )
    print(
        f"    {_c('1', '--vault-fs <auto|local|nfs|cifs>')}  Backing filesystem for vault mounts (default: auto). "
        f"With nfs/cifs, --vault entries must use host:/export/path or //host/share/path syntax."
    )
    print(
        f"    {_c('1', '--vault-cifs-user <name>')}  SMB username  (used with --vault-fs cifs)"
    )
    print(
        f"    {_c('1', '--vault-cifs-pass <pw>')}    SMB password  (used with --vault-fs cifs)"
    )
    print()
    print(f"  {_c('1', 'Examples:')}\n")
    print(f"    osm init                                    # Interactive setup")
    print(f"    osm init --dry-run                          # Preview without changes")
    print(f"    osm init --mode 3 --vault /path/to/vault \\")
    print(
        f"        --pg-password secret --persistent       # Non-interactive full Docker"
    )
    print(f"    osm init --mode 4 --vault /path/to/vault \\")
    print(f"        --ssh-host 203.0.113.5 --ssh-user ubuntu \\")
    print(f"        --ssh-key $HOME/.ssh/id_ed25519         # Remote Ollama via SSH")
    print(f"    osm status                                  # Check service health")
    print(f"    osm vaults                                  # List configured vault(s)")
    print(f"    osm tunnel                                  # Reconnect SSH tunnel")
    print(f"    osm rebuild                                 # Rebuild Docker images")
    print(f"    osm update                                  # Pull latest images and restart")
    print(f"    osm version                                 # Show version and check for updates")
    print(
        f"    osm remove                                  # Stop services and wipe config"
    )
    print()


# ── Entry point ───────────────────────────────────────────────────────────────

COMMANDS = {
    "init": (cmd_init, "Interactive setup wizard"),
    "status": (cmd_status, "Check service health"),
    "vaults": (cmd_vaults, "List configured Obsidian vault(s)"),
    "dashboard": (cmd_dashboard, "Open monitoring dashboard in browser"),
    "tunnel": (cmd_tunnel, "Reconnect SSH tunnel to remote Ollama host"),
    "rebuild": (cmd_rebuild, "Rebuild Docker images and restart"),
    "update": (cmd_update, "Pull latest Docker images and restart services"),
    "version": (cmd_version, "Show installed version and check for updates"),
    "remove": (cmd_remove, "Stop services, delete volumes and config"),
    "migrate": (cmd_migrate, "Re-embed notes into a new embedding dimension (non-destructive)"),
    "help": (cmd_help, "Show this help message"),
}


_FLAG_MAP = {
    # flag name (without --) → _PARAMS key
    "vault": "vault",
    "pg-password": "pg_password",
    "mode": "mode",
    "persistent": "persistent",  # boolean, value "y"
    "no-persistent": "persistent",  # boolean, value "n"
    "data-dir": "data_dir",
    "ssh-host": "ssh_host",
    "ssh-user": "ssh_user",
    "ssh-port": "ssh_port",
    "ssh-key": "ssh_key",
    "vault-remote": "vault_remote",
    "vault-fs": "vault_fs",  # auto|local|nfs|cifs
    "vault-cifs-user": "vault_cifs_user",
    "vault-cifs-pass": "vault_cifs_pass",
    "yes": "yes",  # boolean — skip all confirms in remove
    "embedding-dim": "embedding_dim",  # osm migrate — new model's output dimension
}


def _parse_flags(args):
    """
    Pull --key=value / --key value / boolean --flag pairs out of args.
    Returns (remaining_args, params_dict).
    """
    global DRY_RUN
    params = {}
    remaining = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--dry-run":
            DRY_RUN = True
            i += 1
            continue
        if a == "--persistent":
            params["persistent"] = "y"
            i += 1
            continue
        if a == "--no-persistent":
            params["persistent"] = "n"
            i += 1
            continue
        if a == "--yes":
            params["yes"] = "y"
            i += 1
            continue
        if a.startswith("--"):
            key = a[2:]
            if "=" in key:
                k, v = key.split("=", 1)
                if k in _FLAG_MAP:
                    params[_FLAG_MAP[k]] = v
                    i += 1
                    continue
            elif (
                key in _FLAG_MAP
                and i + 1 < len(args)
                and not args[i + 1].startswith("--")
            ):
                params[_FLAG_MAP[key]] = args[i + 1]
                i += 2
                continue
        remaining.append(a)
        i += 1
    return remaining, params


def main():
    global DRY_RUN, _PARAMS

    args, _PARAMS = _parse_flags(sys.argv[1:])

    if DRY_RUN:
        info("Dry-run mode — no changes will be made")
        print()

    # No command or explicit help request
    if not args or args[0] in ("--help", "-h"):
        cmd_help()
        sys.exit(0)

    cmd = args[0]

    if cmd not in COMMANDS:
        fail(f"Unknown command: {cmd!r}")
        print()
        cmd_help()
        sys.exit(1)

    COMMANDS[cmd][0]()

    if DRY_RUN:
        _done_dry_run()


if __name__ == "__main__":
    main()
