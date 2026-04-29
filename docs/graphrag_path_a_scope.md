# Path A — Wikilink Graph Augmentation: Implementation Scope

## What It Does

After `search_vault` returns top-K semantic/hybrid results, the graph expansion step
looks up wikilinks stored for those notes and fetches their neighbors — notes you linked
to or that link back to them — that didn't appear in the original result set.

This surfaces missed connections: notes that share an explicit link relationship with a
result but weren't semantically similar enough to rank in the top-K.

---

## Integration Points in `server.py`

### 1. New DB table — `note_links`

Created alongside `notes` in `init_db()`.

```sql
CREATE TABLE IF NOT EXISTS note_links (
    source_path TEXT NOT NULL,
    target_name TEXT NOT NULL,   -- [[link text]] as written
    target_path TEXT,            -- resolved absolute path (NULL if unresolvable)
    PRIMARY KEY (source_path, target_name)
);
CREATE INDEX IF NOT EXISTS note_links_target_idx ON note_links (target_path);
```

One row per `[[wikilink]]` found in a note. `target_path` is filled at index time by
resolving the link name against the vault file tree.

---

### 2. Wikilink extraction — new helper

```python
_WIKILINK_RE = re.compile(r'\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]')

def extract_wikilinks(content: str) -> list[str]:
    return [m.group(1).strip() for m in _WIKILINK_RE.finditer(content)]
```

Handles `[[note]]`, `[[note|alias]]`, `[[note#heading]]`, `[[folder/note]]`.

---

### 3. Link resolution — new helper

```python
def _build_link_index(vault: str) -> dict[str, str]:
    """Map lowercase note name → absolute path for all .md files in vault."""
    index = {}
    for f in Path(vault).rglob("*.md"):
        if not _should_skip_path(f):
            index[f.stem.lower()] = str(f)
    return index
```

Called once per vault at index time. Resolves `[[Resilience Patterns]]` →
`/vault/notes/concepts/Resilience Patterns.md`.

Obsidian's resolution rules:
- Case-insensitive stem match
- Short name (`[[note]]`) matches any depth (`folder/note.md`)
- Full path match takes priority if ambiguous

---

### 4. Link upsert — modify `_upsert_note()`

After the existing `INSERT ... ON CONFLICT DO UPDATE`, extract and store links:

```python
links = extract_wikilinks(content)
resolved = _resolve_links(links, link_index)   # dict[name → path|None]

# Delete stale links for this source, then bulk insert fresh ones
cur.execute("DELETE FROM note_links WHERE source_path = %s", (path,))
if resolved:
    cur.executemany(
        "INSERT INTO note_links (source_path, target_name, target_path) VALUES (%s, %s, %s)",
        [(path, name, tgt) for name, tgt in resolved.items()]
    )
```

The link index (`dict[str, str]`) must be passed through to `_upsert_note`. Thread-safe
because it's read-only after build.

---

### 5. Graph expansion — new function

```python
def expand_via_links(paths: list[str], hop: int = 1) -> list[tuple[str, str, str]]:
    """Return notes reachable within `hop` steps from `paths` via note_links.

    Returns (path, content, reason) tuples where reason is the link path
    that connected them. Excludes nodes already in `paths`.
    """
    seen = set(paths)
    frontier = set(paths)
    expansions: list[tuple[str, str, str]] = []

    for _ in range(hop):
        if not frontier:
            break
        with db_conn() as conn:
            with conn.cursor() as cur:
                # Outgoing: notes this result links to
                cur.execute("""
                    SELECT nl.target_path, n.content, nl.source_path
                    FROM note_links nl
                    JOIN notes n ON n.path = nl.target_path
                    WHERE nl.source_path = ANY(%s)
                      AND nl.target_path IS NOT NULL
                      AND nl.target_path != ALL(%s)
                """, (list(frontier), list(seen)))
                out_rows = cur.fetchall()

                # Incoming: notes that link back to this result
                cur.execute("""
                    SELECT nl.source_path, n.content, nl.target_path
                    FROM note_links nl
                    JOIN notes n ON n.path = nl.source_path
                    WHERE nl.target_path = ANY(%s)
                      AND nl.source_path != ALL(%s)
                """, (list(frontier), list(seen)))
                in_rows = cur.fetchall()

        new_frontier = set()
        for row in out_rows + in_rows:
            p, content, via = row
            if p not in seen:
                expansions.append((p, content, via))
                seen.add(p)
                new_frontier.add(p)
        frontier = new_frontier

    return expansions
```

