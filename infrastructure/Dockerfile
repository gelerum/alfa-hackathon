FROM ghcr.io/astral-sh/uv:0.13.1 AS uv
FROM python:3.12-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home alpha \
    && mkdir /workspace /data /bank-data && chown -R alpha:alpha /workspace /data /bank-data

# Local sibling projects; no source files or Python project are required at the parent root.
# Their dependencies are resolved together by the deployment lockfile.
COPY service-common /workspace/service-common
COPY backend /workspace/backend
COPY tool-gateway /workspace/tool-gateway
COPY company-user-profiler /workspace/company-user-profiler
COPY insight-agent /workspace/insight-agent
COPY bank-materials-source /workspace/bank-materials-source
COPY frontend /workspace/frontend
WORKDIR /workspace/infrastructure
COPY infrastructure/pyproject.toml infrastructure/uv.lock ./
# COPY preserves restrictive host modes (e.g. 0600/0700); the runtime UID is different.
# Only application files are in /workspace; secrets and host venvs are excluded.
RUN uv sync --frozen --no-dev && chmod -R a+rX /workspace
ENV PATH="/workspace/infrastructure/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    FRONTEND_DIR=/workspace/frontend STORAGE_DIR=/data OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
USER alpha
# Fail the build instead of shipping an image that only root can import.
RUN python -c 'import backend_service.api, tool_gateway_service.api, profiler_service.api, insight_service.api, bank_source_service.api; import os; from pathlib import Path; (Path(os.environ["FRONTEND_DIR"]) / "index.html").read_text()'
CMD ["uvicorn", "backend_service.api:app", "--host", "0.0.0.0", "--port", "8000"]
