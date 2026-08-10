FROM ghcr.io/astral-sh/uv:0.8.8 AS uv

FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --home-dir /app \
       --shell /usr/sbin/nologin app

COPY --from=uv /uv /uvx /usr/local/bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

USER app
EXPOSE 8001

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health/ready', timeout=3); urllib.request.urlopen('http://127.0.0.1:8001/', timeout=3)"]

CMD ["uvicorn", "erpnext_agent.main:app", "--host", "0.0.0.0", "--port", "8001", "--no-access-log"]