---

### 6. Wire into `search_vault` tool

Add an optional `graph_expand` boolean parameter (default: `false` to preserve current
behavior). When true, append graph-expanded notes after the semantic results.

**Schema change in `list_tools()`:**
```python
"graph_expand": {
    "type": "boolean",
    "description": "Expand results by following wikilinks from top matches. "
                   "Surfaces connected notes that didn't rank semantically.",
    "default": False,
},
```

**In `call_tool()` after the existing result-formatting block:**
```python
if arguments.get("graph_expand") and results:
    result_paths = [r[0] for r in results]
    neighbors = await loop.run_in_executor(None, expand_via_links, result_paths, 1)
    if neighbors:
        parts.append("\n**Wikilink neighbors:**\n")
        for path, content, via in neighbors:
            rel = _relative(Path(path))
            via_rel = _relative(Path(via))
            preview = content[:300].strip()
            parts.append(f"**{rel}** _(linked from/to {via_rel})_\n\n{preview}\n")
```

---

### 7. New MCP tool — `get_note_connections`

Exposes the graph directly without requiring a search query first.

```
Tool: get_note_connections
Input: { "filepath": "notes/concepts/resilience.md", "hops": 1 }
Output: all notes within N link-hops, with direction (links to / linked from)
```

Useful for "what does this note connect to?" without going through semantic search.

---

## Files Changed

| File | Change |
|---|---|
| `src/server.py` | `init_db()` — add `note_links` table |
| `src/server.py` | `_upsert_note()` — extract + store wikilinks |
| `src/server.py` | `index_note()` — pass link index through |
| `src/server.py` | `index_vault()` — build link index once per vault |
| `src/server.py` | `_build_link_index()` — new helper |
| `src/server.py` | `extract_wikilinks()` — new helper |
| `src/server.py` | `expand_via_links()` — new function |
| `src/server.py` | `list_tools()` — add `graph_expand` param + `get_note_connections` tool |
| `src/server.py` | `call_tool()` — wire expansion + new tool handler |

No new files required. No changes to `config.py`, `dashboard.py`, Docker setup, or tests
beyond adding tests for the new helpers.

---

## What Stays Unchanged

- All three search modes (`hybrid`, `semantic`, `keyword`) are unchanged
- Re-ranking pipeline is unchanged
- Watcher (`VaultEventHandler`) already calls `index_note()` on every file change —
  link extraction will stay in sync automatically
- Cache invalidation works as-is (cache key doesn't include `graph_expand`, which is fine
  since the expansion result depends on `note_links` which is updated on each index)

---

## Limitations

- Links to notes not yet indexed (`target_path = NULL`) are stored but not expandable
  until the target note is indexed
- Multi-hop (`hops > 1`) can return many nodes — recommend keeping `hops = 1` by default
- Does not extract tags, frontmatter YAML links, or inline dataview queries — only `[[wikilinks]]`

---

## Estimated Effort

| Task | Estimate |
|---|---|
| DB migration + `note_links` table | 30 min |
| `extract_wikilinks` + `_build_link_index` | 1 hour |
| Modify `_upsert_note` + `index_vault` | 1 hour |
| `expand_via_links` function | 1 hour |
| Wire into `search_vault` + new tool | 1 hour |
| Tests | 1.5 hours |
| **Total** | **~6 hours** |

No new dependencies. No changes to Docker images or Ollama. Requires a one-time
`reindex_vault` call after deploy to populate `note_links` for existing notes.
