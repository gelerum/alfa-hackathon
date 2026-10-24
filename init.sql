-- Схема хранилища новостей (слайд Scheme: парсер -> БД).
-- Поля согласованы командой: обязательные и желательные.
-- Скрипт можно запускать повторно: существующие объекты не пересоздаются.

CREATE TABLE IF NOT EXISTS news (
    -- Обязательные
    -- article_id = md5 от ссылки: одна и та же новость всегда получает один id,
    -- даже после пересоздания БД, поэтому по нему можно связывать логи.
    article_id    TEXT        GENERATED ALWAYS AS (md5(url)) STORED PRIMARY KEY,
    title         TEXT        NOT NULL,
    body          TEXT        NOT NULL DEFAULT '',   -- если текста нет, парсер кладёт лид
    url           TEXT        NOT NULL UNIQUE,       -- одна новость = одна ссылка (дедупликация)
    source        TEXT        NOT NULL,              -- название: 'RT', 'Kommersant', ...
    source_domain TEXT        GENERATED ALWAYS AS (substring(url FROM '^https?://([^/]+)')) STORED,
    published_at  TIMESTAMPTZ,                       -- NULL, если источник не отдал дату
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Желательные
    language      TEXT        NOT NULL DEFAULT 'ru',
    description   TEXT                               -- лид / анонс от источника
);

-- Временные фильтры и догрузка «новее последней новости источника»
CREATE INDEX IF NOT EXISTS news_published_at        ON news (published_at);
CREATE INDEX IF NOT EXISTS news_source_published_at ON news (source, published_at);
