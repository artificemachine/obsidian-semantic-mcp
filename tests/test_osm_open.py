"""Desktop opening validates the complete batch before dispatching URIs."""

import subprocess
from urllib.parse import parse_qs, urlsplit

import pytest

import osm_init


@pytest.fixture
def vault(tmp_path):
    (tmp_path / ".obsidian").mkdir()
    return tmp_path


@pytest.fixture
def dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(osm_init, "_desktop_uri_command", lambda uri: ["handler", uri], raising=False)
    monkeypatch.setattr(osm_init.subprocess, "run", lambda argv, **kw: calls.append((argv, kw)))
    return calls


def test_open_encodes_multiple_exact_files(vault, dispatch, capsys):
    names = ["space é &?.md", "second.md"]
    for name in names:
        (vault / name).write_bytes(b"---\ntags: []\n---\n")
    osm_init.cmd_open(["--vault", str(vault), *names])
    assert len(dispatch) == 2
    for (argv, kwargs), name in zip(dispatch, names):
        assert parse_qs(urlsplit(argv[1]).query) == {"path": [str(vault / name)], "paneType": ["tab"]}
        assert "%2F" in argv[1] and "+" not in argv[1]
        assert kwargs == {"check": True, "timeout": 10}
        assert (vault / name).read_bytes() == b"---\ntags: []\n---\n"
    assert "tab selection" in capsys.readouterr().out


def test_missing_second_file_prevents_entire_batch(vault, dispatch):
    (vault / "exists.md").touch()
    with pytest.raises(SystemExit, match="1"):
        osm_init.cmd_open(["--vault", str(vault), "exists.md", "missing.md"])
    assert not dispatch
    assert not (vault / "missing.md").exists()


@pytest.mark.parametrize("escape", ["relative", "symlink"])
def test_open_rejects_vault_escape(vault, dispatch, escape):
    outside = vault.parent / (vault.name + "-outside.md")
    outside.touch()
    if escape == "symlink":
        (vault / "escape.md").symlink_to(outside)
        path = "escape.md"
    else:
        path = "../" + outside.name
    with pytest.raises(SystemExit, match="1"):
        osm_init.cmd_open(["--vault", str(vault), path])
    assert not dispatch


def test_base_is_explicit_never_inferred(vault, dispatch):
    (vault / "notes").mkdir()
    (vault / "notes/a.md").touch()
    with pytest.raises(SystemExit, match="1"):
        osm_init.cmd_open(["--vault", str(vault), "a.md"])
    assert not dispatch
    osm_init.cmd_open(["--vault", str(vault), "--base", "notes", "a.md"])
    assert len(dispatch) == 1


def test_dry_run_does_not_dispatch(vault, dispatch):
    (vault / "a.md").touch()
    osm_init.cmd_open(["--vault", str(vault), "--dry-run", "a.md"])
    assert not dispatch


@pytest.mark.parametrize("error", [subprocess.CalledProcessError(1, "handler"), subprocess.TimeoutExpired("handler", 10), OSError("handler failed")])
def test_dispatch_failure_is_nonzero(vault, dispatch, monkeypatch, error):
    (vault / "a.md").touch()
    def failing(*args, **kwargs):
        raise error
    monkeypatch.setattr(osm_init.subprocess, "run", failing)
    with pytest.raises(SystemExit, match="1"):
        osm_init.cmd_open(["--vault", str(vault), "a.md"])


@pytest.mark.parametrize("name", ["a#heading.md", "folder", "image.png"])
def test_open_rejects_ambiguous_or_non_note_targets(vault, dispatch, name):
    path = vault / name
    path.mkdir() if name == "folder" else path.touch()
    with pytest.raises(SystemExit, match="1"):
        osm_init.cmd_open(["--vault", str(vault), name])
    assert not dispatch


def test_main_routes_open_without_generic_flag_parser(vault, dispatch, monkeypatch):
    (vault / "--yes.md").touch()
    monkeypatch.setattr(osm_init.sys, "argv", ["osm", "open", "--vault", str(vault), "--", "--yes.md"])
    osm_init.main()
    assert len(dispatch) == 1


@pytest.mark.parametrize("base", ["missing", ".."])
def test_open_rejects_invalid_base(vault, dispatch, base):
    (vault / "a.md").touch()
    with pytest.raises(SystemExit, match="1"):
        osm_init.cmd_open(["--vault", str(vault), "--base", base, "a.md"])
    assert not dispatch


def test_open_requires_local_vault_metadata(tmp_path, dispatch):
    (tmp_path / "a.md").touch()
    with pytest.raises(SystemExit, match="1"):
        osm_init.cmd_open(["--vault", str(tmp_path), "a.md"])
    assert not dispatch


def test_open_detects_note_change_during_dispatch(vault, dispatch, monkeypatch):
    note = vault / "a.md"
    note.write_text("before")
    monkeypatch.setattr(osm_init.subprocess, "run", lambda *a, **kw: note.write_text("external edit"))
    with pytest.raises(SystemExit, match="1"):
        osm_init.cmd_open(["--vault", str(vault), "a.md"])


def test_linux_handler_uses_native_argv(monkeypatch):
    monkeypatch.setattr(osm_init.platform, "system", lambda: "Linux")
    monkeypatch.setattr(osm_init.shutil, "which", lambda name: "/usr/bin/xdg-open")
    assert osm_init._desktop_uri_command("obsidian://open?path=x") == ["/usr/bin/xdg-open", "obsidian://open?path=x"]


def test_macos_handler_requires_application(monkeypatch):
    monkeypatch.setattr(osm_init.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(osm_init.shutil, "which", lambda name: None)
    with pytest.raises(ValueError, match="Obsidian.app"):
        osm_init._desktop_uri_command("obsidian://open?path=x")


@pytest.mark.parametrize("system", ["Linux", "Other"])
def test_unavailable_handler_fails(monkeypatch, system):
    monkeypatch.setattr(osm_init.platform, "system", lambda: system)
    monkeypatch.setattr(osm_init.shutil, "which", lambda name: None)
    with pytest.raises(ValueError):
        osm_init._desktop_uri_command("obsidian://open?path=x")


@pytest.mark.parametrize("failure", [False, True])
def test_windows_native_uri_dispatch(vault, monkeypatch, failure):
    (vault / "a.md").touch()
    monkeypatch.setattr(osm_init.platform, "system", lambda: "Windows")
    calls = []
    def startfile(uri):
        if failure:
            raise OSError("No URI association")
        calls.append(uri)
    monkeypatch.setattr(osm_init.os, "startfile", startfile, raising=False)
    if failure:
        with pytest.raises(SystemExit, match="1"):
            osm_init.cmd_open(["--vault", str(vault), "a.md"])
    else:
        osm_init.cmd_open(["--vault", str(vault), "a.md"])
        assert parse_qs(urlsplit(calls[0]).query)["path"] == [str(vault / "a.md")]
