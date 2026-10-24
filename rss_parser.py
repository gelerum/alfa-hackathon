"""Универсальный парсер RSS-лент (доска: «RSS / RSSHub»).

Один класс на все сайты с RSS: имя источника, список лент и язык.
Полный текст берётся из самой ленты (если она его отдаёт, как РБК), иначе,
при fetch_body=True, — со страницы статьи через trafilatura. Для сайтов за
пейволлом (FT, WSJ, NYT, Economist, Forbes) fetch_body выключен: страницы
отдают 401/403, и в body попадёт анонс из ленты.

Важно: RSS хранит только последние N материалов (обычно 20–300), истории
за прошлые дни в нём нет, поэтому парсер стоит запускать регулярно.
"""

import calendar
import datetime
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import feedparser
import trafilatura
from bs4 import BeautifulSoup

from news_collector import _get, _to_msk_datetime

# Короче этого анонс считаем «не текстом» и идём за полным текстом на страницу
MIN_BODY_FROM_FEED = 1000


class RSSParser:
    def __init__(self, name: str, feeds: list[str], language: str = 'ru',
                 fetch_body: bool = False):
        self.name = name
        self.feeds = feeds
        self.language = language
        self.fetch_body = fetch_body

    def fetch(self, since) -> list[dict]:
        since = _to_msk_datetime(since)
        items = {}  # link -> новость; одна статья может быть в нескольких лентах
        for url in self.feeds:
            response = _get(url)
            if response is None:
                continue
            for entry in feedparser.parse(response.content).entries:
                item = self._item(entry)
                if item is None or item['link'] in items:
                    continue
                # без даты пропускать нельзя — дубли всё равно отсечёт БД
                if item['date'] is not None and item['date'] < since:
                    continue
                items[item['link']] = item

        if self.fetch_body:
            need = [i for i in items.values()
                    if not i['text'] and len(i['summary']) < MIN_BODY_FROM_FEED]
            with ThreadPoolExecutor(max_workers=8) as pool:
                for item, text in zip(need, pool.map(_page_text, [i['link'] for i in need])):
                    item['text'] = text
        return list(items.values())

    def _item(self, entry) -> Optional[dict]:
        link = (entry.get('link') or '').strip()
        title = _html_to_text(entry.get('title', ''))
        if not link or not title:
            return None
        summary = _html_to_text(entry.get('summary', ''))
        # полный текст в самой ленте: РБК кладёт его в rbc_news:full-text,
        # другие — в content:encoded
        text = _html_to_text(entry.get('rbc_news_full-text', '')
                             or entry.get('yandex_full-text', ''))
        if not text and entry.get('content'):
            content = _html_to_text(entry['content'][0].get('value', ''))
            # короткий content (URA.RU, NYT) — это тот же анонс, а не статья
            text = content if len(content) >= MIN_BODY_FROM_FEED else ''
        tags = entry.get('tags') or []
        return {
            'source': self.name,
            'title': title,
            'date': _entry_date(entry),
            'link': link,
            'category': tags[0].get('term', '') if tags else '',
            'summary': summary,
            'text': text or None,
            'language': self.language,
        }


def _entry_date(entry) -> Optional[datetime.datetime]:
    # feedparser отдаёт дату уже в UTC
    parsed = entry.get('published_parsed') or entry.get('updated_parsed')
    if not parsed:
        return None
    return datetime.datetime.fromtimestamp(calendar.timegm(parsed),
                                           tz=datetime.timezone.utc)


def _html_to_text(html: str) -> str:
    if not html:
        return ''
    return BeautifulSoup(html, 'html.parser').get_text(' ', strip=True)


def _page_text(link: str) -> Optional[str]:
    response = _get(link)
    if response is None:
        return None
    # байты, а не .text: trafilatura сама определит кодировку страницы
    return trafilatura.extract(response.content) or None
