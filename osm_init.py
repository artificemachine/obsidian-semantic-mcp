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

import json
import os
import platform
import requests
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


# ── Terminal output ───────────────────────────────────────────────────────────

_TTY = sys.stdout.isatty()


def _c(code, text):
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def ok(msg):    print(f"  {_c('92', '✓')}  {msg}")
def warn(msg):  print(f"  {_c('93', '⚠')}  {msg}")
def fail(msg):  print(f"  {_c('91', '✗')}  {msg}")
def info(msg):  print(f"  {_c('94', '→')}  {msg}")
def header(msg): print(f"\n{_c('1', msg)}")
def hr():        print("─" * 60)


# ── Dry-run state ─────────────────────────────────────────────────────────────

DRY_RUN     = False
_DRY_ACTIONS: list[str] = []   # collects every skipped action for the summary


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
    kwargs = {"shell": isinstance(cmd, str), "check": check}
    if env:
        kwargs["env"] = env
    if capture:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return subprocess.run(cmd, **kwargs)


def cmd_exists(name):
    return shutil.which(name) is not None


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
            fail(f"--{param_key.replace('_', '-')} {val!r} is not one of: {', '.join(choices)}")
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
    return prompt(question, default=default, choices=["y", "n"], param_key=param_key).lower() == "y"


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
    return prompt("Postgres password (used for the local Docker DB)",
                  default="obsidian", param_key="pg_password")


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
    print("    Bind mount    — lives in a local directory; survives  docker compose down -v")
    print()
    if not confirm("Use a persistent bind mount? (recommended)", default="y",
                   param_key="persistent"):
        return None, None

    default_dir = str(Path.home() / ".local" / "share" / "obsidian-semantic-mcp")
    # When --persistent is set without --data-dir, use the default silently
    if "persistent" in _PARAMS and "data_dir" not in _PARAMS:
        _PARAMS["data_dir"] = default_dir
    raw = prompt("Local data directory", default=default_dir, param_key="data_dir")
    data_dir = Path(raw).expanduser().resolve()

    pgdata_path      = str(data_dir / "pgdata")
    ollama_data_path = str(data_dir / "ollama") if include_ollama else None

    for p in filter(None, [pgdata_path, ollama_data_path]):
        if DRY_RUN:
            _dry(f"mkdir -p {p}")
        else:
            Path(p).mkdir(parents=True, exist_ok=True)
            ok(f"Data directory: {p}")

    return pgdata_path, ollama_data_path


# ── Prerequisite checks ───────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).parent.resolve()


def check_docker():
    if not cmd_exists("docker"):
        fail("Docker not found — install Docker Desktop: https://docs.docker.com/get-docker/")
        return False
    r = run(["docker", "info"], check=False, capture=True)
    if r.returncode != 0:
        fail("Docker daemon is not running — start Docker Desktop first")
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
    cmd = ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new"]
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
        "-N", "-f",                          # background, no remote command
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ExitOnForwardFailure=yes",
        "-L", f"{local_port}:localhost:{remote_port}",
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
    ssh_user    = prompt("SSH username", default=os.environ.get("USER", "ubuntu"),
                         param_key="ssh_user")

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
            raw = prompt("Path to SSH private key", default=default_key or default_fallback)
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
                warn(f"SSH key {key_path} has too-open permissions. Run: chmod 600 {key_path}")

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


# ── .env writer (runtime only — gitignored) ───────────────────────────────────

