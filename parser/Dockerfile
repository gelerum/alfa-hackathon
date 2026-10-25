# Образ парсера: собирает новости из всех источников и пишет их в общую БД.
FROM python:3.14-slim

# .venv/bin первым: `python` в контейнере — окружение из uv.lock
ENV PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH

# OCR для PDF-сканов: tesseract с русской моделью (страницы PDF в картинки
# переводит pypdfium2)
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-rus \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv==0.13.1
WORKDIR /app
# зависимости ставятся строго по uv.lock
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-cache

COPY common.py rss_parser.py telegram_parser.py attachments.py \
     moex_parser.py sbercib_parser.py docs_parser.py \
     sources.py master_parser.py embedder.py run_parser.py ./

CMD ["python", "run_parser.py"]
