# Operational Runbook

Quick reference for diagnosing and resolving incidents with Obsidian Semantic MCP.

## Service Health

```bash
osm status              # Overview of all services + Ollama + Claude config
docker compose ps       # Container states
docker compose logs -f   # Live logs (all services)
docker compose logs -f mcp-server  # MCP server only
```

## Common Incidents

### MCP server won't start

**Symptoms:** Container exits immediately or stays in restart loop.

```bash
docker compose logs mcp-server --tail 50
```

| Log message | Cause | Fix |
|-------------|-------|-----|
| `OBSIDIAN_VAULT not set` | Missing env var | Set `OBSIDIAN_VAULT` in `.env` or shell |
| `could not connect to server` | Postgres not ready | Wait for postgres healthcheck: `docker compose up -d postgres && sleep 10` |
| `Empty embedding returned` | Ollama model missing | `ollama pull nomic-embed-text` |
| `Connection refused` on Ollama URL | Ollama not running | Start Ollama or check `OLLAMA_URL` in `.env` |

### Postgres connection failures

```bash
# Check postgres is healthy
docker compose ps postgres

# Test connection from host
psql -h 127.0.0.1 -p 5433 -U obsidian -d obsidian_brain -c "SELECT 1"

# Check password
grep POSTGRES_PASSWORD .env
```

**Reset from scratch:**
```bash
docker compose down -v   # Wipes volumes — all indexed data lost
docker compose up -d     # Re-creates DB, re-indexes vault
```

### Dashboard shows "Service unreachable"

```bash
# Check dashboard container
docker compose logs dashboard --tail 20

# Check if dashboard can reach postgres
docker exec obsidian-semantic-mcp-dashboard-1 python3 -c "from config import build_dsn; print(build_dsn())"

# Restart dashboard only
docker compose restart dashboard
```

### Indexing is slow or stuck

First-run indexing: ~1-2 seconds per note with local Ollama. A 500-note vault takes 5-15 minutes.

```bash
# Watch progress
docker compose logs -f mcp-server | grep -i index

# Check dashboard for progress bar
open http://localhost:8484
```

**If stuck:**
```bash
docker compose restart mcp-server   # Re-triggers indexing from where it left off
```

### Ollama unreachable (remote mode)

```bash
# Re-establish SSH tunnel
osm tunnel

# Verify tunnel is up
curl http://localhost:11434/api/tags
```

### Claude Desktop can't connect to MCP server

```bash
# Verify MCP server is registered
osm status | grep "Claude Desktop"

# Check the config file
cat ~/Library/Application\ Support/Claude/claude_desktop_config.json | python3 -m json.tool

# Re-register
claude mcp add obsidian-semantic -- docker exec -i obsidian-semantic-mcp-mcp-server-1 python3 src/server.py

# Restart Claude Desktop after config changes
```

## Recovery Procedures

### Full reset (nuclear option)

Wipes all data and re-indexes from scratch:

```bash
osm remove --yes        # Stop services, wipe volumes, remove .env
osm init                # Re-run wizard from scratch
```

### Rollback to previous version

```bash
git checkout v0.3.4                        # Or any previous tag
docker compose up -d --build mcp-server dashboard
```

### Database backup/restore

```bash
# Backup
docker exec obsidian-semantic-mcp-postgres-1 pg_dump -U obsidian obsidian_brain > backup.sql

# Restore
docker exec -i obsidian-semantic-mcp-postgres-1 psql -U obsidian obsidian_brain < backup.sql
```

## Resource Limits

| Service | Memory limit | Expected usage |
|---------|-------------|----------------|
| postgres | 1 GB | 100-300 MB for typical vaults |
| mcp-server | 512 MB | 50-150 MB steady state, spikes during indexing |
| dashboard | 256 MB | 30-60 MB |
| ollama | 4 GB (if containerized) | Depends on model size |

If a container is OOM-killed, increase the limit in `docker-compose.yml` under `deploy.resources.limits.memory`.

## Monitoring

- **Dashboard:** http://localhost:8484 — real-time service status, indexing progress, search stats
- **Logs:** `docker compose logs -f` — all services, JSON format, rotated at 100MB x 3 files
- **Health checks:** Built into all services — `docker compose ps` shows health status
