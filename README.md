# База данных

PostgreSQL 17 + pgvector. Без миграций, Python, сборки своего образа и тестовой обвязки.

- `schema.sql` — вся структура БД: таблицы, индексы, ограничения, триггеры и представления.
- `import.sql` — разовая загрузка трёх CSV средствами PostgreSQL (`COPY`).
- `compose.yaml` — запуск БД и хранение данных в Docker volume.
- `df_*.csv` — исходные данные; должны лежать рядом с `compose.yaml`.

## Запуск

Из каталога `database`:

```sh
docker compose up -d --wait --wait-timeout 600
```

На **пустом volume** Docker сначала применит `schema.sql`, затем импортирует CSV.
Первый запуск дольше обычного: загружается около 1,5 млн строк. При следующих запусках
данные сохраняются, схема и импорт повторно не выполняются.

Подключение по умолчанию:

```text
postgresql://alfa:alfa_dev_password@localhost:5432/alfa_hack
```

Порт опубликован только на `127.0.0.1`. Для изменения параметров можно создать `.env`
рядом с `compose.yaml` и задать `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`,
`POSTGRES_PORT`. Пароль по умолчанию предназначен только для локальной разработки.
Изменение этих переменных не меняет пользователей и пароль уже созданной БД.

## Данные

| CSV | Таблица | Строк |
| --- | --- | ---: |
| `df_Uid_data_hack.csv` | `bank_company_data` | 85 525 |
| `df_Uid_payment_hack.csv` | `bank_payment_edges` | 1 380 585 |
| `df_Fid_Uid_link_hack.csv` | `bank_person_links` | 12 514 |

Из идентификаторов компаний также заполняется `companies`. В `bank_import_batches`
сохраняются имя файла, SHA-256 и количество строк. Каждая исходная строка сохраняется
в `raw_attributes` со всеми колонками, включая не вынесенные в отдельные поля,
пустые строки и исходное написание значений. `source_row_number` — номер записи
с учётом заголовка (первая запись данных — 2).

Валюта, год выручки и период платежей в CSV не заданы и остаются `NULL`.
Значение выручки `1000.0` сохраняется как есть: подтверждения, что это маркер
отсутствующего значения, нет. Прочие таблицы не заполняются выдуманными демоданными.

Посмотреть результат импорта:

```sh
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT dataset, status, row_count FROM bank_import_batches ORDER BY dataset;"'
```

Открыть SQL-консоль:

```sh
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

Логи запуска:

```sh
docker compose logs -f db
```

## Остановка и пересоздание

Остановить с сохранением данных:

```sh
docker compose down
```

**Удалить все данные** и создать БД заново по текущей схеме и CSV:

```sh
docker compose down -v
docker compose up -d --wait --wait-timeout 600
```

После изменения `schema.sql`, `import.sql` или CSV для повторного применения нужен
новый пустой volume. Если первая инициализация завершилась ошибкой, исправь причину
по логам и также пересоздай volume: простой перезапуск не повторяет init-скрипты.
