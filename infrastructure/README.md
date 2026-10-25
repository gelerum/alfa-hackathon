# Запуск проекта для жюри

**`infrastructure/` — единая точка входа для деплоя всего проекта**, включая backend,
frontend, агентов, workers, парсер, PostgreSQL, embeddings и SearXNG.
Все сервисы находятся в **одном репозитории**. Клонируйте его целиком:
одна папка `infrastructure` без соседних каталогов исходников недостаточна.
Запускать отдельный frontend-сервер не нужно.

```text
<единый репозиторий>/
├── infrastructure/         # Compose, Dockerfile, .env, интеграционные тесты
├── service-common/         # общий Python-пакет, не сервис
├── backend/                # backend_service и собственные зависимости
├── frontend/               # HTML/CSS/JS
├── company-user-profiler/  # profiler_service и собственные зависимости
├── insight-agent/          # insight_service и собственные зависимости
├── retrieval-ranking/      # гибридный поиск и ранжирование
├── tool-gateway/           # tool_gateway_service и собственные зависимости
├── bank-materials-source/  # API источника и PDF в materials/
├── database/
├── parser/
└── embedding-service/
```

В корне нет общего Python-приложения: сервисы остаются отдельными пакетами,
но теперь версионируются вместе одним Git-репозиторием. У Python-сервисов свои `pyproject.toml` и `uv.lock`;
`infrastructure/pyproject.toml` объединяет **локальные зависимости** для сборки образа и тестирования.

## Быстрый старт

Нужны Docker Engine, Docker Compose **2.20+** (поддержка `include`), интернет для
первой загрузки моделей и доступ к разрешённому LLM endpoint. Для полного стека
рекомендуется 12–16 ГБ RAM; embedding-модель работает на CPU.

```bash
cd /path/to/alpha/infrastructure
cp .env.example .env
# Заполните INTERNAL_TOKEN, ADMIN_TOKEN, SEARXNG_SECRET (три разных секрета).
# Для каждого: openssl rand -hex 32
# Настройте OPENAI_BASE_URL, OPENAI_API_KEY и OPENAI_MODEL.
# RANKER_URL по умолчанию указывает на retrieval-ranking из этого Compose.
docker compose config --quiet
docker compose up -d --build --wait --wait-timeout 1200
```

Откройте **http://localhost:8092/** — frontend обслуживается backend-контейнером.
**В локальном стеке демо-вход автоматический:** frontend получает сессию через
`POST /api/demo/session`, backend создаёт или переиспользует общего демо-пользователя.
Токен вводить и пользователя вручную создавать не нужно. Используется первая компания из
импорта; если компаний нет, создаётся вымышленная `DEMO_LOCAL`. Профиль и отчёты не подделываются.

