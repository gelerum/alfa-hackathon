"""Сбор новостей в БД.

По умолчанию работает постоянно: проход по всем источникам, пауза
PARSE_INTERVAL_MINUTES (по умолчанию 30 минут), снова проход. Так его
запускает Docker (`docker compose up -d`).
С флагом --once делает один проход и завершается.

Старые новости не удаляются — cleanup() здесь не вызывается.
"""

import datetime
import os
import signal
import sys
import time

from dotenv import load_dotenv

from master_parser import Master_Parser
from sources import ALL_PARSERS


def run_once(mp: Master_Parser):
    started = time.monotonic()
    print(f'=== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} сбор начат', flush=True)
    stats = mp.run()
    print(f'=== добавлено всего: {sum(stats.values())} из {len(stats)} источников '
          f'за {time.monotonic() - started:.0f} с', flush=True)


def main():
    load_dotenv()
    mp = Master_Parser(os.environ['DATABASE_URL'], ALL_PARSERS)
    if '--once' in sys.argv:
        run_once(mp)
        return

    interval = float(os.environ.get('PARSE_INTERVAL_MINUTES', 30)) * 60
    # docker stop шлёт SIGTERM; процесс №1 в контейнере без обработчика его
    # игнорирует, и Docker ждал бы 10 секунд до принудительного завершения
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:
        try:
            run_once(mp)
        except Exception as e:  # например, БД недоступна — пробуем на следующем круге
            print(f'=== сбор упал: {e}', flush=True)
        print(f'=== следующий сбор через {interval / 60:.0f} мин', flush=True)
        time.sleep(interval)


if __name__ == '__main__':
    main()
