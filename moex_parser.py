"""Новости Московской биржи через открытый API ISS (iss.moex.com).

Список: /iss/sitenews.json?start=N — по 50 новостей, от новых к старым,
полный текст: /iss/sitenews/<id>.json. Ключ и регистрация не нужны.
Время в API московское.
"""

from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from bs4 import BeautifulSoup

import attachments
from news_collector import _get, _parse_dt, _to_msk_datetime

ISS = 'https://iss.moex.com/iss'
MAX_PAGES = 20  # предохранитель: не больше 1000 новостей за запуск


class MOEXParser:
    name = 'MOEX'

    def fetch(self, since) -> list[dict]:
        since = _to_msk_datetime(since)
        news = []
        for page in range(MAX_PAGES):
            response = _get(f'{ISS}/sitenews.json?start={page * 50}')
            if response is None:
                break
            block = response.json()['sitenews']
            rows = [dict(zip(block['columns'], row)) for row in block['data']]
            if not rows:
                break
            fresh = [r for r in rows if _parse_dt(r['published_at']) >= since]
            news += fresh
            if len(fresh) < len(rows):  # дошли до новостей старше since
                break

        with ThreadPoolExecutor(max_workers=8) as pool:
            bodies = list(pool.map(_body, [r['id'] for r in news]))
        return [{
            'source': self.name,
            'title': r['title'],
            'date': _parse_dt(r['published_at']),
            'link': f'https://www.moex.com/n{r["id"]}',
            'category': r.get('tag') or '',
            'summary': '',
            'text': body,
            'language': 'ru',
        } for r, body in zip(news, bodies)]


def _body(news_id: int) -> Optional[str]:
    response = _get(f'{ISS}/sitenews/{news_id}.json')
    if response is None:
        return None
    block = response.json()['content']
    if not block['data']:
        return None
    html = dict(zip(block['columns'], block['data'][0]))['body'] or ''
    soup = BeautifulSoup(html, 'html.parser')
    text = soup.get_text('\n', strip=True)
    # к новостям биржи часто приложены PDF/XLSX (параметры, уведомления)
    file_urls = [a['href'] for a in soup.select('a[href]')
                 if attachments.FILE_LINK_RE.search(a['href'])]
    files = attachments.files_to_text(
        [u if u.startswith('http') else f'https://www.moex.com{u}' for u in file_urls])
    return '\n\n'.join(part for part in (text, files) if part) or None
