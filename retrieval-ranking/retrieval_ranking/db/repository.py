"""Compose context, search and trace adapters behind the ranking service port."""

from typing import Any
from uuid import UUID

from ..config import Settings
from ..domain import CompanyContext, RankingQuery, RetrievalBatch, ScoredCandidate
from ..errors import RankingError
from .connection import connect, set_timeout
from .context import ContextReader
from .runs import RunStore
from .search import NewsSearcher


class PostgresRepository:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.contexts = ContextReader(settings)
        self.news = NewsSearcher(settings)
        self.runs = RunStore(settings)

    def retrieve(self, query: RankingQuery) -> RetrievalBatch:
        with connect(self.settings) as connection, connection.transaction():
            # Pin context, ANN candidates and article revisions to one short database snapshot.
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
            )
            set_timeout(connection, self.settings)
            context = self.contexts.read(connection, query)
            candidates, terms = self.news.search(connection, context, query)
            return RetrievalBatch(context=context, candidates=candidates, lexical_terms=terms)

    def start_run(
        self,
        run_id: UUID,
        query: RankingQuery,
        context: CompanyContext,
        snapshot: dict[str, Any],
    ) -> None:
        with connect(self.settings) as connection, connection.transaction():
            set_timeout(connection, self.settings)
            self.runs.start(connection, run_id, query, context, snapshot)

    def complete_run(
        self,
        run_id: UUID,
        items: list[ScoredCandidate],
        metadata: dict[str, Any],
    ) -> None:
        with connect(self.settings) as connection, connection.transaction():
            set_timeout(connection, self.settings)
            self.runs.complete(connection, run_id, items, metadata)

    def fail_run(self, run_id: UUID) -> None:
        with connect(self.settings) as connection, connection.transaction():
            set_timeout(connection, self.settings)
            self.runs.fail(connection, run_id)

    def check_ready(self) -> None:
        with connect(self.settings) as connection, connection.transaction():
            set_timeout(connection, self.settings)
            extension = connection.execute(
                "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
            ).fetchone()
            if extension is None:
                raise RankingError("vector_extension_missing", "pgvector is required", 503)
            # Verify the concrete shared-schema columns used by this service, without requiring
            # a particular company to have been profiled before the API itself becomes ready.
            connection.execute(
                """
                SELECT p.profile_id, f.embedding_id, e.encoding_role, c.model_revision,
                       n.content_version, r.recorded_at, ne.embedding_config_id,
                       rr.context_snapshot, ri.features_snapshot
                FROM company_profiles p
                CROSS JOIN company_context_facets f
                CROSS JOIN search_embeddings e
                CROSS JOIN embedding_configs c
                CROSS JOIN news n
                CROSS JOIN news_revisions r
                CROSS JOIN news_embeddings ne
                CROSS JOIN ranking_runs rr
                CROSS JOIN ranking_items ri
                LIMIT 0
                """,
            )
