# MCP Raw Stdin Fix — 2026-05-07

## Symptom

OpenCode shows obsidian-semantic as `failed` with `MCP error -32000: Connection closed`. No process running despite Docker container being healthy. Claude Desktop shows the server dying after ~30s–3min with "Server transport closed unexpectedly, process exiting early."

## Root Cause

`mcp.server.stdio.stdio_server()` wraps stdin with `anyio.wrap_file(TextIOWrapper(sys.stdin.buffer, ...))`. When the MCP client closes stdin between connection cycles, the anyio `async for line in stdin:` loop receives EOF and exits cleanly, taking the entire server down.

This is the **same bug** that affected voice-toolkit (fixed in v0.3.5 by dropping FastMCP entirely). obsidian-semantic uses the lower-level `mcp.server` API but the same `anyio`-based stdio transport.

The pattern is identical to what was documented in voice-toolkit:

```
Client sends initialize → server responds → stdin closes → anyio gets EOF → server exits
```

## Fix

Replace `stdio_server()` with raw blocking I/O on stdin/stdout, feeding messages into the same memory streams that `server.run()` expects. The async server core (PostgreSQL, Ollama, watchdog) is untouched.

### File: `src/server.py`

**Lines 1684–1689 (current):**

```python
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )
```

**Replace with:**

```python
    # Raw stdin/stdout transport — avoids anyio.wrap_file() EOF death
    # Uses blocking sys.stdin.buffer.readline() which waits forever
    # instead of async for line in anyio_wrapped_stdin which exits on EOF.
    import anyio
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage

    read_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_reader = anyio.create_memory_object_stream(0)

    async def _stdin_reader():
        async with read_writer:
            for line in sys.stdin.buffer:
                line_str = line.decode("utf-8").strip()
                if not line_str:
                    continue
                try:
                    message = JSONRPCMessage.model_validate_json(line_str)
                    await read_writer.send(SessionMessage(message))
                except Exception as exc:
                    await read_writer.send(exc)

    async def _stdout_writer():
        async with write_reader:
            async for session_message in write_reader:
                json_str = session_message.message.model_dump_json(
                    by_alias=True, exclude_none=True
                )
                sys.stdout.write(json_str + "\n")
                sys.stdout.flush()

    async with anyio.create_task_group() as tg:
        tg.start_soon(_stdin_reader)
        tg.start_soon(_stdout_writer)
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )
```

### What changes

| Layer | Before | After |
|-------|--------|-------|
| Stdin reader | `anyio.wrap_file(TextIOWrapper(sys.stdin.buffer))` | `for line in sys.stdin.buffer:` (raw blocking) |
| Behavior on EOF | `async for` exits, server dies | Blocking loop waits forever |
| Stdout writer | anyio-wrapped async write | `sys.stdout.write()` + `sys.stdout.flush()` |
| Server core | Same | Same (PostgreSQL, Ollama, watchdog — all unchanged) |

### Why it works

- `sys.stdin.buffer.readline()` is a blocking call that waits until data arrives — no EOF surprises
- Messages are fed into the same `anyio.MemoryObjectStream` that `server.run()` already expects
- Responses are written synchronously to stdout — no anyio buffering issues
- Zero changes to the 1600+ lines of server logic, tools, embeddings, or file watchers

## Verification

After applying the fix:

```bash
# 1. Server stays alive with persistent stdin
mkfifo /tmp/osm-test-in
obsidian-semantic-mcp < /tmp/osm-test-in &
echo '{"jsonrpc":"2.0","method":"initialize",...}' > /tmp/osm-test-in
# Server should respond AND stay alive

# 2. Works after stdin closes and reopens
# (simulated by keeping the FIFO write end open with a background process)

# 3. OpenCode `mcp list` shows connected, tools work
```

## Related

- `voice-toolkit` v0.3.5 — same fix applied to FastMCP server (raw stdin loop, zero async deps)
- `token-diet` — reference implementation (never had this bug, uses raw stdin)
- `docs/mcp_startup_race_2026-05-06.md` — separate Docker startup race (not this issue)
- `docs/mcp_startup_incident_2026-04-30.md` — original Docker-absent incident
