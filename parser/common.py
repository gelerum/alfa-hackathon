"""Общее для всех парсеров: загрузка страниц с повторами, даты, интерфейс источника."""

import datetime
import random
import time
from typing import Optional, Protocol

import requests

MSK = datetime.timezone(datetime.timedelta(hours=3))

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 '
    'Firefox/140.0',
]


class SourceParser(Protocol):
    """Источник новостей. Новый источник — класс с именем и методом fetch."""
    name: str

    def fetch(self, since: datetime.datetime) -> list[dict]: ...
    # -> [{'source', 'title', 'date', 'link', 'category', 'summary', 'text', 'language'}, ...]


def to_msk(value) -> datetime.datetime:
    """date / datetime / 'YYYY-MM-DD' -> datetime с поясом МСК (без пояса — считается МСК)."""
    if isinstance(value, str):
        value = datetime.datetime.fromisoformat(value)
    if not isinstance(value, datetime.datetime):
        value = datetime.datetime.combine(value, datetime.time())
    if value.tzinfo is None:
        value = value.replace(tzinfo=MSK)
    return value.astimezone(MSK)


def parse_dt(value: str) -> Optional[datetime.datetime]:
    """ISO-дата ('...+03:00', '...Z') или '2026-10-9 12:10'; без пояса — МСК."""
    value = value.strip()
    try:
        parsed = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        try:
            parsed = datetime.datetime.strptime(value, '%Y-%m-%d %H:%M')
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MSK)
    return parsed.astimezone(MSK)


def http_get(url: str, attempts: int = 3) -> Optional[requests.Response]:
    """GET с повторами; None — страницы нет (404) или все попытки неудачны."""
    for attempt in range(attempts):
        try:
            response = requests.get(
                url, timeout=30,
                headers={'User-Agent': random.choice(USER_AGENTS),
                         'Accept-Language': 'ru-RU,ru;q=0.9'})
            if response.status_code == 404:
                return None
            response.raise_for_status()
            response.encoding = 'utf-8'
            return response
        except requests.RequestException as e:
            print(f'Ошибка запроса {url} - {e}, попытка {attempt + 1}', flush=True)
            time.sleep(1 + attempt)
        except ValueError as e:
            # битый адрес (например, кириллический домен в %-кодировке из поста) —
            # повторять бессмысленно, а ронять весь источник из-за одной ссылки нельзя
            print(f'Некорректный адрес {url[:120]} - {e}', flush=True)
            return None
    return None
