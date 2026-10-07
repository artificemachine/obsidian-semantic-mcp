"""
End-to-end: the real OSM stdio server (JSON-RPC over pipes) searching through a
disposable Caasiopeia stand-in on a loopback port.

What is real: the stdio transport, MCP dispatch, the Caasiopeia HTTP client,
source scoping and the external-id resolver over a temporary vault directory.
What is stubbed, and why: PostgreSQL and the background indexer (`-m "not pg"`
suites may not require a database), so `expand_via_links` is replaced inside
the subprocess by a recorder that returns one neighbor. No real Caasiopeia,
production vault, database, keychain or MCP registration is touched.
"""
import json
import os
import queue
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
SOURCE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
API_KEY = f"synthetic-e2e-{uuid.uuid4().hex}"
RPC_TIMEOUT = 30

WRAPPER = '''
import asyncio, json, os, sys
if os.environ.get("OSM_TEST_SRC"):
    sys.path.insert(0, os.environ["OSM_TEST_SRC"])
from src import server

server.background_init = lambda vault_paths: None
SEEDS = os.environ["OSM_TEST_SEEDS"]

def fake_expand(paths, hops=1):
    with open(SEEDS, "w", encoding="utf-8") as fh:
        json.dump(list(paths), fh)
    vault = os.environ["OBSIDIAN_VAULT"]
    return [(os.path.join(vault, "notes", "neighbor.md"), "neighbor body", paths[0])]

server.expand_via_links = fake_expand

async def fake_local_search(*args):
    return [server.TextContent(type="text", text="local fallback result")]

server._search_vault_local = fake_local_search
asyncio.run(server.main())
'''


