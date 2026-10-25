import email.utils
import random
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import attachments
from common import USER_AGENTS, http_get, to_msk


class PageDocsParser:
    def __init__(self, name: str, pages: list[str], subpages_prefix: Optional[str] = None,
                 language: str = 'ru'):
        self.name = name
        self.pages = pages
        self.subpages_prefix = subpages_prefix
        self.language = language

    def fetch(self, since) -> list[dict]:
        since = to_msk(since)
        docs = {}  # ссылка на файл -> (заголовок страницы, текст ссылки, страница)
        for page_url, soup in self._pages():
            main = soup.select_one('main') or soup.body
            h1 = soup.select_one('h1')
            page_title = h1.get_text(' ', strip=True) if h1 else ''
            for a in main.select('a[href]'):
                url = urljoin(page_url, a['href'])
                if attachments.FILE_LINK_RE.search(url) and url not in docs:
                    docs[url] = (page_title, a.get_text(' ', strip=True), page_url)

        with ThreadPoolExecutor(max_workers=8) as pool:
            dates = dict(zip(docs, pool.map(_last_modified, docs)))
        fresh = [url for url, date in dates.items() if date and date >= since]
        with ThreadPoolExecutor(max_workers=4) as pool:
            # файл — это и есть вся новость, поэтому текст без подписи «[Файл: …]»:
            # тот же документ из RSS получит такой же hash
            texts = [text for _, text in pool.map(attachments.file_text, fresh)]

        items = []
        for url, text in zip(fresh, texts):
            page_title, link_text, page_url = docs[url]
            link_text = link_text or unquote(urlparse(url).path.rsplit('/', 1)[-1])
            items.append({
                'source': self.name,
                'title': f'{page_title}: {link_text}' if page_title else link_text,
                'date': dates[url],
                'link': url,
                'category': page_title,
                'summary': f'Новый или обновлённый документ на странице {page_url}',
                'text': text,
                'language': self.language,
            })
        return items

    def _pages(self):
        """(адрес, разобранная страница) для стартовых страниц и их подразделов."""
        result, seen = [], set()
        queue = list(self.pages)
        for i, url in enumerate(queue):
            if url in seen:
                continue
            seen.add(url)
            response = http_get(url)
            if response is None:
                continue
            soup = BeautifulSoup(response.text, 'html.parser')
            result.append((url, soup))
            # подразделы берём только со стартовых страниц — на один уровень вглубь
            if self.subpages_prefix and i < len(self.pages):
                main = soup.select_one('main') or soup.body
                for a in main.select('a[href]'):
                    sub = urljoin(url, a['href']).split('#')[0]
                    if urlparse(sub).path.startswith(self.subpages_prefix) and sub not in seen:
                        queue.append(sub)
        return result


def _last_modified(url: str):
    try:
        response = requests.head(url, headers={'User-Agent': random.choice(USER_AGENTS)},
                                 timeout=20, allow_redirects=True)
        value = response.headers.get('last-modified')
        return email.utils.parsedate_to_datetime(value) if value else None
    except (requests.RequestException, TypeError, ValueError):
        return None
