# Альфа-Бизнес — персональная новостная аналитика

Единый репозиторий проекта. Сервисы сохраняют собственные Python-пакеты,
зависимости и контейнеры; общий код расположен в `service-common/`.

```text
backend/                  пользовательский API, документы, очередь и отчёты
frontend/                 интерфейс бизнес-обзора
company-user-profiler/    контекст компании и личный фокус пользователя
retrieval-ranking/        гибридный поиск и ранжирование новостей
insight-agent/            обоснованные бизнес-выводы
service-common/           общие контракты, runtime и клиенты
tool-gateway/             инструменты агентов
bank-materials-source/    API банковских материалов и исходные PDF в materials/
parser/                   сбор новостей
database/                 PostgreSQL/pgvector, SQL и предоставленные CSV
embedding-service/        векторизация
infrastructure/           единый Compose, конфигурация и межсервисные тесты
```

Запуск выполняется из [`infrastructure/`](infrastructure/README.md).
Локальный `.env` не хранится в Git; пример конфигурации — `infrastructure/.env.example`.
После запуска backend интерфейс доступен на http://localhost:8092/.

Текущий checkout содержит незавершённые изменения интеграции ranker и интерфейса;
это рабочий снимок объединения, а не проверенный релиз для жюри.
Истории исходных репозиториев сохранены merge-коммитами без squash.
Сторонние компоненты: [THIRD_PARTY_NOTICES](infrastructure/THIRD_PARTY_NOTICES.md).
