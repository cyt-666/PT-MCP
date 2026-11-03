FROM ghcr.io/astral-sh/uv:0.13.7-python3.12-trixie-slim AS uv
FROM python:3.12-slim-trixie

COPY --from=uv /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
RUN uv sync --locked --no-dev --no-editable

RUN useradd --system --uid 10001 --create-home ptmcp \
    && mkdir -p /app/data \
    && chown ptmcp:ptmcp /app/data

USER 10001:10001

EXPOSE 8000

ENTRYPOINT ["python", "-m", "pt_mcp"]
CMD ["--host", "0.0.0.0", "--port", "8000"]
