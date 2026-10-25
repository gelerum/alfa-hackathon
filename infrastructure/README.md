# Локальная инфраструктура

Один Compose поднимает три сервиса в общей сети `alpha_default`:

| Сервис | С хоста | Из общей Docker-сети |
| --- | --- | --- |
| PostgreSQL 17 + pgvector | `127.0.0.1:5432` | `db:5432` |
| Qwen3-Embedding-0.6B, CPU | `http://127.0.0.1:8001` | `http://embeddings:8000` |
| SearXNG, HTML + JSON API | `http://127.0.0.1:8080` | `http://searxng:8080` |

Все опубликованные порты привязаны к `127.0.0.1`, не ко внешним интерфейсам.
Контейнеры общей сети могут обращаться к сервисам напрямую; подключай к ней только
доверенные приложения. API embeddings и SearXNG без аутентификации.

## Запуск

Требуются Docker Engine, Docker Compose v2 с поддержкой `--wait` и интернет.

```sh
cd infrastructure
cp .env.example .env
# При необходимости измени .env; для SEARXNG_SECRET сгенерируй openssl rand -hex 32.
docker compose up -d --build --wait --wait-timeout 1200
```

Запускай стек **из этой папки**, а не отдельные Compose в `database` и
`embedding-service` одновременно: иначе будут конфликты опубликованных портов.
Уже работающие отдельные стеки можно остановить командой `docker compose down`
из соответствующих папок (**без `-v`**, чтобы не удалять их данные).

Этот Compose использует отдельные volumes `alpha_postgres_data`, `alpha_model_cache`
и `alpha_searxng_cache`. Ранее созданные volumes отдельных стеков он автоматически
**не подхватывает и не удаляет**. Если там уже есть важные данные, перенеси их
отдельно через backup/restore до перехода; не удаляй старые volumes.

На первом запуске:

1. PostgreSQL применит `../database/schema.sql`, затем `../database/import.sql`
   и загрузит три CSV из `../database/`. Эти файлы должны присутствовать.
2. Embedding-сервис скачает около 1,2 ГБ весов модели; используется CPU-only PyTorch,
   8 потоков и максимум 2048 токенов на текст. На хосте нужна свободная RAM, желательно
   хотя бы 6–8 ГБ для embedding-сервиса плюс память для БД и SearXNG.
3. SearXNG загрузит `searxng/settings.yml`. JSON-формат включён. Для частного
   локального использования limiter выключен — отдельный Redis/Valkey не нужен.

Между этими тремя сервисами нет зависимостей запуска. Парсер и агенты в этот Compose
пока не включены. Их автоматический вызов embeddings/поиска здесь не настраивается.

## Проверки

```sh
docker compose ps
curl --fail http://127.0.0.1:8001/health
curl --fail http://127.0.0.1:8080/healthz
curl --fail --get http://127.0.0.1:8080/search \
  --data-urlencode 'q=новости российской промышленности' \
  --data-urlencode 'format=json'
```

Healthcheck SearXNG проверяет сам сервер, а не доступность внешних поисковиков.
Движки могут ограничивать запросы, требовать CAPTCHA или быть недоступны с текущего
IP. При необходимости настрой список engines в `searxng/settings.yml`.

Проверить загрузку CSV:

```sh
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT dataset, status, row_count FROM bank_import_batches ORDER BY dataset;"'
```

Документация embedding API: `http://127.0.0.1:8001/docs` и
[`../embedding-service/README.md`](../embedding-service/README.md).

## Подключение приложений

Для процессов на хосте адреса есть в `.env.example`. Эти переменные не подключают
будущие приложения автоматически: их нужно передать клиентскому коду.

Чтобы отдельный Compose с парсером/агентами использовал те же сервисы по DNS-именам:

```yaml
services:
  parser:
    # build: ...
    networks:
      - backend
    environment:
      DATABASE_URL: postgresql://alfa:alfa_dev_password@db:5432/alfa_hack
      EMBEDDING_BASE_URL: http://embeddings:8000
      SEARXNG_BASE_URL: http://searxng:8080

networks:
  backend:
    external: true
    name: alpha_default
```

Подставь актуальные credentials БД. Сначала подними infrastructure, чтобы сеть уже
существовала. `localhost` из контейнера — сам контейнер, не соседний сервис.

## Управление

```sh
docker compose logs -f              # все логи
docker compose logs -f embeddings   # только модель
docker compose down                # остановить, сохранив данные и кэши
```

После изменения настроек SearXNG: `docker compose restart searxng`.
После изменения кода embedding-сервиса: `docker compose up -d --build embeddings`.
Настройки `.env` применяются через `docker compose up -d`, а не просто `restart`.
Изменение пароля в `.env` не меняет пароль уже созданного пользователя PostgreSQL.
Схема и CSV применяются только к пустому volume; изменение SQL не обновляет существующую БД.

**`docker compose down -v` удаляет всю БД и оба кэша.** На следующем запуске
CSV загрузятся заново, а веса модели будут скачаны повторно. Не используй эту
команду, если в БД уже появились нужные данные приложений.

SearXNG по умолчанию использует `latest`; для воспроизводимого развёртывания можно
задать проверенный тег или digest через `SEARXNG_IMAGE` в `.env`.
