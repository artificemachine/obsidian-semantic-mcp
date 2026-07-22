FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest@sha256:ecd4de2f060c64bea0ff8ecb182ddf46ba3fcccdc8a60cfdbaf20d1a047d7437 /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
# Wheel build (uv sync --no-editable) needs every force-included file present.
COPY src/ ./src/
COPY osm_init.py obsidian_semantic_mcp.py docker-compose.yml ./
RUN uv sync --frozen --no-dev --no-editable

# Put the venv on PATH so `python3 src/server.py` works directly
# (needed when MCP clients use `docker compose exec -T mcp-server ...`)
ENV PATH="/app/.venv/bin:$PATH"

# SECURITY: appuser UID may not match host vault owner — pass --build-arg UID=$(id -u) for bind mounts
RUN useradd -r -s /bin/false appuser
USER appuser

# MCP server (stdio) is the default entrypoint
CMD ["python3", "src/server.py"]
