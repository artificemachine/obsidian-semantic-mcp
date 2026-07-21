"""tests/test_dashboard_security.py — Security & Correctness plan, iterations 1-2.

Iteration 1: the dashboard binds 127.0.0.1 by default and never prints the
Postgres password to stdout.
Iteration 2: every mutating dashboard endpoint requires a bearer token.

Side-effect fence: no test in this file may read or write the real
`~/.config/obsidian-semantic-mcp/` directory. `DASHBOARD_TOKEN` is set as a
module-level env default before `dashboard` is ever imported, so importing
or reloading the module always takes the env-override fast path in
`config.resolve_dashboard_token()` and never touches the filesystem. Tests
that specifically exercise file-based token persistence monkeypatch
`config.OSM_CONFIG_DIR` to a `tmp_path` and explicitly unset
`DASHBOARD_TOKEN` for the duration of that test only.
"""
from __future__ import annotations

import http.client
import http.server
import importlib
import os
import re
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# Minimal env so server.py / dashboard.py import without crashing. Matches
# the established pattern in tests/test_unit.py. DASHBOARD_TOKEN is set here
# specifically so importing `dashboard` — which resolves a token at import
# time to build HTML_PAGE — never touches the real config directory.
os.environ.setdefault("OBSIDIAN_VAULT", "/tmp/test_vault")
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/test")
os.environ.setdefault("OLLAMA_URL", "http://localhost:11434")
os.environ.setdefault("DASHBOARD_TOKEN", "test-fixture-token-not-a-secret")

import config  # noqa: E402
import dashboard  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_SRC = REPO_ROOT / "src" / "dashboard.py"
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
ENV_EXAMPLE = REPO_ROOT / ".env.example"


def _reload_dashboard():
    """Reload dashboard.py so its module-level constants re-read the
    current env (DASHBOARD_BIND, DASHBOARD_TOKEN, DATABASE_URL, ...)."""
    return importlib.reload(dashboard)


@pytest.fixture(autouse=True)
def _restore_dashboard_token_env(monkeypatch):
    """Every test in this file gets the safe default token env, even if a
    prior test in the same session deleted it — belt-and-suspenders on top
    of the module-level setdefault, since monkeypatch.delenv in one test
    only reverts at that test's teardown, not before the next test runs."""
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-fixture-token-not-a-secret")


# ─────────────────────────────── Iteration 1 ────────────────────────────────

def test_smoke_dashboard_module_imports_cleanly():
    """Smoke: importing dashboard with DATABASE_URL set does not raise."""
    mod = _reload_dashboard()
    assert mod is dashboard


def test_default_bind_is_loopback(monkeypatch):
    monkeypatch.delenv("DASHBOARD_BIND", raising=False)
    mod = _reload_dashboard()
    assert mod.DASHBOARD_BIND == "127.0.0.1"


def test_bind_is_overridable_via_env(monkeypatch):
    monkeypatch.setenv("DASHBOARD_BIND", "0.0.0.0")
    mod = _reload_dashboard()
    assert mod.DASHBOARD_BIND == "0.0.0.0"


def test_no_hardcoded_wildcard_bind_in_source():
    """grep-equivalent: no literal '0.0.0.0' HTTPServer bind remains in
    dashboard.py — the bind address must always flow through DASHBOARD_BIND."""
    src = DASHBOARD_SRC.read_text()
    assert '"0.0.0.0"' not in src


