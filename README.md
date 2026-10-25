# Backend — Python / FastAPI

Реализация находится в `backend_service/`:
- `api.py` — пользовательское API и раздача frontend;
- `documents.py` — извлечение документов и chunk RAG;
- `reports.py` — расписание, версии отчётов и интеграция с ranker;
- `ingestion.py` — приём банковских материалов;
- `worker.py` — очередь заданий и запуск обработчиков.

Зависимости проекта — в собственных `pyproject.toml` и `uv.lock`.
Общие контракты подключаются из соседнего `../service-common`.

Локальный HTTP-сервер, из этой папки:

```bash
uv sync
# Передать DATABASE_URL и остальные настройки через окружение.
uv run uvicorn backend_service.api:app --port 8092
```

Frontend берётся из соседней `../frontend/`; можно задать `FRONTEND_DIR` явно.
`/` — интерфейс, `/api/*` — API, `/admin/docs` — Swagger.
LLM и обработка файлов не выполняются внутри пользовательского HTTP-запроса.

Для всего стека и workers используйте [../infrastructure/](../infrastructure/README.md).
Workers разных ролей используют обработчики из соответствующих проектов, поэтому
их запускают в общей установленной среде из `infrastructure`, а не из одного backend.
