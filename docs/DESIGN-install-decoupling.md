# Design Scope: repo-independent `osm` install

**Date:** 2026-05-21 | **Status:** scoping | **Trigger:** the dev `osm` install points at the local checkout path and breaks if that checkout moves, violating the "Strict Installation Decoupling" rule in `CLAUDE.md`.

---

## What is actually true today (two install models)

| Model | How `osm` is installed | `PROJECT_ROOT` resolves to | Repo-independent? |
|---|---|---|---|
| **Production** (`install.sh`) | Clones repo to `~/.local/share/obsidian-semantic-mcp` (stable), symlinks `~/.local/bin/osm` → `…/scripts/osm`. `osm init` runs `uv sync` there. | `Path(__file__).parent` = the install dir, which *contains* `docker-compose.yml`. Self-contained. | **Yes** — independent of any dev checkout. |
| **Dev** (`uv tool install --editable <local-checkout>/…`) — *this machine* | Editable, tracks the dev repo. | `Path(__file__).parent` = the local checkout path (movable dev checkout). | **No** — this is the reported problem. |

The project already has a repo-independent path. The reported issue is that this machine is on the *dev* model, not the production one.

## Why a plain `uv tool install` / pip install does not work today

Two independent defects, both confirmed:

1. **Packaging.** The wheel (`uv build`) contains only `src/config.py`, `src/dashboard.py`, `src/launcher.py`, `src/server.py`. It **omits `osm_init.py`** despite `include = ["osm_init.py", …]`. So `osm = "osm_init:main"` raises `ModuleNotFoundError: No module named 'osm_init'`. The wheel also omits `docker-compose.yml`.

2. **`PROJECT_ROOT` resolution.** `osm_init.py:310`:
   ```python
   PROJECT_ROOT = Path(__file__).parent.resolve()
   ```
   In a non-editable venv, `__file__` is in `site-packages`, which has no `docker-compose.yml`. The ~17 `PROJECT_ROOT` usages (compose `--project-directory`, `.env`, override file, `uv sync --project`) all then point at the wrong place.

Note the asymmetry: `src/launcher.py` (the `obsidian-semantic-mcp` MCP entry) **already** resolves correctly via `_project_root()`: `OSM_PROJECT_ROOT` env → `~/.config/obsidian-semantic-mcp/project_root` → dev-checkout fallback. The `osm` CLI does not use this; it hardcodes `__file__`. `osm_init._write_project_root_config()` *writes* that config file but never *reads* it.

---

## Track A — fix this machine now (no code change)

Switch from the dev install to the production install so `osm` lives in a stable, self-contained dir:

```bash
# from a clean checkout or the installer
curl -fsSL https://raw.githubusercontent.com/celstnblacc/obsidian-semantic-mcp/main/install.sh | bash
# (or point INSTALL_DIR / clone source at the local repo)
uv tool uninstall obsidian-semantic-mcp   # remove the editable dev tool first
```

Result: `osm` runs from `~/.local/share/obsidian-semantic-mcp`; the local checkout path can be moved or deleted freely. Zero code change. This is the intended design.

Caveat: `install.sh` clones from GitHub `main`. To install the current local code instead, run it against the local path or `git clone` the local repo into the install dir.

## Track B — make the package install (uv tool / pip) viable and decoupled

Only needed if pip/uv-tool/PyPI is wanted as a first-class install channel. Three changes:

1. **pyproject — force-include the top-level modules** so the wheel actually ships them:
   ```toml
   [tool.hatch.build.targets.wheel.force-include]
   "osm_init.py" = "osm_init.py"
   "obsidian_semantic_mcp.py" = "obsidian_semantic_mcp.py"
   ```
   Verify: `uv build && unzip -l dist/*.whl | grep osm_init`.

2. **`osm_init.py` — config-first `PROJECT_ROOT` resolver**, mirroring `launcher._project_root()`. Replace the line-310 constant with a function: `OSM_PROJECT_ROOT` env → `~/.config/obsidian-semantic-mcp/project_root` → dev fallback (`__file__.parent` if it has `docker-compose.yml`). Blast radius: ~17 `PROJECT_ROOT` references become "resolved deploy dir."