def _build_installed_python(tmp_path):
    repo = Path(__file__).resolve().parent.parent
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    build = subprocess.run(
        ["uv", "build", "--offline", "--wheel", "--out-dir", str(wheel_dir)],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert build.returncode == 0, build.stderr
    wheel = next(wheel_dir.glob("*.whl"))
    venv = tmp_path / "venv"
    create = subprocess.run(
        ["uv", "venv", "--offline", "--python", sys.executable,
         str(venv)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert create.returncode == 0, create.stderr
    python = venv / "bin" / "python"
    dependency_env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(venv)}
    dependency_env.pop("VIRTUAL_ENV", None)
    sync = subprocess.run(
        ["uv", "sync", "--frozen", "--offline", "--no-dev", "--no-install-project",
         "--project", str(repo)],
        cwd=tmp_path,
        env=dependency_env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert sync.returncode == 0, sync.stderr
    install = subprocess.run(
        ["uv", "pip", "install", "--offline", "--no-deps", "--python", str(python),
         str(wheel)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert install.returncode == 0, install.stderr
    return python, venv


def _start_installed_server(python, tmp_path, vault, caas_stand_in, backend):
    wrapper = tmp_path / f"run_{backend}.py"
    wrapper.write_text(WRAPPER, encoding="utf-8")
    env = _env(tmp_path, vault, caas_stand_in.url)
    env.pop("OSM_TEST_SRC")
    if backend == "local":
        for name in (
            "OSM_RETRIEVAL_BACKEND", "CAASIOPEIA_BASE_URL", "CAASIOPEIA_API_KEY",
            "CAASIOPEIA_SOURCE_MAP", "CAASIOPEIA_SOURCE_ROOTS",
        ):
            env.pop(name, None)
        env["OSM_RETRIEVAL_BACKEND"] = "local"
    proc = subprocess.Popen(
        [str(python), str(wrapper)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    rpc = Rpc(proc)
    init = rpc.call("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "installed-e2e", "version": "0"},
    })
    assert "result" in init, init
    rpc.notify("notifications/initialized")
    return proc, rpc


def _stop_server(proc):
    proc.kill()
    proc.wait(timeout=10)
    proc.stdin.close()
    proc.stdout.close()
    proc.stderr.close()


class CaasStandIn(ThreadingHTTPServer):
    """Loopback fake of POST /v1/context that records what it receives."""

    def __init__(self):
        self.requests = []
        self.status = 200
        self.passages = []
        super().__init__(("127.0.0.1", 0), _Handler)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}"


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        state = self.server
        state.requests.append(
            {"path": self.path, "body": body, "headers": dict(self.headers)}
        )
        if state.status != 200:
            payload = b"{}"
            self.send_response(state.status)
        else:
            payload = json.dumps({
                "passages": state.passages,
                "tokens_used": 5,
                "cache": "miss",
                "trace_id": body["trace_id"],
                "degraded": False,
                "degradation_reason": None,
                "model_id": "fixture",
            }).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def _passage(external_id, text, source_id=SOURCE_ID):
    return {
        "chunk_id": str(uuid.uuid4()),
        "document_id": str(uuid.uuid4()),
        "source_id": source_id,
        "external_id": external_id,
        "title": None,
        "heading_path": ["Plan"],
        "ordinal": 0,
        "score": 0.9,
        "text": text,
        "tokens": 5,
        "pruned": False,
    }


class Rpc:
    """Line-oriented JSON-RPC client with a hard per-call timeout."""

    def __init__(self, proc):
        self.proc = proc
        self.lines = queue.Queue()
        self.next_id = 0
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def call(self, method, params=None):
        self.next_id += 1
        message = {"jsonrpc": "2.0", "method": method, "id": self.next_id}
        if params is not None:
            message["params"] = params
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()
        while True:
            try:
                line = self.lines.get(timeout=RPC_TIMEOUT)
            except queue.Empty:
                raise AssertionError(f"no response to {method} within {RPC_TIMEOUT}s")
            assert line is not None, f"server exited before answering {method}"
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("id") == self.next_id:
                return data

    def notify(self, method):
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()


def _env(tmp_path, vault, caas_url, **extra):
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "OBSIDIAN_VAULT": str(vault),
        "DATABASE_URL": "postgresql://osm:unused@127.0.0.1:1/unused",
        "OSM_RETRIEVAL_BACKEND": "caasiopeia",
        "CAASIOPEIA_BASE_URL": caas_url,
        "CAASIOPEIA_API_KEY": API_KEY,
        "CAASIOPEIA_SOURCE_MAP": f"{Path(vault).name}={SOURCE_ID}",
        "OSM_TEST_SRC": str(SRC),
        "OSM_TEST_SEEDS": str(tmp_path / "seeds.json"),
    }
    env.update(extra)
    return env


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "foo.md").write_text("# foo\n", encoding="utf-8")
    (root / "notes" / "neighbor.md").write_text("# neighbor\n", encoding="utf-8")
    (tmp_path / "outside.md").write_text("# secret\n", encoding="utf-8")
    return root


@pytest.fixture
def caas_stand_in():
    stand_in = CaasStandIn()
    thread = threading.Thread(target=stand_in.serve_forever, daemon=True)
    thread.start()
    yield stand_in
    stand_in.shutdown()
    stand_in.server_close()


@pytest.fixture
def osm(tmp_path, vault, caas_stand_in):
    wrapper = tmp_path / "run_server.py"
    wrapper.write_text(WRAPPER, encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(wrapper)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_env(tmp_path, vault, caas_stand_in.url),
    )
    rpc = Rpc(proc)
    init = rpc.call("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "e2e", "version": "0"},
    })
    assert "result" in init, init
    rpc.notify("notifications/initialized")
    yield rpc
    proc.kill()
    proc.wait(timeout=10)
    proc.stdin.close()
    proc.stdout.close()
    proc.stderr.close()


def _search(rpc, **arguments):
    arguments.setdefault("query", "what is the plan")
    response = rpc.call("tools/call", {"name": "search_vault", "arguments": arguments})
    assert "result" in response, response
    return "\n".join(part["text"] for part in response["result"]["content"])


def test_stdio_search_vault_uses_disposable_caas_and_expands_verified_links(
    osm, caas_stand_in, tmp_path, vault
):
    tools = osm.call("tools/list")["result"]["tools"]
    assert "search_vault" in [tool["name"] for tool in tools]
    caas_stand_in.passages = [
        _passage("notes/foo.md", "the plan is to ship"),
        _passage("../outside.md", "must never seed the graph"),
    ]

    text = _search(osm, graph_expand=True, limit=5)

    assert text.startswith("_Retrieval backend: Caasiopeia._")
    request = caas_stand_in.requests[0]
    assert request["path"] == "/v1/context"
    assert request["body"]["source_ids"] == [SOURCE_ID]
    assert request["body"]["query"] == "what is the plan"
    assert request["body"]["mode"] == "hybrid"
    assert request["headers"]["Authorization"] == f"Bearer {API_KEY}"
    assert request["headers"]["x-caas-trace-id"] == request["body"]["trace_id"]
    assert "the plan is to ship" in text
    seeds = json.loads((tmp_path / "seeds.json").read_text(encoding="utf-8"))
    assert seeds == [str(vault / "notes" / "foo.md")], "only the verified passage seeds"
    assert "**notes/neighbor.md** _(linked via notes/foo.md)_" in text
    assert "neighbor body" in text
    assert API_KEY not in text


def test_stdio_caas_outage_falls_back_to_local_ranking(
    osm, caas_stand_in, tmp_path
):
    caas_stand_in.status = 503

    text = _search(osm)

    assert text.startswith("_Retrieval backend: local (fallback from Caasiopeia)._")
    assert text.endswith("local fallback result")
    assert not (tmp_path / "seeds.json").exists()
    assert API_KEY not in text
    assert "Traceback" not in text


def test_startup_refuses_caasiopeia_without_required_configuration(tmp_path, vault):
    env = _env(tmp_path, vault, "http://127.0.0.1:1")
    del env["CAASIOPEIA_SOURCE_MAP"]

    done = subprocess.run(
        [sys.executable, str(SRC / "server.py")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        input="",
        check=False,
    )

    assert done.returncode == 1
    assert "CAASIOPEIA_SOURCE_MAP" in done.stderr
    assert API_KEY not in done.stderr + done.stdout


def test_installed_artifact_search_workflows_for_both_backends(tmp_path, vault, caas_stand_in):
    python, venv = _build_installed_python(tmp_path)
    module = subprocess.run(
        [str(python), "-c",
         "import json, osm_init, src.server, src.launcher; "
         "print(json.dumps({'osm_init': osm_init.__file__, 'server': src.server.__file__, "
         "'launcher': src.launcher.__file__}))"],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path / "home"),
             "DATABASE_URL": "postgresql://osm:unused@127.0.0.1:1/unused",
             "OBSIDIAN_VAULT": str(vault)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert module.returncode == 0, module.stderr
    modules = json.loads(module.stdout.strip())
    for installed_module in modules.values():
        assert Path(installed_module).is_relative_to(venv)
        assert str(SRC.parent) not in installed_module

    local_proc, local_rpc = _start_installed_server(python, tmp_path, vault, caas_stand_in, "local")
    try:
        local_text = _search(local_rpc)
        assert local_text.startswith("_Retrieval backend: local._")
        assert not caas_stand_in.requests
    finally:
        _stop_server(local_proc)

    caas_stand_in.passages = [_passage("notes/foo.md", "installed wheel result")]
    caas_proc, caas_rpc = _start_installed_server(python, tmp_path, vault, caas_stand_in, "caasiopeia")
    try:
        caas_text = _search(caas_rpc)
        assert caas_text.startswith("_Retrieval backend: Caasiopeia._")
        assert "installed wheel result" in caas_text
        assert caas_stand_in.requests[-1]["headers"]["Authorization"] == f"Bearer {API_KEY}"
        assert API_KEY not in caas_text
    finally:
        _stop_server(caas_proc)
