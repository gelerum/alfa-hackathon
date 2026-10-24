import datetime
import psycopg

DEFAULT_WINDOW = datetime.timedelta(days=6)  # окно «6 days» с доски, пока как черновое

class Master_Parser:
    def __init__(self, dsn: str, parsers: list):
        self.dsn = dsn
        self.parsers = parsers

    def run(self) -> dict:
        """Опрашивает все источники и пишет новое в БД. -> {'RT': 12, 'Kommersant': 30}"""
        stats = {}
        with psycopg.connect(self.dsn) as conn:
            for parser in self.parsers:
                since = self._last_pub_date(conn, parser.name) or \
                        datetime.datetime.now(datetime.timezone.utc) - DEFAULT_WINDOW
                try:
                    items = parser.fetch(since)
                except Exception as e:  # упал один источник — остальные всё равно собираем
                    print(f'{parser.name}: ошибка {e}')
                    stats[parser.name] = 0
                    continue
                stats[parser.name] = self._save(conn, items)
        return stats

    def cleanup(self, days: int = 7) -> int:
        """Удаляет новости старше days дней. -> сколько строк удалено"""
        if days < 0:
            raise ValueError('days не может быть отрицательным')
        with psycopg.connect(self.dsn) as conn:
            # у новости без даты публикации возраст считаем по времени сбора
            cur = conn.execute(
                'DELETE FROM news '
                'WHERE coalesce(published_at, fetched_at) < now() - make_interval(days => %s)',
                (days,))
            return cur.rowcount

    def _last_pub_date(self, conn, source: str):
        # будущие даты (журналы с выпуском наперёд) не учитываем,
        # иначе догрузка источника встанет до этой даты
        row = conn.execute('SELECT max(published_at) FROM news '
                           'WHERE source = %s AND published_at <= now()',
                           (source,)).fetchone()
        return row[0]

    def _save(self, conn, items: list[dict]) -> int:
        rows = [{
            'title': i['title'],
            'body': i['text'] or i['summary'] or '',
            'url': i['link'],
            'source': i['source'],
            'published_at': i['date'],
            'description': i['summary'] or None,
        } for i in items]
        with conn.cursor() as cur:
            cur.executemany(
                '''INSERT INTO news (title, body, url, source, published_at, description)
                   VALUES (%(title)s, %(body)s, %(url)s, %(source)s,
                           %(published_at)s, %(description)s)
                   ON CONFLICT (url) DO NOTHING''',
                rows)
            return cur.rowcount