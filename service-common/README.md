# Общий локальный Python-пакет

`agent_common/` содержит только переиспользуемый код: конфигурацию, соединение с БД,
контракты, embedding-клиент, агентный runtime, проверку цитат и лимит HTTP body.
Это **не сервис**, отдельный контейнер ему не нужен.

Предметная логика находится в проектах `backend`, `company-user-profiler`, `insight-agent`,
`tool-gateway` и `bank-materials-source`, а не здесь.
Каждый из них объявляет зависимость `agent-common` в своём `pyproject.toml`.
Пока все проекты лежат рядом, uv использует локальный путь `../service-common`.
Никаких GitHub URL и автоматического клонирования нет.

Установка: `uv sync` из этой папки. Запуск и интеграционные тесты всего стека — из
[`../infrastructure/`](../infrastructure/README.md).
