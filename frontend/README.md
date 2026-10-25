# Frontend

Статический интерфейс: `index.html`, `style.css`, `app.js`. Node.js и npm для сборки не нужны.
Файлы раздаются backend-сервисом с `/` и `/frontend/*`, запросы идут на `/api/*` того же хоста.
Открытие `index.html` как file:// не заменяет запуск backend и БД.

Из соседнего `../infrastructure`:

```bash
docker compose up -d --build backend
```

Откройте http://localhost:8092/. После изменений файлов пересоберите backend-образ.
Для выполнения фоновых заданий и агентов нужен полный стек, а не только frontend с backend.
Подробнее: [инструкция запуска](../infrastructure/README.md).
