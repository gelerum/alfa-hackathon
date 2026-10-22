"""Сборщик новостей Russia Today и Коммерсанта.

По мотивам https://habr.com/ru/articles/930188/ (парсеры RT и Коммерсанта),
но без поискового запроса: get_news() должна отдавать ВСЕ новости за период.

RT:
  * свежие новости берутся из ленты «Все новости» (/listing/type.News/...) —
    она отдаёт заголовок, дату, анонс и рубрику без захода в статью;
  * лента хранит только ~1000 последних новостей (около 3 дней), поэтому
    более старые добираются из годовых sitemap_YYYY*.xml;
  * текст статьи — div.article__text, как в статье на Хабре.

Коммерсант:
  * sitemap_news.xml — материалы за последние ~2 дня с заголовками и датами;
  * sitemaps/archive/sitemap-YYYY-MM.xml — все материалы за месяц;
  * метаданные и текст — со страницы документа обычным requests
    (селекторы из статьи doc_header__name / doc__body + новая вёрстка сайта).
  Поиск и «Показать ещё» на сайте сейчас закрыты капчей, поэтому
  Selenium-подход из статьи не используется.
"""

import datetime
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from tqdm.auto import tqdm


import pandas as pd
import requests
from bs4 import BeautifulSoup

MSK = datetime.timezone(datetime.timedelta(hours=3))

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 '
    'Firefox/140.0',
]

RT_BASE = 'https://russiatoday.ru'  # russian.rt.com редиректит сюда
RT_LISTING = RT_BASE + '/listing/type.News/prepare/all-news/{size}/{page}'
RT_PAGE_SIZE = 100

KOM_BASE = 'https://www.kommersant.ru'
KOM_NEWS_SITEMAP = KOM_BASE + '/sitemaps/sitemap_news.xml'
KOM_MONTH_SITEMAP = KOM_BASE + '/sitemaps/archive/sitemap-{year}-{month:02d}.xml'

SITEMAP_URL_RE = re.compile(
    r'<url>\s*<loc>([^<]+)</loc>\s*<lastmod>([^<]+)</lastmod>', re.S)
NEWS_SITEMAP_RE = re.compile(
    r'<loc>([^<]+)</loc>.*?<news:publication_date>([^<]+)'
    r'</news:publication_date>\s*<news:title>([^<]*)</news:title>'
    r'(?:\s*<news:keywords>([^<]*)</news:keywords>)?', re.S)

COLUMNS = ['source', 'title', 'date', 'link', 'category', 'summary', 'text']


def get_news(date, fetch_text: bool = True, max_workers: int = 8,
             show_progress: bool = True) -> pd.DataFrame:
    """Все новости RT и Коммерсанта с ``date`` до текущего момента.

    :param date: datetime.date / datetime.datetime / строка 'YYYY-MM-DD'.
                 Время без часового пояса считается московским.
    :param fetch_text: загружать ли полный текст новостей RT (для
                       Коммерсанта страница статьи грузится всегда — только
                       там есть заголовок).
    :param max_workers: число параллельных запросов к страницам статей.
    :param show_progress: показывать прогресс-бары по этапам загрузки.
    :return: DataFrame, отсортированный по дате (от старых к новым), с
             колонками source, title, date (datetime64 с поясом МСК), link,
             category, summary, text.
    """
    since = _to_msk_datetime(date)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        news = (_get_rt_news(since, fetch_text, pool, show_progress)
                + _get_kommersant_news(since, pool, show_progress))
    df = pd.DataFrame(news, columns=COLUMNS)
    df['date'] = pd.to_datetime(df['date'], utc=True).dt.tz_convert(MSK)
    return df.sort_values('date', ignore_index=True)


# ---------------------------------------------------------------- common ---

def _to_msk_datetime(value) -> datetime.datetime:
    if isinstance(value, str):
        value = datetime.datetime.fromisoformat(value)
    if not isinstance(value, datetime.datetime):
        value = datetime.datetime.combine(value, datetime.time())
    if value.tzinfo is None:
        value = value.replace(tzinfo=MSK)
    return value.astimezone(MSK)


def _parse_dt(value: str) -> Optional[datetime.datetime]:
    """ISO-дата из sitemap/meta ('...+03:00', '...Z') или '2026-10-9 12:10'."""
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


