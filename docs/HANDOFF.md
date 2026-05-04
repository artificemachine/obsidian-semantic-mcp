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