def test_startup_banner_does_not_leak_password():
    """Regression guard for the Stage 5 MEDIUM finding at (pre-fix)
    src/dashboard.py:866 — the startup banner printed the raw DATABASE_URL,
    including the Postgres password, to stdout. Spawns the real __main__
    block as a subprocess (binds an ephemeral port, no live DB contact —
    the banner only stringifies DATABASE_URL, it never connects)."""
    secret = "s3cr3t-p4ssw0rd-marker"
    env = {
        **os.environ,
        "DATABASE_URL": f"postgresql://obsidian:{secret}@localhost:5432/obsidian_brain",
        "OBSIDIAN_VAULT": "/tmp/test_vault",
        "DASHBOARD_PORT": "0",  # OS picks a free ephemeral port — never binds a fixed one
        "DASHBOARD_TOKEN": "test-fixture-token-not-a-secret",
        "PYTHONUNBUFFERED": "1",
    }
    proc = subprocess.Popen(
        [sys.executable, "-u", str(DASHBOARD_SRC)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    try:
        lines = []
        deadline = time.time() + 10
        while time.time() < deadline and len(lines) < 4:
            line = proc.stdout.readline()
            if not line:
                break
            lines.append(line)
        banner = "".join(lines)
        assert secret not in banner, f"password leaked in startup banner: {banner!r}"
        assert "127.0.0.1" in banner, f"expected loopback bind in banner: {banner!r}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_compose_dashboard_sets_explicit_bind():
    """Integration: docker-compose.yml's dashboard service must set
    DASHBOARD_BIND=0.0.0.0 explicitly, or the container becomes unreachable
    from the host now that the code default is loopback-only."""
    compose = yaml.safe_load(COMPOSE_FILE.read_text())
    dash_env = compose["services"]["dashboard"]["environment"]
    assert dash_env.get("DASHBOARD_BIND") == "0.0.0.0"


def test_env_example_documents_dashboard_bind():
    """Contract: DASHBOARD_BIND must be documented in .env.example."""
    assert "DASHBOARD_BIND" in ENV_EXAMPLE.read_text()


# ─────────────────────────────── Iteration 2 ────────────────────────────────
#
# A real DashboardHandler bound to an ephemeral 127.0.0.1 port, per-test. This
# is a genuine HTTP round trip (not a mocked handler) so a broken auth gate
# would actually be exercised, not assumed. VAULT_PATHS is left empty by
# default so /api/reindex's authenticated-but-unconfigured path returns 400
# without ever touching a DB connection; individual tests override it.

TEST_TOKEN = "iteration2-test-token-not-a-secret"


class _FakeFreeLockCM:
    """Stand-in for dashboard.reindex_lock() that always reports "acquired"
    and never touches a real DB — iteration 5 replaced the process-local
    threading.Lock these auth-focused tests used to reset directly with a
    Postgres advisory lock; these tests care about the auth gate, not lock
    contention (that's tests/test_advisory_lock.py's job), so a lock that
    is always free keeps their behavior identical to before iteration 5."""

    def __enter__(self):
        return True

    def __exit__(self, *exc_info):
        return False


@contextmanager
def _running_dashboard(monkeypatch, token: str = TEST_TOKEN):
    monkeypatch.setattr(dashboard, "DASHBOARD_TOKEN", token)
    monkeypatch.setattr(dashboard, "reindex_lock", lambda: _FakeFreeLockCM())
    monkeypatch.setattr(dashboard, "VAULT_PATHS", [])
    httpd = http.server.HTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_post_reindex_without_token_returns_401(monkeypatch):
    with _running_dashboard(monkeypatch) as base_url:
        r = requests.post(f"{base_url}/api/reindex")
        assert r.status_code == 401
        assert r.json() == {"ok": False, "message": "unauthorized"}


def test_unauthorized_request_is_logged(monkeypatch, caplog):
    """Regression for the arch-audit's observability finding: dashboard.py
    had zero structured logging, so a failed auth attempt left no trace.
    An unauthorized request must now produce a log record — this is a
    security-relevant event, not just an HTTP status code."""
    with caplog.at_level("WARNING", logger="dashboard"):
        with _running_dashboard(monkeypatch) as base_url:
            r = requests.post(f"{base_url}/api/reindex")
            assert r.status_code == 401
    assert any("unauthorized" in rec.message for rec in caplog.records)


def test_post_reindex_full_without_token_returns_401(monkeypatch):
    with _running_dashboard(monkeypatch) as base_url:
        r = requests.post(f"{base_url}/api/reindex/full")
        assert r.status_code == 401


def test_post_prune_without_token_returns_401(monkeypatch):
    with _running_dashboard(monkeypatch) as base_url:
        r = requests.post(f"{base_url}/api/prune")
        assert r.status_code == 401


def test_post_ollama_start_without_token_returns_401(monkeypatch):
    with _running_dashboard(monkeypatch) as base_url:
        r = requests.post(f"{base_url}/api/ollama/start")
        assert r.status_code == 401


def test_post_with_valid_token_is_accepted(monkeypatch):
    """A valid token must reach the handler and get the endpoint's
    pre-existing status code (400 here, because VAULT_PATHS is empty) —
    not 401. Proves the gate lets a correct token *through*, which a test
    that only checks "not 401" for garbage input would not prove."""
    with _running_dashboard(monkeypatch) as base_url:
        r = requests.post(
            f"{base_url}/api/reindex",
            headers={"Authorization": f"Bearer {TEST_TOKEN}"},
        )
        assert r.status_code == 400
        assert r.json() == {"ok": False, "message": "No vault configured"}


def test_post_with_wrong_token_returns_401(monkeypatch):
    with _running_dashboard(monkeypatch) as base_url:
        r = requests.post(
            f"{base_url}/api/reindex",
            headers={"Authorization": "Bearer wrong-token-entirely"},
        )
        assert r.status_code == 401


def test_token_comparison_uses_compare_digest():
    """Guards against a later `token == DASHBOARD_TOKEN` regression, which
    would reintroduce a timing side-channel."""
    src = DASHBOARD_SRC.read_text()
    assert "compare_digest" in src


def test_get_endpoints_remain_unauthenticated(monkeypatch):
    """Pins the deliberate design decision: GET is read-only and, as of
    iteration 1, loopback-bound — gating it would need to solve initial
    page-load auth, out of scope. A future reader must not "fix" this."""
    with _running_dashboard(monkeypatch) as base_url:
        r = requests.get(f"{base_url}/api/reindex/status")
        assert r.status_code == 200
        r = requests.get(f"{base_url}/api/stats")
        assert r.status_code == 200


# ── Token file persistence — config.resolve_dashboard_token() ───────────────
# Every test below monkeypatches config.OSM_CONFIG_DIR to a tmp_path and
# explicitly deletes DASHBOARD_TOKEN from the env for the duration of the
# test. Neither the real ~/.config/obsidian-semantic-mcp/ directory nor any
# path under the repo root is ever touched.

def test_token_file_created_with_0600_when_absent(tmp_path, monkeypatch):
    fake_config_dir = tmp_path / ".config" / "obsidian-semantic-mcp"
    monkeypatch.setattr(config, "OSM_CONFIG_DIR", fake_config_dir)
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)

    token = config.resolve_dashboard_token()

    token_file = fake_config_dir / "dashboard_token"
    assert token_file.exists()
    assert token_file.read_text(encoding="utf-8").strip() == token
    mode = token_file.stat().st_mode & 0o777
    assert mode == 0o600, f"expected mode 0600, got {oct(mode)}"


def test_token_file_is_reused_across_restarts(tmp_path, monkeypatch):
    fake_config_dir = tmp_path / ".config" / "obsidian-semantic-mcp"
    monkeypatch.setattr(config, "OSM_CONFIG_DIR", fake_config_dir)
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)

    first = config.resolve_dashboard_token()
    second = config.resolve_dashboard_token()
    assert first == second


