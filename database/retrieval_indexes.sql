CREATE INDEX IF NOT EXISTS news_revisions_publication_idx ON news_revisions(published_at DESC);
CREATE INDEX IF NOT EXISTS news_revisions_snapshot_idx
  ON news_revisions(article_id,recorded_at DESC,content_version DESC);
CREATE INDEX IF NOT EXISTS news_revisions_fts_idx ON news_revisions USING gin (
  to_tsvector('russian',coalesce(title,'') || ' ' || coalesce(body,'') || ' ' || coalesce(description,''))
);
