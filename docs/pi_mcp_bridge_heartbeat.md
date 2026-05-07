# pi mcp-bridge: Heartbeat Must Start at Spawn Time

**Affects:** [earendil-works/pi](https://github.com/earendil-works/pi) with the
community `mcp-bridge.ts` extension.

**Symptom:** `pi` hangs on startup — `[mcp-bridge:obsidian-semantic] Starting...`
appears but `Registered N tools` never follows.  All other MCP servers
(voice-toolkit, tilth, serena, ...) register normally.

---

## Root Cause

obsidian-semantic uses a custom asyncio stdio transport that reads stdin with
a blocking synchronous loop:

```python
# src/server.py — _stdin_reader()
async def _stdin_reader():
    async with read_writer:
        for line in sys.stdin.buffer:   # ← blocking call, freezes event loop
            line_str = line.decode("utf-8").strip()
            if not line_str:
                await anyio.sleep(0)   # ← yields event loop on empty line
                continue
            ...
            await read_writer.send(SessionMessage(message))
```

`sys.stdin.buffer.readline()` is a **synchronous blocking call**.  Inside
asyncio's single-threaded model this freezes the entire event loop until the
next line of stdin arrives.  `_stdout_writer` — which flushes MCP responses to
stdout — can only run when the event loop is free.

The heartbeat (`heartbeat: true` in mcp.json) fixes this by sending `\n`
newlines to stdin every 200 ms.  Each empty line triggers `await anyio.sleep(0)`
which yields the event loop, letting `_stdout_writer` flush the queued response.

**The bug:** the community `mcp-bridge.ts` started the heartbeat *after*
receiving the `initialize` response — but `initialize` itself requires the same
yield mechanism to send its response.  The result is a deadlock on the very
first request:

```
Bridge sends initialize
→ _stdin_reader reads it, sends to MCP framework, hits `for line in sys.stdin.buffer:`
→ event loop frozen (no more stdin data)
→ MCP framework cannot process initialize (needs event loop)
→ initialize response never written
→ heartbeat never started (waiting for initialize response)
→ permanent deadlock
```

---

## Fix

Move the heartbeat timer from `initializeServer()` to `spawnServer()` so it
runs from the moment the process is spawned, covering the `initialize` exchange.

### In `~/.pi/agent/extensions/mcp-bridge.ts`

**In `spawnServer()`, after the `server` object is created:**

```typescript
  const server: RegisteredServer = {
    name: config.name,
    process: proc,
    requestId: 0,
    pending: new Map(),
    buffer: "",
  };

  // ADD THIS BLOCK ↓
  // Start heartbeat immediately at spawn time for servers that need it.
  // Servers using a blocking `for line in sys.stdin.buffer:` loop freeze their
  // asyncio event loop between reads. A periodic \n yields the event loop so
  // responses can be written — this must start before initialize, not after.
  if (config.heartbeat && proc.stdin) {
    const heartbeatTimer = setInterval(() => {
      try {
        proc.stdin?.write("\n");
      } catch {
        clearInterval(heartbeatTimer);
      }
    }, 200);
    proc.on("close", () => clearInterval(heartbeatTimer));
  }
  // ADD THIS BLOCK ↑
```

**In `initializeServer()`, replace the old heartbeat + try/finally block:**

```typescript
  // REMOVE this entire block ↓
  let heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  if (config.heartbeat && server.process.stdin) {
    heartbeatTimer = setInterval(() => {
      try {
        server.process.stdin?.write("\n");
      } catch {
        if (heartbeatTimer) clearInterval(heartbeatTimer);
      }
    }, 200);
  }

  try {
    const result = (await sendRequest(server, "tools/list")) as { tools: ToolDef[] };
    return result.tools ?? [];
  } finally {
    if (heartbeatTimer) clearInterval(heartbeatTimer);
  }
  // REMOVE this entire block ↑

  // REPLACE with just ↓
  const result = (await sendRequest(server, "tools/list")) as { tools: ToolDef[] };
  return result.tools ?? [];
```

---

## Automatic Application

`osm init` detects the community `mcp-bridge.ts` and applies this patch
automatically when it finds the unfixed version.  Run `osm init` again after
installing pi to apply it:

```bash
osm init
```

---

## Correct `~/.pi/agent/mcp.json` Entry

```json
{
  "name": "obsidian-semantic",
  "command": "docker",
  "args": [
    "compose",
    "--project-directory", "/absolute/path/to/obsidian-semantic-mcp",
    "exec", "-T", "mcp-server",
    "python3", "/app/src/server.py"
  ],
  "env": {},
  "heartbeat": true
}
```

`heartbeat: true` is **required**.  Without it obsidian-semantic's asyncio loop
never yields and no MCP responses reach the bridge.

`osm init` writes this entry automatically (with the correct project path) when
`pi` is detected on PATH.

---

## Verification

After applying the fix, run `pi --help` (or just `pi`) and confirm:

```
[mcp-bridge:obsidian-semantic] Starting...
[mcp-bridge:obsidian-semantic] Registered 11 tools: search_vault, ...
[mcp-bridge] Total: N tools from N servers
```

---

## Related

- `docs/mcp_raw_stdin_fix_2026-05-07.md` — why obsidian-semantic uses a
  blocking stdin loop instead of the standard anyio transport
- `docs/mcp_startup_race_2026-05-06.md` — separate Docker startup race condition
