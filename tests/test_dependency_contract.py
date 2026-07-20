"""tests/test_dependency_contract.py — Security & Correctness plan, iteration 4.

Removes the phantom `starlette`/`uvicorn` pins from `[project] dependencies`
(dashboard.py has always used stdlib `http.server`, never Starlette/uvicorn)
and adds a permanent contract against reintroducing an unimported runtime
dependency.

IMPORTANT DISCOVERY (see docs — flagged in the implementation report as a
plan/reality mismatch): `starlette` and `uvicorn` are NOT purely dead
weight — `uv pip show mcp` / `importlib.metadata.distribution("mcp").requires`
shows they are unconditional (non-extra) transitive dependencies of the
`mcp` SDK itself (`mcp`'s own HTTP/SSE transport machinery). Removing our
own explicit pin is still correct and worth doing (we stop claiming
ownership of a version we don't import or audit), but it does NOT remove
`starlette`/`uvicorn` from the dependency tree or from `uv.lock` as a whole
— `mcp` still pulls them in. The plan's literal acceptance criterion
"`grep -c 'starlette' uv.lock` returns 0" is therefore unsatisfiable for the
whole file; the achievable and meaningful version of that check is scoped to
THIS project's own `[[package]] name = "obsidian-semantic-mcp"` block in
uv.lock, which is what `test_lockfile_has_no_starlette_or_uvicorn` below
checks.
"""
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
UV_LOCK = REPO_ROOT / "uv.lock"
SRC_DIR = REPO_ROOT / "src"

# Explicit mapping for distributions whose PyPI name doesn't match their
# Python import name — per the plan, rather than guessing at runtime.
DIST_TO_MODULE = {
    "psycopg2-binary": "psycopg2",
    "pyjwt": "jwt",
    "python-dotenv": "dotenv",
    "pyyaml": "yaml",
}

# Distributions that are declared direct dependencies but are not imported
# anywhere under src/ by our own code — because they are hard (non-extra)
# transitive requirements of `mcp` itself (see module docstring). Pinning
# them here is a deliberate choice (own the exact version rather than let
# mcp's own range resolve arbitrarily), not dead weight like starlette/
# uvicorn were. This allowlist exists so the contract stays meaningful for
# *future* additions without relitigating this iteration's out-of-scope
# finding (dependency CVE bumps / unused-transitive-pin cleanup is Stage 4
# work per the plan, not this iteration's).
KNOWN_TRANSITIVE_ONLY = {"httpx", "pyjwt"}


def _module_name(dist_name: str) -> str:
    if dist_name in DIST_TO_MODULE:
        return DIST_TO_MODULE[dist_name]
    return dist_name.replace("-", "_")


def _load_dependencies() -> list[str]:
    data = tomllib.loads(PYPROJECT.read_text())
    deps = data["project"]["dependencies"]
    # Strip version pins: "mcp==1.26.0" -> "mcp"
    return [re.split(r"[=<>!~\[]", d, 1)[0].strip() for d in deps]


def _src_imports_module(module_name: str) -> bool:
    """True if `import <module_name>` or `from <module_name>` appears
    anywhere under src/."""
    pattern = re.compile(
        rf"^\s*(import\s+{re.escape(module_name)}\b|from\s+{re.escape(module_name)}\b)",
        re.MULTILINE,
    )
    for py_file in SRC_DIR.rglob("*.py"):
        if pattern.search(py_file.read_text(encoding="utf-8")):
            return True
    return False


# ── Smoke ─────────────────────────────────────────────────────────────────

def test_smoke_dashboard_and_server_import_after_removal():
    """starlette/uvicorn were never imported by our code — removing the pin
    must not affect either module's importability."""
    import subprocess

    for module in ("dashboard", "server"):
        result = subprocess.run(
            [sys.executable, "-c", f"import sys; sys.path.insert(0,'src'); import {module}"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            env={
                "PATH": "/usr/bin:/bin",
                "OBSIDIAN_VAULT": "/tmp/test_vault",
                "DATABASE_URL": "postgresql://localhost/test",
            },
        )
        assert result.returncode == 0, (
            f"import {module} failed after removing starlette/uvicorn: {result.stderr}"
        )


# ── Unit / contract ───────────────────────────────────────────────────────

def test_no_unimported_runtime_dependencies():
    """Every [project] dependency's import name must appear under src/,
    unless explicitly allowlisted as a known transitive-only pin (see
    KNOWN_TRANSITIVE_ONLY and the module docstring). Fails on any newly
    added phantom dependency — the exact defect class starlette/uvicorn
    were before this iteration."""
    deps = _load_dependencies()
    unimported = []
    for dist in deps:
        if dist in KNOWN_TRANSITIVE_ONLY:
            continue
        module = _module_name(dist)
        if not _src_imports_module(module):
            unimported.append(dist)

    assert not unimported, (
        f"the following [project] dependencies are not imported anywhere "
        f"under src/ and are not in KNOWN_TRANSITIVE_ONLY: {unimported}. "
        f"Either import them, add a DIST_TO_MODULE mapping if the import "
        f"name differs from the PyPI name, or remove the dependency."
    )


def test_starlette_and_uvicorn_removed_from_pyproject():
    deps = _load_dependencies()
    assert "starlette" not in deps
    assert "uvicorn" not in deps


# ── Integration ───────────────────────────────────────────────────────────

def test_lockfile_has_no_starlette_or_uvicorn():
    """This project's own package block in uv.lock must not list
    starlette/uvicorn as direct dependencies.

    Does NOT assert their total absence from uv.lock — they remain present
    as transitive dependencies of `mcp` (see module docstring); that is
    expected and correct, not a regression this test should catch.
    """
    lock_text = UV_LOCK.read_text()
    match = re.search(
        r'name = "obsidian-semantic-mcp"\nversion = "[^"]+"\nsource = \{[^}]*\}\n'
        r'dependencies = \[(.*?)\]\n',
        lock_text,
        re.DOTALL,
    )
    assert match, "could not locate obsidian-semantic-mcp's own [[package]] block in uv.lock"
    own_deps_block = match.group(1)
    assert '"starlette"' not in own_deps_block
    assert '"uvicorn"' not in own_deps_block
