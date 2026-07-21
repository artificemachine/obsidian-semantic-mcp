# Proposed rename: `obsidian-semantic-mcp` → `mnemosyne-mcp`

*Date: 2026-05-18*
*Status: proposal only — no action taken yet. This is the daily-driver vault MCP and is currently working.*

---

## TL;DR

The current name `obsidian-semantic-mcp` is the outlier in the user's stack — every other custom project (trayzury, ozarys, nocture, hablatone, skwad, tessera, OpenClaw) uses a single evocative word, not a descriptive compound.

The proposed replacement is **`mnemosyne-mcp`** — the Greek Titaness of memory, mother of the nine Muses, daughter of Gaia and Uranus.

The candidate `mnemos-mcp` was also considered but rejected because `mnemos` is a truncation/back-formation, not a real standalone Greek word. `mnemosyne` is the proper noun.

---

## Why `mnemosyne`

| Reason | Detail |
|---|---|
| Real proper noun with meaning | Greek goddess of memory — literally what this MCP provides |
| Single evocative word | Matches the user's broader naming style (trayzury, ozarys, hablatone, ...) |
| Mythological gravitas | In Greek myth, drinking from Mnemosyne's spring in Hades let souls retain knowledge across lives. The vault is that spring; this MCP is the drinking. |
| Length parity with other names | 9 chars, same as `hablatone` — not an outlier |
| Pairs cleanly with `tessera-mcp` | Greek goddess + Latin tile — both ancient, both evocative, both single words. Clear siblings. |

## Why not `mnemos`

| Reason | Detail |
|---|---|
| Not a real word | `mnemos` is a back-formation / truncation, not a standalone Greek word |
| Less recognizable | Reads as a placeholder rather than a meaningful name |
| Saves only 3 chars | Marginal benefit for losing the mythological reference |

## Mental model for the rename

```
mnemosyne (the goddess of memory)        ←  indexes the written memory (.md notes)
tessera (the mosaic tiles)               ←  indexes everything else (PDFs, images, audio, code, ...)
```

Greek + Latin, goddess + tile, written + visual — two siblings covering the full vault between them.

## Why this rename is NOT urgent

`obsidian-semantic-mcp` is the user's daily driver:
- Currently registered: `obsidian-semantic ✓ Connected` in `claude mcp list`
- Docker-based deployment with a dashboard
- ~2,800 lines of Python
- Active maintenance (v0.11.0 series, transport stabilization recently)

A rename touches all of: folder name, `pyproject.toml`, Python module, Docker compose service names, Claude Code registration, vault notes that reference it, the global CLAUDE.md if it appears there.

**Defer the rename to a quiet moment with no in-flight changes.** It is cosmetic; the project works fine under its current name.

## When you do rename

Rough checklist:

1. `mv ~/DevOpsSec/obsidian-semantic-mcp ~/DevOpsSec/mnemosyne-mcp`
2. Rename Python package directory: `obsidian_semantic_mcp/` → `mnemosyne_mcp/` (if applicable)
3. Update `pyproject.toml`: `name = "mnemosyne-mcp"`
4. Update `docker-compose.yml` service names if present
5. Update `install.sh` / `install.ps1` paths and registration calls
6. Update `README.md`, `CLAUDE.md`, `AGENTS.md`, all docs
7. Update `~/.git-push-allowlist` entry
8. `claude mcp remove obsidian-semantic && claude mcp add mnemosyne ...`
9. Grep `~/Documents/OBSIDIAN_ICLOUD/coredev` for `obsidian-semantic-mcp` references and update
10. Update the global `~/.claude/CLAUDE.md` if the name appears there

Estimated effort: 30-60 minutes including verification.

## See also

- `microsoft_agent_framework/docs/naming-mnemos-mnemosyne.md` — full naming rationale (this doc's sibling)
- `microsoft_agent_framework/docs/mcp-vs-maf-vs-superharness-three-roles.md` — where this MCP fits in the broader stack