def write_env(vault, pg_password, ollama_url, ssh_params=None,
              pgdata_path=None, ollama_data_path=None):
    """
    Write .env in the project root at runtime. This file is gitignored.

    vault may be a string (single vault) or list of strings (multi-vault).

    ssh_params, if provided, is a dict with keys:
      user, host, remote_port, local_port, key_path (optional)
    These are stored as OSM_SSH_* vars so `osm tunnel` can reconnect.

    pgdata_path / ollama_data_path, if set, are written as PGDATA_PATH /
    OLLAMA_DATA_PATH so Docker Compose uses bind mounts instead of named volumes.
    """
    env_path = PROJECT_ROOT / ".env"
    vaults = vault if isinstance(vault, list) else [vault]
    if len(vaults) > 1:
        lines = [
            f"OBSIDIAN_VAULTS={','.join(vaults)}",
            f"OBSIDIAN_VAULT={vaults[0]}",
            f"POSTGRES_PASSWORD={pg_password}",
            f"OLLAMA_URL={ollama_url}",
        ]
    else:
        lines = [
            f"OBSIDIAN_VAULT={vaults[0]}",
            f"POSTGRES_PASSWORD={pg_password}",
            f"OLLAMA_URL={ollama_url}",
        ]
    if pgdata_path:
        lines.append(f"PGDATA_PATH={pgdata_path}")
    if ollama_data_path:
        lines.append(f"OLLAMA_DATA_PATH={ollama_data_path}")
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
        ok("Claude Code CLI: obsidian-semantic already registered — global, shared across all projects")
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
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
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
        ok(f"Claude Desktop: obsidian-semantic already configured — global, shared across all projects")
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


def _docker_entry():
    """Claude Desktop config entry for all Docker-based installs."""
    container = f"{PROJECT_ROOT.name}-mcp-server-1"
    return {
        "command": "docker",
        "args": ["exec", "-i", container, "python3", "src/server.py"],
        "env": {},
    }


def _native_entry(vault, db_url):
    """Claude Desktop config entry for native install.

    vault may be a string or list of strings.
    """
    vaults = vault if isinstance(vault, list) else [vault]
    env = {"DATABASE_URL": db_url}
    if len(vaults) > 1:
        env["OBSIDIAN_VAULTS"] = ",".join(vaults)
    else:
        env["OBSIDIAN_VAULT"] = vaults[0]
    return {
        "command": str(PROJECT_ROOT / ".venv" / "bin" / "python3"),
        "args": [str(PROJECT_ROOT / "src" / "server.py")],
        "env": env,
    }


# ── Docker compose helpers ────────────────────────────────────────────────────

def compose(args, env=None):
    return run(
        ["docker", "compose", "--project-directory", str(PROJECT_ROOT)] + args,
        env=env,
    )


def _write_compose_override(vaults):
    """Generate docker-compose.override.yml for multi-vault volume mounts."""
    if len(vaults) <= 1:
        override = PROJECT_ROOT / "docker-compose.override.yml"
        if override.exists():
            override.unlink()
        return
    container_paths = []
    vol_lines = []
    for v in vaults:
        name = Path(v).name
        container_path = f"/{name}"
        container_paths.append(container_path)
        vol_lines.append(f"      - {v}:{container_path}")
    vol_block = "\n".join(vol_lines)
    # Dashboard mounts are read-only
    vol_ro_lines = [f"{l}:ro" for l in vol_lines]
    vol_ro_block = "\n".join(vol_ro_lines)
    vaults_env = ",".join(container_paths)
    content = (
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
    override_path = PROJECT_ROOT / "docker-compose.override.yml"
    if DRY_RUN:
        _dry(f"write {override_path}", "contents shown below")
        print()
        for line in content.splitlines():
            print(f"    {_c('90', line)}")
        print()
        return
    override_path.write_text(content)
    ok(f"Wrote {override_path} with {len(vaults)} vault mounts")


def compose_up(services=None, env=None):
    args = ["up", "-d"] + (list(services) if services else [])
    cmd = ["docker", "compose", "--project-directory", str(PROJECT_ROOT)] + args
    if DRY_RUN:
        run(cmd, env=env)
        return
    kw: dict = {}
    if env:
        kw["env"] = env
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, **kw)
    for line in proc.stdout:
        print(line, end="", flush=True)
    proc.wait()


