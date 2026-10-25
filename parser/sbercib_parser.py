import json
import re
import time
from typing import Optional

import trafilatura
from bs4 import BeautifulSoup

import attachments
from common import http_get, parse_dt, to_msk

BASE = 'https://sbercib.ru'
PAUSE = 1.0     # секунды между запросами
MAX_PAGES = 15  # по 9 публикаций на странице


class SberCIBParser:
    name = 'SberCIB'

    def fetch(self, since) -> list[dict]:
        since = to_msk(since)
        items = []
        for page in range(1, MAX_PAGES + 1):
            pages = _next_data(f'{BASE}/publications?page={page}')
            if not pages:
                break
            fresh = [p for p in pages if parse_dt(p['go_live_at']) >= since]
            items += [self._item(p) for p in fresh]
            # порядок на сайте не строго по дате — листаем, пока не попадётся
            # страница целиком из старых публикаций
            if not fresh:
                break
        for item in items:
            time.sleep(PAUSE)
            item['text'] = _publication_text(item['link'])
        return items

    def _item(self, page: dict) -> dict:
        categories = page.get('categories') or []
        return {
            'source': self.name,
            'title': page['title'],
            'date': parse_dt(page['go_live_at']),
            'link': f'{BASE}/publication/{page["meta"]["slug"]}',
            'category': categories[0].get('name', '') if categories else '',
            'summary': _strip(page.get('intro_text') or page.get('announcement_description')
                              or page['meta'].get('search_description') or ''),
            'text': None,
            'language': 'ru',
        }


def _next_data(url: str) -> list[dict]:
    time.sleep(PAUSE)
    response = http_get(url)
    if response is None:
        return []
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', response.text, re.S)
    if not match:
        return []
    data = json.loads(match.group(1))['props']['pageProps'].get('data') or {}
    return data.get('media_pages') or []


def _publication_text(link: str) -> Optional[str]:
    response = http_get(link)
    if response is None:
        return None
    text = trafilatura.extract(response.content) or ''
    files = attachments.files_to_text(attachments.file_links(response.content, response.url))
    return '\n\n'.join(part for part in (text, files) if part) or None


def _strip(html: str) -> str:
    return BeautifulSoup(html, 'html.parser').get_text(' ', strip=True)
