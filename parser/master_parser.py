"""Master_Parser: опрашивает все источники и пишет новости в общую БД (репозиторий database).

Контракт database/README.md, раздел «Новости»:
- news и news_revisions сохраняются вместе в одной транзакции и коммитятся,
  каждый источник — отдельной транзакцией;
- изменился текст — новая content_version: новая строка в news_revisions
  (append-only), а news обновляется до последней версии;
- точный дубль (тот же текст под другой ссылкой) сохраняется с duplicate_of_id;
- published_at — известная дата публикации, иначе NULL;
- новости не удаляются (удалять ревизии запрещает триггер БД).
Эмбеддинги считаются после коммита отдельным шагом — embedder.py.
"""

import datetime
import hashlib
import re
from urllib.parse import urlparse

import psycopg
from psycopg.types.json import Jsonb

# Каждый проход повторно просматривает 7 дней: догружаем ранее недоступные
# публикации, даже если у источника уже есть более свежие записи.
DEFAULT_WINDOW = datetime.timedelta(days=7)
# Короче этого текст за точный дубль не считаем: «S&P 500 -0.52%» у разных
# каналов совпадёт случайно
MIN_DUPLICATE_TEXT = 100

UPSERT_NEWS = """
    INSERT INTO news (article_id, canonical_url, source_name, source_domain, title, body,
                      description, language, published_at, content_version,
                      duplicate_of_id, metadata)
    VALUES (%(article_id)s, %(url)s, %(source)s, %(domain)s, %(title)s, %(body)s,
            %(description)s, %(language)s, %(published_at)s, %(content_version)s,
            %(duplicate_of_id)s, %(metadata)s)
    ON CONFLICT (article_id) DO UPDATE SET
        title = EXCLUDED.title, body = EXCLUDED.body, description = EXCLUDED.description,
        language = EXCLUDED.language,
        published_at = coalesce(EXCLUDED.published_at, news.published_at),
        content_version = EXCLUDED.content_version,
        metadata = news.metadata || EXCLUDED.metadata
    WHERE news.content_version <> EXCLUDED.content_version
    RETURNING (xmax = 0) AS inserted
"""
INSERT_REVISION = """
    INSERT INTO news_revisions (article_id, content_version, title, body, description, published_at)
    SELECT %(article_id)s, %(content_version)s, %(title)s, %(body)s, %(description)s, n.published_at
    FROM news n WHERE n.article_id = %(article_id)s
    ON CONFLICT DO NOTHING
"""


class Master_Parser:
    def __init__(self, dsn: str, parsers: list):
        self.dsn = dsn
        self.parsers = parsers

    def run(self) -> dict:
        """Опрашивает все источники и пишет новое в БД. -> {'RBC': 12, 'MOEX': 30, ...}"""
        stats = {}
        with psycopg.connect(self.dsn) as conn:
            for parser in self.parsers:
                floor = datetime.datetime.now(datetime.timezone.utc) - DEFAULT_WINDOW
                try:
                    items = parser.fetch(floor)
                    added, changed = self._save(conn, items)
                    conn.commit()  # каждый источник сохраняется отдельно
                except Exception as e:  # упал один источник — остальные всё равно собираем
                    conn.rollback()
                    print(f"{parser.name}: ошибка {e}", flush=True)
                    stats[parser.name] = 0
                    continue
                stats[parser.name] = added
                print(f"{parser.name}: +{added}" + (f", обновлено {changed}" if changed else ""), flush=True)
        return stats

    def _last_pub_date(self, conn, source: str):
        # будущие даты (журналы с выпуском наперёд) не учитываем,
        # иначе догрузка источника встанет до этой даты
        row = conn.execute(
            "SELECT max(published_at) FROM news WHERE source_name = %s AND published_at <= now()", (source,)
        ).fetchone()
        return row[0]

    def _save(self, conn, items: list[dict]) -> tuple[int, int]:
        """-> (сколько новых новостей, у скольких изменился текст)."""
        rows = {}
        for item in items:
            # Без заголовка и ссылки новость не сохранить (NOT NULL в схеме)
            if item.get("title") and item.get("link"):
                row = _row(item)
                rows.setdefault(row["article_id"], row)
        if not rows:
            return 0, 0

        originals = self._originals(conn, [r["text_sha256"] for r in rows.values() if r["text_sha256"]])
        added = changed = 0
        with conn.cursor() as cur:
            for row in rows.values():
                original = (
                    originals.setdefault(row["text_sha256"], row["article_id"])
                    if row["text_sha256"]
                    else None
                )
                row["duplicate_of_id"] = original if original != row["article_id"] else None
                result = cur.execute(UPSERT_NEWS, row).fetchone()
                if result is None:  # такая версия текста уже есть
                    continue
                cur.execute(INSERT_REVISION, row)
                if result[0]:
                    added += 1
                else:
                    changed += 1
        return added, changed

    def _originals(self, conn, hashes: list[str]) -> dict:
        """Уже сохранённые новости с таким же текстом: хэш текста -> article_id оригинала."""
        if not hashes:
            return {}
        found = conn.execute(
            "SELECT DISTINCT ON (metadata->>'text_sha256') metadata->>'text_sha256', article_id "
            "FROM news WHERE metadata->>'text_sha256' = ANY(%s) AND duplicate_of_id IS NULL "
            "ORDER BY metadata->>'text_sha256', fetched_at",
            (hashes,),
        ).fetchall()
        return dict(found)


def _row(item: dict) -> dict:
    url = item["link"].strip()
    title = _clean(item["title"])
    description = _clean(item.get("summary")) or None
    body = _clean(item.get("text")) or description  # нет текста — лид
    normalized = re.sub(r"\s+", " ", body or "").strip().lower()
    text_sha256 = _sha256(normalized) if len(normalized) >= MIN_DUPLICATE_TEXT else None
    version_source = "\x1f".join([title, description or "", body or ""])
    return {
        "article_id": _sha256(url)[:32],
        "url": url,
        "source": item["source"],
        "domain": urlparse(url).hostname,
        "title": title,
        "body": body,
        "description": description,
        "language": item.get("language") or "ru",
        "published_at": item.get("date"),
        # версия = хэш текста: изменился текст — новая версия и новая ревизия
        "content_version": _sha256(version_source)[:16],
        "text_sha256": text_sha256,
        # хэш текста в metadata: по нему ищутся точные дубли
        "metadata": Jsonb(
            {"category": item.get("category") or None, "text_sha256": text_sha256, "parser": "alfa_hack"}
        ),
    }


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean(text):
    # PostgreSQL не принимает символ \x00 в тексте, а на некоторых страницах он встречается
    return text.replace("\x00", "").strip() if text else text
