FROM python:3.13-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.10 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Headless Chromium for browser_json recipes and the discovery agent.
RUN playwright install --with-deps chromium && rm -rf /var/lib/apt/lists/*

COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --create-home app
USER app

EXPOSE 8765
# Listens on $PORT when the platform sets it (Railway does), else 8765.
CMD ["t212-mcp", "--http", "--host", "0.0.0.0"]
