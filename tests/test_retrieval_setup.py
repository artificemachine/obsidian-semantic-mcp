"""Native retrieval settings stay private to the installed OSM runtime."""
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


SOURCE_ID = "12345678-1234-5678-1234-567812345678"
API_KEY = "synthetic-secret-for-runtime-test"


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