`DEMO_MODE=true` включён по умолчанию только в локальном Compose. Это общий тестовый
аккаунт, не банковская авторизация: нельзя публиковать такой режим с реальными данными.
Чтобы закрыть автоматический вход, задайте `DEMO_MODE=false` и пересоздайте backend;
при переходе к недемо-развёртыванию также отзовите ранее выданные демо-токены.
Ручное создание отдельных пользователей остаётся в [ARCHITECTURE.md](ARCHITECTURE.md#сквозной-сценарий).
Без работающего retrieval-ranking и доступной LLM аналитические отчёты не будут сгенерированы:
запуск контейнеров не подменяет эти зависимости тестовыми ответами.

Только backend с frontend и БД, без остальных вычислительных сервисов:

```bash
# После заполнения .env; для первого запуска также нужен --build.
docker compose up -d --build backend
```

Для обработки документов и генерации аналитики нужен полный стек, не только backend.
После изменения frontend или Python-кода пересоберите образ: `docker compose up -d --build`.

## Файлы деплоя

- `compose.yaml` — основной entry point, БД, embeddings и SearXNG.
- `application.compose.yaml` — включаемые основным Compose приложения и workers.
  Отдельно его запускать не нужно.
- `Dockerfile` — пока общий Python-образ для запуска разных сервисов и ролей workers.
  Он устанавливает отдельные соседние Python-проекты и копирует frontend, не содержит их исходники.
  Контекст сборки — родительская рабочая папка (`..`); в её корне никакие файлы не требуются.
- `pyproject.toml`, `uv.lock` — зафиксированная среда сборки и интеграционных тестов с локальными path-зависимостями.
- `tests/` — межсервисные регрессионные тесты; бизнес-логики приложения здесь нет.
- `Dockerfile.dockerignore` — исключения контекста сборки этого образа, включая `.env`.
- `database.Dockerfile` и `database.Dockerfile.dockerignore` — образ PostgreSQL/pgvector
  с SQL/CSV, скопированными с правами `0644`, без bind-mount этих файлов.
- `init-empty-database.sh` — ручное восстановление инициализации только пустой БД.
- `.env.example` → `.env` — единая конфигурация стека.
- `searxng/settings.yml` — настройки поисковика.

## Адреса

| Сервис | С хоста | Из Docker-сети |
| --- | --- | --- |
| Frontend + Backend | http://localhost:8092/ | `backend:8000` |
| Backend API / Swagger | http://localhost:8092/api/companies / http://localhost:8092/admin/docs | `backend:8000/api` |
| Tool gateway | http://localhost:8090/docs | `tool-gateway:8000` |
| Company-user-profiler | http://localhost:8093/docs | `company-user-profiler:8000` |
| Insight-agent | http://localhost:8094/docs | `insight-agent:8000` |
| Моделируемый источник банка | http://localhost:8091/docs | `bank-materials-source:8000` |
| PostgreSQL 17 + pgvector | `127.0.0.1:5432` | `db:5432` |
| Qwen3-Embedding-0.6B | http://localhost:8001/docs | `embeddings:8000` |
| SearXNG | http://localhost:8080 | `searxng:8080` |

Все опубликованные порты привязаны к `127.0.0.1`. Для публичного размещения нужен
TLS reverse proxy и отдельная настройка доступа. Embeddings и SearXNG без аутентификации;
подключайте к общей сети `alpha_default` только доверенные приложения.
`localhost` в контейнере — сам контейнер, а не соседний сервис.
Для запуска Python-процессов на хосте переопределите адрес БД на `127.0.0.1`.
При изменении PostgreSQL credentials согласованно измените `DATABASE_URL`.

## Podman и восстановление после Permission denied

С Podman используйте `podman compose` вместо `docker compose` во всех командах.
Образ приложений нормализует права скопированных исходников и проверяет импорт от
непривилегированного пользователя при сборке. SQL и CSV упакованы в образ БД:
права `0600` на хосте и SELinux labels bind-mount больше не мешают их чтению PostgreSQL.

Если старый запуск упал на `01_schema.sql: Permission denied`, entrypoint при повторном
старте уже не выполняет init-скрипты. **Не нужно удалять volume.** Из этой папки:

```bash
podman compose up -d --build --wait --wait-timeout 600 db
# Только если init ранее упал ДО создания схемы:
podman compose exec -T db sh /usr/local/bin/init-empty-database
podman compose up -d --build --wait --wait-timeout 600 backend
curl --fail http://127.0.0.1:8092/api/health
```

Скрипт восстановления откажется работать при наличии пользовательских таблиц и не
удаляет данные. Если он отказал, БД уже инициализирована либо схема частично создана:
нужно проверить её состояние, а не выполнять скрипты повторно вслепую.
При обычном чистом запуске ручная команда не нужна — init выполняется автоматически.

## Первый запуск и данные

На **пустом** volume БД выполняются `../database/schema.sql`, `import.sql`
(импорт трёх CSV) и `application.sql`. Изменение SQL не обновляет существующую БД:
для нужных старых данных потребуется миграция/backup, а не удаление volume.
Embedding-сервис скачивает около 1,2 ГБ весов в кэш; первая готовность может занять время.
PDF источника банка хранятся в `../bank-materials-source/materials/` и включены в образ приложений.

Имя проекта осталось `alpha`, имена volumes сохранены: `alpha_postgres_data`,
`alpha_model_cache`, `alpha_searxng_cache`, `alpha_documents_data`, `alpha_bank_source_data`.
Перенос файлов деплоя сам по себе не требует удаления данных.
Не запускайте одновременно отдельные Compose из `database/`, `embedding-service/`
или `parser/`: возможны конфликты портов и дублирование сервисов.

## Проверки и управление

Python-проверки без Docker, из `infrastructure/`:

```bash
uv sync
uv run pytest -q
# Для PostgreSQL-интеграционных тестов — только отдельная тестовая БД с pgvector:
TEST_DATABASE_URL='postgresql://...' uv run pytest -q
```

Без `TEST_DATABASE_URL` DB-тесты пропускаются. Браузерные E2E пока отключены в существующих тестах;
их пропуск не означает проверенный браузерный сценарий.

Все команды Docker ниже также выполняются из `infrastructure/`:

```bash
docker compose ps
curl --fail http://127.0.0.1:8092/api/health
curl --fail http://127.0.0.1:8001/health
curl --fail http://127.0.0.1:8080/healthz
curl --fail --get http://127.0.0.1:8080/search \
  --data-urlencode 'q=новости российской промышленности' \
  --data-urlencode 'format=json'
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT dataset, status, row_count FROM bank_import_batches ORDER BY dataset;"'
docker compose logs -f backend backend-worker document-worker profiler-worker insight-worker
docker compose down # сохранить данные
```

Healthcheck SearXNG проверяет сервер, не доступность внешних движков: возможны CAPTCHA
и блокировки. Настройки `.env` применяются через `docker compose up -d`, не `restart`.
Изменение пароля в `.env` не меняет пароль уже созданного пользователя PostgreSQL.
SearXNG по умолчанию использует `latest`; для воспроизводимости задайте проверенный digest.

**`docker compose down -v` удаляет БД, документы, mock-каталог и кэши моделей/поиска.**
Для обычной остановки или переноса деплоя эта команда не нужна.
