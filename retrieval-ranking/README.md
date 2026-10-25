# Retrieval-ranking

Сервис подбирает бизнес-новости под готовый контекст компании или user-company.
Поиск выполняется в PostgreSQL: multi-query pgvector cosine + FTS. Затем
Qwen3-Reranker-0.6B на CPU оценивает пары контекст–новость, сервис возвращает
Top K и сохраняет оценки всех кандидатов. Дообученные веса не используются.

```text
ready-контекст и query-векторы в общей БД
  -> vector Top L по каждому аспекту + FTS Top B
  -> объединение article_id -> загрузка текстов кандидатов
  -> Qwen CPU -> Top K + ranking_runs / ranking_items
```

Подключение модуля: [TEAM_INTEGRATION](docs/TEAM_INTEGRATION.md).
Схема и границы: [ARCHITECTURE](ARCHITECTURE.md).
Проверка качества и CPU: [VALIDATION](docs/VALIDATION.md).
HTTP-схема без запуска сервера: [OpenAPI](docs/openapi.json).

## Окружение

- Python 3.12.13, uv 0.13.1; зависимости закреплены в uv.lock.
- PostgreSQL 17 + pgvector с общей схемой alpha-database версии
  `861ea8dde6e84bd1495dae8d9e7e866bf62c3bb8`.
- CPU, один worker API. CUDA не требуется.
- Интернет для первой загрузки модели либо готовый Hugging Face cache.
  Startup загружает модель и выполняет прогрев до открытия API.

Все команды выполняются из корня этого репозитория. Сервис не выполняет
миграции, не парсит новости и не строит профили. Upstream должен заранее
сохранить ready-профили, аспекты, совместимые query/document-векторы и текущие
news_revisions. Версия модели, подготовка текста и embedding_config_id должны
совпадать у query и document. Полный пул новостей в Python не загружается.

## Запуск через uv

```sh
uv sync --locked
cp .env.example .env
# Указать RETR_RANK_DATABASE_URL нужной БД в .env.
uv run --locked uvicorn main:app --host 127.0.0.1 --port 8002 --workers 1
```

.env.example по умолчанию указывает на изолированную synthetic demo.
Для общей БД замените DSN. Пример также задаёт точную инструкцию V0-i2,
выбранную по небольшому validation-сравнению; исходный fallback в Settings —
V0 при отсутствии переопределения. Инструкция сохраняется в трассировке.
Оба варианта используют одни публичные базовые веса без обучения.

Модель: Qwen/Qwen3-Reranker-0.6B, revision
`e61197ed45024b0ed8a2d74b80b4d909f1255473`.
Кэш и окружение локальны: .cache/ и .venv/ не публикуются.

## Synthetic demo

Docker должен быть запущен. Следующие команды управляют только проектом
alpha-retrieval-ranking-test, PostgreSQL на 127.0.0.1:55439 и его тестовыми БД:

```sh
uv run --locked python scripts/test_database.py start
uv run --locked python scripts/test_database.py demo
```

Запустите API командой выше с demo DSN из .env.example, затем:

```sh
curl --fail-with-body http://127.0.0.1:8002/ready
curl --fail-with-body http://127.0.0.1:8002/rank \
  -H 'Content-Type: application/json' \
  -d '{"company_id":"U_TEST","user_id":"00000000-0000-0000-0000-0000000000c9","as_of":"2026-01-10T00:00:00Z","top_k":3}'
```

Компании, пользователи, новости и 1024-мерные векторы в demo синтетические.
Пароль retr_rank_test предназначен только для этой изолированной тестовой БД.
Ранкер — настоящий Qwen; сценарий проверяет техническую связность, а не качество
рекомендаций. Хранилище тестового PostgreSQL временное.

Для сброса только отмеченной demo: `python scripts/test_database.py demo --reset`
через uv run --locked. Для остановки тестового проекта:

```sh
uv run --locked python scripts/test_database.py stop
```

В WSL helper может использовать docker.exe, если нативная интеграция CLI
недоступна. Другие репозитории и настройки WSL он не меняет.

## Docker с общей БД

Общая БД и внешняя сеть alpha_default должны уже существовать.
Этот Compose запускает только retrieval-ranking.

```sh
cp .env.example .env
# В .env задать DSN общей БД:
# RETR_RANK_DATABASE_URL=postgresql://<user>:<password>@db:5432/<database>
docker compose config --quiet
docker compose up --build -d
```

В контейнере hostname БД — db, а не localhost. Для другой сети задайте
ALPHA_NETWORK. API с хоста: 127.0.0.1:8002; из общей сети:
http://retrieval-ranking:8000. Не используйте локальный demo DSN в этом Compose.
Модельный cache сохраняется в собственном volume проекта.
Образ использует CPU PyTorch, непривилегированного пользователя и
`uv sync --locked --no-dev`. Один worker предотвращает несколько копий модели.

## API

| Endpoint | Назначение |
| --- | --- |
| GET /health | Процесс работает после завершения startup |
| GET /ready | Модель загружена, требуемые объекты БД доступны |
| POST /rank | Найти, оценить, сохранить и вернуть Top K |
| GET /docs, /openapi.json | Живая документация |

