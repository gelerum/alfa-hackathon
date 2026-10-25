from typing import Any, Protocol
from uuid import UUID

from .domain import CompanyContext, RankingQuery, RerankScores, RetrievalBatch, ScoredCandidate


class Reranker(Protocol):
    def score(self, context_text: str, documents: list[str]) -> RerankScores: ...


class Repository(Protocol):
    def retrieve(self, query: RankingQuery) -> RetrievalBatch: ...

    def start_run(
        self,
        run_id: UUID,
        query: RankingQuery,
        context: CompanyContext,
        snapshot: dict[str, Any],
    ) -> None: ...

    def complete_run(
        self,
        run_id: UUID,
        items: list[ScoredCandidate],
        metadata: dict[str, Any],
    ) -> None: ...

    def fail_run(self, run_id: UUID) -> None: ...

    def check_ready(self) -> None: ...
