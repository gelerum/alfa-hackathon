# Tool gateway — Python / FastAPI

Реализация — `tool_gateway_service/api.py` и `tool_gateway_service/tools.py`.
Общие схемы и embedding-клиент подключаются из локального `../service-common`.
Все агентные tool-вызовы проходят HTTP-границу gateway.

Запуск из этой папки: `uv sync`, затем `uv run uvicorn tool_gateway_service.api:app --port 8090`.
Настройки передаются через окружение. Полный стек: [infrastructure](../infrastructure/README.md).
Контракты и безопасность: [ARCHITECTURE.md](../infrastructure/ARCHITECTURE.md).

`POST /tools/{name}` требует service token. Тело: `{scope, arguments}`.
Scope сверяется с сохранённым `agent_runs.input_snapshot.scope`, membership и ролью агента;
LLM не может менять компанию, пользователя или закреплённый контекст.

Инструменты:
- `company_context.get`, `company_bank_data.get`;
- `company_documents.search`, `company_documents.read`;
- `news.search`, `news.read`;
- `bank_analytics.search`, `bank_analytics.read`;
- `web.search` через SearXNG, `web.fetch` с SSRF-защитой.

Поиск возвращает source IDs, текст и локаторы. Search snippets web-выдачи не являются
цитируемым доказательством. Нет инструментов SQL, shell или произвольной записи в БД.
