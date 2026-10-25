# Insight-agent

Реализация — `insight_service/service.py`, HTTP API — `insight_service/api.py`.
Цикл plan/tools/validation/review/repair подключается из `../service-common`.
Код company-user-profiler в этот проект не входит.

Результат — версионированный отчёт, раскрывающиеся карточки и отдельные claims с источниками.
Внешний ranker вызывает сервис после своего отбора; здесь ранжировщик не реализован.

Локальный API, из этой папки:

```bash
uv sync
# Настройки БД и INTERNAL_TOKEN передать через окружение.
uv run uvicorn insight_service.api:create_insight --factory --port 8094
```

`POST /runs` с `INTERNAL_TOKEN`: `request_id` существующего ranking request и ровно одно из
`ranking_run_id` / `article_ids`. Контексты и ревизии закрепляются до анализа.
Архив новостей, документы, аналитика банка и web доступны через tool-gateway.

Предыдущие выводы не являются первичными доказательствами. Поздняя новость из исходного
окна может привести к новой версии, не переписывая историю.

Полный стек и workers: [../infrastructure/](../infrastructure/README.md).
JSON-контракт: [ARCHITECTURE.md](../infrastructure/ARCHITECTURE.md).
