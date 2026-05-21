"""
Shared fixtures and helpers for osm_init test suites.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import osm_init

_ORIGINAL_PROJECT_ROOT = osm_init.PROJECT_ROOT


@pytest.fixture(autouse=True)
def _isolate_pi_agent(monkeypatch):
    """register_pi_agent writes ~/.pi/agent/mcp.json on hosts where ``pi`` is
    installed; stub it so the suite never mutates the developer's real pi config
    and stays deterministic regardless of host state."""
    monkeypatch.setattr(osm_init, "register_pi_agent", lambda *a, **kw: None)


def _reset():
    """Reset global mutable state in osm_init between tests."""
    osm_init.DRY_RUN = False
    osm_init._DRY_ACTIONS.clear()
    osm_init._PARAMS.clear()
    # Restore PROJECT_ROOT so a test that crashes mid-_with_root() doesn't
    # leave subsequent tests pointing at a cleaned-up tmp_path.
    osm_init.PROJECT_ROOT = _ORIGINAL_PROJECT_ROOT
