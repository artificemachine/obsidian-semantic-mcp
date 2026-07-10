# handoff-osm.md — embed concurrency vs. ollama serving mismatch

**Filed:** 2026-07-10, from a sentinel-macos investigation session (not a code session in this repo).
**Status:** fixed — `EMBED_WORKERS` 4→1, `EMBED_TIMEOUT` 15s→30s (`src/server.py:99-100`), TDD (`TestEmbedConcurrencyDefaults` in `tests/test_unit.py`), full suite 332 passed. See CHANGELOG.md v0.14.5. Branch `fix/embed-concurrency-cpu-only-thrash`, not yet merged/pushed.

## What happened

`sentinel-macos`'s `overheat` correlator flagged host CPU pegged at 747% (`OrbStack Helper vmgr`) and system load average 191 on a 10-core Mac. Traced through `docker stats` into this project's compose stack: `obsidian-semantic-mcp-ollama-1` was sustained at 949-1223% CPU for 14h40m+ during a full vault reindex (walking 2023-era daily notes).

`docker logs obsidian-semantic-mcp-mcp-server-1` showed a continuous stream of:
```
embed attempt 1 failed: HTTPConnectionPool(host='ollama', port=11434): Read timed out. (read timeout=15) — retrying in 1s
embed attempt 2 failed: ... — retrying in 2s
embed_batch attempt 1 failed (16 items): ... — retrying in 1s
```

## Root cause

- `src/server.py:97` — `EMBED_WORKERS` defaults to 4 (parallel embedding threads on the client side).
- Confirmed live via `docker exec obsidian-semantic-mcp-ollama-1 ps aux`: the `llama-server` process ollama spawns is running with `-np 1` — a single request slot, CPU-only (no GPU passthrough in this OrbStack VM). This isn't set anywhere in this repo's compose/Dockerfile — it's ollama's own runtime default given the container's resource limits (`deploy.resources.limits.memory: 1g` in `docker-compose.yml`).
- Result: 3 of the 4 concurrent embed threads always queue behind the 1 active slot. Under load, queued calls exceed `EMBED_TIMEOUT=15s` (`src/server.py:96`), fail, and the exponential-backoff retry (`src/server.py:384`, `:422`) resubmits at the same 4-way concurrency — refilling the queue it just drained from. Self-reinforcing, not transient.
- The retry-once design at `src/server.py:738-746` even names the symptom ("Ollama tends to wedge under heavy concurrent load...") but the fix (retry once) doesn't touch the concurrency mismatch that causes it.

## Confirmed NOT a fix

Restarting the `ollama` container mid-thrash does not help — `mcp-server` resumes hammering it at full 4-way concurrency within seconds of the healthcheck passing again. CPU was back to 1084%+ almost immediately in the live test.

## Suggested fix (not yet applied — needs a plan_proposed pass before touching code)

1. Cap `EMBED_WORKERS` to ollama's effective parallel slot count for CPU-only deployments, or make it configurable per-deployment instead of a flat default of 4.
2. Raise `EMBED_TIMEOUT` for CPU-only inference so legitimate single-threaded embed latency under queueing isn't misclassified as a failure.
3. Consider a startup ramp-up in `mcp-server` (don't resume full-concurrency embed immediately after ollama's healthcheck flips healthy) to avoid re-triggering the storm right after any restart.

## Next session

Pick this up as a normal task: plan (TDD) → approve → implement → report. Not urgent (only bites during a full reindex on CPU-only ollama), but will recur on the next full reindex until fixed.
