"""Парсер публичных Telegram-каналов через веб-превью t.me/s/<канал>.

Без API и без аккаунта: страница отдаёт ~20 последних постов, более старые
листаются параметром ?before=<id поста>. Один объект — один канал, поэтому
догрузка «новее последней новости в БД» считается для каждого канала отдельно.
Посты без текста (только картинка или видео) пропускаются. Документы из
поста через веб-превью не скачать — в текст пишется их имя и размер; файлы
по ссылкам в тексте поста переводятся в текст (модуль attachments).
"""

import re
from typing import Optional

from bs4 import BeautifulSoup

import attachments
from common import http_get, parse_dt, to_msk

TITLE_LENGTH = 150      # заголовка у поста нет — берём начало текста
MIN_TITLE_LENGTH = 30
MAX_PAGES = 100      # предохранитель: не больше ~2000 постов за запуск (ТАСС пишет ~170 в день)


class TelegramParser:
    def __init__(self, channel: str, language: str = 'ru'):
        self.channel = channel
        self.name = f't.me/{channel}'
        self.language = language

    def fetch(self, since) -> list[dict]:
        since = to_msk(since)
        items = []
        before = None
        for _ in range(MAX_PAGES):
            url = f'https://t.me/s/{self.channel}' + (f'?before={before}' if before else '')
            response = http_get(url)
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
        text = text_node.get_text('\n', strip=True) if text_node else ''
        # Документ в посте: сам файл без Telegram API не скачать, поэтому
        # сохраняем хотя бы его имя и размер — пост без текста тоже не теряется
        documents = [_document_line(d) for d in post.select('.tgme_widget_message_document')]
        if not text and not documents:
            return None  # только картинка или видео без подписи
        title = _title(text) if text else documents[0]
        # ссылки на файлы в тексте поста (PDF на сайте ЦБ и т. п.) переводим в текст
        file_urls = [a['href'] for a in (text_node.select('a[href]') if text_node else [])
                     if attachments.FILE_LINK_RE.search(a['href'])]
        files = attachments.files_to_text(file_urls) if file_urls else ''
        text = '\n\n'.join(part for part in (text, '\n'.join(documents), files) if part)
        time_tag = post.select_one('a.tgme_widget_message_date time[datetime]')
        return {
            'source': self.name,
            'title': title,
            'date': parse_dt(time_tag['datetime']) if time_tag else None,
            'link': f'https://t.me/{post["data-post"]}',
            'category': '',
            'summary': '',
            'text': text,
            'language': self.language,
        }


def _document_line(document) -> str:
    name = document.select_one('.tgme_widget_message_document_title')
    extra = document.select_one('.tgme_widget_message_document_extra')
    line = f'[Файл в посте: {name.get_text(strip=True) if name else "без имени"}'
    return line + (f', {extra.get_text(strip=True)}]' if extra else ']')


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
