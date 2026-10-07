"""Native retrieval settings stay private to the installed OSM runtime."""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))


SOURCE_ID = "12345678-1234-5678-1234-567812345678"
API_KEY = "synthetic-secret-for-runtime-test"


@pytest.fixture(autouse=True)
def block_unplanned_http(monkeypatch):
    request = requests.Session.request

    def unexpected_request(self, method, url, *args, **kwargs):
        if urlsplit(url).hostname in ("127.0.0.1", "localhost", "::1"):
            return request(self, method, url, *args, **kwargs)
        pytest.fail(f"unexpected HTTP request to {urlsplit(url).hostname or '<invalid-host>'}")

    from urllib.parse import urlsplit
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