def wait_for_postgres(timeout=90):
    info("Waiting for postgres to be healthy…")
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = run(
            ["docker", "compose", "--project-directory", str(PROJECT_ROOT),
             "exec", "-T", "postgres", "pg_isready", "-U", "obsidian", "-d", "obsidian_brain"],
            check=False, capture=True,
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

    info("Pulling nomic-embed-text (first run may take a few minutes)…")
    run(["ollama", "pull", "nomic-embed-text"])
    ok("Model ready: nomic-embed-text")
    if not DRY_RUN:
        info("Verifying nomic-embed-text is available…")
        if _verify_ollama_model("http://localhost:11434", "nomic-embed-text"):
            ok("nomic-embed-text verified — embeddings responding")
        else:
            warn("Model verification failed — embeddings did not respond")
            info("Try manually:  ollama pull nomic-embed-text")

    # ── Python env ────────────────────────────────────────────────────────────
    header("Python environment")
    if not cmd_exists("uv"):
        fail("uv not found — install from https://github.com/astral-sh/uv")
        sys.exit(1)
    run(["uv", "sync", "--project", str(PROJECT_ROOT)])
    ok("Dependencies installed in .venv")

    # ── Claude Desktop + CLI config ───────────────────────────────────────────
    header("Claude Desktop configuration")
    entry = _native_entry(vault, db_url)
    update_claude_config(entry)
    register_claude_cli(entry)

    _done_native(vault)


def mode_full_docker():
    header("Full Docker  (Postgres + Ollama + MCP server all in containers)")
    hr()

    if not check_docker() or not check_compose():
        sys.exit(1)

    vaults = prompt_vault()
    pg_pw = prompt_pg_password()
    pgdata_path, ollama_data_path = prompt_persistent_storage(include_ollama=True)
    write_env(vaults, pg_pw, "http://ollama:11434",
              pgdata_path=pgdata_path, ollama_data_path=ollama_data_path)
    _write_compose_override(vaults)

    header("Starting all services")
    env = {**os.environ, "OBSIDIAN_VAULT": vaults[0], "POSTGRES_PASSWORD": pg_pw,
           "OLLAMA_URL": "http://ollama:11434", "COMPOSE_PROFILES": "full-docker"}
    if len(vaults) > 1:
        env["OBSIDIAN_VAULTS"] = ",".join(f"/{Path(v).name}" for v in vaults)
    if pgdata_path:
        env["PGDATA_PATH"] = pgdata_path
    if ollama_data_path:
        env["OLLAMA_DATA_PATH"] = ollama_data_path
    compose_up(env=env)
    wait_for_postgres()

    header("Claude Desktop configuration")
    entry = _docker_entry()
    update_claude_config(entry)
    register_claude_cli(entry)
    _done_docker()


def mode_docker_host_ollama():
    header("Docker + host Ollama  (Postgres in Docker, Ollama already running on this machine)")
    hr()

    if not check_docker() or not check_compose():
        sys.exit(1)

    if not check_ollama_at("localhost"):
        fail("Ollama is not running — start it first:  ollama serve")
        info("Then re-run:  osm init")
        sys.exit(1)

    # host.docker.internal resolves to the Docker host on macOS and Windows;
    # on Linux you may need to pass --add-host or use the bridge IP.
    system = platform.system()
    ollama_host = "host.docker.internal" if system in ("Darwin", "Windows") else "172.17.0.1"
    ollama_url  = f"http://{ollama_host}:11434"

    vaults = prompt_vault()
    pg_pw = prompt_pg_password()
    pgdata_path, _ = prompt_persistent_storage(include_ollama=False)
    write_env(vaults, pg_pw, ollama_url, pgdata_path=pgdata_path)
    _write_compose_override(vaults)

    header("Starting services (postgres, mcp-server, dashboard)")
    env = {**os.environ, "OBSIDIAN_VAULT": vaults[0], "POSTGRES_PASSWORD": pg_pw, "OLLAMA_URL": ollama_url}
    if len(vaults) > 1:
        env["OBSIDIAN_VAULTS"] = ",".join(f"/{Path(v).name}" for v in vaults)
    if pgdata_path:
        env["PGDATA_PATH"] = pgdata_path
    compose_up(services=["postgres", "mcp-server", "dashboard"], env=env)
    wait_for_postgres()

    header("Claude Desktop configuration")
    entry = _docker_entry()
    update_claude_config(entry)
    register_claude_cli(entry)
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
    remote_vault = prompt("Path to vault on remote machine (absolute)",
                          param_key="vault_remote")
    default_mount = str(Path.home() / "obsidian-remote-vault")
    mount_point   = prompt("Local mount point", default=default_mount)

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
    header("Docker + remote Ollama  (Postgres in Docker, Ollama on another host via SSH)")
    hr()

    if not check_docker() or not check_compose():
        sys.exit(1)

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
        print(f"     Check: SSH key path, host reachability, and that SSH is enabled on the remote.")
        print()
        if not confirm("Continue anyway?", default="n"):
            fail("Aborted — fix SSH connectivity and re-run osm init")
            sys.exit(1)
    elif not DRY_RUN:
        ok(f"SSH connection to {ssh_user}@{remote_host} succeeded")

    tunnel_ok = open_ssh_tunnel(ssh_user, remote_host, remote_port,
                                local_tunnel_port, key_path)
    if tunnel_ok:
        time.sleep(1)
        check_ollama_at("localhost", local_tunnel_port)
    else:
        if not confirm("Tunnel failed — continue anyway?", default="n"):
            sys.exit(0)

    # Docker containers reach the host-side tunnel via host.docker.internal
    # (macOS/Windows Docker Desktop) or the bridge gateway (Linux).
    system      = platform.system()
    tunnel_host = "host.docker.internal" if system in ("Darwin", "Windows") else "172.17.0.1"
    ollama_url  = f"http://{tunnel_host}:{local_tunnel_port}"

    # ── Vault path ────────────────────────────────────────────────────────────
    header("Obsidian vault")
    vault = _prompt_vault_location(ssh_user, remote_host, key_path)
    vaults = vault if isinstance(vault, list) else [vault]

    # ── Write .env with SSH params for future reconnect ───────────────────────
    pg_pw = prompt_pg_password()
    pgdata_path, _ = prompt_persistent_storage(include_ollama=False)
    ssh_params = {
        "user":        ssh_user,
        "host":        remote_host,
        "remote_port": remote_port,
        "local_port":  local_tunnel_port,
        "key_path":    key_path,
    }
    write_env(vaults, pg_pw, ollama_url, ssh_params=ssh_params, pgdata_path=pgdata_path)
    _write_compose_override(vaults)

    # ── Start Docker services ─────────────────────────────────────────────────
    header("Starting services (postgres, mcp-server, dashboard)")
    env = {**os.environ, "OBSIDIAN_VAULT": vaults[0], "POSTGRES_PASSWORD": pg_pw,
           "OLLAMA_URL": ollama_url}
    if len(vaults) > 1:
        env["OBSIDIAN_VAULTS"] = ",".join(f"/{Path(v).name}" for v in vaults)
    if pgdata_path:
        env["PGDATA_PATH"] = pgdata_path
    compose_up(services=["postgres", "mcp-server", "dashboard"], env=env)
    wait_for_postgres()

    header("Claude Desktop configuration")
    entry = _docker_entry()
    update_claude_config(entry)
    register_claude_cli(entry)
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


def _link_osm_to_path():
    """
    Write a self-contained launcher script to $HOME/.local/bin/osm.

    Unlike a symlink, the launcher survives the repo being moved or deleted:
    it tries the original install path first, then falls back to the XDG
    standard location, then prints a clear reinstall error.
    """
    bin_dir  = Path.home() / ".local" / "bin"
    launcher = bin_dir / "osm"
    xdg_data = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    xdg_osm  = xdg_data / "obsidian-semantic-mcp" / "scripts" / "osm"
    primary  = PROJECT_ROOT / "scripts" / "osm"

    script = (
        "#!/usr/bin/env bash\n"
        "# osm launcher — generated by osm init\n"
        f'_PRIMARY="{primary}"\n'
        f'_XDG="{xdg_osm}"\n'
        '_INSTALL_URL="https://raw.githubusercontent.com/celstnblacc/obsidian-semantic-mcp/main/install.sh"\n'
        'if [ -f "$_PRIMARY" ]; then\n'
        '    exec "$_PRIMARY" "$@"\n'
        'elif [ -f "$_XDG" ]; then\n'
        '    exec "$_XDG" "$@"\n'
        'else\n'
        '    echo "osm: installation not found" >&2\n'
        '    echo "  Looked in: $_PRIMARY" >&2\n'
        '    echo "  And:       $_XDG" >&2\n'
        '    echo "  Reinstall: curl -fsSL $_INSTALL_URL | bash" >&2\n'
        '    exit 1\n'
        'fi\n'
    )

    if DRY_RUN:
        _dry(f"write {launcher}", "self-contained launcher (survives repo move/delete)")
        return

    bin_dir.mkdir(parents=True, exist_ok=True)
    try:
        launcher.unlink(missing_ok=True)  # remove symlink before writing — write_text follows symlinks
        launcher.write_text(script)
        launcher.chmod(0o755)
        primary.chmod(0o755)
        ok(f"Installed: {launcher}  (osm is now globally available)")
        # PATH hint if $HOME/.local/bin is not in PATH
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


def cmd_tunnel():
    """Re-open the SSH tunnel using credentials stored in .env."""
    header("OSM Tunnel — reconnect SSH tunnel")
    hr()

    env = _read_env()
    user        = env.get("OSM_SSH_USER")
    host        = env.get("OSM_SSH_HOST")
    remote_port = env.get("OSM_SSH_REMOTE_PORT", "11434")
    local_port  = env.get("OSM_SSH_LOCAL_PORT", "11435")
    key_path    = env.get("OSM_SSH_KEY")

    if not user or not host:
        fail("No SSH config found in .env — run osm init first")
        sys.exit(1)

    info(f"Reconnecting: {user}@{host} (tunnel localhost:{local_port} → {host}:{remote_port})")
    ok_flag = open_ssh_tunnel(user, host, int(remote_port), int(local_port), key_path)
    if ok_flag:
        time.sleep(1)
        check_ollama_at("localhost", int(local_port))
    else:
        sys.exit(1)


# ── Status command ────────────────────────────────────────────────────────────

def cmd_status():
    header("OSM Status")
    hr()

    r = run(
        ["docker", "compose", "--project-directory", str(PROJECT_ROOT), "ps", "--format", "table"],
        check=False, capture=True,
    )
    if r.returncode == 0 and r.stdout.strip():
        print(r.stdout)
    else:
        info("No Docker services running")

    check_ollama_at("localhost")

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


# ── Rebuild command ───────────────────────────────────────────────────────────

def cmd_rebuild():
    header("Rebuilding Docker images")
    hr()
    compose(["up", "-d", "--build", "mcp-server", "dashboard"])
    ok("Rebuild complete")
    info("Dashboard:  http://localhost:8484")


# ── Remove command ────────────────────────────────────────────────────────────

def cmd_remove():
    header("OSM Remove — tear down Obsidian Semantic MCP")
    hr()
    print()
    warn("This will:")
    print("    • Stop and remove all Docker containers and volumes  (all indexed embeddings lost)")
    print("    • Delete .env from this project")
    print("    • Remove obsidian-semantic from claude_desktop_config.json")
    print("    • Remove obsidian-semantic from Claude Code CLI  ($HOME/.claude.json)")
    print("    • Delete the osm launcher from $HOME/.local/bin/osm")
    print()

    force = _PARAMS.get("yes") == "y"
    if not DRY_RUN and not force and not confirm("Continue?", default="n"):
        info("Aborted — nothing changed")
        return

    # ── Docker services + volumes ─────────────────────────────────────────────
    header("Stopping Docker services")
    r = run(
        ["docker", "compose", "--project-directory", str(PROJECT_ROOT), "ps", "-q"],
        check=False, capture=True,
    )
    if not DRY_RUN and not (r.stdout or "").strip():
        info("No running Docker services found — skipping")
    else:
        run(["docker", "compose", "--project-directory", str(PROJECT_ROOT), "down", "-v"],
            check=False)
        if not DRY_RUN:
            ok("Docker services stopped and volumes removed")

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
        warn("Unknown platform — remove obsidian-semantic from claude_desktop_config.json manually")
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
            r = run(["claude", "mcp", "remove", "--scope", "user", "obsidian-semantic"],
                    check=False)
            if r.returncode == 0:
                ok("Removed obsidian-semantic from Claude Code CLI")
            else:
                info("obsidian-semantic not found in Claude Code CLI — skipping")
    else:
        info("claude CLI not found — skipping")

    # ── osm launcher ──────────────────────────────────────────────────────────
    header("Removing osm launcher")
    launcher = Path.home() / ".local" / "bin" / "osm"
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
    "1": ("Native",                  "Homebrew + local Postgres + local Ollama",         mode_native_macos),
    "2": ("Docker + host Ollama",    "Postgres in Docker, Ollama already on this Mac",   mode_docker_host_ollama),
    "3": ("Full Docker",             "Everything in containers  (recommended)",           mode_full_docker),
    "4": ("Docker + remote Ollama",  "Postgres in Docker, Ollama on another machine via SSH", mode_docker_remote_ollama),
}