def test_env_token_overrides_file(tmp_path, monkeypatch):
    fake_config_dir = tmp_path / ".config" / "obsidian-semantic-mcp"
    monkeypatch.setattr(config, "OSM_CONFIG_DIR", fake_config_dir)
    monkeypatch.setenv("DASHBOARD_TOKEN", "the-env-token")

    token = config.resolve_dashboard_token()

    assert token == "the-env-token"
    assert not fake_config_dir.exists(), "env override must write nothing to disk"


def test_token_is_never_written_to_repo_root(tmp_path, monkeypatch):
    """Guards the decoupling guardrail directly: no `.env`, `dashboard_token`,
    or similar new file appears under the repo root after token resolution."""
    fake_config_dir = tmp_path / ".config" / "obsidian-semantic-mcp"
    monkeypatch.setattr(config, "OSM_CONFIG_DIR", fake_config_dir)
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)

    before = set(REPO_ROOT.iterdir())
    config.resolve_dashboard_token()
    after = set(REPO_ROOT.iterdir())

    assert after == before, f"repo root gained files: {after - before}"


def test_token_resolution_precedence(tmp_path, monkeypatch):
    """State machine: env-set -> env wins; env-unset + file-present -> file
    wins; env-unset + file-absent -> generate and persist."""
    fake_config_dir = tmp_path / ".config" / "obsidian-semantic-mcp"
    monkeypatch.setattr(config, "OSM_CONFIG_DIR", fake_config_dir)

    # env-unset + file-absent -> generate and persist
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    generated = config.resolve_dashboard_token()
    token_file = fake_config_dir / "dashboard_token"
    assert token_file.exists()

    # env-unset + file-present -> file wins (same value, no regeneration)
    reread = config.resolve_dashboard_token()
    assert reread == generated

    # env-set -> env wins, even though a file already exists
    monkeypatch.setenv("DASHBOARD_TOKEN", "env-wins-token")
    assert config.resolve_dashboard_token() == "env-wins-token"


