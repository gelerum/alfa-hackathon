import re
import time
from typing import Optional

import psycopg
import requests
from psycopg.types.json import Jsonb

DEFAULT_CONFIG_KEY = 'qwen3-embedding-0.6b@97b0c614:v1'
BATCH = 8                 # максимум текстов в одном запросе к сервису
MAX_CHARS = 4000          # лимит сервиса — 2048 токенов; 4000 символов русского текста укладываются
TEXT_PREP_VERSION = 'news-title-body-4000c-v1'
REQUEST_TIMEOUT = 300     # CPU-модель: 8 длинных текстов считаются десятки секунд
BUSY_RETRIES = 60         # 429/503 — модель занята другим клиентом, ждём

# Шаблоны фиксируются в embedding_configs (append-only) при её первом создании.
# Query-формат — тот, что сервис добавляет сам (embedding-service/main.py).
QUERY_TEMPLATE = 'Instruct: {instruction}\nQuery: {text}'
QUERY_INSTRUCTION = ("Retrieve business news relevant to the company's activities, markets, "
                     "risks, and opportunities.")
DOCUMENT_TEMPLATE = '{title}\n\n{body}'

PENDING = '''
    SELECT r.article_id, r.content_version, r.title, r.body, r.description
    FROM news n
    JOIN news_revisions r ON r.article_id = n.article_id AND r.content_version = n.content_version
    WHERE n.duplicate_of_id IS NULL
      AND NOT (n.article_id = ANY(%(skip)s::text[]))
      AND NOT EXISTS (SELECT 1 FROM news_embeddings e
                      WHERE e.article_id = r.article_id AND e.content_version = r.content_version
                        AND e.embedding_config_id = %(config)s)
    ORDER BY n.published_at DESC NULLS LAST
    LIMIT %(limit)s
'''


class Embedder:
    def __init__(self, dsn: str, base_url: str, config_key: str = DEFAULT_CONFIG_KEY,
                 time_budget: float = 20 * 60):
        self.dsn = dsn
        self.base_url = base_url.rstrip('/')
        self.config_key = config_key
        self.time_budget = time_budget

    def run(self) -> int:
        """Считает векторы для новых ревизий, пока есть время. -> сколько записано."""
        health = self._health()
        if health is None:
            return 0
        started = time.monotonic()
        done, skip = 0, []
        with psycopg.connect(self.dsn) as conn:
            config_id = self._config(conn, health)
            if config_id is None:
                return 0
            while time.monotonic() - started < self.time_budget:
                pending = conn.execute(PENDING, {'config': config_id, 'skip': skip,
                                                 'limit': BATCH}).fetchall()
                if not pending:
                    break
                vectors = self._embed([_document(*row[2:]) for row in pending])
                if vectors is None:  # сервис недоступен — остальное в следующий раз
                    break
                with conn.cursor() as cur:
                    for (article_id, version, *_), vector in zip(pending, vectors):
                        if vector is None:  # текст не удалось уложить в лимит модели
                            skip.append(article_id)
                            continue
                        cur.execute(
                            'INSERT INTO news_embeddings (article_id, content_version, '
                            'embedding_config_id, embedding) VALUES (%s, %s, %s, %s::vector) '
                            'ON CONFLICT DO NOTHING',
                            (article_id, version, config_id, _vector(vector)))
                        done += cur.rowcount
                conn.commit()
        return done

    def _health(self) -> Optional[dict]:
        try:
            response = requests.get(f'{self.base_url}/health', timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            print(f'Эмбеддинги пропущены: сервис {self.base_url} недоступен ({e})', flush=True)
            return None

    def _config(self, conn, health: dict):
        """Конфигурация по ключу; создаётся при первом запуске. Модель должна совпадать."""
        conn.execute(
            'INSERT INTO embedding_configs (config_key, model_id, model_revision, dimensions, '
            'distance_metric, query_template, document_template, normalize, extra) '
            "VALUES (%s, %s, %s, %s, 'cosine', %s, %s, true, %s) ON CONFLICT (config_key) DO NOTHING",
            (self.config_key, health['model'], health['revision'], health['dimensions'],
             QUERY_TEMPLATE, DOCUMENT_TEMPLATE,
             Jsonb({'query_instruction': QUERY_INSTRUCTION,
                    'document_text_prep': TEXT_PREP_VERSION,
                    'document_max_chars': MAX_CHARS,
                    'max_input_tokens': health.get('max_input_tokens'),
                    'created_by': 'alfa_hack parser'})))
        conn.commit()
        config_id, model, revision = conn.execute(
            'SELECT embedding_config_id, model_id, model_revision FROM embedding_configs '
            'WHERE config_key = %s', (self.config_key,)).fetchone()
        if (model, revision) != (health['model'], health['revision']):
            # смешивать векторы разных моделей нельзя — нужен новый ключ конфигурации
            print(f'Эмбеддинги пропущены: конфигурация {self.config_key} для {model}@{revision}, '
                  f'а сервис отдаёт {health["model"]}@{health["revision"]}', flush=True)
            return None
        return config_id

    def _embed(self, texts: list[str]) -> Optional[list]:
        """Векторы в порядке texts; None на месте текста, который не влез в лимит модели."""
        texts = list(texts)
        result: list = [None] * len(texts)
        todo = list(range(len(texts)))
        busy = 0
        while todo:
            try:
                response = requests.post(f'{self.base_url}/embed', timeout=REQUEST_TIMEOUT,
                                         json={'role': 'document', 'inputs': [texts[i] for i in todo]})
            except requests.RequestException as e:
                print(f'Эмбеддинги: сервис недоступен ({e})', flush=True)
                return None
            if response.status_code in (429, 503) and busy < BUSY_RETRIES:
                busy += 1
                time.sleep(float(response.headers.get('Retry-After', 2)))
                continue
            if response.status_code == 422:
                # длинные тексты сервис не режет сам, а называет их номера — укорачиваем вдвое
                too_long = [todo[int(i)] for i in re.findall(r'\d+', response.text.split(']')[0])
                            if int(i) < len(todo)]
                if not too_long:  # 422 по другой причине — эти тексты не закодировать
                    break
                for i in too_long:
                    texts[i] = texts[i][:len(texts[i]) // 2]
                # бросаем только тексты, которые пришлось урезать почти до нуля;
                # короткие новости из той же пачки отправляются как есть
                dropped = {i for i in too_long if len(texts[i]) < 200}
                todo = [i for i in todo if i not in dropped]
                continue
            if not response.ok:
                print(f'Эмбеддинги: ошибка {response.status_code} {response.text[:200]}', flush=True)
                return None
            for i, vector in zip(todo, response.json()['embeddings']):
                result[i] = vector
            todo = []
        return result


def _document(title: str, body: Optional[str], description: Optional[str]) -> str:
    # Формат DOCUMENT_TEMPLATE, версия подготовки — TEXT_PREP_VERSION
    text = f'{title}\n\n{body or description or ""}'.strip()
    text = re.sub(r'[ \t]+', ' ', re.sub(r'\n{3,}', '\n\n', text))
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS].rsplit(' ', 1)[0]
    return text


def _vector(values: list[float]) -> str:
    return '[' + ','.join(f'{v:.7g}' for v in values) + ']'
