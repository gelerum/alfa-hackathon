# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.13.1 AS uv
FROM python:3.12.13-slim

COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    HF_HOME=/app/.cache/huggingface \
    HF_XET_CACHE=/app/.cache/huggingface/xet \
    TOKENIZERS_PARALLELISM=false
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/app/.cache/uv \
    uv sync --locked --no-dev --no-managed-python --no-install-project
COPY retrieval_ranking ./retrieval_ranking
COPY main.py ./
RUN useradd --uid 10001 --create-home app \
    && mkdir -p /app/.cache/huggingface \
    && chown -R app:app /app/.cache \
    && chmod -R a+rX /app
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=15s --start-period=300s --retries=3 \
    CMD ["/app/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=10)"]
CMD ["/app/.venv/bin/uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
