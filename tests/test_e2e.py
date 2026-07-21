#!/usr/bin/env python3
"""
End-to-end test — launches the MCP server as a subprocess
and exercises every tool over the JSON-RPC stdio transport.
"""

import json
import subprocess
import sys
import os
import time

SERVER_CMD = [
    sys.executable,
    os.path.join(os.path.dirname(__file__), "..", "src", "server.py"),
]

ID = 0

def next_id():
    global ID
    ID += 1
    return ID


def send(proc, method, params=None, *, is_notification=False):
    msg = {"jsonrpc": "2.0", "method": method}
    if params:
        msg["params"] = params
    if not is_notification:
        msg["id"] = next_id()
    raw = json.dumps(msg)
    proc.stdin.write(raw + "\n")
    proc.stdin.flush()
    return msg.get("id")


def recv(proc, timeout=60):
    """Read one JSON-RPC response, accumulating lines until valid JSON with an id."""
    deadline = time.time() + timeout
    buf = ""
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        buf += line
        try:
            data = json.loads(buf)
            buf = ""
            # skip server-initiated notifications (no "id")
            if "id" in data:
                return data
        except json.JSONDecodeError:
            continue
    return None


OK   = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
passed = 0
failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  {OK}  {label}" + (f" — {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  {FAIL}  {label}" + (f" — {detail}" if detail else ""))


def _build_server_env(vault: str) -> dict:
    """Build the subprocess env for the MCP server.

    Requires DATABASE_URL or POSTGRES_PASSWORD — mirrors config.py so the
    server doesn't crash after launch with a cryptic DB error.
    """
    env = {**os.environ, "OBSIDIAN_VAULT": vault}
    if not env.get("DATABASE_URL") and not env.get("POSTGRES_PASSWORD"):
        print(
            "Error: set DATABASE_URL or POSTGRES_PASSWORD before running this test.\n"
            "  Example: DATABASE_URL=postgresql://localhost/obsidian_brain "
            "OBSIDIAN_VAULT=/path/to/vault uv run python3 tests/test_e2e.py"
        )
        sys.exit(1)
    return env


def main():
    global passed, failed

    vault = os.environ.get("OBSIDIAN_VAULT", "")
    if not vault:
        print("Set OBSIDIAN_VAULT before running this test.")
        sys.exit(1)

    env = _build_server_env(vault)

    print("\n--- obsidian-semantic-mcp E2E test ---\n")
    print("Starting MCP server...")
    proc = subprocess.Popen(
        SERVER_CMD,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )

    try:
        # ── 1. Initialize ────────────────────────────────────────────────────
        print("\n[1] MCP Initialize")
        send(proc, "initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "0.1.0"},
        })
        resp = recv(proc)
        check("Server responded to initialize",
              resp and "result" in resp,
              resp["result"].get("serverInfo", {}).get("name", "") if resp and "result" in resp else str(resp))

        # Send initialized notification
        send(proc, "notifications/initialized", is_notification=True)

        # ── 2. List tools ────────────────────────────────────────────────────
        print("\n[2] List tools")
        send(proc, "tools/list")
        resp = recv(proc)
        tools = []
        if resp and "result" in resp:
            tools = [t["name"] for t in resp["result"].get("tools", [])]
        check("tools/list returned tools", len(tools) > 0, ", ".join(tools))
        check("search_vault registered", "search_vault" in tools)
        check("list_indexed_notes registered", "list_indexed_notes" in tools)
        check("reindex_vault registered", "reindex_vault" in tools)

        # ── 3. Wait for background indexing ──────────────────────────────────
        print("\n[3] Wait for indexing (up to 90s)...")
        indexed = False
        for i in range(18):
            time.sleep(5)
            send(proc, "tools/call", {
                "name": "list_indexed_notes",
                "arguments": {},
            })
            resp = recv(proc, timeout=10)
            if resp and "result" in resp:
                content = resp["result"].get("content", [{}])
                text = content[0].get("text", "") if content else ""
                if "notes indexed" in text:
                    count = text.split("notes indexed")[0].strip().split("**")[-1]
                    print(f"    ...{count} notes indexed so far")
                    indexed = True
                    break
                elif "No notes indexed" in text:
                    print(f"    ...still indexing ({(i+1)*5}s)")
        check("Vault indexed", indexed)

        # ── 4. Semantic search ───────────────────────────────────────────────
        print("\n[4] Semantic search")
        time.sleep(3)  # let indexing settle
        send(proc, "tools/call", {
            "name": "search_vault",
            "arguments": {"query": "project setup and configuration", "limit": 3},
        })
        resp = recv(proc, timeout=60)
        if resp and "result" in resp:
            content = resp["result"].get("content", [{}])
            text = content[0].get("text", "") if content else ""
            results = text.count("similarity:")
            check("search_vault returned results", results > 0, f"{results} results")
            check("Results include similarity scores", "similarity:" in text)
            if results == 0:
                print(f"    DEBUG resp text[:300]: {repr(text[:300])}")
        elif resp and "error" in resp:
            check("search_vault responded", False, f"error: {resp['error']}")
        else:
            check("search_vault responded", False, "no response (timeout?)")

        # ── 5. Reindex ───────────────────────────────────────────────────────
        print("\n[5] Reindex trigger")
        send(proc, "tools/call", {
            "name": "reindex_vault",
            "arguments": {},
        })
        resp = recv(proc, timeout=10)
        if resp and "result" in resp:
            content = resp["result"].get("content", [{}])
            text = content[0].get("text", "") if content else ""
            check("reindex_vault acknowledged", "Re-indexing started" in text)
        else:
            check("reindex_vault responded", False, str(resp))

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    # ── Summary ──────────────────────────────────────────────────────────────
    total = passed + failed
    print(f"\n{'='*40}")
    print(f"  {passed}/{total} checks passed")
    if failed == 0:
        print("  \033[92mMCP server is fully operational.\033[0m\n")
    else:
        print("  \033[91mSome checks failed — review output above.\033[0m\n")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
