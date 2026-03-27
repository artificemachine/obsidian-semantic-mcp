FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-editable

# Put the venv on PATH so `python3 src/server.py` works directly
# (needed when Claude Desktop uses `docker exec ... python3 src/server.py`)
ENV PATH="/app/.venv/bin:$PATH"

COPY src/ src/
COPY obsidian_semantic_mcp.py ./

# SECURITY: appuser UID may not match host vault owner — pass --build-arg UID=$(id -u) for bind mounts
RUN useradd -r -s /bin/false appuser
USER appuser

# MCP server (stdio) is the default entrypoint
CMD ["python3", "src/server.py"]