Минимальный корпоративный запрос: `{"company_id":"<id>","top_k":10}`.
user_id необязателен. Персональный запрос требует активного членства и ready
user_preferences; user_context_id можно передавать только с user_id.
profile_id и embedding_config_id закрепляют конкретные версии.
as_of по умолчанию — текущий UTC; published_after задаёт нижнюю границу.
Даты требуют часового пояса; будущий as_of запрещён.

Ответ содержит ranking_run_id, выбранные версии, candidate_count и items.
У новости: article_id, content_version, title, text, source_name, source_url,
published_at, rank, score, retrieval_hits и диагностика усечения.
Векторы не возвращаются. Все оценки сохраняются; rank вне Top K равен NULL.

score = logit(yes)-logit(no), не вероятность. Отрицательные оценки допустимы.
Порога релевантности пока нет: выдача упорядочена относительно данного пула.
При нехватке кандидатов items короче K, при пустом пуле items=[].

| Статус | Причина |
| --- | --- |
| 403 | Нет активного членства пользователя |
| 409 | Нет совместимого ready-контекста или превышен лимит аспектов |
| 422 | Неверные поля, даты или лимиты |
| 429 | CPU worker занят; Retry-After: 1 |
| 503 | БД или операция ранжирования недоступна |

API внутреннее, без аутентификации. Вызывающее приложение связывает user_id
с авторизованным пользователем. POST создаёт новый ranking_run; идемпотентность
повторов не заявлена. Подробный формат ошибок — в TEAM_INTEGRATION.

## Настройки и CPU-лимиты

Переменные имеют префикс RETR_RANK_; полный список — [.env.example](.env.example).

| Настройка | По умолчанию |
| --- | --- |
| DATABASE_URL | Обязательна |
| VECTOR_TOP_L / FTS_TOP_B | 5 / 20 |
| MAX_FACETS / MAX_CANDIDATES | 16 / 100 |
| MAX_FTS_TERMS / MAX_TERM_CHARACTERS | 32 / 120 |
| MAX_TOP_K | 100 |
| RERANKER_BATCH_SIZE / TORCH_THREADS | 2 / 4 |
| RERANKER_MAX_TOKENS / CONTEXT_MAX_TOKENS | 2048 / 512 |
| CONNECT_TIMEOUT_SECONDS / STATEMENT_TIMEOUT_MS | 5 / 10000 |
| INCLUDE_UNDATED | false |
| INSTRUCTION | V0 в коде; V0-i2 в .env.example |
| MODEL_CACHE_DIR | .cache/huggingface/hub |
| MODEL_LOCAL_FILES_ONLY | false |

MAX_FACETS * VECTOR_TOP_L + FTS_TOP_B <= MAX_CANDIDATES.
top_k ограничивает выдачу, а не число оценок. Тексты обрезаются по токен-бюджету;
признаки усечения возвращаются и сохраняются. Без даты новости по умолчанию
исключены. Сырые cosine и FTS scores не суммируются с оценкой ранкера.

Одновременно выполняется один запрос. CPU-время зависит от числа и длины
кандидатов; на реальном пуле из 37 новостей один прогон занял около 177 секунд.
Это не SLA. Память должна покрывать модель, тексты и соседние компоненты;
маленький synthetic smoke не задаёт верхнюю границу RAM.
Эксперимент 1024 токена / 12 потоков не стал настройкой по умолчанию.

## Проверки

Минимальный набор содержит 106 тестов: 84 обычных без модели/БД,
20 PostgreSQL без модели и 2 opt-in с моделью. Тесты по умолчанию
не скачивают веса и не подключаются к БД.

```sh
uv lock --check
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest -q -m 'not integration and not model'

uv run --locked python scripts/test_database.py start
RETR_RANK_RUN_DB_TESTS=1 uv run --locked pytest -q -m 'integration and not model'

RETR_RANK_RUN_MODEL_TESTS=1 uv run --locked pytest -q -m 'model and not integration'

RETR_RANK_RUN_DB_TESTS=1 RETR_RANK_RUN_MODEL_TESTS=1 \
  uv run --locked pytest -q tests/integration/test_live_pipeline.py
```

DB-тесты используют отдельные retr_rank_test_* на 55439; общая БД не затрагивается.
Копия схемы и её SHA — в tests/fixtures/schema-provenance.json.
Demo helper зависит только от этих fixtures и compose.test.yaml.

## Данные и публикация

В минимальный релиз входят сервис, конфигурация, документация и synthetic fixtures.
Исследования, датасеты, локальные отчёты и кэши сохраняются локально и не входят
в публикацию; приложение от них не зависит. Условия компонентов:
[THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES.md).

По переданному пользователем ответу организаторов, исходные 3 CSV и 9 PDF
можно хранить только в приватном репозитории с доступом команды и жюри
alfa-hack-2026; публичная публикация оригиналов запрещена.
Это переданный ответ, в этом проходе независимо не проверялся.
Оригиналы и реальные пароли/токены не входят в минимальный набор.

Организаторы также подтвердили удобство тестовых логинов и паролей стенда
в README приватного репозитория. У этого сервиса собственных пользовательских
аккаунтов нет; здесь указаны только credentials изолированной synthetic БД.
