"""Guard: every module-level constant derived from the user's home is isolated for tests (issue #81).

A constant such as ``OSM_CONFIG_DIR = Path.home() / ...`` is computed when its module is imported,
so redirecting HOME later changes nothing. Each one needs an explicit ``monkeypatch.setattr`` in
tests/conftest.py, otherwise the suite reads and writes the developer's real installation. The first
three of these were found one failing test at a time; this test finds the next one when it is added.
"""
import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFTEST = REPO_ROOT / "tests" / "conftest.py"

# module file -> the name conftest.py uses for it in ``monkeypatch.setattr(<alias>, "NAME", ...)``
MODULES = {
    "osm_init.py": "osm_init",
    "src/config.py": "config",
    "src/launcher.py": "launcher",
}

# Imported from a module above, so the AST scan cannot see where it comes from.
HOME_ROOTS = {"OSM_CONFIG_DIR"}

# (file, name) pairs that no fixture can redirect, each with the reason and what contains the risk.
NOT_REDIRECTABLE = {
    ("src/server.py", "_ENV_SEARCH_PATHS"): "read by load_dotenv at import, before any fixture; "
                                           "the autouse fixture clears the variables it can load",
}
SCANNED_ONLY = ["src/server.py"]


def _home_derived(relative: str) -> list[str]:
    source = (REPO_ROOT / relative).read_text()
    derived = set(HOME_ROOTS)
    found: list[str] = []
    for node in ast.parse(source).body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        text = ast.get_source_segment(source, node.value) or ""
        refs = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
        if "Path.home()" in text or "expanduser" in text or "XDG_" in text or refs & derived:
            derived.update(names)
            found.extend(names)
    return found


def unisolated(conftest_text: str) -> list[str]:
    """``file:NAME`` for each home-derived constant that conftest does not setattr."""
    missing = []
    for relative, alias in MODULES.items():
        for name in _home_derived(relative):
            if not re.search(rf'setattr\(\s*{alias}\s*,\s*"{re.escape(name)}"', conftest_text):
                missing.append(f"{relative}:{name}")
    for relative in SCANNED_ONLY:
        for name in _home_derived(relative):
            if (relative, name) not in NOT_REDIRECTABLE:
                missing.append(f"{relative}:{name}")
    return missing


def test_every_home_derived_constant_is_isolated_by_conftest():
    assert unisolated(CONFTEST.read_text()) == []


def test_the_guard_flags_a_constant_whose_patch_is_removed():
    stripped = "\n".join(
        line for line in CONFTEST.read_text().splitlines() if '"_DASHBOARD_TOKEN_FILE"' not in line
    )
    assert "osm_init.py:_DASHBOARD_TOKEN_FILE" in unisolated(stripped)


def test_the_guard_flags_an_unlisted_constant_in_a_module_it_only_scans():
    assert ("src/server.py", "_ENV_SEARCH_PATHS") in NOT_REDIRECTABLE
    assert _home_derived("src/server.py") == ["_ENV_SEARCH_PATHS"]