3. **Deploy location.** The compose stack (`docker-compose.yml`, `.env`, overrides, data) must live in a stable dir (e.g. `~/.local/share/obsidian-semantic-mcp`), recorded in `~/.config/.../project_root`. `osm init` must *deploy there* (copy/clone the compose assets) instead of assuming they sit next to the code. The `.env` already targets `~/.local/share/obsidian-semantic-mcp/.env` per the README, so this is partly established.

## Implication for the PyPI workflow (`publish-pypi.yml`, shipped in #46)

Until Track B lands, a PyPI release publishes a package where:
- the `osm` setup CLI is broken (no `osm_init` in the wheel), and
- the `obsidian-semantic-mcp` launcher works only against an already-deployed stack.

So PyPI is **not** a working install channel yet. Decision needed: (a) hold/disable `publish-pypi.yml` until Track B, or (b) keep it but document that pip installs only the launcher. Recommend (a) to avoid shipping a broken `pip install`.

---

## Recommendation

- **Immediate need (move the dev repo freely):** Track A. Zero code, matches the intended design.
- **Track B** only if pip/PyPI distribution is a real goal. It is a genuine refactor (resolver + deploy model), not a one-liner, and it is the prerequisite for the PyPI workflow to be meaningful.
- **Meanwhile:** treat `publish-pypi.yml` as not-yet-functional (do not push a version tag expecting a usable PyPI package).

---

## Locked decisions (2026-05-21) — implementation plan for Sonnet `high`

**Core decision: separate the runtime/deploy location from the code location.** The compose stack (`docker-compose.yml`, generated `docker-compose.override.yml`, `.env`, data) lives in a stable **deploy dir**, independent of where `osm_init.py` physically sits. This is what actually makes the install survive the dev repo being moved — config-based resolution alone does not, because a stack deployed *inside* the movable repo still goes stale.

1. **Deploy dir** = `${OSM_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/obsidian-semantic-mcp}`. Matches `install.sh` and the README's `.env` path. Overridable via the existing `--data-dir` flag.

2. **`PROJECT_ROOT` becomes resolved, not `__file__`.** Replace `osm_init.py:310` with `PROJECT_ROOT = _resolve_project_root()`, mirroring `launcher._project_root()`:
   - `OSM_PROJECT_ROOT` env →
   - `~/.config/obsidian-semantic-mcp/project_root` →
   - co-located: `Path(__file__).parent` **iff** it contains `docker-compose.yml` (covers the `install.sh` self-contained dir and a dev checkout) →
   - default: the deploy dir.
   The ~17 existing `PROJECT_ROOT` references stay as-is (they read the variable). It remains a module-level value, so the conftest `_reset()` monkeypatch keeps working.

3. **`osm init` provisions the deploy dir.** When the resolved root has no `docker-compose.yml` (pip / uv-tool install), copy the packaged `docker-compose.yml` into the deploy dir, write `.env` + override there, then record the dir in `~/.config/.../project_root`. When code + compose are already co-located (`install.sh` / dev), keep current behavior and just record the config.

4. **Packaging (pyproject).** `force-include` `osm_init.py`, `obsidian_semantic_mcp.py`, **and** `docker-compose.yml` so a wheel install can self-provision. Verify: `uv build && unzip -l dist/*.whl | grep -E 'osm_init|docker-compose'`.

5. **PyPI.** With 1–4, `pip install obsidian-semantic-mcp` becomes viable (code + compose in the wheel; self-provisions on `osm init`). `publish-pypi.yml` is meaningful only after this lands.

### TDD task order (Sonnet)
1. RED: assert `uv build`'s wheel contains `osm_init.py` + `docker-compose.yml`. GREEN: pyproject `force-include`.
2. RED: unit-test `_resolve_project_root()` per branch (env / config / co-located / default). GREEN: implement resolver; replace line 310.
3. RED: test `osm init` provisions compose into a tmp deploy dir and writes config when not co-located. GREEN: implement provisioning in the init flow.
4. Regression: full suite green.
5. E2E (manual): clean non-editable `uv tool install` → `osm init --data-dir <tmp>` → `osm status` works → move/delete the dev repo → `osm status` still works.

### Guardrails
- Do not break `install.sh` (the co-located branch must catch it).
- Do not change `launcher.py` (already correct; reuse its pattern).
- This machine's migration: re-init the stack into the deploy dir, after which the local checkout path can move freely.
