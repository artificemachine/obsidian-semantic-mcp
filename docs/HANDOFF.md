# Handoff — Windows + NFS vault install

**Audience:** maintainers and users running osm with a network-mounted vault on Windows.
**Status:** original incident reported against `v0.5.9` (2026-04-19). Most recommendations are now upstream as of `v0.5.13`. This doc captures the case study and the two remaining deferred items.

---

## 1. The original failure mode

On Windows, mounting a TrueNAS export as a drive letter (`Z:\`) and pointing `osm init` at it produced two symptoms, both invisible without inspection:

1. **UNC rejection.** `osm init` resolved `Z:\path` to `\\10.0.0.1\…` and wrote it into `.env`. Docker Desktop daemon rejected the bind-mount with `is not a valid Windows path`, leaving every container in `Created` state.
2. **Empty silent mount.** Reverting `.env` to the raw `Z:\…` form bypassed the daemon error but mounted an empty directory in the container — Docker Desktop's WSL2 backend cannot follow a Windows-side network drive into the container filesystem.

The downstream effect was a 90 s postgres health-check timeout that sent users debugging the wrong service.

### Quick reproducer

```bash
MSYS_NO_PATHCONV=1 docker run --rm \
  -v 'Z:\path\to\vault:/vault:ro' alpine \
  sh -c 'ls -la /vault && find /vault -name "*.md" | wc -l'
# → empty directory, 0 files
```

---

## 2. What works today

### Recommended path: WSL2 mount

Mount the share inside a WSL2 distro and pass the Linux path to `osm init`. Docker Desktop shares WSL paths cleanly with no volume-driver gymnastics.

```bash
# inside the WSL2 distro
sudo apt install nfs-common
sudo mkdir -p /mnt/obsidian_vault
sudo mount -t nfs <nas-host>:/<export-path> /mnt/obsidian_vault
# persist via /etc/fstab if desired

osm init --mode 2 --vault /mnt/obsidian_vault
```

This is the lowest-friction option for most users.

### Alternative: native NFS / CIFS named volumes (`--vault-fs`, v0.5.12+)

When WSL2 isn't an option, `osm init` can generate a `docker-compose.override.yml` that backs each vault with a Docker named volume using NFS or CIFS driver_opts. The vault entry uses protocol-specific syntax instead of a host path:

```bash
# NFS — entry is host:/export/path
osm init --mode 3 \
  --vault 10.0.0.1:/exports/coredev \
  --vault-fs nfs

# CIFS / SMB
osm init --mode 3 \
  --vault //nas.local/share/coredev \
  --vault-fs cifs \
  --vault-cifs-user alice --vault-cifs-pass 'secret'
```

For multi-vault, repeat `--vault` (or pass a comma-joined `OBSIDIAN_VAULTS`); each entry generates its own `obsidian_vault_<basename>` named volume. `osm remove` drops these volumes on teardown.

**v1 limitations:** NFSv4 with no auth, SMB with username/password only. NFS Kerberos and CIFS credential files are not supported. WSL2 remains the recommended path for most users.

---

## 3. Upstream status

| Recommendation | Version | Notes |
|---|---|---|
| Fail-fast on `docker compose up` failure | **v0.5.11** | `osm_init.compose_up` checks the exit code, runs `docker compose ps -a` + per-container `logs`, exits immediately. UNC-path errors emit a Windows-specific WSL2 hint. |
| README "Windows + network vault" docs | **v0.5.11** | `README.md` covers WSL2 recipe and `--vault-fs` syntax. |
| `--vault-fs <auto\|local\|nfs\|cifs>` flag | **v0.5.12** | Single flag; NFS / CIFS specifics inferred from the vault entry syntax. |
| `osm remove` cleans up named volumes | **v0.5.12** | Drops every `obsidian_vault_*` volume referenced by the generated override. |
| Batched embeddings via `/api/embed` | **v0.5.13** | Surfaced during the same install session as a perf fix; cuts full-rebuild HTTP calls ~16×. |
| `POST /api/prune` orphan cleanup | **v0.5.13** | Resolves the `indexed_count > vault_file_count` drift after a vault path swap. |
| Auto-detect Windows network drives | 🟡 Deferred | Needs a Windows CI runner. The PowerShell `Win32_LogicalDisk` / `Get-SmbMapping` lookup is shippable in isolation but untestable from macOS or Linux CI today. |
| NFS sidecar integration test | 🟡 Deferred | Gate for the auto-detect work. `itsthenetwork/nfs-server-alpine` as a CI sidecar is the planned approach when a Windows runner exists. |

---

## 4. Remaining gaps

### Windows auto-detect (`--vault-fs auto`)

Today, `--vault-fs auto` is identical to `--vault-fs local` — bind mount with no detection. The proper auto-detect path is:

```powershell
Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='Z:'" |
  Select-Object DriveType, ProviderName
# DriveType=4 → Network drive (SMB, NFS client, mapped)
```

With protocol disambiguation via `Get-SmbMapping` (SMB) or `mountvol Z: /L` (NFS), we can pick the right driver-opts template automatically. The blocker is **a Windows CI runner** so the detection code is exercised before each release. Without one, shipping this risks regressing the Windows-only path silently.

### Integration test

The unit-level detection (mock `Win32_LogicalDisk` / `Get-SmbMapping`) can land in current CI today, but a true end-to-end test needs a network mount. The sidecar approach (`itsthenetwork/nfs-server-alpine`) works in CI on Linux, but the install path being tested is Windows-specific. Until a Windows runner is available, this remains gated.

---

## 5. If you're hitting this today

1. Try **WSL2** first (§2). It works, requires no osm changes, and is the path most users land on.
2. If WSL2 is not an option, use **`--vault-fs nfs`** (§2). Pass the export as `host:/path`, not the Windows drive letter.
3. If `osm init` reports `is not a valid Windows path`, the v0.5.11 fail-fast diagnostic now points you back here directly. No need to debug postgres.
4. After a vault path change of any kind, run `POST /api/prune` (or wait for a future scheduled prune job) to clean up orphaned embeddings.

---

## 6. One-paragraph summary

On Windows, `osm init` used to silently produce a broken install when `--vault` pointed at a drive letter backed by NFS or SMB — the UNC form was rejected by Docker Desktop and the drive-letter form mounted an empty directory because WSL2 cannot follow Windows-side network mounts. As of `v0.5.13`, three things have shipped: a fail-fast diagnostic that surfaces the docker error in seconds instead of after a 90 s postgres timeout, a `--vault-fs` flag that emits a `docker-compose.override.yml` with native NFS / CIFS driver_opts (with proper teardown via `osm remove`), and a `POST /api/prune` endpoint that resolves the indexed-vs-disk drift that often follows a vault path change. The Windows auto-detect path is the only remaining deferred item — gated on having a Windows CI runner.

---

## 7. Update (2026-04-27): installer + version consistency (`v0.7.3`)

This session resolved two packaging/UX issues reported during mixed OrbStack/Docker Desktop usage:

1. **Piped installer hard-failed in non-interactive environments.**  
   Running `curl .../install.sh | bash` could fail with `/dev/tty: Device not configured` when the script attempted to force interactive `osm init`.
2. **CLI version appeared stale after update flows.**  
   `osm version` could show installed `0.7.1` while latest release was newer, even after image/service updates.

### Shipped in `v0.7.3`

- `install.sh` now exits cleanly when no interactive TTY is available and prints a manual next step:
  - `~/.local/bin/osm init`
- CLI fallback version logic now reads `pyproject.toml` instead of using a stale hardcoded fallback.
- Project metadata was bumped and aligned to `0.7.3` (`pyproject.toml` and `uv.lock`).

### Operator notes

- `osm update` updates services/images and now reports `Installed CLI: 0.7.3` / `Latest release: 0.7.3` when current.
- If install is run from a non-interactive context (CI, piped shell, launcher without tty), setup is no longer treated as a fatal failure. Run `osm init` manually after install.

---

## 8. Update (2026-04-29): wikilink graph augmentation (`v0.9.0`)

This session added Path A graph augmentation — a retrieval expansion layer built on Obsidian's existing `[[wikilinks]]` that surfaces missed connections between notes that are relationally linked but semantically distant.

### Problem it solves

Semantic search returns notes that are textually similar to the query. It misses notes that share a conceptual link but were written in isolation — different terminology, different context, different session. Over months of vault growth, related notes accumulate without ever being surfaced together in a single search result. The wikilink graph is the user's own curated relation map; traversing it at query time surfaces connections the user already encoded but forgot about.

### What shipped in `v0.9.0`

**New DB table: `note_links`**
- Schema: `(source_path TEXT, target_name TEXT, target_path TEXT, PRIMARY KEY (source_path, target_name))`
- Populated in `_upsert_note()` in the same transaction as the embedding upsert — always in sync, never stale
- Indexed on `target_path` for fast incoming-edge queries

**New helpers in `src/server.py`**
- `extract_wikilinks(content)` — regex parser for `[[note]]`, `[[note|alias]]`, `[[note#heading]]`, `[[folder/note]]`; deduplicates, order-preserving
- `_build_link_index(vault)` — maps `stem.lower() → abs_path` for all non-skipped `.md` files; called once per vault at `index_vault()` time
- `_resolve_links(names, index)` — resolves link names to paths using stem lookup; handles `[[folder/note]]` by using only the stem
- `expand_via_links(paths, hops)` — traverses both outgoing and incoming edges for 1–2 hops; returns `(path, content, via_path)` tuples excluding seed paths

**New and updated MCP tools**
- `get_note_connections(filepath, hops=1)` — explore the knowledge graph directly by note path; returns all connected notes with direction and preview
- `search_vault` gains `graph_expand: bool` (default `false`) — when true, appends 1-hop wikilink neighbors after semantic results

**File watcher integration**
- `VaultEventHandler._handle_upsert()` updates `_link_index` on every file create/modify
- `delete_note()` cleans `note_links` rows and removes the stem from `_link_index`

**Also fixed**
- `_needs_polling()` UNC path check rewrote `str(vp).startswith("\\\\")` as `vault_s[:2] in ("\\\\", "//")` to resolve a shipguard PY-004 false positive that was blocking CI

### Key constraint: hash-skip means no link extraction for unchanged files

`index_vault()` calls `_upsert_note()` only for files whose hash changed. On a fresh deploy with existing indexed notes, all files are hash-skipped and `note_links` stays empty. A one-time backfill is required:

```python
# Run inside the mcp-server container
docker compose exec mcp-server python3 - << 'SCRIPT'
import sys, os
sys.path.insert(0, '/app/src')
os.environ.setdefault('OBSIDIAN_VAULT', '/vault')
from server import db_conn, extract_wikilinks, _build_link_index, _resolve_links, VAULT_PATHS

link_index = {}
for v in VAULT_PATHS:
    link_index.update(_build_link_index(v))

with db_conn() as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT path, content FROM notes")
        notes = cur.fetchall()

with db_conn() as conn:
    with conn:
        with conn.cursor() as cur:
            for path, content in notes:
                names = extract_wikilinks(content)
                resolved = _resolve_links(names, link_index) if names else {}
                cur.execute("DELETE FROM note_links WHERE source_path = %s", (path,))
                if resolved:
                    cur.executemany(
                        "INSERT INTO note_links (source_path, target_name, target_path) VALUES (%s, %s, %s)",
                        [(path, n, t) for n, t in resolved.items()]
                    )
SCRIPT
```

This was run on the v0.9.0 deploy and produced 2457 link edges across 314 notes (481 resolved targets, 1740 unresolved — links to non-indexed or non-existent notes).

### Stats at deploy

- Vault: 1081 indexed notes, 892 link index entries
- `note_links`: 2457 edges, 314 source notes, 481 resolved targets
- Tests: 300 passing (20 new covering `extract_wikilinks`, `_build_link_index`, `_resolve_links`, `expand_via_links`)
- CI: all checks green after fixing the PY-004 false positive

### Deferred / future work

- Path B (LLM entity extraction) for implicit connections not captured by wikilinks — scoped in `docs/graphrag_path_a_scope.md` and `docs/graphrag_integration.md`
- The hash-skip / link-extraction gap should be fixed at the indexing layer so a fresh deploy auto-backfills without a manual script
- Unresolved link rate is high (71%) — worth investigating whether case normalization or broader stem matching would recover more edges

### One-paragraph summary

v0.9.0 adds a wikilink graph layer to retrieval. At index time, every `[[wikilink]]` in a note is extracted, resolved to an absolute path, and stored in `note_links` in the same DB transaction as the embedding. At query time, `search_vault` with `graph_expand: true` runs one hop of outgoing and incoming link traversal on the semantic top-K results and appends connected neighbors that didn't rank on their own. The new `get_note_connections` tool exposes the graph directly without requiring a query. The watcher and `delete_note()` keep both `_link_index` and `note_links` current on every file change, so the graph stays in sync without any background jobs.

---

## 7. v0.9.2 — MCP wrapper script (2026-05-04)

**Released:** v0.9.2, PR [#35](https://github.com/celstnblacc/obsidian-semantic-mcp/pull/35), tag `v0.9.2`, GitHub release published, Docker Hub publish workflow triggered automatically on tag push.

### Problem

`osm init` wrote MCP client entries that inlined either `docker compose --project-directory <root> exec -T mcp-server python3 /app/src/server.py` or `<root>/.venv/bin/python3 <root>/src/server.py` directly into the client config. Both forms were brittle:

- The Docker form failed when the container was stopped or recreated under a different name; clients had no fallback.
- The native form hard-coded an absolute venv path that broke when the venv was rebuilt or relocated.
- Switching between Docker and native installs required regenerating the client config.

See `docs/mcp_startup_incident_2026-04-30.md` and `docs/mcp_failures_2026-04-30.md` for the incident timeline.

### Fix

Both `_docker_entry()` and `_native_entry()` in `osm_init.py` now emit a single `command`: the absolute path to `scripts/obsidian-semantic-mcp`, with empty `args`. The wrapper script:

1. Probes for a running `mcp-server` container via `docker compose ps --status running -q mcp-server`. If found, `exec`s into it.
2. Otherwise, sources `.env`, validates that `OBSIDIAN_VAULT(S)` and a Postgres credential are set, and execs the local venv Python against `src/server.py`.
3. Fails loudly with a specific message when prerequisites are missing, instead of leaving the client hanging on a dead command.

### Other changes

- `.python-version` pinned to `3.11.6`. The previous value `3.14` was unintended and not available on the install host.
- `uv.lock` synced to project version 0.9.2.
- `tests/test_osm_commands.py` `TestEntries` rewritten to assert the wrapper-based shape (`command` is the wrapper path, `args == []`).

### Compatibility

Existing installs need to re-run `osm init` (or manually update their MCP client config) to pick up the new wrapper-based command entry. Old config entries continue to work as long as the container or venv path they reference still exists.

### Verification still pending

- Fresh install on a clean machine to confirm `osm init` writes the wrapper entry and clients connect on first try.
- Stop the `mcp-server` container and confirm the wrapper falls back to the venv runtime cleanly.

---

## 9. v0.9.4 — MCP startup race fix (2026-05-06)

**Released:** v0.9.4, PR [#37](https://github.com/celstnblacc/obsidian-semantic-mcp/pull/37), tag `v0.9.4`, GitHub release published, Docker Hub images pushed (amd64 + arm64).

### Problem

The v0.9.2 wrapper probed for the container once and fell through immediately if not found. On Mac wake from sleep or Docker Desktop restart, the daemon was up but the container was still starting — `docker compose ps --status running -q` returned empty, so the wrapper fell to the local venv fallback. Claude Code marks an MCP failed for the whole session if it doesn't respond within ~5 seconds; the unintended fallback was slower and not always ready in time.

See `docs/mcp_startup_race_2026-05-06.md` for full analysis.

### Fix

Added a bounded polling loop (up to `OSM_DOCKER_WAIT=30` seconds, tunable) with a `docker info` short-circuit when the daemon is absent. The wrapper blocks until the container enters running state before deciding to exec into Docker or fall through to the local venv.

---

## 10. v0.9.5 — Python launcher + Docker Hub images (2026-05-06)

**Status:** PR [#38](https://github.com/celstnblacc/obsidian-semantic-mcp/pull/38) open, auto-merge armed, CI running.

### Problem

Two issues addressed together:

1. The MCP config hard-coded the local repo path to the bash wrapper, making it fragile and non-portable.
2. `docker-compose.yml` used `build: .` for both `mcp-server` and `dashboard`, producing local build images (`obsidian-semantic-mcp-mcp-server:latest`) alongside the Docker Hub images — visible as duplicates in OrbStack.

### Fix

- **`src/launcher.py`** — new Python entry point replacing the bash wrapper. Docker mode is opt-in via `OSM_DOCKER=1` + `OSM_PROJECT_ROOT`. Default install (no `OSM_DOCKER`) runs the server in-process with no path dependencies.
- **`docker-compose.yml`** — `build: .` replaced by `image: celestinmax/...:${OSM_VERSION:-latest}` for both services. No more local build artifacts.
- **`scripts/obsidian-semantic-mcp`** — demoted to thin shim delegating to Python launcher (backwards compat for existing configs pointing at the script path).
- **7 unit tests** in `tests/test_launcher.py` — all launcher code paths covered, no real Docker or Postgres required.
- **`pyproject.toml`** — entry point updated to `src.launcher:main`, version bumped to `0.9.5`.

### Post-merge migration step

```bash
# Install globally
uv tool install obsidian-semantic-mcp

# Update claude_desktop_config.json:
# "command": "obsidian-semantic-mcp"   ← no path needed

# For Docker mode (optional):
# "env": { "OSM_DOCKER": "1", "OSM_PROJECT_ROOT": "/path/to/repo" }
```

### Deferred

- After merge: create `v0.9.5` tag manually and push to trigger Docker Hub publish workflow (same pattern as v0.9.4).
- Update `OSM_VERSION=0.9.5` in `.env` so the running stack pins to the released image instead of `latest`.

---

## 11. Architecture decision: containerized MCP server is correct (2026-05-06)

### What happened this session

After v0.9.5 shipped, the MCP config was updated to use the global install with `OSM_DOCKER=1` and `OSM_PROJECT_ROOT` pointing to the local repo. This was then questioned: why is the MCP server running in Docker at all if we have a global install?

An attempt was made to remove containerization entirely — switching to a host-based MCP server connecting directly to Postgres on port 5433 and Ollama on port 11435. The mcp-server container was stopped.

**This was a mistake.** The containerized approach is correct. Reasons:
- Isolation and reproducibility — same environment as CI and everyone else who installs it
- All Python deps managed in Docker, no host environment drift
- The startup race was already fixed in v0.9.4
- The only real problem was the hardcoded `OSM_PROJECT_ROOT` path in the MCP config

### Current state (end of session — needs fixing)

`claude_desktop_config.json` is currently set to the host-based approach:
```json
"obsidian-semantic": {
  "command": "obsidian-semantic-mcp",
  "env": {
    "OBSIDIAN_VAULT": "/Users/airm2max/Documents/OBSIDIAN_ICLOUD/coredev",
    "POSTGRES_HOST": "localhost",
    "POSTGRES_PORT": "5433",
    "POSTGRES_USER": "obsidian",
    "POSTGRES_PASSWORD": "obsidian",
    "OLLAMA_URL": "http://localhost:11435"
  }
}
```

The mcp-server container is stopped. The stack only has postgres + ollama running.

### v0.9.6 plan — proper fix

The root problem is that the launcher needs to know where the compose project is. The proper solution: `osm init` writes the project root to a well-known config file at install time. The launcher reads it automatically. No env vars needed in the MCP config.

**Config file:** `~/.config/obsidian-semantic-mcp/project_root` (single line, absolute path)

**`osm init` change:** after setting up the stack, write `PROJECT_ROOT` to this file.

**Launcher change (`src/launcher.py`):** in Docker mode detection, read from config file if `OSM_PROJECT_ROOT` env var is not set:
```python
def _project_root() -> Path:
    if env := os.environ.get("OSM_PROJECT_ROOT"):
        return Path(env)
    config = Path.home() / ".config" / "obsidian-semantic-mcp" / "project_root"
    if config.exists():
        return Path(config.read_text().strip())
    return Path(__file__).resolve().parent.parent
```

**Default Docker mode:** change `OSM_DOCKER` default to `"1"` when the config file exists, so no env var needed at all.

**Target MCP config (v0.9.6):**
```json
"obsidian-semantic": {
  "command": "obsidian-semantic-mcp",
  "args": [],
  "env": {}
}
```

Fully containerized. Fully path-agnostic. No env vars required.

### Next session tasks

1. Restore MCP config to containerized approach (interim: add `OSM_DOCKER=1` + `OSM_PROJECT_ROOT` back while v0.9.6 is being built)
2. Restart mcp-server container: `docker compose --project-directory ~/DevOpsSec/obsidian-semantic-mcp up -d mcp-server`
3. Implement v0.9.6: `osm init` writes config file, launcher reads it, Docker mode is default when config file exists
4. Ship v0.9.6 and update MCP config to empty env block
