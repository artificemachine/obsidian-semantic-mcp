"""Native retrieval settings stay private to the installed OSM runtime."""
import json
import os
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))


SOURCE_ID = "12345678-1234-5678-1234-567812345678"
API_KEY = f"synthetic-runtime-{uuid.uuid4()}"
_ALLOWED_LOOPBACK_PORTS = set()


@pytest.fixture(autouse=True)
def block_unplanned_http(monkeypatch):
    request = requests.Session.request
    _ALLOWED_LOOPBACK_PORTS.clear()

    def unexpected_request(self, method, url, *args, **kwargs):
        parts = urlsplit(url)
        if parts.hostname == "127.0.0.1" and parts.port in _ALLOWED_LOOPBACK_PORTS:
            return request(self, method, url, *args, **kwargs)
        pytest.fail(f"unexpected HTTP request to {parts.hostname or '<invalid-host>'}")

    monkeypatch.setattr(requests.Session, "request", unexpected_request)


def _retrieval_settings(vault="main"):
    return {
        "OSM_RETRIEVAL_BACKEND": "caasiopeia",
        "CAASIOPEIA_BASE_URL": "http://caas.invalid:8080",
        "CAASIOPEIA_SOURCE_MAP": f"{vault}={SOURCE_ID}",
        "CAASIOPEIA_SOURCE_ROOTS": f"{vault}=notes",
        "CAASIOPEIA_TOKEN_BUDGET": "900",
    }


def test_native_retrieval_settings_round_trip(monkeypatch, tmp_path):
    import osm_init
    from src import launcher

    vault = tmp_path / "main"
    vault.mkdir()
    settings = _retrieval_settings()
    osm_init._write_native_runtime([str(vault)], "postgresql://localhost/synthetic", settings)
    monkeypatch.setattr(os, "environ", {"CAASIOPEIA_API_KEY": API_KEY})

    with patch.object(launcher, "_run_server") as server:
        launcher.main()

    server.assert_called_once()
    assert os.environ["OSM_RETRIEVAL_BACKEND"] == "caasiopeia"
    assert os.environ["CAASIOPEIA_BASE_URL"] == settings["CAASIOPEIA_BASE_URL"]
    assert os.environ["CAASIOPEIA_SOURCE_MAP"] == settings["CAASIOPEIA_SOURCE_MAP"]
    assert os.environ["CAASIOPEIA_SOURCE_ROOTS"] == settings["CAASIOPEIA_SOURCE_ROOTS"]
    assert os.environ["CAASIOPEIA_TOKEN_BUDGET"] == "900"


def test_native_credentials_never_persist(monkeypatch, tmp_path):
    import osm_init
    from src import launcher

    runtime_dir = tmp_path / "runtime"
    monkeypatch.setattr(osm_init, "OSM_CONFIG_DIR", runtime_dir)
    monkeypatch.setattr(launcher, "OSM_CONFIG_DIR", runtime_dir)
    launcher.write_native_runtime(
        ["/synthetic/main"],
        "postgresql://localhost/synthetic",
        retrieval_settings=_retrieval_settings(),
        config_dir=runtime_dir,
    )
    stored = (runtime_dir / "native_runtime.json").read_text()
    assert API_KEY not in stored
    assert "CAASIOPEIA_API_KEY" not in stored
    assert osm_init._native_entry(["/synthetic/main"], "postgresql://localhost/synthetic")["env"] == {}

    with pytest.raises(ValueError, match="retrieval"):
        launcher.write_native_runtime(
            ["/synthetic/main"],
            "postgresql://localhost/synthetic",
            retrieval_settings={**_retrieval_settings(), "CAASIOPEIA_API_KEY": API_KEY},
            config_dir=runtime_dir,
        )
    assert (runtime_dir / "native_runtime.json").read_text() == stored


def test_legacy_native_runtime_remains_valid(monkeypatch, tmp_path):
    from src import launcher

    runtime_dir = tmp_path / "legacy"
    runtime_dir.mkdir()
    path = runtime_dir / "native_runtime.json"
    path.write_text(json.dumps({
        "version": 1,
        "env": {
            "OSM_DOCKER": "0",
            "DATABASE_URL": "postgresql://localhost/legacy",
            "OBSIDIAN_VAULT": "/synthetic/legacy",
        },
    }))
    path.chmod(0o600)
    monkeypatch.setattr(launcher, "OSM_CONFIG_DIR", runtime_dir)
    monkeypatch.setattr(os, "environ", {})

    assert launcher._load_native_runtime()
    from src.config import resolve_retrieval_backend
    assert resolve_retrieval_backend(os.environ) == "local"
    assert os.environ["OBSIDIAN_VAULT"] == "/synthetic/legacy"


def test_native_explicit_backend_override(monkeypatch, tmp_path):
    from src import launcher

    runtime_dir = tmp_path / "override"
    launcher.write_native_runtime(
        ["/synthetic/main"],
        "postgresql://localhost/synthetic",
        retrieval_settings=_retrieval_settings(),
        config_dir=runtime_dir,
    )
    monkeypatch.setattr(launcher, "OSM_CONFIG_DIR", runtime_dir)
    monkeypatch.setattr(os, "environ", {"OSM_RETRIEVAL_BACKEND": "local"})

    assert launcher._load_native_runtime()
    assert os.environ["OSM_RETRIEVAL_BACKEND"] == "local"
    assert "CAASIOPEIA_BASE_URL" not in os.environ
    assert "CAASIOPEIA_SOURCE_MAP" not in os.environ


def test_native_same_backend_keeps_stored_settings_under_environment_precedence(monkeypatch, tmp_path):
    from src import launcher

    runtime_dir = tmp_path / "same-backend"
    settings = _retrieval_settings()
    launcher.write_native_runtime(
        ["/synthetic/main"],
        "postgresql://localhost/synthetic",
        retrieval_settings=settings,
        config_dir=runtime_dir,
    )
    monkeypatch.setattr(launcher, "OSM_CONFIG_DIR", runtime_dir)
    monkeypatch.setattr(os, "environ", {
        "OSM_RETRIEVAL_BACKEND": "caasiopeia",
        "CAASIOPEIA_BASE_URL": "http://explicit.invalid:9090",
    })

    assert launcher._load_native_runtime()
    assert os.environ["OSM_RETRIEVAL_BACKEND"] == "caasiopeia"
    assert os.environ["CAASIOPEIA_BASE_URL"] == "http://explicit.invalid:9090"
    assert os.environ["CAASIOPEIA_SOURCE_MAP"] == settings["CAASIOPEIA_SOURCE_MAP"]


