"""Perform bounded vector/lexical retrieval in PostgreSQL and fetch only their union."""

import hashlib
import json
import math
import unicodedata
from collections import defaultdict
from typing import Any

from ..config import Settings
from ..domain import Candidate, CompanyContext, RankingQuery, RetrievalHit
from ..errors import RankingError

# Keep this expression identical to news_fts_idx in the shared schema.
FTS_DOCUMENT = (
    "to_tsvector('russian', coalesce(n.title,'') || ' ' || "
    "coalesce(n.body,'') || ' ' || coalesce(n.description,''))"
)
NEWS_FILTER = """
    n.duplicate_of_id IS NULL AND n.fetched_at <= %(as_of)s
    AND r.recorded_at <= %(as_of)s
    AND (
        (n.published_at IS NULL AND %(include_undated)s)
        OR (n.published_at <= %(as_of)s
            AND (%(published_after)s::timestamptz IS NULL
                 OR n.published_at >= %(published_after)s::timestamptz))
    )
"""
VECTOR_FROM = (
    """
    FROM news_embeddings e
    JOIN news n ON n.article_id = e.article_id AND n.content_version = e.content_version
    JOIN news_revisions r
      ON r.article_id = n.article_id AND r.content_version = n.content_version
    WHERE e.embedding_config_id = %(config)s AND e.created_at <= %(as_of)s
      AND vector_norm(e.embedding) > 0 AND
"""
    + NEWS_FILTER
)
VECTOR_SQL = (
    """
    SELECT e.article_id, e.content_version, e.embedding <=> %(vector)s::vector AS distance
"""
    + VECTOR_FROM
    + """
    ORDER BY e.embedding <=> %(vector)s::vector
    LIMIT %(limit)s
"""
)
# Materialization deliberately prevents an approximate index plan for the underfill retry.
EXACT_VECTOR_SQL = (
    """
    WITH eligible AS MATERIALIZED (
        SELECT e.article_id, e.content_version, e.embedding
"""
    + VECTOR_FROM
    + """
    )
    SELECT article_id, content_version, embedding <=> %(vector)s::vector AS distance
    FROM eligible ORDER BY distance, article_id LIMIT %(limit)s
"""
)
FTS_SQL = (
    """
    WITH individual AS (
        SELECT phraseto_tsquery('russian', term) AS query
        FROM unnest(%(terms)s::text[]) AS terms(term)
    ), lexical AS (
        SELECT coalesce(
            string_agg('(' || query::text || ')', ' | ') FILTER (WHERE numnode(query) > 0),
            ''
        )::tsquery AS query FROM individual
    )
    SELECT n.article_id, n.content_version,
           ts_rank_cd("""
    + FTS_DOCUMENT
    + """, lexical.query) AS score
    FROM news n
    JOIN news_revisions r
      ON r.article_id = n.article_id AND r.content_version = n.content_version
    CROSS JOIN lexical
    WHERE numnode(lexical.query) > 0
      AND """
    + FTS_DOCUMENT
    + """ @@ lexical.query AND """
    + NEWS_FILTER
    + """
    ORDER BY score DESC, n.article_id
    LIMIT %(limit)s
"""
)


