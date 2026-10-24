# Alfa_hack — подбор новостей под компанию

Сервис собирает новости, хранит их в PostgreSQL и подбирает те, что важны конкретной компании.

Архитектура по схеме команды (слайд Scheme):

```
источники → парсер → БД (init.sql) → Retrieval / Rank → Explain AI → интерфейс
```

Сейчас готовы **парсер с БД** (`Master_Parser`) и черновой **анализ** (`main.py`: похожесть на компанию и сводка по темам). Это скелет: каждую часть можно заменить, не трогая остальные.

## Быстрый старт: собрать новости в БД через Docker

1. Запустите Docker Desktop.
2. Создайте в корне проекта файл `.env` с паролем для базы (подробнее в разделе [`.env`](#env)):

   ```
   DB_PASSWORD=придумайте_пароль
   ```

3. Соберите новости:

   ```bash
   docker compose run --rm parser
   ```

Первый запуск сам поднимет PostgreSQL, создаст таблицу из `init.sql` и соберёт новости за последние 6 дней. Это занимает около 15 минут. Следующие запуски берут только новое (секунды) и удаляют новости старше 7 дней:

```
Добавлено: {'RT': 1, 'Kommersant': 5}
Удалено старых: 0
```

Посмотреть, что лежит в базе:

```bash
docker compose exec db psql -U alfa -d alfa_news -c "SELECT source, count(*), max(published_at) FROM news GROUP BY source;"
```

Остановить базу: `docker compose down`. Данные при этом сохранятся. Чтобы удалить и их: `docker compose down -v`.

База в контейнере доступна снаружи на порту **6770**, чтобы не пересекаться с локальным PostgreSQL.

## Парсер и БД из Python

```python
import os
from dotenv import load_dotenv
from master_parser import Master_Parser
from news_collector import RTParser, KommersantParser

load_dotenv()
mp = Master_Parser(os.environ['DATABASE_URL'], [RTParser(), KommersantParser()])

mp.run()        # -> {'RT': 12, 'Kommersant': 30}  сколько новых новостей добавлено
mp.cleanup()    # -> 140  сколько удалено новостей старше 7 дней
mp.cleanup(3)   # удалить всё старше 3 дней
```

Готовый скрипт с этими шагами — `run_parser.py`, его и запускает Docker.

Как работает `Master_Parser`:
- **`run()`** опрашивает каждый источник. Он берёт только новости новее последней новости этого источника в БД, а если в БД пусто — за 6 дней. Даты в будущем при этом не учитываются: у журналов они бывают «на выпуск вперёд». Если один источник упал, остальные всё равно собираются. Повторно пришедшая новость отбрасывается по ссылке.
- **`cleanup(days=7)`** удаляет новости старше `days` дней. Если у новости нет даты публикации, возраст считается по времени сбора.

### Как добавить источник

Источнику нужно только имя и метод `fetch` (интерфейс `SourceParser` в `news_collector.py`):

```python
class MySourceParser:
    name = 'MySource'
    def fetch(self, since) -> list[dict]:
        # вернуть новости новее since:
        # [{'source', 'title', 'date', 'link', 'category', 'summary', 'text'}, ...]
        ...

mp = Master_Parser(dsn, [RTParser(), KommersantParser(), MySourceParser()])
```

### Таблица `news` (`init.sql`)

| Поле | Что хранит |
|---|---|
| `article_id` | md5 от ссылки: у одной новости всегда один id, по нему можно связывать логи |
| `title` | заголовок |
| `body` | полный текст; если его нет — лид |
| `url` | ссылка на оригинал, уникальная (так отсекаются дубли) |
| `source`, `source_domain` | название источника и домен (домен БД берёт из ссылки сама) |
| `published_at` | дата публикации (может быть пустой) |
| `fetched_at` | когда новость собрана, ставится автоматически |
| `language` | язык, по умолчанию `ru` |
| `description` | лид / анонс от источника |

### Источники

- **RT** — свежие новости из ленты сайта, старше ~3 дней — из sitemap.
- **Коммерсант** — из sitemap, текст со страницы статьи.

## `.env`

Файл не попадает в git. В нём две строки:

```
DATABASE_URL=postgresql://alfa:ПАРОЛЬ@localhost:6769/alfa_news
DB_PASSWORD=пароль_для_базы_в_docker
```

- `DATABASE_URL` — для запуска из Python на своей машине (локальный PostgreSQL). Для серверной БД достаточно поменять эту строку.
- `DB_PASSWORD` — пароль базы в Docker. Строку подключения для контейнера `compose.yaml` собирает сам.

### Локальный PostgreSQL без Docker

Команды для PowerShell. Пароль `psql` спросит сам.

```powershell
Set-Alias psql 'C:\Program Files\PostgreSQL\18\bin\psql.exe'
psql -h localhost -p 6769 -U postgres -c "CREATE USER alfa WITH PASSWORD 'ПАРОЛЬ';"
psql -h localhost -p 6769 -U postgres -c "CREATE DATABASE alfa_news OWNER alfa;"
psql -h localhost -p 6769 -U alfa -d alfa_news -f init.sql
```

После этого `python run_parser.py` из `.venv` пишет в локальную базу.

## Анализ новостей (`main.py`)

Собирает новости с заданной даты, сортирует их по похожести на описание компании и печатает сводку: главные темы и по одной характерной новости на каждую. Пока он берёт новости напрямую с сайтов, а не из БД.

```bash
python main.py -d 2026-10-20 -c company_decription.txt -p 0.55
```

Быстрый вариант, примерно за минуту вместо 7:

```bash
python main.py -d 2026-10-20 -c company_decription.txt -i short -m cointegrated/rubert-tiny2 -p 0.6 -t 5
```

| Параметр | Что значит |
|---|---|
| `-d` | с какой даты собирать новости (время московское) |
| `-c` | txt-файл в UTF-8 с описанием компании |
| `-p` | порог похожести от 0 до 1 (по умолчанию 0, то есть все) |
| `-m` | модель эмбеддингов (по умолчанию `deepvk/USER-bge-m3`) |
| `-i` | что подавать в модель: `full` — текст новости, `short` — заголовок + анонс |
| `-t` | сколько тем показать в сводке (по умолчанию 5) |

Модули анализа:
- **`Tokinizer.py`** — `tokenise(text)` чистит текст и режет на подслова, `embed(tokens)` даёт вектор «смысла», `embed_many([...])` — векторы для списка, `load_model(name)` выбирает модель.
- **`exAI.py`** — `summarize(news, n_topics)` делит новости на темы и в каждой берёт новость, ближайшую к «среднему смыслу» темы. Текст не генерируется.

Похожесть — косинус между вектором новости и вектором описания компании.

**Порог `-p` у каждой модели свой:** запустите с `-p 0`, посмотрите на оценки и поднимите порог.

### Сравнение режимов

237 новостей за 22.10.2026. Время — токенизация + эмбеддинги.

| Режим | Время | Совпадение топ-10 с основным | Разброс оценок |
|---|---|---|---|
| USER-bge-m3, `full` (основной) | 6.4 мин | 10/10 | 0.26–0.64 |
| USER-bge-m3, `short` | 2 мин | 6/10 | 0.25–0.66 |
| rubert-tiny2, `full` | 25 с | 3/10 | 0.24–0.74 |
| rubert-tiny2, `short` | 9 с | 3/10 | 0.35–0.84 |

## Установка без Docker

Парсер и БД:

```bash
pip install -r requirements.txt
```

Для анализа (`main.py`) дополнительно:

```bash
pip install sentence-transformers spacy scikit-learn numpy
pip install --no-deps spacy-cleaner
python -m spacy download ru_core_news_sm
```

Почему так:
- В `requirements.txt` сейчас только зависимости парсера: из него собирается Docker-образ, и в образ не попадают тяжёлые модели.
- `spacy-cleaner` ставится отдельно, потому что требует старый spaCy, который не собирается на Python 3.14 (с новым работает).
- При первом запуске модель скачивается с Hugging Face: USER-bge-m3 — около 1.4 ГБ, rubert-tiny2 — около 120 МБ.

Предупреждения `Token indices sequence length is longer than ...` и `UNEXPECTED` при загрузке rubert-tiny2 безопасны.
