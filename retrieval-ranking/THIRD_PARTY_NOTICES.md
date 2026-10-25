# Сторонние компоненты

Точные версии Python-пакетов и транзитивных зависимостей закреплены в uv.lock.
Лицензионные тексты сторонних компонентов сохраняются в их дистрибутивах.
Ниже перечислены основные компоненты минимального сервиса и его проверок.

| Компонент | Назначение | Лицензия / источник |
| --- | --- | --- |
| Python | Исполнение сервиса | PSF; [python.org](https://www.python.org/) |
| uv | Зависимости и запуск | MIT или Apache-2.0; [astral-sh/uv](https://github.com/astral-sh/uv) |
| FastAPI, Starlette, Uvicorn | HTTP API | MIT, BSD-3-Clause, BSD-3-Clause; upstream Python-дистрибутивы |
| Pydantic, pydantic-settings | Валидация и конфигурация | MIT; [pydantic](https://github.com/pydantic) |
| Psycopg 3 / psycopg-binary | Клиент PostgreSQL | LGPL-3.0-or-later; [psycopg.org](https://www.psycopg.org/) |
| PyTorch CPU | Вычисление оценок модели | BSD-3-Clause; [pytorch](https://github.com/pytorch/pytorch) |
| Transformers, Hugging Face Hub, Tokenizers | Загрузка модели и токенизатора | Apache-2.0; [huggingface](https://github.com/huggingface) |
| Safetensors | Формат весов | Apache-2.0; [safetensors](https://github.com/huggingface/safetensors) |
| Qwen3-Reranker-0.6B | Оценка релевантности | Apache-2.0; [карточка модели](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B) |
| PostgreSQL | Общая и изолированная тестовая БД | PostgreSQL License; [postgresql.org](https://www.postgresql.org/) |
| pgvector | Векторный поиск SQL | PostgreSQL License; [pgvector](https://github.com/pgvector/pgvector) |
| pytest, HTTPX, Ruff | Проверки | MIT, BSD-3-Clause, MIT; upstream Python-дистрибутивы |

## Модель и внешние сервисы

Используется публичный checkpoint Qwen/Qwen3-Reranker-0.6B, revision
`e61197ed45024b0ed8a2d74b80b4d909f1255473`, закреплённая в
retrieval_ranking/config.py. Веса скачиваются отдельно и остаются в локальном
или Docker-кэше; в минимальную публикацию они не входят.

Сервис использует checkpoint как scorer: один forward для пары
контекст–новость, чтение финальных yes/no logits,
score = logit(yes)-logit(no). Текст не генерируется, generate() не вызывается.
Дообученные веса, внешний генеративный API и поведенческая модель не включены.
V0/V0-i2 различаются инструкцией, а не весами.

Document/query-векторы поставляются upstream через общую БД; этот сервис
не запускает embedding-модель и не сохраняет векторы за другой модуль.
Компания, пользовательский контекст и новости также поступают из upstream.
Их атрибуция и условия использования должны сохраняться владельцами источников.

## Схема БД и synthetic fixtures

tests/fixtures/schema.sql — неизменённая копия схемы команды из
[alpha-database](https://github.com/gelerum/alpha-database), commit
`861ea8dde6e84bd1495dae8d9e7e866bf62c3bb8`.
Исходный путь и SHA-256 записаны в
[tests/fixtures/schema-provenance.json](tests/fixtures/schema-provenance.json).

Компании, пользователи, статьи и 1024-мерные векторы в
tests/fixtures/seed.py созданы для технических проверок.
Они не являются клиентскими данными Альфы и не являются реальными
embedding-выходами Qwen. Тестовые пароли служат только изолированной demo/БД.
Эти fixtures проверяют контракты и SQL, а не качество рекомендаций.
Настоящий reranker используется в отдельных opt-in smoke-проверках.

## Данные хакатона

В минимальный публикуемый набор не входят исходные банковские CSV/PDF,
исследовательские датасеты, их разметка, корпуса издательских статей,
локальные выгрузки и модельные кэши. Локальные исследования сохраняются
отдельно и не являются зависимостью сервиса.

По переданному пользователем ответу организаторов исходные 3 CSV и 9 PDF
разрешены только в приватном репозитории с доступом команды и жюри
alfa-hack-2026; публичная публикация оригиналов запрещена.
Это переданный ответ, независимо в этом проходе не проверявшийся.
Он не является лицензией на публичное распространение исходных данных.