class NewsSearcher:
    def __init__(self, settings: Settings):
        self.settings = settings

    def search(
        self,
        connection,
        context: CompanyContext,
        query: RankingQuery,
    ) -> tuple[list[Candidate], list[str]]:
        hits: dict[tuple[str, str], list[RetrievalHit]] = defaultdict(list)
        filters = {
            "as_of": query.as_of,
            "published_after": query.published_after,
            "include_undated": self.settings.include_undated,
        }
        # Iterative scans help pgvector 0.8+ refill results discarded by metadata filters.
        # Older pgvector ignores this custom setting; the exact underfill retry still applies.
        connection.execute("SELECT set_config('hnsw.iterative_scan', 'strict_order', true)")
        for facet in context.facets:
            self._vector_hits(connection, facet, context, filters, hits)
        terms = lexical_terms(context, self.settings)
        if terms and self.settings.fts_top_b:
            self._lexical_hits(connection, terms, filters, hits)
        if len(hits) > self.settings.max_candidates:
            raise RankingError(
                "candidate_budget_exceeded", "The candidate budget was exceeded", 500
            )
        return self._load_candidates(connection, hits), terms

    def _vector_hits(self, connection, facet, context, filters, hits) -> None:
        parameters = {
            **filters,
            "config": context.embedding_config_id,
            "vector": json.dumps(facet.embedding, allow_nan=False, separators=(",", ":")),
            "limit": self.settings.vector_top_l,
        }
        rows = connection.execute(VECTOR_SQL, parameters).fetchall()
        if len(rows) < self.settings.vector_top_l:
            rows = connection.execute(EXACT_VECTOR_SQL, parameters).fetchall()
        # SQL chooses neighbors; this sort only makes the returned hit ranks reproducible.
        rows.sort(key=lambda row: (row["distance"], row["article_id"]))
        for rank, row in enumerate(rows, start=1):
            if not math.isfinite(row["distance"]):
                raise RankingError("invalid_document_vector", "Non-finite cosine distance", 503)
            hits[(row["article_id"], row["content_version"])].append(
                RetrievalHit(
                    method="vector",
                    query_id=str(facet.facet_id),
                    rank=rank,
                    score=1.0 - row["distance"],
                    score_type="cosine_similarity",
                    facet_id=facet.facet_id,
                    facet_type=facet.facet_type,
                )
            )

    def _lexical_hits(self, connection, terms, filters, hits) -> None:
        rows = connection.execute(
            FTS_SQL,
            {**filters, "terms": terms, "limit": self.settings.fts_top_b},
        ).fetchall()
        term_hash = hashlib.sha256(
            json.dumps(terms, ensure_ascii=False).encode("utf-8"),
        ).hexdigest()[:16]
        for rank, row in enumerate(rows, start=1):
            hits[(row["article_id"], row["content_version"])].append(
                RetrievalHit(
                    method="fts",
                    query_id=f"fts:{term_hash}",
                    rank=rank,
                    score=float(row["score"]),
                    score_type="ts_rank_cd",
                )
            )

    @staticmethod
    def _load_candidates(connection, hits) -> list[Candidate]:
        if not hits:
            return []
        articles = [key[0] for key in sorted(hits)]
        versions = [key[1] for key in sorted(hits)]
        rows = connection.execute(
            """
            SELECT n.article_id, r.content_version, r.title, r.body, r.description,
                   n.published_at, n.canonical_url, n.source_name, n.event_id
            FROM unnest(%s::text[], %s::text[]) wanted(article_id, content_version)
            JOIN news n ON n.article_id = wanted.article_id
            JOIN news_revisions r ON r.article_id = wanted.article_id
                                 AND r.content_version = wanted.content_version
            ORDER BY n.article_id
            """,
            (articles, versions),
        ).fetchall()
        if len(rows) != len(hits):
            raise RankingError("candidate_revision_missing", "A retrieved revision is missing", 503)
        return [
            Candidate(
                article_id=row["article_id"],
                content_version=row["content_version"],
                title=row["title"],
                text=row["body"] or "",
                description=row["description"],
                published_at=row["published_at"],
                source_url=row["canonical_url"],
                source_name=row["source_name"],
                event_id=row["event_id"],
                hits=hits[(row["article_id"], row["content_version"])],
            )
            for row in rows
        ]


def lexical_terms(context: CompanyContext, settings: Settings) -> list[str]:
    """Use short declared search terms, with user watchlists taking priority when capped."""
    raw: list[Any] = []
    if context.preferences:
        raw.extend(context.preferences.watchlist)
        raw.extend(context.preferences.focus_topics)
        raw.extend(context.preferences.interest_markets)
    if context.company_name:
        raw.append(context.company_name)
    for facet in context.facets:
        raw.extend(facet.keywords)
        for entity in facet.entities:
            raw.extend(entity_names(entity))
    result = []
    seen = set()
    for value in raw:
        if not isinstance(value, str):
            continue
        term = " ".join(unicodedata.normalize("NFKC", value).split())
        if not term or len(term) > settings.max_term_characters:
            continue
        identity = term.casefold()
        if identity not in seen:
            result.append(term)
            seen.add(identity)
        if len(result) == settings.max_fts_terms:
            break
    return result


def entity_names(entity: Any) -> list[Any]:
    if isinstance(entity, str):
        return [entity]
    if not isinstance(entity, dict):
        return []
    names = [entity.get("name"), entity.get("canonical_name")]
    aliases = entity.get("aliases", [])
    if isinstance(aliases, list):
        names.extend(aliases)
    elif isinstance(aliases, str):
        names.append(aliases)
    return names