MODES_LINUX = {
    "1": ("Docker + host Ollama",    "Postgres in Docker, Ollama on this machine",       mode_docker_host_ollama),
    "2": ("Full Docker",             "Everything in containers  (recommended)",           mode_full_docker),
    "3": ("Docker + remote Ollama",  "Postgres in Docker, Ollama on another machine",    mode_docker_remote_ollama),
}

MODES_WINDOWS = {
    "1": ("Docker + host Ollama",    "Postgres in Docker, Ollama already on this PC",    mode_docker_host_ollama),
    "2": ("Full Docker",             "Everything in containers  (recommended)",           mode_full_docker),
    "3": ("Docker + remote Ollama",  "Postgres in Docker, Ollama on another machine",    mode_docker_remote_ollama),
}


# ── Init command ──────────────────────────────────────────────────────────────

def cmd_init():
    print()
    hr()
    print(_c("1", "  Obsidian Semantic MCP — Setup Wizard"))
    hr()

    system = platform.system()
    if system == "Darwin":
        ver  = platform.mac_ver()[0]
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
        print(f"     {_c('93', f'Recommended: {rec_key}) {rec_name}')}"
              f"  — skips the ~3 GB Ollama image pull")
        print()

    choice = prompt("Choose", choices=list(modes.keys()), param_key="mode")
    _, _, handler = modes[choice]
    handler()