def _get(url: str, attempts: int = 3) -> Optional[requests.Response]:
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
            tqdm.write(f'Ошибка запроса {url} - {e}, попытка {attempt + 1}')
            time.sleep(1 + attempt)
    return None


def _text(node) -> str:
    return node.get_text(' ', strip=True) if node else ''


def _meta(soup, prop: str) -> str:
    node = (soup.find('meta', attrs={'property': prop})
            or soup.find('meta', attrs={'name': prop}))
    return node['content'].strip() if node and node.get('content') else ''


def _sitemap_urls(url: str) -> list[tuple[str, datetime.datetime]]:
    response = _get(url)
    if response is None:
        return []
    return [(loc.strip(), _parse_dt(lastmod))
            for loc, lastmod in SITEMAP_URL_RE.findall(response.text)]


# -------------------------------------------------------------------- RT ---

def _get_rt_news(since: datetime.datetime, fetch_text: bool,
                 pool: ThreadPoolExecutor, progress: bool) -> list[dict]:
    news, reached_since = _rt_from_listing(since, progress)

    if not reached_since:
        known = {_rt_path(item['link']) for item in news}
        links = [f'{RT_BASE}{_rt_path(loc)}'
                 for loc, lastmod in _rt_sitemap_urls(since, progress)
                 if '/news/' in loc and lastmod and lastmod >= since
                 and _rt_path(loc) not in known]
        # lastmod может быть позже публикации (правки) — фильтруем по дате
        # со страницы статьи.
        for item in tqdm(pool.map(_rt_article, links), total=len(links),
                         desc='RT: старые новости', disable=not progress):
            if item and item['date'] >= since:
                news.append(item)

    if fetch_text:
        missing = [item for item in news if item['text'] is None]
        pages = pool.map(_rt_article, [i['link'] for i in missing])
        # tqdm первым в zip, иначе последний шаг бара не засчитывается
        for page, item in zip(tqdm(pages, total=len(missing),
                                   desc='RT: тексты', disable=not progress),
                              missing):
            if page:
                item['text'] = page['text']
    return news


def _rt_path(link: str) -> str:
    return re.sub(r'^https?://[^/]+', '', link)


def _rt_from_listing(since: datetime.datetime,
                     progress: bool) -> tuple[list[dict], bool]:
    """Лента «Все новости». Возвращает новости и флаг «дошли до since»."""
    with tqdm(desc='RT: лента', unit=' стр', disable=not progress) as bar:
        return _rt_listing_pages(since, bar)


def _rt_listing_pages(since: datetime.datetime, bar) -> tuple[list[dict], bool]:
    news = []
    page = 0
    while True:
        response = _get(RT_LISTING.format(size=RT_PAGE_SIZE, page=page))
        bar.update(1)
        if response is None:
            return news, False
        soup = BeautifulSoup(response.text, 'html.parser')
        cards = soup.select('div.card_all-news')
        if not cards:
            return news, False  # лента закончилась раньше, чем since

        oldest = None
        for card in cards:
            link = card.select_one('.card__heading a[href]')
            published = _rt_card_date(card)
            if not link or published is None:
                continue
            oldest = published if oldest is None else min(oldest, published)
            if published < since:
                continue
            news.append({
                'source': 'RT',
                'title': _text(link),
                'date': published,
                'link': RT_BASE + link['href'],
                'category': _text(card.select_one('.card__category')),
                'summary': _text(card.select_one('.card__summary')),
                'text': None,
            })

        if oldest is not None and oldest < since:
            return news, True
        page += 1


def _rt_card_date(card) -> Optional[datetime.datetime]:
    """У прошлых дней в карточке <time datetime="2026-10-21 18:07">,
    у сегодняшних — только текст «06:03»."""
    time_tag = card.select_one('time[datetime]')
    if time_tag:
        return _parse_dt(time_tag['datetime'])
    match = re.fullmatch(r'(\d{1,2}):(\d{2})',
                         _text(card.select_one('.card__date')))
    if not match:
        return None
    now = datetime.datetime.now(MSK)
    published = now.replace(hour=int(match[1]), minute=int(match[2]),
                            second=0, microsecond=0)
    if published > now + datetime.timedelta(minutes=5):
        published -= datetime.timedelta(days=1)  # переход через полночь
    return published


