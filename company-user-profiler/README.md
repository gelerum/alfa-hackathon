# Company-user-profiler

Реализация профилирования — `profiler_service/service.py`, HTTP API — `profiler_service/api.py`.
Общий ограниченный агентный цикл — в локальной зависимости `../service-common/agent_common/runtime.py`.
Код insight-agent в этот проект не входит и для запуска HTTP API не нужен.

Сервис ставит задания в очередь PostgreSQL; `profiler-worker` выполняет tools через gateway,
проверку и публикацию профиля компании и личного контекста пользователя.
Факты компании и личные цели разделены. LLM/HTTP-вызовы выполняются вне SQL-транзакций.

Локальный API, из этой папки:

```bash
uv sync
# Настройки БД и INTERNAL_TOKEN передать через окружение.
uv run uvicorn profiler_service.api:create_profiler --factory --port 8093
```

`POST /runs` требует `INTERNAL_TOKEN`, `request_id`, `company_id`, `user_id`.
Документы и анкета поступают через Backend. Проверка ссылок не заменяет экспертную оценку качества.

Полный стек и workers: [../infrastructure/](../infrastructure/README.md).
Контракты и сценарии: [ARCHITECTURE.md](../infrastructure/ARCHITECTURE.md).
