# Образ парсера: собирает новости из всех источников и пишет их в БД.
FROM python:3.14-slim

WORKDIR /app

# OCR для PDF-сканов: tesseract с русской моделью (страницы PDF в картинки
# переводит pypdfium2 из requirements.txt)
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-rus \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY news_collector.py rss_parser.py telegram_parser.py attachments.py \
     moex_parser.py sbercib_parser.py docs_parser.py \
     sources.py master_parser.py run_parser.py ./

# вывод print/tqdm сразу виден в docker logs, без буферизации
ENV PYTHONUNBUFFERED=1

CMD ["python", "run_parser.py"]