# ── Help command ──────────────────────────────────────────────────────────────

_INSTALL_URL = "https://raw.githubusercontent.com/celstnblacc/obsidian-semantic-mcp/main/install.sh"


def cmd_help():
    print(f"\n  {_c('1', 'osm')} — Obsidian Semantic MCP CLI\n")
    print(f"  {_c('1', 'Install (one-liner):')}\n")
    print(f"    curl -fsSL {_INSTALL_URL} | bash\n")
    print(f"  {_c('1', 'Usage:')}  osm <command> [flags]\n")
    print(f"  {_c('1', 'Commands:')}\n")
    for name, (_, desc) in COMMANDS.items():
        print(f"    {_c('1', f'osm {name:<10}')}  {desc}")
    print()
    print(f"  {_c('1', 'Flags:')}\n")
    print(f"    {_c('1', '--dry-run')}              Print every action that would run — make no changes")
    print(f"    {_c('1', '--yes')}                 Skip confirmation prompts  (use with: osm remove)")
    print()
    print(f"  {_c('1', 'init flags')}  (skip interactive prompts — usable from scripts or AI agents)\n")
    print(f"    {_c('1', '--mode <1-4>')}          Installation mode (macOS: 1=native 2=docker+host-ollama 3=full-docker 4=remote-ollama)")
    print(f"    {_c('1', '--vault <path>')}         Absolute path to Obsidian vault")
    print(f"    {_c('1', '--pg-password <pw>')}     Postgres password  (default: obsidian)")
    print(f"    {_c('1', '--persistent')}           Use bind-mount volumes for persistent storage")
    print(f"    {_c('1', '--no-persistent')}        Use named Docker volumes (wiped by down -v)")
    print(f"    {_c('1', '--data-dir <path>')}      Bind-mount data directory  (implies --persistent)")
    print(f"    {_c('1', '--ssh-host <host>')}      Remote Ollama host  (mode 4)")
    print(f"    {_c('1', '--ssh-user <user>')}      SSH username  (mode 4)")
    print(f"    {_c('1', '--ssh-port <port>')}      Remote Ollama port  (default: 11434, mode 4)")
    print(f"    {_c('1', '--ssh-key <path>')}       SSH private key path  (mode 4)")
    print(f"    {_c('1', '--vault-remote <path>')}  Vault path on remote machine — mount via sshfs  (mode 4)")
    print()
    print(f"  {_c('1', 'Examples:')}\n")
    print(f"    osm init                                    # Interactive setup")
    print(f"    osm init --dry-run                          # Preview without changes")
    print(f"    osm init --mode 3 --vault /path/to/vault \\")
    print(f"        --pg-password secret --persistent       # Non-interactive full Docker")
    print(f"    osm init --mode 4 --vault /path/to/vault \\")
    print(f"        --ssh-host 203.0.113.5 --ssh-user ubuntu \\")
    print(f"        --ssh-key $HOME/.ssh/id_ed25519         # Remote Ollama via SSH")
    print(f"    osm status                                  # Check service health")
    print(f"    osm tunnel                                  # Reconnect SSH tunnel")
    print(f"    osm rebuild                                 # Rebuild Docker images")
    print(f"    osm remove                                  # Stop services and wipe config")
    print()


# ── Entry point ───────────────────────────────────────────────────────────────

COMMANDS = {
    "init":    (cmd_init,    "Interactive setup wizard"),
    "status":  (cmd_status,  "Check service health"),
    "tunnel":  (cmd_tunnel,  "Reconnect SSH tunnel to remote Ollama host"),
    "rebuild": (cmd_rebuild, "Rebuild Docker images and restart"),
    "remove":  (cmd_remove,  "Stop services, delete volumes and config"),
    "help":    (cmd_help,    "Show this help message"),
}


_FLAG_MAP = {
    # flag name (without --) → _PARAMS key
    "vault":        "vault",
    "pg-password":  "pg_password",
    "mode":         "mode",
    "persistent":   "persistent",   # boolean, value "y"
    "no-persistent":"persistent",   # boolean, value "n"
    "data-dir":     "data_dir",
    "ssh-host":     "ssh_host",
    "ssh-user":     "ssh_user",
    "ssh-port":     "ssh_port",
    "ssh-key":      "ssh_key",
    "vault-remote": "vault_remote",
    "yes":          "yes",          # boolean — skip all confirms in remove
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
            elif key in _FLAG_MAP and i + 1 < len(args) and not args[i + 1].startswith("--"):
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
