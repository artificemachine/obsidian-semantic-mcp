"""Phase 2b: ensure the MCP server responds to `initialize` when invoked
over an anonymous pipe (the spawn pattern used by Claude Code's MCP client
via Node.js child_process.spawn).

The known-bad behavior (May 8 2026): `for line in sys.stdin.buffer:` in
the raw-stdin transport in server.py blocks the asyncio event loop, so
the initialize response is queued on the write stream but `_stdout_writer`
never gets scheduled. Claude Code times out at 30s.

The fix: wrap the blocking readline in a thread offload (e.g.
`anyio.to_thread.run_sync`) so the event loop stays free between reads.

This test runs the launcher as a subprocess with pipe stdin/stdout and
verifies the initialize response arrives within 20 seconds without
closing stdin. It is an integration test: requires PostgreSQL on
localhost:5432 (or sets DATABASE_URL/POSTGRES_PASSWORD via env to point
elsewhere). If neither is available, the test skips.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _initialize_request() -> bytes:
    return (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            }
        )
        + "\n"
    ).encode()


def _have_postgres() -> bool:
    """Quick check that postgres is reachable. Skip the test if not."""
    if os.environ.get("DATABASE_URL"):
        return True
    if os.environ.get("POSTGRES_PASSWORD"):
        return True
    # Try the Docker-installed postgres on its mapped port.
    try:
        import socket
        s = socket.socket()
        s.settimeout(0.5)
        s.connect(("127.0.0.1", 5433))
        s.close()
        return True
    except Exception:
        return False


@pytest.mark.skipif(
    not _have_postgres(),
    reason="postgres not reachable; integration test requires a DB",
)
def test_initialize_responds_over_anonymous_pipe(tmp_path):
    """Reproduce the May 8 stdin hang: spawn server with pipe stdin/stdout,
    write `initialize`, do NOT close stdin, expect response within 20s.

    This test is the canonical RED case for the bug. It will fail until
    the stdin reader is converted to a non-blocking thread offload.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "hello.md").write_text("# hello\n")

    env = os.environ.copy()
    env["OBSIDIAN_VAULT"] = str(vault)
    env.setdefault("POSTGRES_PASSWORD", "obsidian_brain")
    env.setdefault(
        "DATABASE_URL",
        "postgresql://obsidian_brain:obsidian_brain@127.0.0.1:5433/obsidian_brain",
    )
    env["OSM_DOCKER"] = "0"  # force in-process server
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(
        [sys.executable, "-m", "src.launcher"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        env=env,
        cwd=REPO_ROOT,
    )
    try:
        proc.stdin.write(_initialize_request())
        proc.stdin.flush()
        # CRITICAL: do NOT close stdin. Claude Code keeps the pipe open
        # for the lifetime of the connection; the bug only manifests
        # when stdin stays open.

        ready, _, _ = select.select([proc.stdout], [], [], 20.0)
        if not ready:
            stderr_tail = proc.stderr.read1(4000).decode(errors="replace")
            pytest.fail(
                "no response on stdout within 20s — stdin hang regression. "
                f"stderr tail:\n{stderr_tail[-1500:]}"
            )

        line = proc.stdout.readline()
        assert line, "stdout closed without responding"

        response = json.loads(line)
        assert response.get("jsonrpc") == "2.0"
        assert response.get("id") == 1
        assert "result" in response, f"expected result, got {response}"
        result = response["result"]
        assert "serverInfo" in result
        assert "obsidian-semantic" in result["serverInfo"]["name"].lower()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
