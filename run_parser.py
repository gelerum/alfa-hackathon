"""Сбор новостей в общую БД.

По умолчанию работает постоянно: проход по всем источникам, затем эмбеддинги
новых новостей, пауза PARSE_INTERVAL_MINUTES (по умолчанию 30 минут), снова
проход. Так его запускает Docker (`docker compose up -d`).
С флагом --once делает один проход и завершается.

Переменные окружения:
  DATABASE_URL                — общая БД (репозиторий database);
  EMBEDDING_BASE_URL          — embedding-service; не задан — эмбеддинги не считаются;
  EMBEDDING_CONFIG_KEY        — ключ embedding_configs, общий с профайлером;
  EMBED_TIME_BUDGET_MINUTES   — сколько минут за проход тратить на эмбеддинги (20).
"""

import datetime
import os
import signal
import sys
import time

from dotenv import load_dotenv

from embedder import DEFAULT_CONFIG_KEY, Embedder
from master_parser import Master_Parser
from sources import ALL_PARSERS


def run_once(mp: Master_Parser, embedder: Embedder | None):
    started = time.monotonic()
    print(f'=== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} сбор начат', flush=True)
    stats = mp.run()
    print(f'=== добавлено всего: {sum(stats.values())} из {len(stats)} источников '
          f'за {time.monotonic() - started:.0f} с', flush=True)
    if embedder is not None:
        started = time.monotonic()
        done = embedder.run()
        print(f'=== эмбеддингов записано: {done} за {time.monotonic() - started:.0f} с', flush=True)


def main():
    load_dotenv()
    dsn = os.environ['DATABASE_URL']
    mp = Master_Parser(dsn, ALL_PARSERS)
    embedder = None
    if os.environ.get('EMBEDDING_BASE_URL'):
        embedder = Embedder(
            dsn, os.environ['EMBEDDING_BASE_URL'],
            config_key=os.environ.get('EMBEDDING_CONFIG_KEY') or DEFAULT_CONFIG_KEY,
            time_budget=float(os.environ.get('EMBED_TIME_BUDGET_MINUTES', 20)) * 60)
    if '--once' in sys.argv:
        run_once(mp, embedder)
        return

    interval = float(os.environ.get('PARSE_INTERVAL_MINUTES', 30)) * 60
    # docker stop шлёт SIGTERM; процесс №1 в контейнере без обработчика его
    # игнорирует, и Docker ждал бы 10 секунд до принудительного завершения
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:
        try:
            run_once(mp, embedder)
        except Exception as e:  # например, БД недоступна — пробуем на следующем круге
            print(f'=== сбор упал: {e}', flush=True)
        print(f'=== следующий сбор через {interval / 60:.0f} мин', flush=True)
        time.sleep(interval)


if __name__ == '__main__':
    main()
