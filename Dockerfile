# Образ парсера: собирает новости из всех источников и пишет их в БД.
FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY news_collector.py rss_parser.py telegram_parser.py sources.py \
     master_parser.py run_parser.py ./

# вывод print/tqdm сразу виден в docker logs, без буферизации
ENV PYTHONUNBUFFERED=1

CMD ["python", "run_parser.py"]