def _rt_sitemap_urls(since: datetime.datetime, progress: bool) -> list:
    response = _get(RT_BASE + '/sitemap.xml')
    if response is None:
        return []
    sitemaps = [loc.strip()
                for loc in re.findall(r'<loc>([^<]+)</loc>', response.text)
                if (year := re.search(r'sitemap_(\d{4})', loc))
                and int(year.group(1)) >= since.year]
    urls = []
    for loc in tqdm(sitemaps, desc='RT: sitemap', disable=not progress):
        urls += _sitemap_urls(RT_BASE + _rt_path(loc))
    return urls


def _rt_article(link: str) -> Optional[dict]:
    response = _get(link)
    if response is None:
        return None
    soup = BeautifulSoup(response.text, 'html.parser')
    published = _parse_dt(_meta(soup, 'mediator_published_time')
                          or _meta(soup, 'published_time_telegram')
                          or (soup.select_one('.article__date time[datetime]')
                              or {}).get('datetime', ''))
    if published is None:
        tqdm.write(f'Не удалось определить дату новости - {link}')
        return None
    return {
        'source': 'RT',
        'title': _text(soup.select_one('h1.article__heading'))
                 or _meta(soup, 'og:title'),
        'date': published,
        'link': link,
        'category': _text(soup.select_one('a.nav__link_header_active')),
        'summary': _text(soup.select_one('.article__summary')),
        'text': _text(soup.select_one('div.article__text')),
    }


# ------------------------------------------------------------ Kommersant ---

def _get_kommersant_news(since: datetime.datetime, pool: ThreadPoolExecutor,
                         progress: bool) -> list[dict]:
    candidates = {}  # link -> данные из sitemap_news (заголовок, рубрика)

    # Месяц до since тоже смотрим: материалы на стыке месяцев могут лежать
    # в соседнем файле.
    now = datetime.datetime.now(MSK)
    year, month = (since.year, since.month - 1) if since.month > 1 \
        else (since.year - 1, 12)
    month_sitemaps = []
    while (year, month) <= (now.year, now.month):
        month_sitemaps.append(KOM_MONTH_SITEMAP.format(year=year, month=month))
        year, month = (year, month + 1) if month < 12 else (year + 1, 1)

    with tqdm(total=1 + len(month_sitemaps), desc='Коммерсант: sitemap',
              disable=not progress) as bar:
        response = _get(KOM_NEWS_SITEMAP)
        if response is not None:
            for loc, published, title, keywords in \
                    NEWS_SITEMAP_RE.findall(response.text):
                published = _parse_dt(published)
                if published and published >= since:
                    candidates[loc.strip()] = {'title': title.strip(),
                                               'category': keywords.strip()}
        bar.update(1)

        for sitemap in month_sitemaps:
            for loc, lastmod in _sitemap_urls(sitemap):
                if lastmod and lastmod >= since:
                    candidates.setdefault(loc, {})
            bar.update(1)

    links = list(candidates)
    news = []
    articles = pool.map(_kommersant_article, links)
    for item, link in zip(tqdm(articles, total=len(links),
                               desc='Коммерсант: статьи', disable=not progress),
                          links):
        if item is None or item['date'] < since:
            continue
        item['title'] = item['title'] or candidates[link].get('title', '')
        item['category'] = item['category'] or candidates[link].get('category', '')
        news.append(item)
    return news


def _kommersant_article(link: str) -> Optional[dict]:
    response = _get(link)
    if response is None:
        return None
    soup = BeautifulSoup(response.text, 'html.parser')

    time_tag = (soup.select_one('time.doc_header__publish_time[datetime]')
                or soup.select_one('time[datetime]'))
    published = _parse_dt(_meta(soup, 'article:published_time')
                          or (time_tag or {}).get('datetime', ''))
    if published is None:
        tqdm.write(f'Не удалось определить дату материала - {link}')
        return None

    paragraphs = soup.select('.doc-text-block__paragraph')
    if paragraphs:
        text = '\n'.join(_text(p) for p in paragraphs)
    else:
        text = _text(soup.select_one('div.doc__body')
                     or soup.select_one('div.doc-body__content'))

    return {
        'source': 'Kommersant',
        'title': _text(soup.select_one('h1.doc_header__name')
                       or soup.select_one('h1.doc-title-block__title'))
                 or _meta(soup, 'og:title'),
        'date': published,
        'link': link,
        'category': _text(soup.select_one('a.doc-title-block__breadcrumb-link'))
                    or _meta(soup, 'article:section'),
        'summary': _meta(soup, 'description'),
        'text': text,
    }
