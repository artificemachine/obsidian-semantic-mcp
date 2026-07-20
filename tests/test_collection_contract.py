"""tests/test_collection_contract.py — Iteration 0 of the security/correctness plan.

Guards against a regression to the bug this iteration fixes: `pyproject.toml`
`[tool.pytest.ini_options] testpaths` used to enumerate four files by name,
so any other `tests/test_*.py` file silently never ran under a bare `pytest`
invocation. `testpaths = ["tests"]` makes collection directory-based; this
test proves the filesystem and pytest's collection agree, so the same
regression (a file added to tests/ but never collected) fails loudly.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent


def _collect_ids(*args: str) -> list[str]:
    """Run `pytest --collect-only -q` with the given extra args, return node ids."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    lines = result.stdout.splitlines()
    # Node-id lines look like "tests/test_foo.py::test_bar"; the summary
    # line ("N tests collected...") and blank lines are filtered out.
    return [line for line in lines if "::" in line]


def test_every_test_file_is_collected():
    """Every tests/test_*.py file must contribute the same collected item
    count to a bare `pytest --collect-only` run as it does when collected
    on its own.

    This is the regression the iteration fixes: `testpaths` used to
    enumerate four files by name, so any other file's tests were silently
    invisible to a bare `pytest` run — not a collection *error*, just an
    empty contribution. Comparing standalone vs. bare-run item counts
    catches that silent-drop shape directly. Files with legitimately zero
    pytest-style tests (e.g. tests/test_e2e.py, a manual script) correctly
    compare 0 == 0 and pass.
    """
    globbed = sorted(p.name for p in TESTS_DIR.glob("test_*.py"))
    assert globbed, "no tests/test_*.py files found — glob itself is broken"

    bare_ids = _collect_ids()
    assert bare_ids, "bare `pytest --collect-only -q` collected nothing at all"

    mismatched = []
    for name in globbed:
        standalone_ids = _collect_ids(f"tests/{name}")
        bare_ids_for_file = [nid for nid in bare_ids if nid.startswith(f"tests/{name}::")]
        if len(standalone_ids) != len(bare_ids_for_file):
            mismatched.append(
                f"{name}: standalone={len(standalone_ids)} bare={len(bare_ids_for_file)}"
            )

    assert not mismatched, (
        "the following tests/test_*.py files contribute a different item "
        f"count to a bare `pytest --collect-only` run than when collected "
        f"standalone — a file is being silently excluded: {mismatched}. "
        f"Check [tool.pytest.ini_options] testpaths in pyproject.toml."
    )