def test_invalid_saved_url_or_source_map_preserves_existing_runtime(tmp_path):
    from src import launcher

    runtime_dir = tmp_path / "safe-write"
    launcher.write_native_runtime(
        ["/synthetic/main"],
        "postgresql://localhost/synthetic",
        retrieval_settings=_retrieval_settings(),
        config_dir=runtime_dir,
    )
    path = runtime_dir / "native_runtime.json"
    previous = path.read_bytes()
    for settings in (
        {**_retrieval_settings(), "CAASIOPEIA_BASE_URL": "https://user:secret@caas.invalid"},
        {**_retrieval_settings(), "CAASIOPEIA_BASE_URL": "http://caas.invalid\nCAASIOPEIA_API_KEY=secret"},
        {**_retrieval_settings(), "CAASIOPEIA_SOURCE_MAP": "main=not-a-uuid"},
    ):
        with pytest.raises(ValueError, match="retrieval"):
            launcher.write_native_runtime(
                ["/synthetic/main"],
                "postgresql://localhost/synthetic",
                retrieval_settings=settings,
                config_dir=runtime_dir,
            )
        assert path.read_bytes() == previous


def test_native_caasiopeia_requires_runtime_credential_before_launch(monkeypatch, tmp_path, capsys):
    from src import launcher

    vault = tmp_path / "main"
    vault.mkdir()
    runtime_dir = tmp_path / "missing-key"
    launcher.write_native_runtime(
        [str(vault)],
        "postgresql://localhost/synthetic",
        retrieval_settings=_retrieval_settings(),
        config_dir=runtime_dir,
    )
    monkeypatch.setattr(launcher, "OSM_CONFIG_DIR", runtime_dir)
    monkeypatch.setattr(os, "environ", {})
    with patch.object(launcher, "_run_server") as server, pytest.raises(SystemExit):
        launcher.main()
    server.assert_not_called()
    output = capsys.readouterr().err
    assert "CAASIOPEIA_API_KEY" in output
    assert API_KEY not in output


def test_init_backend_prompt_and_flag(monkeypatch):
    import osm_init

    monkeypatch.setattr(osm_init, "_PARAMS", {"retrieval_backend": "caasiopeia"})
    assert osm_init._select_retrieval_backend(interactive=False, mode="2") == "caasiopeia"
    monkeypatch.setattr(osm_init, "_PARAMS", {})
    monkeypatch.setenv("OSM_RETRIEVAL_BACKEND", "local")
    assert osm_init._select_retrieval_backend(interactive=False, mode="2") == "local"
    monkeypatch.delenv("OSM_RETRIEVAL_BACKEND")
    monkeypatch.setattr(osm_init, "prompt", lambda *a, **kw: "local")
    assert osm_init._select_retrieval_backend(interactive=True, mode="2") == "local"


def test_caas_setup_preflight_before_mutations(monkeypatch, tmp_path):
    import osm_init
    from src import caasiopeia_client

    vault = tmp_path / "main"
    vault.mkdir()
    env = {
        "OSM_RETRIEVAL_BACKEND": "caasiopeia",
        "CAASIOPEIA_BASE_URL": "http://caas.invalid",
        "CAASIOPEIA_API_KEY": API_KEY,
        "CAASIOPEIA_SOURCE_MAP": f"vault={SOURCE_ID}",
    }
    monkeypatch.setattr(osm_init, "_PARAMS", {})
    monkeypatch.setattr(osm_init, "DRY_RUN", False)
    monkeypatch.setattr(osm_init, "PROJECT_ROOT", tmp_path / "deploy")
    monkeypatch.setattr(osm_init, "_prompt_single_vault", lambda: str(vault))
    monkeypatch.setattr(osm_init, "prompt_vault", lambda: [str(vault)])
    monkeypatch.setattr(osm_init.os, "environ", env)
    mutations = []
    monkeypatch.setattr(osm_init, "_ensure_deploy_dir", lambda: mutations.append("deploy"))
    monkeypatch.setattr(osm_init, "check_docker", lambda: True)
    monkeypatch.setattr(osm_init, "check_compose", lambda: True)

    class FailedClient:
        def __init__(self, *args, **kwargs):
            pass

        def search(self, *args, **kwargs):
            raise caasiopeia_client.CaasUnauthorized("synthetic unauthorized")

    monkeypatch.setattr(caasiopeia_client, "CaasClient", FailedClient)
    with pytest.raises(SystemExit):
        osm_init._prepare_retrieval_for_setup(interactive=False, mode="3")
    assert mutations == []


def test_caas_missing_runtime_key_is_actionable_and_redacted(monkeypatch, tmp_path, capsys):
    import osm_init

    vault = tmp_path / "main"
    vault.mkdir()
    monkeypatch.setattr(osm_init, "_PARAMS", {"retrieval_backend": "caasiopeia", "vault": str(vault)})
    monkeypatch.setattr(osm_init, "DRY_RUN", False)
    monkeypatch.setattr(osm_init, "PROJECT_ROOT", tmp_path / "deploy")
    monkeypatch.setattr(osm_init, "_saved_retrieval_settings", lambda mode: {})
    monkeypatch.setattr(osm_init.os, "environ", {
        "CAASIOPEIA_BASE_URL": "http://caas.invalid",
        "CAASIOPEIA_SOURCE_MAP": f"vault={SOURCE_ID}",
    })
    with pytest.raises(SystemExit):
        osm_init._prepare_retrieval_for_setup(interactive=False, mode="2")
    output = capsys.readouterr().out
    assert "CAASIOPEIA_API_KEY" in output
    assert API_KEY not in output


