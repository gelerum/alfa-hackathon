"""Парсер публичных Telegram-каналов через веб-превью t.me/s/<канал>.

Без API и без аккаунта: страница отдаёт ~20 последних постов, более старые
листаются параметром ?before=<id поста>. Один объект — один канал, поэтому
догрузка «новее последней новости в БД» считается для каждого канала отдельно.
Посты без текста (только картинка или видео) пропускаются.
"""

import re
from typing import Optional

from bs4 import BeautifulSoup

from news_collector import _get, _parse_dt, _to_msk_datetime

TITLE_LENGTH = 150      # заголовка у поста нет — берём начало текста
MIN_TITLE_LENGTH = 30
MAX_PAGES = 100      # предохранитель: не больше ~2000 постов за запуск (ТАСС пишет ~170 в день)


class TelegramParser:
    def __init__(self, channel: str, language: str = 'ru'):
        self.channel = channel
        self.name = f't.me/{channel}'
        self.language = language

    def fetch(self, since) -> list[dict]:
        since = _to_msk_datetime(since)
        items = []
        before = None
        for _ in range(MAX_PAGES):
            url = f'https://t.me/s/{self.channel}' + (f'?before={before}' if before else '')
            response = _get(url)
            if response is None:
                break
            posts = BeautifulSoup(response.text, 'html.parser') \
                .select('div.tgme_widget_message[data-post]')
            if not posts:
                break

            reached_since = False
            for post in posts:
                item = self._item(post)
                if item is None:
                    continue
                if item['date'] is not None and item['date'] < since:
                    reached_since = True
                    continue
                items.append(item)

            oldest_id = min(int(p['data-post'].rsplit('/', 1)[1]) for p in posts)
            if reached_since or oldest_id <= 1:
                break
            before = oldest_id
        return items

    def _item(self, post) -> Optional[dict]:
        text_node = post.select_one('.tgme_widget_message_text')
        if text_node is None:
            return None
        text = text_node.get_text('\n', strip=True)
        if not text:
            return None
        time_tag = post.select_one('a.tgme_widget_message_date time[datetime]')
        return {
            'source': self.name,
            'title': _title(text),
            'date': _parse_dt(time_tag['datetime']) if time_tag else None,
            'link': f'https://t.me/{post["data-post"]}',
            'category': '',
            'summary': '',
            'text': text,
            'language': self.language,
        }


def _title(text: str) -> str:
    # первые строки поста часто — эмодзи или «⚡️Срочно»: берём строки с буквами
    # и добираем следующие, пока заголовок не станет осмысленной длины
    lines = [line.strip() for line in text.split('\n') if re.search(r'[^\W\d_]', line)]
    title = ''
    for line in lines:
        title = f'{title} {line}'.strip()
        if len(title) >= MIN_TITLE_LENGTH:
            break
    title = title or text.strip()
    if len(title) <= TITLE_LENGTH:
        return title
    return title[:TITLE_LENGTH].rsplit(' ', 1)[0] + '…'