def test_unreadable_token_file_falls_back_to_generated(tmp_path, monkeypatch):
    """Chaos: a config dir that exists but whose token file cannot be read
    (permissions, race) must not raise — fall back to a freshly generated
    token rather than crashing the dashboard at import time."""
    fake_config_dir = tmp_path / ".config" / "obsidian-semantic-mcp"
    fake_config_dir.mkdir(parents=True)
    token_file = fake_config_dir / "dashboard_token"
    token_file.write_text("unreadable-content", encoding="utf-8")
    token_file.chmod(0o000)
    monkeypatch.setattr(config, "OSM_CONFIG_DIR", fake_config_dir)
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)

    try:
        token = config.resolve_dashboard_token()
        assert token  # did not raise, returned something usable
    finally:
        token_file.chmod(0o600)  # restore so tmp_path cleanup can delete it


# ── Integration / contract / regression / chaos / e2e ───────────────────────

def test_ui_script_sends_auth_header_on_every_post():
    """Every `method: 'POST'` fetch in the rendered HTML_PAGE must carry an
    Authorization header nearby in the same fetch() call."""
    mod = _reload_dashboard()
    post_markers = [m.start() for m in re.finditer(r"method:\s*'POST'", mod.HTML_PAGE)]
    assert post_markers, "no POST fetch calls found in HTML_PAGE — test itself may be broken"
    for pos in post_markers:
        window = mod.HTML_PAGE[max(0, pos - 250):pos + 100]
        assert "Authorization" in window, (
            f"POST fetch near offset {pos} has no Authorization header nearby: {window!r}"
        )


def test_compose_dashboard_passes_token_env():
    compose = yaml.safe_load(COMPOSE_FILE.read_text())
    dash_env = compose["services"]["dashboard"]["environment"]
    assert "DASHBOARD_TOKEN" in dash_env


def test_401_body_matches_existing_error_shape(monkeypatch):
    """Contract: the 401 body must carry the same {"ok", "message"} keys the
    other do_POST branches (400/409/500) already use."""
    with _running_dashboard(monkeypatch) as base_url:
        r = requests.post(f"{base_url}/api/reindex")
        assert r.status_code == 401
        body = r.json()
        assert set(body.keys()) == {"ok", "message"}
        assert body["ok"] is False


def test_cross_origin_simple_post_is_rejected(monkeypatch):
    """Reproduces the audit's CSRF scenario directly: a "simple request"
    cross-origin POST (no Authorization header, Content-Type: text/plain —
    the shape a no-preflight cross-origin fetch/form-submit produces) must
    be rejected."""
    with _running_dashboard(monkeypatch) as base_url:
        r = requests.post(
            f"{base_url}/api/reindex",
            headers={"Content-Type": "text/plain"},
            data="whatever a forged cross-origin request sends",
        )
        assert r.status_code == 401


@pytest.mark.parametrize(
    "header_value",
    ["Bearer", "Bearer ", "Basic xyz", "Bearer " + ("x" * 10_000)],
)
def test_malformed_authorization_header_returns_401(monkeypatch, header_value):
    with _running_dashboard(monkeypatch) as base_url:
        r = requests.post(
            f"{base_url}/api/reindex",
            headers={"Authorization": header_value},
        )
        assert r.status_code == 401


def test_reindex_roundtrip_with_token(monkeypatch):
    """E2E: a valid-token POST to /api/reindex with a configured (stubbed)
    vault returns 200 and actually invokes index_vault exactly once."""
    stub_index_vault = MagicMock()
    monkeypatch.setattr(dashboard, "index_vault", stub_index_vault)

    with _running_dashboard(monkeypatch) as base_url:
        # _running_dashboard defaults VAULT_PATHS to [] — override for this test.
        monkeypatch.setattr(dashboard, "VAULT_PATHS", ["/fake/vault"])
        r = requests.post(
            f"{base_url}/api/reindex",
            headers={"Authorization": f"Bearer {TEST_TOKEN}"},
        )
        assert r.status_code == 200
        assert r.json() == {"ok": True, "message": "started"}

        deadline = time.time() + 5
        while time.time() < deadline and stub_index_vault.call_count == 0:
            time.sleep(0.05)
        assert stub_index_vault.call_count == 1
        stub_index_vault.assert_called_once_with("/fake/vault")
