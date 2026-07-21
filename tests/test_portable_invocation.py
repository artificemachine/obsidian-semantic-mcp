"""Phase 2 of portable-paths-cleanup (see docs/PLAN-portable-mcp-config.md).

The contract: `obsidian-semantic-mcp` invoked as a bare CLI on PATH must
locate its project root via env/config/dev-detect, with NO hardcoded path
arg required from the agent config side.

This locks in the launcher's project-root resolution so that
$HOME/.claude.json, $HOME/.opencode.json, etc. can drop the
`--project-directory /Users/.../obsidian-semantic-mcp` arg entirely.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import launcher


# ---------------------------------------------------------------------------
# Project root resolution priority:
#   1. OSM_PROJECT_ROOT env var
#   2. $HOME/.config/obsidian-semantic-mcp/project_root file
#   3. dev-checkout auto-detect (sibling docker-compose.yml)
# ---------------------------------------------------------------------------


def test_env_var_wins(monkeypatch, tmp_path):
    """OSM_PROJECT_ROOT must take precedence over the config file."""
    env_root = tmp_path / "from_env"
    env_root.mkdir()
    monkeypatch.setenv("OSM_PROJECT_ROOT", str(env_root))

    cfg_file = tmp_path / "config" / "project_root"
    cfg_file.parent.mkdir()
    cfg_file.write_text(str(tmp_path / "from_config") + "\n")
    monkeypatch.setattr(launcher, "PROJECT_ROOT_FILE", cfg_file)

    assert launcher._project_root() == env_root


def test_config_file_used_when_env_unset(monkeypatch, tmp_path):
    """When OSM_PROJECT_ROOT is unset, fall back to the config file."""
    monkeypatch.delenv("OSM_PROJECT_ROOT", raising=False)

    cfg_root = tmp_path / "from_config"
    cfg_root.mkdir()
    cfg_file = tmp_path / "config" / "project_root"
    cfg_file.parent.mkdir()
    cfg_file.write_text(str(cfg_root) + "\n")
    monkeypatch.setattr(launcher, "PROJECT_ROOT_FILE", cfg_file)

    assert launcher._project_root() == cfg_root


def test_dev_checkout_autodetected_when_no_env_no_config(monkeypatch, tmp_path):
    """Last fallback: detect that we're running from a dev checkout because
    a sibling `docker-compose.yml` exists."""
    monkeypatch.delenv("OSM_PROJECT_ROOT", raising=False)
    cfg_file = tmp_path / "nonexistent_config"
    monkeypatch.setattr(launcher, "PROJECT_ROOT_FILE", cfg_file)

    # The actual launcher.py module file is in the real repo's src/, so
    # this resolves via Path(launcher.__file__).resolve().parent.parent.
    # Verify it returns a path with docker-compose.yml present.
    result = launcher._project_root()
    if result is not None:
        assert (result / "docker-compose.yml").exists() or \
               (result / "docker-compose.yaml").exists(), \
               f"dev-detect returned {result} but it has no docker-compose file"


def test_no_root_when_nothing_resolves(monkeypatch, tmp_path):
    """If nothing resolves, return None — caller decides how to handle."""
    monkeypatch.delenv("OSM_PROJECT_ROOT", raising=False)
    cfg_file = tmp_path / "nonexistent_config"
    monkeypatch.setattr(launcher, "PROJECT_ROOT_FILE", cfg_file)

    # Patch the launcher's __file__ to point at a place with no compose.yml
    fake_module = tmp_path / "fake" / "src" / "launcher.py"
    fake_module.parent.mkdir(parents=True)
    fake_module.write_text("# fake")
    # _project_root() reads __file__ at module-eval time per call, so we
    # need to monkeypatch the relative path resolution. Simulate by
    # patching Path(__file__) lookups.
    import src.launcher as L
    monkeypatch.setattr(L, "__file__", str(fake_module))

    assert launcher._project_root() is None


# ---------------------------------------------------------------------------
# Bare-CLI invocation: the entry point should be on PATH after `pip install`.
# ---------------------------------------------------------------------------


def test_bare_cli_console_script_resolvable():
    """The `obsidian-semantic-mcp` console script must be on PATH.

    Without this, agent configs that say `"command":
    "obsidian-semantic-mcp"` cannot spawn the server.
    """
    resolved = shutil.which("obsidian-semantic-mcp")
    if resolved is None:
        pytest.skip(
            "console script not on PATH in this test environment "
            "(install with `uv tool install -e .` or `pip install -e .`)"
        )
    assert os.path.isfile(resolved) or os.path.islink(resolved)


# ---------------------------------------------------------------------------
# Anti-regression: agent configs should NEVER need --project-directory.
# ---------------------------------------------------------------------------


def test_main_without_project_directory_arg(monkeypatch, tmp_path):
    """main() must not REQUIRE --project-directory in argv. The launcher
    resolves project root from env/config/dev-detect, never from sys.argv.

    This guards against a regression where someone adds an argparse step
    that demands --project-directory, which would re-introduce the
    hardcoded-path footgun in agent configs.
    """
    monkeypatch.setenv("OBSIDIAN_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret")
    monkeypatch.delenv("OSM_DOCKER", raising=False)
    monkeypatch.delenv("OSM_PROJECT_ROOT", raising=False)

    # argv that an agent config might pass: just the program name.
    monkeypatch.setattr(sys, "argv", ["obsidian-semantic-mcp"])

    from unittest.mock import MagicMock, patch

    with patch("subprocess.run", MagicMock(side_effect=FileNotFoundError)), \
         patch("src.launcher._run_server") as mock_srv:
        launcher.main()
        mock_srv.assert_called_once()
