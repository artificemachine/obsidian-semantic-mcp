# Obsidian Semantic MCP

A persistent memory layer for Claude Desktop — semantic search across your entire Obsidian vault using local embeddings and PostgreSQL + pgvector.

## The Problem

AI assistants forget everything between sessions. You repeat context, lose continuity, and start from zero every time. Your notes, projects, and preferences sit in Obsidian but never make it into your AI conversations automatically.

## The Solution

**Obsidian Semantic MCP** turns your vault into a queryable brain for Claude. It:

- Indexes every note as a vector embedding (via Ollama + `nomic-embed-text`)
- Stores embeddings in PostgreSQL with pgvector for fast semantic search
- Watches your vault for changes and re-indexes automatically
- Provides full vault CRUD (read, write, search, list) — works even when Obsidian is closed
- Exposes everything through MCP so Claude can retrieve and manage vault content on the fly

No cloud services. No API keys. Everything runs locally.

## Quick Start

This guide uses **Docker** (recommended for all platforms). Prefer running natively? Jump to [Native Install (macOS)](#native-install-macos).

**Before you start:**
- **`uv`** must be installed — `curl -LsSf https://astral.sh/uv/install.sh | sh` (macOS/Linux) or `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"` (Windows)
- **Docker Desktop** must be installed and running (Windows: enable WSL2 backend)
- Know your vault path — in Obsidian: Settings → About → Vault location

### 1. Clone and run the setup wizard

```bash
git clone https://github.com/celstnblacc/obsidian-semantic-mcp.git && cd obsidian-semantic-mcp
uv sync
uv run osm init
```

> **Tip:** Run `uv run osm init --dry-run` first to preview every action without making any changes.
>
> `scripts/osm` is a direct wrapper — if you prefer not to use `uv run`, `scripts/osm init` works identically without activating the venv.
>
> **One server, all projects:** `obsidian-semantic` is registered globally — running `osm init` from any other project is safe and idempotent. If already configured, it skips registration and informs you.
>
> **Exit the wizard at any prompt:** type `q`, `quit`, `exit`, or `skip` — or press `Ctrl+C`.

The wizard detects your OS and asks which installation mode you want:

**macOS:**
```
  1)  Native              Homebrew + local Postgres + local Ollama
  2)  Docker + host Ollama    Postgres in Docker, Ollama already on this Mac
  3)  Full Docker         Everything in containers  (recommended)
  4)  Docker + remote Ollama  Postgres in Docker, Ollama on another machine
```

**Linux:**
```
  1)  Docker + host Ollama    Postgres in Docker, Ollama on this machine
  2)  Full Docker         Everything in containers  (recommended)
  3)  Docker + remote Ollama  Postgres in Docker, Ollama on another machine
```

**Windows (requires Docker Desktop with WSL2 backend):**
```
  1)  Docker + host Ollama    Postgres in Docker, Ollama already on this PC
  2)  Full Docker         Everything in containers  (recommended)
  3)  Docker + remote Ollama  Postgres in Docker, Ollama on another machine
```

> **Which mode?** Pick **Full Docker** (mode 3 on macOS, mode 2 on Linux/Windows) unless you already have Ollama running locally — in that case pick **Docker + host Ollama** to avoid re-downloading the ~7GB model.

It then:
- Installs prerequisites (Homebrew packages or Docker images)
- Pulls `nomic-embed-text` if needed
- Writes a `.env` file (gitignored) with your vault path and credentials
- Updates `claude_desktop_config.json` automatically

### 2. Add to Claude Code

```bash
claude mcp add obsidian-semantic -- docker exec -i obsidian-semantic-mcp-mcp-server-1 python3 src/server.py
```

### 3. Add to Claude Desktop

Add to `$HOME/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "obsidian-semantic": {
      "command": "docker",
      "args": ["exec", "-i", "obsidian-semantic-mcp-mcp-server-1", "python3", "src/server.py"]
    }
  }
}
```

### 4. Restart Claude Desktop

The server indexes your vault on first run, then watches for changes automatically.

> **First-run indexing takes roughly 1–2 seconds per note** — expect 5–15 minutes for a 500-note vault. Monitor progress at http://localhost:8484. Claude will return no results until indexing completes.

> Prefer running without Docker? See [Native Install (macOS)](#native-install-macos).
> Want to skip the wizard? See [Manual start](#manual-start-without-wizard).

---

### Manual start (without wizard)

```bash
OBSIDIAN_VAULT="/path/to/your/vault" POSTGRES_PASSWORD=obsidian docker compose up -d
```

> Docker Compose also reads a `.env` file in the repo root (gitignored).

First run pulls all images and the `nomic-embed-text` model automatically. This starts:

| Service | Port | Description |
|---------|------|-------------|
| PostgreSQL + pgvector | 5433 | Vector storage (avoids conflict with host pg) |
| Ollama | 11435 | Local embeddings (auto-pulls model) |
| MCP server | stdio | Claude Desktop connects via `docker exec` |
| Dashboard | 8484 | http://localhost:8484 |

### Useful commands

```bash
# View server logs
docker compose logs -f mcp-server

# Rebuild after code changes
docker compose up -d --build mcp-server dashboard

# Stop everything
docker compose down

# Stop and wipe all data (re-index from scratch)
# ⚠️  WARNING: -v deletes all indexed embeddings. Re-indexing will restart from scratch.
docker compose down -v
```

### GPU support (optional)

For faster embeddings on Linux with NVIDIA GPU, add to the `ollama` service in `docker-compose.yml`:

```yaml
deploy:
  resources:
    reservations:
      devices:
        - capabilities: [gpu]
```

---

## Using with Claude

Once the MCP server is connected, Claude can access your vault directly — no special syntax needed. Just talk to it naturally.

### Example prompts

```
"Search my notes for anything about project X"
"What did I write about ketosis last month?"
"Find my notes on the Zettelkasten method"
"Read my Daily/2026-03-14.md note"
"Append this meeting summary to my inbox note"
"Write a new note at Projects/obsidian-mcp.md with this content"
"List all files in my Fleeting folder"
"Show me what's been modified recently"
"Re-index my vault"
```

Claude will automatically choose the right MCP tool (`search_vault`, `get_file`, `write_file`, etc.) based on your request.

### osm — Setup & management CLI

Use `osm` to set up, manage, and tear down the stack. The wizard installs all prerequisites, configures Docker, and updates Claude Desktop automatically.

| Command | Description |
|---------|-------------|
| `osm init` | Interactive setup wizard |
| `osm init --mode <1-4> --vault <path>` | Non-interactive setup (agent/script friendly) |
| `osm init --dry-run` | Preview all actions without making any changes |
| `osm status` | Check service health (Docker, Ollama, Claude Desktop) |
| `osm rebuild` | Rebuild Docker images after a code change |
| `osm tunnel` | Reconnect SSH tunnel (remote Ollama mode) |
| `osm remove` | Stop services and wipe all volumes and config |
| `osm remove --yes` | Non-interactive teardown (agent/script friendly) |
| `osm help` | Full flag reference |

**`osm init` flags:** `--mode`, `--vault`, `--pg-password`, `--persistent` / `--no-persistent`, `--data-dir`, `--ssh-host`, `--ssh-user`, `--ssh-port`, `--ssh-key`, `--vault-remote`

## Architecture

```
Claude Desktop
    ↓ MCP protocol (stdio)
src/server.py (unified MCP server)
    ├── Semantic search (pgvector cosine similarity)
    ├── Vault CRUD (direct filesystem access)
    └── Live file watcher (watchdog)
    ↓
PostgreSQL + pgvector (vector storage + IVFFlat index)
    ↓
Ollama / nomic-embed-text (local 768-dim embeddings)
    ↓
Your Obsidian vault (e.g. $HOME/Documents/MyVault)
```

## Project Structure

```
obsidian-semantic-mcp/
├── scripts/
│   └── osm                # CLI wrapper — `uv run osm init` or `scripts/osm init`
├── src/
│   ├── server.py          # MCP server — semantic search + vault CRUD (10 tools)
│   └── dashboard.py       # Monitoring dashboard (http://localhost:8484)
├── tests/
│   ├── test_unit.py            # Unit tests (no real DB/Ollama needed)
│   ├── test_osm_init.py        # Unit tests for the osm CLI wizard
│   ├── test_dashboard_smoke.py # Dashboard static analysis + live HTTP smoke tests
│   ├── test_setup.py           # Prerequisites checker (deps, DB, Ollama) — run directly
│   └── test_e2e.py             # End-to-end MCP protocol test — run directly
├── osm_init.py            # Interactive setup wizard (used by scripts/osm)
├── Dockerfile             # Python 3.13 + uv
├── docker-compose.yml     # Full stack: postgres, ollama, server, dashboard
├── pyproject.toml         # Project metadata + dependencies (osm script entry)
├── uv.lock                # Pinned lockfile
└── LICENSE                # Apache 2.0
```

## Prerequisites

- An Obsidian vault on your filesystem
- **macOS native:** Homebrew (auto-installs everything else)
- **Docker modes:** Docker Desktop (macOS/Linux/Windows)
- **Windows:** Docker Desktop with WSL2 backend enabled, `uv` installed via `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`

## MCP Tools

### Semantic Search

| Tool | Description |
|------|-------------|
| `search_vault` | Semantic search by meaning across the entire vault. Returns ranked excerpts with similarity scores. |
| `simple_search` | Exact text/keyword search across vault files. |

### Vault Management

| Tool | Description |
|------|-------------|
| `list_files` | List files and directories in a vault directory. |
| `get_file` | Read the full content of a single file. |
| `get_files_batch` | Read multiple files at once. |
| `append_content` | Append content to a file (creates if missing). |
| `write_file` | **Overwrites** the target file completely — existing content is replaced with no undo. Use `append_content` if you want to add to an existing file instead. |
| `recent_changes` | Get recently modified files. |

### Index Management

| Tool | Description |
|------|-------------|
| `list_indexed_notes` | List all indexed notes with their last indexed timestamp. |
| `reindex_vault` | Force a full re-index of all notes. Runs in the background. |

## Multi-Vault Support

To index multiple vaults, set `OBSIDIAN_VAULTS` to a comma-separated list of absolute paths:

```bash
OBSIDIAN_VAULTS="/path/to/vault1,/path/to/vault2" docker compose up -d
```

Or in docker-compose.yml (uncomment the multi-vault lines):

```yaml
environment:
  OBSIDIAN_VAULTS: /vault1,/vault2   # multi-vault
volumes:
  - /path/to/vault1:/vault1
  - /path/to/vault2:/vault2
```

When using multiple vaults, the `search_vault` MCP tool gains a `vault` parameter to filter results by vault name. The dashboard also shows a vault selector. Each vault is watched and indexed independently.

## How It Works

1. **Indexing** — On startup, the server walks your vault, reads each `.md` file, generates a 768-dim embedding via Ollama, and upserts it into PostgreSQL with pgvector. Unchanged files (same SHA-256 hash) are skipped on subsequent runs. **First-run indexing takes roughly 1–2 seconds per note** with a local Ollama instance — expect 5–15 minutes for a 500-note vault. Watch progress at http://localhost:8484 or via `docker compose logs -f mcp-server`.
2. **Watching** — A file watcher (`watchdog`) monitors the vault for creates, updates, deletes, and moves — re-embedding changed files automatically.
3. **Searching** — When Claude calls `search_vault`, the query is embedded and matched against stored vectors using cosine similarity (IVFFlat index). The top results are returned with similarity scores and content previews.
4. **CRUD** — All file operations use direct filesystem access, so the server works whether Obsidian is open or not. Path traversal outside the vault is blocked.

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `OBSIDIAN_VAULT` | Absolute path to your Obsidian vault. Mounted **read-write** so MCP write/append tools can create and update notes. Mount with `:ro` if you only need search. | *required* |
| `OBSIDIAN_VAULTS` | Comma-separated list of vault paths for multi-vault mode. Overrides `OBSIDIAN_VAULT` when set. | — |
| `POSTGRES_PASSWORD` | PostgreSQL password (Docker) | *required for Docker* |
| `DATABASE_URL` | Full connection string (overrides POSTGRES_* vars) | built from POSTGRES_* vars |
| `POSTGRES_HOST` | PostgreSQL host | `localhost` |
| `POSTGRES_PORT` | PostgreSQL port | `5432` |
| `POSTGRES_DB` | Database name | `obsidian_brain` |
| `POSTGRES_USER` | Database user | `obsidian` |
| `OLLAMA_URL` | Ollama API endpoint | `http://localhost:11434` |
| `EMBEDDING_MODEL` | Ollama model for embeddings | `nomic-embed-text` |
| `EMBED_WORKERS` | Parallel threads for bulk indexing | `4` |
| `RERANK_MODEL` | Optional Ollama model for cross-encoder re-ranking (e.g. `llama3.2`). Disabled when empty. | — |
| `RERANK_CANDIDATES` | Candidate pool size fetched before re-ranking | `20` |

## Monitoring Dashboard

A built-in dashboard is available at http://localhost:8484 (started automatically with Docker). It shows:

- Service health (PostgreSQL, Ollama, embedding model)
- Indexed notes count, vault coverage, DB size
- Recently indexed files
- **Re-index** — incremental re-index (skips unchanged notes, fast)
- **Clear & Rebuild** — wipes all embeddings and re-indexes from scratch
- A "Start Ollama" button if Ollama is down

To run the dashboard without Docker:

```bash
OBSIDIAN_VAULT="/path/to/your/vault" uv run python3 src/dashboard.py
```

## Testing

### Unit tests (no real DB or Ollama needed)

```bash
uv run pytest -q
```

Runs 188 fast unit tests covering embedding, search, vault path safety, connection pool, and the osm CLI wizard.

### `test_dashboard_smoke.py` — Dashboard health checks (Docker stack)

Offline static analysis (always runs — no services needed) + live HTTP smoke tests (auto-skipped when the dashboard is unreachable).

```bash
# Offline + live (stack must be running)
uv run pytest tests/test_dashboard_smoke.py -v

# Target a remote instance
DASHBOARD_URL=http://host:8484 uv run pytest tests/test_dashboard_smoke.py -v
```

Checks: JS string safety, DOM element completeness, `/api/stats` schema, service health, response time, and routing.

### `test_setup.py` — Prerequisites check (native installs)

Verifies Python deps, vault path, PostgreSQL + pgvector, Ollama, and embedding smoke test. Run directly — not via pytest.

```bash
OBSIDIAN_VAULT="/path/to/your/vault" uv run python3 tests/test_setup.py
```

### `test_e2e.py` — End-to-end MCP test (native installs)

Launches the server, initializes MCP protocol, waits for indexing, runs semantic search, and verifies results. Run directly — not via pytest.

```bash
OBSIDIAN_VAULT="/path/to/your/vault" uv run python3 tests/test_e2e.py
```

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `uv: command not found` | `uv` not installed or not in PATH | Run `curl -LsSf https://astral.sh/uv/install.sh \| sh` then restart your terminal |
| `Cannot connect to the Docker daemon` | Docker Desktop not running | Start Docker Desktop from Applications (macOS) or system tray (Windows/Linux) |
| `Permission denied: /path/to/vault` | Vault path not readable by Docker | On macOS: Docker Desktop → Settings → Resources → File Sharing — add your vault path |
| `ModuleNotFoundError: No module named 'mcp'` | System Python instead of venv | Use `.venv/bin/python3` in config, or use Docker |
| `ModuleNotFoundError: No module named 'psycopg2'` in Docker | Container built before venv PATH fix | `docker compose up -d --build mcp-server` |
| `Search returns 0 results` | IVFFlat index built on empty table | Run `psql obsidian_brain -c "REINDEX INDEX notes_embedding_idx;"` |
| `Vault indexing is in progress — no results yet` | First-boot indexing not complete | Wait for indexing to finish (check `docker compose logs -f mcp-server`) |
| `Cannot reach Ollama` | Ollama not running | Run `ollama serve` or `docker compose up ollama` |
| `Skipped <file>: vector must have at least 1 dimension` | Ollama returned empty embedding (blank/tiny file) | Expected — file is skipped and indexing continues |
| `Skipped <file>: 500 Server Error` | Ollama internal error (file too large or model issue) | Expected — file is skipped; try `ollama pull nomic-embed-text` to refresh model |
| `pgvector extension not found` | Not installed for your PG version | Use Docker, or build from source (see native install) |
| Server crashes on startup | `OBSIDIAN_VAULT` not set | Set the env var in your config or docker compose command |
| Docker container can't see vault | Wrong path or missing volume | Ensure `OBSIDIAN_VAULT` is an absolute path accessible to Docker |

---

## Native Install (macOS)

If you prefer running without Docker:

### 1. Clone and install

```bash
git clone https://github.com/celstnblacc/obsidian-semantic-mcp.git && cd obsidian-semantic-mcp
uv sync
```

### 2. Install system dependencies

```bash
brew install postgresql@17 pgvector ollama
brew services start postgresql@17
ollama serve &
ollama pull nomic-embed-text
```

> **PostgreSQL 16:** Homebrew's `pgvector` bottle requires pg17 or pg18. If you must use pg16, build pgvector from source:
> ```bash
> cd /tmp
> git clone --branch v0.8.2 --depth 1 https://github.com/pgvector/pgvector.git
> cd pgvector
> make PG_CONFIG=$(brew --prefix postgresql@16)/bin/pg_config
> make install PG_CONFIG=$(brew --prefix postgresql@16)/bin/pg_config
> ```

### 3. Set up the database

```bash
createdb obsidian_brain
psql obsidian_brain -c "CREATE EXTENSION vector;"
```

### 4. Verify

```bash
OBSIDIAN_VAULT="/path/to/your/vault" uv run python3 tests/test_setup.py
```

### 5. Configure Claude Desktop

Add to `$HOME/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "obsidian-semantic": {
      "command": "/absolute/path/to/obsidian-semantic-mcp/.venv/bin/python3",
      "args": ["/absolute/path/to/obsidian-semantic-mcp/src/server.py"],
      "env": {
        "OBSIDIAN_VAULT": "/absolute/path/to/your/vault",
        "DATABASE_URL": "postgresql://localhost/obsidian_brain"
      }
    }
  }
}
```

> **Important:** Use `.venv/bin/python3` — not system Python. Homebrew Python won't have the required packages.

### 6. Restart Claude Desktop

The server indexes your vault on first run, then watches for changes automatically.

## Cost

Everything runs locally. No cloud APIs, no subscriptions. The only cost is disk space for the database (~a few MB for most vaults).

## License

Apache 2.0