def test_caas_setup_preflight_uses_scoped_public_query_loopback_only(monkeypatch, tmp_path):
    import osm_init

    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, self.headers.get("Authorization"), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            body = json.dumps({
                "passages": [], "tokens_used": 0, "cache": "miss",
                "trace_id": SOURCE_ID, "degraded": False,
                "degradation_reason": None, "model_id": "synthetic",
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    _ALLOWED_LOOPBACK_PORTS.add(server.server_port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        vault = tmp_path / "vault"
        vault.mkdir()
        monkeypatch.setattr(osm_init, "_PARAMS", {"retrieval_backend": "caasiopeia", "vault": str(vault)})
        monkeypatch.setattr(osm_init, "DRY_RUN", False)
        monkeypatch.setattr(osm_init, "PROJECT_ROOT", tmp_path / "deploy")
        monkeypatch.setattr(osm_init, "_saved_retrieval_settings", lambda mode: {})
        monkeypatch.setattr(osm_init.os, "environ", {
            "CAASIOPEIA_BASE_URL": f"http://127.0.0.1:{server.server_port}",
            "CAASIOPEIA_API_KEY": API_KEY,
            "CAASIOPEIA_SOURCE_MAP": f"vault={SOURCE_ID}",
        })
        osm_init._prepare_retrieval_for_setup(interactive=False, mode="2")
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
        _ALLOWED_LOOPBACK_PORTS.discard(server.server_port)

    path, authorization, payload = received[0]
    assert path == "/v1/context"
    assert authorization == f"Bearer {API_KEY}"
    assert payload["query"] == "obsidian-semantic-mcp setup connectivity check"
    assert payload["source_ids"] == [SOURCE_ID]
    assert "passages" not in payload


@pytest.mark.parametrize("failure_name", ["CaasUnauthorized", "CaasTimeout", "CaasUnavailable"])
def test_caas_typed_preflight_failures_do_not_mutate(monkeypatch, tmp_path, failure_name):
    import osm_init
    from src import caasiopeia_client

    vault = tmp_path / "main"
    vault.mkdir()
    monkeypatch.setattr(osm_init, "_PARAMS", {"retrieval_backend": "caasiopeia", "vault": str(vault)})
    monkeypatch.setattr(osm_init, "DRY_RUN", False)
    monkeypatch.setattr(osm_init, "PROJECT_ROOT", tmp_path / "deploy")
    monkeypatch.setattr(osm_init.os, "environ", {
        "CAASIOPEIA_BASE_URL": "http://caas.invalid",
        "CAASIOPEIA_API_KEY": API_KEY,
        "CAASIOPEIA_SOURCE_MAP": f"vault={SOURCE_ID}",
    })
    mutations = []
    monkeypatch.setattr(osm_init, "_ensure_deploy_dir", lambda: mutations.append("deploy"))
    failure_type = getattr(caasiopeia_client, failure_name)

    class FailedClient:
        def __init__(self, *args, **kwargs):
            assert kwargs == {"connect_timeout": 10, "read_timeout": 10}

        def search(self, *args, **kwargs):
            raise failure_type("synthetic response includes no useful detail")

    monkeypatch.setattr(caasiopeia_client, "CaasClient", FailedClient)
    with pytest.raises(SystemExit):
        osm_init._prepare_retrieval_for_setup(interactive=False, mode="2")
    assert mutations == []


def test_caas_missing_runtime_key_is_actionable_and_redacted(monkeypatch, tmp_path, capsys):
    import osm_init

    vault = tmp_path / "main"
    vault.mkdir()
    monkeypatch.setattr(osm_init, "_PARAMS", {"retrieval_backend": "caasiopeia", "vault": str(vault)})
    monkeypatch.setattr(osm_init, "DRY_RUN", False)
    monkeypatch.setattr(osm_init, "PROJECT_ROOT", tmp_path / "deploy")
    monkeypatch.setattr(osm_init, "_saved_retrieval_settings", lambda mode: {})
    monkeypatch.setattr(osm_init.os, "environ", {
        "CAASIOPEIA_BASE_URL": "http://caas.invalid",
        "CAASIOPEIA_SOURCE_MAP": f"vault={SOURCE_ID}",
    })
    with pytest.raises(SystemExit):
        osm_init._prepare_retrieval_for_setup(interactive=False, mode="2")
    output = capsys.readouterr().out
    assert "CAASIOPEIA_API_KEY" in output
    assert API_KEY not in output


def test_local_setup_ignores_stale_caas_settings(monkeypatch, tmp_path):
    import osm_init

    monkeypatch.setattr(osm_init, "PROJECT_ROOT", tmp_path)
    tmp_path.joinpath(".env").write_text(
        f"OWNER_SETTING=keep\nCAASIOPEIA_API_KEY={API_KEY}\nCAASIOPEIA_BASE_URL=http://old\n"
    )
    env = {"CAASIOPEIA_API_KEY": API_KEY, "CAASIOPEIA_BASE_URL": "http://old"}
    monkeypatch.setattr(osm_init.os, "environ", env)
    monkeypatch.setattr(osm_init, "_PARAMS", {"retrieval_backend": "local"})
    monkeypatch.setattr(osm_init, "DRY_RUN", False)
    monkeypatch.setattr(osm_init, "_INIT_VAULTS", None, raising=False)
    monkeypatch.setattr(osm_init, "_RETRIEVAL_SETTINGS", {"OSM_RETRIEVAL_BACKEND": "local"}, raising=False)
    osm_init._prepare_retrieval_for_setup(interactive=False, mode="2")
    assert osm_init._RETRIEVAL_SETTINGS == {"OSM_RETRIEVAL_BACKEND": "local"}
    osm_init.write_env(["/vault"], "pw", "http://ollama", dashboard_token="token")
    text = tmp_path.joinpath(".env").read_text()
    assert "OWNER_SETTING=keep" in text
    assert API_KEY not in text
    assert osm_init._compose_retrieval_env({"CAASIOPEIA_API_KEY": API_KEY})["CAASIOPEIA_API_KEY"] == ""


@pytest.mark.parametrize(
    "mode,handler_name",
    [("1", "mode_native_macos"), ("2", "mode_docker_host_ollama"),
     ("3", "mode_full_docker"), ("4", "mode_docker_remote_ollama")],
)
def test_init_backend_choice_reaches_every_mode(monkeypatch, tmp_path, mode, handler_name):
    import osm_init

    vaults = [str(tmp_path / "one"), str(tmp_path / "two")]
    monkeypatch.setattr(osm_init, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(osm_init, "_PARAMS", {"retrieval_backend": "caasiopeia", "vault": str(tmp_path / "one")})
    monkeypatch.setattr(osm_init, "DRY_RUN", False)
    monkeypatch.setattr(osm_init.os, "environ", {
        "CAASIOPEIA_BASE_URL": "http://caas.invalid",
        "CAASIOPEIA_API_KEY": API_KEY,
        "CAASIOPEIA_SOURCE_MAP": f"one={SOURCE_ID},two=abcdefab-cdef-abcd-efab-cdefabcdefab",
        "CAASIOPEIA_SOURCE_ROOTS": "one=notes,two=docs",
    })
    monkeypatch.setattr(osm_init, "_INIT_VAULTS", vaults, raising=False)
    monkeypatch.setattr(osm_init, "_prompt_single_vault", lambda: pytest.fail("vaults were prompted again"))
    monkeypatch.setattr(osm_init, "platform", type("Platform", (), {"system": staticmethod(lambda: "Darwin")})())
    monkeypatch.setattr(osm_init, "check_docker", lambda: True)
    monkeypatch.setattr(osm_init, "check_compose", lambda: True)
    monkeypatch.setattr(osm_init, "cmd_exists", lambda name: True)
    monkeypatch.setattr(osm_init, "run", lambda *a, **kw: type("Result", (), {"returncode": 0, "stdout": "obsidian_brain"})())
    monkeypatch.setattr(osm_init, "_ensure_deploy_dir", lambda: None)
    monkeypatch.setattr(osm_init, "prompt_pg_password", lambda: "pw")
    monkeypatch.setattr(osm_init, "prompt_persistent_storage", lambda **kw: (None, None))
    monkeypatch.setattr(osm_init, "_write_compose_override", lambda v: None)
    monkeypatch.setattr(osm_init, "compose_up", lambda **kw: None)
    monkeypatch.setattr(osm_init, "wait_for_postgres", lambda **kw: True)
    monkeypatch.setattr(osm_init, "_ensure_ollama_model", lambda *a, **kw: None)
    monkeypatch.setattr(osm_init, "check_ollama_at", lambda *a, **kw: True)
    monkeypatch.setattr(osm_init, "check_ollama_inference_at", lambda *a, **kw: True)
    monkeypatch.setattr(osm_init, "_test_ssh_connection", lambda *a, **kw: True)
    monkeypatch.setattr(osm_init, "prompt_ssh_credentials", lambda: ("user", "host", 22, None))
    monkeypatch.setattr(osm_init, "open_ssh_tunnel", lambda *a, **kw: True)
    runtime_capture = []
    monkeypatch.setattr(osm_init, "_write_native_runtime", lambda *a, **kw: runtime_capture.append(kw.get("retrieval_settings")))
    monkeypatch.setattr(osm_init, "register_with_clients", lambda *a, **kw: None)
    for name in ("_done_native", "_done_docker", "_done_docker_remote"):
        monkeypatch.setattr(osm_init, name, lambda *a, **kw: None)
    captured = []
    monkeypatch.setattr(osm_init, "write_env", lambda *a, **kw: captured.append(kw.get("retrieval_settings")))
    class GoodClient:
        def __init__(self, *args, **kwargs):
            pass

        def search(self, *args, **kwargs):
            assert set(kwargs["source_ids"]) == {SOURCE_ID, "abcdefab-cdef-abcd-efab-cdefabcdefab"}

    from src import caasiopeia_client
    monkeypatch.setattr(caasiopeia_client, "CaasClient", GoodClient)
    handler = getattr(osm_init, handler_name)
    osm_init._prepare_retrieval_for_setup(interactive=False, mode=mode, handler=handler)
    handler()
    settings = osm_init._RETRIEVAL_SETTINGS
    assert settings["CAASIOPEIA_SOURCE_MAP"] == f"one={SOURCE_ID},two=abcdefab-cdef-abcd-efab-cdefabcdefab"
    assert settings["CAASIOPEIA_SOURCE_ROOTS"] == "one=notes,two=docs"
    assert "CAASIOPEIA_API_KEY" not in settings
    if handler is osm_init.mode_native_macos:
        assert runtime_capture == [settings]


def test_docker_selection_written_without_key(monkeypatch, tmp_path):
    import osm_init

    monkeypatch.setattr(osm_init, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(osm_init, "_PARAMS", {})
    monkeypatch.setattr(osm_init, "_RETRIEVAL_SETTINGS", {
        "OSM_RETRIEVAL_BACKEND": "caasiopeia",
        "CAASIOPEIA_BASE_URL": "http://localhost:8123",
        "CAASIOPEIA_SOURCE_MAP": f"vault={SOURCE_ID}",
    }, raising=False)
    monkeypatch.setattr(osm_init, "DRY_RUN", False)
    osm_init.write_env("/tmp/vault", "pw", "http://ollama", dashboard_token="token")
    saved = tmp_path.joinpath(".env").read_text()
    assert "OSM_RETRIEVAL_BACKEND=caasiopeia" in saved
    assert "CAASIOPEIA_API_KEY=" in saved
    assert API_KEY not in saved
    assert tmp_path.joinpath(".env").stat().st_mode & 0o777 == 0o600


def test_setup_dry_run_redacts_credentials(monkeypatch, tmp_path, capsys):
    import osm_init

    monkeypatch.setattr(osm_init, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(osm_init, "_PARAMS", {})
    monkeypatch.setattr(osm_init, "_RETRIEVAL_SETTINGS", {
        "OSM_RETRIEVAL_BACKEND": "caasiopeia",
        "CAASIOPEIA_BASE_URL": "http://localhost:8123",
    }, raising=False)
    monkeypatch.setattr(osm_init, "DRY_RUN", True)
    monkeypatch.setattr(osm_init.os, "environ", {"CAASIOPEIA_API_KEY": API_KEY})
    osm_init.write_env("/tmp/vault", "pw", "http://ollama", dashboard_token="token")
    output = capsys.readouterr().out
    assert API_KEY not in output
    assert not tmp_path.joinpath(".env").exists()


def test_native_saved_choice_and_legacy_noninteractive_default(monkeypatch, tmp_path):
    import osm_init
    from src import launcher

    runtime_dir = tmp_path / "native"
    launcher.write_native_runtime(
        ["/synthetic/main"],
        "postgresql://localhost/synthetic",
        retrieval_settings=_retrieval_settings(),
        config_dir=runtime_dir,
    )
    monkeypatch.setattr(osm_init, "OSM_CONFIG_DIR", runtime_dir)
    monkeypatch.setattr(osm_init, "platform", type("Platform", (), {"system": staticmethod(lambda: "Darwin")})())
    monkeypatch.setattr(osm_init, "_PARAMS", {})
    monkeypatch.setattr(osm_init.os, "environ", {})
    assert osm_init._select_retrieval_backend(interactive=False, mode="1") == "caasiopeia"
    assert osm_init._select_retrieval_backend(interactive=False, mode="2") == "local"


@pytest.mark.parametrize("bad", ["http://caas.invalid\nCAASIOPEIA_API_KEY=leak", "http://caas.invalid$VAR", "http://caas.invalid\x00"])
def test_caas_saved_values_reject_dotenv_hazards(monkeypatch, tmp_path, bad):
    import osm_init

    vault = tmp_path / "main"
    vault.mkdir()
    monkeypatch.setattr(osm_init, "_PARAMS", {"retrieval_backend": "caasiopeia"})
    monkeypatch.setattr(osm_init, "DRY_RUN", False)
    monkeypatch.setattr(osm_init, "PROJECT_ROOT", tmp_path / "deploy")
    monkeypatch.setattr(osm_init, "_prompt_single_vault", lambda: str(vault))
    monkeypatch.setattr(osm_init.os, "environ", {
        "CAASIOPEIA_BASE_URL": bad,
        "CAASIOPEIA_API_KEY": API_KEY,
        "CAASIOPEIA_SOURCE_MAP": f"vault={SOURCE_ID}",
    })
    with pytest.raises(SystemExit):
        osm_init._prepare_retrieval_for_setup(interactive=False, mode="2")


def test_native_rejects_url_path_before_preflight_or_mutation(monkeypatch, tmp_path):
    import osm_init

    vault = tmp_path / "main"
    vault.mkdir()
    monkeypatch.setattr(osm_init.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(osm_init, "_PARAMS", {"retrieval_backend": "caasiopeia", "vault": str(vault)})
    monkeypatch.setattr(osm_init, "DRY_RUN", False)
    monkeypatch.setattr(osm_init, "OSM_CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(osm_init, "PROJECT_ROOT", tmp_path / "deploy")
    monkeypatch.setattr(osm_init, "_saved_retrieval_settings", lambda mode: {})
    monkeypatch.setattr(osm_init.os, "environ", {
        "CAASIOPEIA_BASE_URL": "http://caas.invalid/v1",
        "CAASIOPEIA_API_KEY": API_KEY,
        "CAASIOPEIA_SOURCE_MAP": f"vault={SOURCE_ID}",
    })
    with pytest.raises(SystemExit):
        osm_init._prepare_retrieval_for_setup(interactive=False, mode="1", handler=osm_init.mode_native_macos)
    assert not (tmp_path / "config").exists()
    assert not (tmp_path / "deploy").exists()


def test_docker_rejects_url_path_before_preflight_or_mutation(monkeypatch, tmp_path):
    import osm_init

    vault = tmp_path / "main"
    vault.mkdir()
    monkeypatch.setattr(osm_init, "_PARAMS", {"retrieval_backend": "caasiopeia", "vault": str(vault)})
    monkeypatch.setattr(osm_init, "DRY_RUN", False)
    monkeypatch.setattr(osm_init, "PROJECT_ROOT", tmp_path / "deploy")
    monkeypatch.setattr(osm_init, "_saved_retrieval_settings", lambda mode: {})
    monkeypatch.setattr(osm_init.os, "environ", {
        "CAASIOPEIA_BASE_URL": "http://caas.invalid/v1",
        "CAASIOPEIA_API_KEY": API_KEY,
        "CAASIOPEIA_SOURCE_MAP": f"vault={SOURCE_ID}",
    })
    with pytest.raises(SystemExit):
        osm_init._prepare_retrieval_for_setup(
            interactive=False, mode="2", handler=osm_init.mode_docker_host_ollama
        )
    assert not (tmp_path / "deploy").exists()


def test_docker_dotenv_comment_in_source_root_round_trips(monkeypatch, tmp_path):
    import osm_init

    vault = tmp_path / "main"
    vault.mkdir()
    monkeypatch.setattr(osm_init, "_PARAMS", {"retrieval_backend": "caasiopeia", "vault": str(vault)})
    monkeypatch.setattr(osm_init, "DRY_RUN", False)
    monkeypatch.setattr(osm_init, "PROJECT_ROOT", tmp_path / "deploy")
    monkeypatch.setattr(osm_init, "_saved_retrieval_settings", lambda mode: {})
    monkeypatch.setattr(osm_init.os, "environ", {
        "CAASIOPEIA_BASE_URL": "http://caas.invalid",
        "CAASIOPEIA_API_KEY": API_KEY,
        "CAASIOPEIA_SOURCE_MAP": f"vault={SOURCE_ID}",
        "CAASIOPEIA_SOURCE_ROOTS": "vault=notes # archive",
    })
    class GoodClient:
        def __init__(self, *args, **kwargs):
            pass

        def search(self, *args, **kwargs):
            return None

    from src import caasiopeia_client
    monkeypatch.setattr(caasiopeia_client, "CaasClient", GoodClient)
    osm_init._prepare_retrieval_for_setup(interactive=False, mode="2")
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    monkeypatch.setattr(osm_init, "PROJECT_ROOT", deploy)
    osm_init.write_env([str(vault)], "pw", "http://ollama", dashboard_token="token")
    assert osm_init._read_env()["CAASIOPEIA_SOURCE_ROOTS"] == "vault=notes # archive"
    from dotenv import dotenv_values
    assert dotenv_values(deploy / ".env")["CAASIOPEIA_SOURCE_ROOTS"] == "vault=notes # archive"


def test_docker_source_root_with_spaces_round_trips(monkeypatch, tmp_path):
    import osm_init

    monkeypatch.setattr(osm_init, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(osm_init, "_PARAMS", {})
    monkeypatch.setattr(osm_init, "_RETRIEVAL_SETTINGS", {
        "OSM_RETRIEVAL_BACKEND": "caasiopeia",
        "CAASIOPEIA_BASE_URL": "http://caas.invalid",
        "CAASIOPEIA_SOURCE_MAP": f"vault={SOURCE_ID}",
        "CAASIOPEIA_SOURCE_ROOTS": "vault=some folder/notes",
    }, raising=False)
    monkeypatch.setattr(osm_init, "DRY_RUN", False)
    osm_init.write_env("/tmp/vault", "pw", "http://ollama", dashboard_token="token")
    assert osm_init._read_env()["CAASIOPEIA_SOURCE_ROOTS"] == "vault=some folder/notes"


def test_docker_repeat_init_uses_host_url_for_probe_and_container_url_for_compose(monkeypatch, tmp_path):
    import osm_init
    from src import caasiopeia_client

    vault = tmp_path / "vault"
    vault.mkdir()
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    monkeypatch.setattr(osm_init, "PROJECT_ROOT", deploy)
    monkeypatch.setattr(osm_init, "_PARAMS", {"retrieval_backend": "caasiopeia", "vault": str(vault)})
    monkeypatch.setattr(osm_init, "DRY_RUN", False)
    monkeypatch.setattr(osm_init.os, "environ", {
        "CAASIOPEIA_BASE_URL": "http://localhost:8123",
        "CAASIOPEIA_API_KEY": API_KEY,
        "CAASIOPEIA_SOURCE_MAP": f"vault={SOURCE_ID}",
    })
    probes = []

    class RecordingClient:
        def __init__(self, base_url, *args, **kwargs):
            probes.append(base_url)

        def search(self, *args, **kwargs):
            return None

    monkeypatch.setattr(caasiopeia_client, "CaasClient", RecordingClient)
    osm_init._prepare_retrieval_for_setup(interactive=False, mode="2")
    osm_init.write_env([str(vault)], "pw", "http://ollama", dashboard_token="token")
    expected_container_url = osm_init._docker_caas_url("http://localhost:8123")
    assert osm_init._read_env()["CAASIOPEIA_BASE_URL"] == expected_container_url
    assert osm_init._read_env()["OSM_CAASIOPEIA_HOST_URL"] == "http://localhost:8123"

    monkeypatch.setattr(osm_init.os, "environ", {"CAASIOPEIA_API_KEY": API_KEY})
    osm_init._prepare_retrieval_for_setup(interactive=False, mode="2")
    compose_env = osm_init._compose_retrieval_env({})
    assert probes == ["http://localhost:8123", "http://localhost:8123"]
    assert osm_init._RETRIEVAL_SETTINGS["CAASIOPEIA_BASE_URL"] == expected_container_url
    assert compose_env["CAASIOPEIA_BASE_URL"] == expected_container_url


@pytest.mark.parametrize("system,expected", [("Darwin", "host.docker.internal"), ("Linux", "172.17.0.1"), ("Windows", "host.docker.internal")])
def test_docker_caas_url_translates_loopback_only(monkeypatch, system, expected):
    import osm_init

    monkeypatch.setattr(osm_init.platform, "system", lambda: system)
    assert osm_init._docker_caas_url("http://localhost:8123/v1") == f"http://{expected}:8123/v1"
    assert osm_init._docker_caas_url("http://192.0.2.4:8123/v1") == "http://192.0.2.4:8123/v1"
    assert osm_init._docker_caas_url("http://[::1]:8123/v1") == f"http://{expected}:8123/v1"


def test_remote_vault_preflight_failure_happens_before_mount(monkeypatch, tmp_path):
    import osm_init
    from src import caasiopeia_client

    mount = tmp_path / "obsidian-remote-vault"
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(osm_init.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(osm_init, "_PARAMS", {
        "retrieval_backend": "caasiopeia",
        "vault_remote": "/notes/vault",
    })
    monkeypatch.setattr(osm_init, "DRY_RUN", False)
    monkeypatch.setattr(osm_init, "PROJECT_ROOT", tmp_path / "deploy")
    monkeypatch.setattr(osm_init, "OSM_CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(osm_init.os, "environ", {
        "CAASIOPEIA_BASE_URL": "http://caas.invalid",
        "CAASIOPEIA_API_KEY": API_KEY,
        "CAASIOPEIA_SOURCE_MAP": f"vault={SOURCE_ID}",
    })
    monkeypatch.setattr(osm_init, "prompt", lambda *a, **kw: str(mount))
    monkeypatch.setattr(osm_init, "_saved_retrieval_settings", lambda mode: {})

    class FailedClient:
        def __init__(self, *args, **kwargs):
            pass

        def search(self, *args, **kwargs):
            raise caasiopeia_client.CaasUnavailable("offline")

    monkeypatch.setattr(caasiopeia_client, "CaasClient", FailedClient)
    with pytest.raises(SystemExit):
        osm_init._prepare_retrieval_for_setup(
            interactive=False, mode="4", handler=osm_init.mode_docker_remote_ollama
        )
    assert not mount.exists()
    assert osm_init._REMOTE_VAULT_SELECTION == ("/notes/vault", str(mount))


def _saved_docker_caas_env(path):
    path.write_text(
        "OBSIDIAN_VAULT=/synthetic/Research\n"
        "POSTGRES_PASSWORD=synthetic-db-password\n"
        "OSM_RETRIEVAL_BACKEND=caasiopeia\n"
        "CAASIOPEIA_BASE_URL=http://host.docker.internal:8123\n"
        "OSM_CAASIOPEIA_HOST_URL=http://localhost:8123\n"
        f"CAASIOPEIA_SOURCE_MAP=vault={SOURCE_ID}\n"
        "CAASIOPEIA_SOURCE_ROOTS=vault=notes\n"
        "CAASIOPEIA_TOKEN_BUDGET=900\n"
        "OWNER_SETTING=keep\n"
    )


def test_rebuild_preserves_saved_backend(monkeypatch, tmp_path):
    import osm_init

    deploy = tmp_path / "deploy"
    deploy.mkdir()
    _saved_docker_caas_env(deploy / ".env")
    monkeypatch.setattr(osm_init, "PROJECT_ROOT", deploy)
    monkeypatch.setattr(osm_init, "_RETRIEVAL_SETTINGS", None)
    monkeypatch.setattr(osm_init.os, "environ", {"CAASIOPEIA_API_KEY": API_KEY})
    compose_calls = []
    monkeypatch.setattr(osm_init, "compose", lambda args, **kwargs: compose_calls.append((args, kwargs)))
    monkeypatch.setattr(osm_init, "info", lambda *args: None)
    monkeypatch.setattr(osm_init, "warn", lambda *args: None)

    osm_init._build_or_pull_custom_services()

    args, kwargs = compose_calls[-1]
    assert args == ["up", "-d", "--build", "mcp-server", "dashboard"]
    env = kwargs["env"]
    assert env["OSM_RETRIEVAL_BACKEND"] == "caasiopeia"
    assert env["CAASIOPEIA_BASE_URL"] == "http://host.docker.internal:8123"
    assert env["CAASIOPEIA_SOURCE_MAP"] == f"vault={SOURCE_ID}"
    assert env["CAASIOPEIA_SOURCE_ROOTS"] == "vault=notes"
    assert env["CAASIOPEIA_API_KEY"] == API_KEY
    assert "API_KEY" not in (deploy / ".env").read_text()
    assert (deploy / ".env").read_text().find("OSM_CAASIOPEIA_HOST_URL=http://localhost:8123") >= 0


@pytest.mark.parametrize("command", ["rebuild", "update"])
def test_rebuild_missing_caas_key_stops_before_changes(monkeypatch, tmp_path, command):
    import osm_init

    deploy = tmp_path / "deploy"
    deploy.mkdir()
    _saved_docker_caas_env(deploy / ".env")
    original = (deploy / ".env").read_bytes()
    monkeypatch.setattr(osm_init, "PROJECT_ROOT", deploy)
    monkeypatch.setattr(osm_init, "_RETRIEVAL_SETTINGS", None)
    monkeypatch.setattr(osm_init.os, "environ", {})
    monkeypatch.setattr(osm_init, "_fetch_latest_release_tag", lambda: None)
    changes = []
    monkeypatch.setattr(osm_init, "compose", lambda *args, **kwargs: changes.append("compose"))
    monkeypatch.setattr(osm_init, "_update_env_var", lambda *args, **kwargs: changes.append("version"))
    monkeypatch.setattr(osm_init, "run", lambda *args, **kwargs: changes.append("build"))

    with pytest.raises(SystemExit):
        getattr(osm_init, f"cmd_{command}")()

    assert changes == []
    assert (deploy / ".env").read_bytes() == original


def test_rebuild_local_scrubs_stale_caas_compose_environment(monkeypatch, tmp_path):
    import osm_init

    deploy = tmp_path / "deploy"
    deploy.mkdir()
    _saved_docker_caas_env(deploy / ".env")
    monkeypatch.setattr(osm_init, "PROJECT_ROOT", deploy)
    monkeypatch.setattr(osm_init, "_RETRIEVAL_SETTINGS", None)
    monkeypatch.setattr(osm_init.os, "environ", {"CAASIOPEIA_API_KEY": API_KEY})
    monkeypatch.setattr(osm_init, "_update_env_var", lambda *args, **kwargs: None)
    monkeypatch.setattr(osm_init, "_compose_image_name", lambda service: f"test/{service}")
    monkeypatch.setattr(osm_init, "run", lambda *args, **kwargs: None)
    compose_calls = []
    monkeypatch.setattr(osm_init, "compose", lambda args, **kwargs: compose_calls.append(kwargs))
    monkeypatch.setattr(osm_init, "info", lambda *args: None)
    monkeypatch.setattr(osm_init, "warn", lambda *args: None)
    monkeypatch.setattr(osm_init, "PROJECT_ROOT", deploy)
    (deploy / "Dockerfile").touch()
    (deploy / "Dockerfile.dashboard").touch()
    saved = osm_init._read_env()
    saved["OSM_RETRIEVAL_BACKEND"] = "local"
    osm_init._update_env_var("OSM_RETRIEVAL_BACKEND", "local")
    monkeypatch.setattr(osm_init, "_read_env", lambda: saved)

    osm_init._build_or_pull_custom_services()

    env = compose_calls[-1]["env"]
    assert env["OSM_RETRIEVAL_BACKEND"] == "local"
    assert env["CAASIOPEIA_API_KEY"] == ""
    assert env["CAASIOPEIA_BASE_URL"] == ""
    assert env["CAASIOPEIA_SOURCE_MAP"] == ""


@pytest.mark.parametrize("bad_container_url", [
    "http://user:password@host:8123",
    "http://host:8123/v1",
    "http://host:8123?token=secret",
    "http://host:bad-port",
])
def test_rebuild_rejects_unsafe_saved_container_url_before_compose(
    monkeypatch, tmp_path, bad_container_url
):
    import osm_init

    deploy = tmp_path / "deploy"
    deploy.mkdir()
    _saved_docker_caas_env(deploy / ".env")
    env_path = deploy / ".env"
    env_path.write_text(env_path.read_text().replace(
        "http://host.docker.internal:8123", bad_container_url
    ))
    original = env_path.read_bytes()
    monkeypatch.setattr(osm_init, "PROJECT_ROOT", deploy)
    monkeypatch.setattr(osm_init, "_RETRIEVAL_SETTINGS", None)
    monkeypatch.setattr(osm_init.os, "environ", {"CAASIOPEIA_API_KEY": API_KEY})
    calls = []
    monkeypatch.setattr(osm_init, "compose", lambda *args, **kwargs: calls.append("compose"))
    with pytest.raises(SystemExit):
        osm_init._build_or_pull_custom_services()
    assert calls == []
    assert env_path.read_bytes() == original


def test_remove_clears_owned_retrieval_settings(monkeypatch, tmp_path):
    import osm_init
    from src import launcher

    deploy = tmp_path / "deploy"
    deploy.mkdir()
    env_path = deploy / ".env"
    env_path.write_text(
        "OBSIDIAN_VAULT=/synthetic/vault\n"
        "OSM_RETRIEVAL_BACKEND=caasiopeia\n"
        f"CAASIOPEIA_SOURCE_MAP=vault={SOURCE_ID}\n"
        "OWNER_SETTING=keep\n"
    )
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    unrelated = config_dir / "unrelated.json"
    unrelated.write_text('{"keep": true}\n')
    monkeypatch.setattr(osm_init, "PROJECT_ROOT", deploy)
    monkeypatch.setattr(osm_init, "OSM_CONFIG_DIR", config_dir)
    monkeypatch.setattr(launcher, "OSM_CONFIG_DIR", config_dir)
    monkeypatch.setattr(osm_init, "_PARAMS", {"yes": "y"})
    monkeypatch.setattr(osm_init, "DRY_RUN", False)
    monkeypatch.setattr(osm_init, "run", lambda *args, **kwargs: type("Result", (), {"stdout": "", "returncode": 0})())
    monkeypatch.setattr(osm_init, "_remove_named_volumes_from_override", lambda: None)
    monkeypatch.setattr(osm_init, "_claude_cfg_path", lambda: None)
    monkeypatch.setattr(osm_init, "remove_opencode_config", lambda: None)
    monkeypatch.setattr(osm_init, "remove_codex_config", lambda: None)
    monkeypatch.setattr(osm_init, "cmd_exists", lambda name: False)
    monkeypatch.setattr(osm_init, "_osm_launcher_path", lambda: tmp_path / "missing-osm")

    osm_init.cmd_remove()

    assert env_path.read_text() == "OWNER_SETTING=keep\n"
    assert unrelated.read_text() == '{"keep": true}\n'


def test_installed_setup_independent_of_checkout(tmp_path):
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
    wheels = list(wheel_dir.glob("*.whl"))
    assert len(wheels) == 1

    venv = tmp_path / "venv"
    make_venv = subprocess.run(
        ["uv", "venv", "--offline", "--python", sys.executable,
         str(venv)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert make_venv.returncode == 0, make_venv.stderr
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
         str(wheels[0])],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert install.returncode == 0, install.stderr

    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / ".env").write_text(
        "OBSIDIAN_VAULT=/synthetic/Research\n"
        "OSM_RETRIEVAL_BACKEND=caasiopeia\n"
        "CAASIOPEIA_BASE_URL=http://host.docker.internal:8123\n"
        "OSM_CAASIOPEIA_HOST_URL=http://localhost:8123\n"
        f"CAASIOPEIA_SOURCE_MAP=vault={SOURCE_ID}\n"
        "CAASIOPEIA_SOURCE_ROOTS=vault=notes\n"
        "CAASIOPEIA_TOKEN_BUDGET=900\n"
    )
    output_home = tmp_path / "home"
    output_home.mkdir()
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(output_home),
           "CAASIOPEIA_API_KEY": API_KEY,
           "DATABASE_URL": "postgresql://osm:unused@127.0.0.1:1/unused",
           "OBSIDIAN_VAULT": "/synthetic/Research",
           "OSM_TEST_DEPLOY": str(deploy)}
    code = (
        "import json, osm_init, src.server, src.launcher; from pathlib import Path; "
        "osm_init.PROJECT_ROOT = Path(__import__('os').environ['OSM_TEST_DEPLOY']); "
        "resolved = osm_init._prepare_saved_retrieval_for_rebuild(); "
        "print(json.dumps({'module': str(Path(osm_init.__file__).resolve()), "
        "'server': str(Path(src.server.__file__).resolve()), "
        "'launcher': str(Path(src.launcher.__file__).resolve()), "
        "'backend': resolved['OSM_RETRIEVAL_BACKEND'], "
        "'url': resolved['CAASIOPEIA_BASE_URL'], "
        "'source_map': resolved['CAASIOPEIA_SOURCE_MAP']}))"
    )
    run = subprocess.run(
        [str(python), "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout.strip().splitlines()[-1])
    for name in ("module", "server", "launcher"):
        assert Path(result[name]).is_relative_to(venv)
        assert str(repo) not in result[name]
    assert result["backend"] == "caasiopeia"
    assert result["url"] == "http://host.docker.internal:8123"
    assert result["source_map"] == f"vault={SOURCE_ID}"
    assert API_KEY not in run.stdout + run.stderr + (deploy / ".env").read_text()

    native_settings = _retrieval_settings("Research")
    write_native = (
        "import src.launcher; from pathlib import Path; "
        f"src.launcher.write_native_runtime(['/synthetic/Research'], "
        "'postgresql://osm:unused@127.0.0.1:1/unused', retrieval_settings="
        f"{native_settings!r})"
    )
    saved = subprocess.run(
        [str(python), "-c", write_native], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert saved.returncode == 0, saved.stderr
    runtime = output_home / ".config" / "obsidian-semantic-mcp" / "native_runtime.json"
    assert API_KEY not in runtime.read_text()
    restart = subprocess.run(
        [str(python), "-c",
         "import json, os, src.launcher; "
         "src.launcher._run_server = lambda: None; src.launcher.main(); "
         "print(json.dumps({k: os.environ[k] for k in ('OSM_RETRIEVAL_BACKEND', "
         "'CAASIOPEIA_BASE_URL', 'CAASIOPEIA_SOURCE_MAP', 'CAASIOPEIA_SOURCE_ROOTS')}))"],
        cwd=tmp_path,
        env={key: value for key, value in env.items() if key != "OSM_TEST_DEPLOY"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert restart.returncode == 0, restart.stderr
    restored = json.loads(restart.stdout.strip().splitlines()[-1])
    assert restored == {
        "OSM_RETRIEVAL_BACKEND": "caasiopeia",
        "CAASIOPEIA_BASE_URL": "http://caas.invalid:8080",
        "CAASIOPEIA_SOURCE_MAP": f"Research={SOURCE_ID}",
        "CAASIOPEIA_SOURCE_ROOTS": "Research=notes",
    }
    assert API_KEY not in restart.stdout + restart.stderr


def test_remote_vault_is_mounted_only_after_preflight(monkeypatch, tmp_path):
    import osm_init

    mount = tmp_path / "obsidian-remote-vault"
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(osm_init, "_REMOTE_VAULT_SELECTION", ("/notes/vault", str(mount)))
    monkeypatch.setattr(osm_init, "_INIT_VAULTS", [str(mount)])
    monkeypatch.setattr(osm_init, "_RETRIEVAL_BACKEND", "caasiopeia")
    monkeypatch.setattr(osm_init, "cmd_exists", lambda name: name == "sshfs")
    monkeypatch.setattr(osm_init.subprocess, "run", lambda *a, **kw: type("Result", (), {"returncode": 0})())
    result = osm_init._prompt_vault_location("user", "host")
    assert result == str(mount)
    assert mount.is_dir()
    assert osm_init._REMOTE_VAULT_SELECTION is None
