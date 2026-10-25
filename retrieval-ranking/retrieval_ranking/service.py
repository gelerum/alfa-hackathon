"""Orchestrate one bounded retrieval/ranking run without holding a DB transaction."""

import hashlib
import logging
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from threading import Lock
from uuid import UUID, uuid4

from .config import FORMAT_VERSION, MODEL_ID, MODEL_REVISION, SCORE_TYPE, Settings
from .domain import CompanyContext, RankingQuery, RerankScores, RetrievalBatch, ScoredCandidate
from .errors import BusyError, RankingError
from .ports import Repository, Reranker
from .text import render_context, render_news

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RankingResult:
    run_id: UUID
    query: RankingQuery
    context: CompanyContext
    candidate_count: int
    items: list[ScoredCandidate]
    context_truncated: bool


class RankingService:
    def __init__(self, settings: Settings, repository: Repository, reranker: Reranker):
        self.settings = settings
        self.repository = repository
        self.reranker = reranker
        # One CPU model worker; callers receive 429 instead of an unbounded inference queue.
        self._lock = Lock()

    def rank(self, query: RankingQuery) -> RankingResult:
        self._validate_query(query)
        if not self._lock.acquire(blocking=False):
            raise BusyError()
        try:
            return self._rank(query)
        finally:
            self._lock.release()

    def _rank(self, query: RankingQuery) -> RankingResult:
        batch = self.repository.retrieve(query)
        self._validate_batch(batch, query)
        context_text = render_context(batch.context)
        run_id = uuid4()
        snapshot = self._snapshot(batch.context, context_text, batch.lexical_terms)
        self.repository.start_run(run_id, query, batch.context, snapshot)
        try:
            documents = [render_news(candidate) for candidate in batch.candidates]
            scores = (
                self.reranker.score(context_text, documents)
                if documents
                else RerankScores([], False, [], [])
            )
            self._validate_scores(scores, len(documents))
            ordered = sorted(
                zip(
                    batch.candidates,
                    scores.margins,
                    scores.document_truncated,
                    scores.token_counts,
                    strict=True,
                ),
                key=lambda item: (-item[1], item[0].article_id),
            )
            items = [
                ScoredCandidate(
                    candidate,
                    score,
                    index if index <= query.top_k else None,
                    truncated,
                    token_count,
                )
                for index, (candidate, score, truncated, token_count) in enumerate(ordered, start=1)
            ]
            self.repository.complete_run(
                run_id,
                items,
                {
                    "candidate_count": len(items),
                    "returned_count": min(query.top_k, len(items)),
                    "context_truncated": scores.context_truncated,
                    "score_type": SCORE_TYPE,
                },
            )
        except Exception:
            try:
                self.repository.fail_run(run_id)
            except Exception:
                logger.exception("Could not mark ranking run %s as failed", run_id)
            raise
        return RankingResult(
            run_id,
            query,
            batch.context,
            len(items),
            [item for item in items if item.rank is not None],
            scores.context_truncated,
        )

    def _validate_query(self, query: RankingQuery) -> None:
        if not query.company_id.strip() or len(query.company_id) > 128:
            raise RankingError("invalid_query", "company_id must contain 1 to 128 characters", 422)
        if not 1 <= query.top_k <= self.settings.max_top_k:
            raise RankingError("invalid_query", "top_k exceeds the configured API limit", 422)
        if query.user_context_id and not query.user_id:
            raise RankingError("invalid_query", "user_context_id requires user_id", 422)
        dates = [query.as_of] + ([query.published_after] if query.published_after else [])
        if any(value.tzinfo is None or value.utcoffset() is None for value in dates):
            raise RankingError("invalid_query", "Timestamps must include a UTC offset", 422)
        if query.as_of > datetime.now(UTC):
            raise RankingError("invalid_query", "as_of must not be in the future", 422)
        if query.published_after and query.published_after > query.as_of:
            raise RankingError("invalid_query", "published_after must not exceed as_of", 422)

    def _validate_batch(self, batch: RetrievalBatch, query: RankingQuery) -> None:
        context = batch.context
        preferences = context.preferences
        owner = preferences.user_id if preferences else None
        pinned = [
            (query.profile_id, context.profile_id),
            (query.embedding_config_id, context.embedding_config_id),
            (query.user_context_id, preferences.user_context_id if preferences else None),
        ]
        if (
            context.company_id != query.company_id
            or owner != query.user_id
            or any(expected is not None and expected != actual for expected, actual in pinned)
        ):
            raise RankingError(
                "invalid_retrieval_output",
                "Retrieved context differs from the requested owner or version",
                500,
            )
        articles = [candidate.article_id for candidate in batch.candidates]
        if len(articles) > self.settings.max_candidates or len(set(articles)) != len(articles):
            raise RankingError(
                "invalid_retrieval_output",
                "Retrieved candidate count or article uniqueness is invalid",
                500,
            )

    @staticmethod
    def _validate_scores(scores: RerankScores, count: int) -> None:
        lengths = [len(scores.margins), len(scores.document_truncated), len(scores.token_counts)]
        if any(length != count for length in lengths):
            raise RankingError("invalid_model_output", "Reranker output length mismatch", 500)
        if any(
            type(value) not in (int, float) or not math.isfinite(value) for value in scores.margins
        ):
            raise RankingError("invalid_model_output", "Reranker returned a non-finite score", 500)
        if any(type(value) is not int or value < 0 for value in scores.token_counts):
            raise RankingError(
                "invalid_model_output", "Reranker returned an invalid token count", 500
            )
        if type(scores.context_truncated) is not bool or any(
            type(value) is not bool for value in scores.document_truncated
        ):
            raise RankingError(
                "invalid_model_output",
                "Reranker returned invalid truncation flags",
                500,
            )

    def _snapshot(self, context: CompanyContext, text: str, terms: list[str]) -> dict:
        snapshot = asdict(context)
        # Vectors are already identified by immutable facet/config IDs; avoid duplicating them.
        for facet in snapshot["facets"]:
            facet.pop("embedding")
        snapshot.update(
            {
                "context_text": text,
                "context_text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "lexical_terms": terms,
                "format_version": FORMAT_VERSION,
                "model": {
                    "id": MODEL_ID,
                    "revision": MODEL_REVISION,
                    "device": "cpu",
                    "dtype": "float32",
                    "instruction": self.settings.instruction,
                },
                "retrieval_policy": {
                    "vector_top_l": self.settings.vector_top_l,
                    "fts_top_b": self.settings.fts_top_b,
                    "include_undated": self.settings.include_undated,
                    "ann_underfill": "exact_sql_fallback",
                },
                "token_policy": {
                    "max_tokens": self.settings.reranker_max_tokens,
                    "context_max_tokens": self.settings.context_max_tokens,
                },
            }
        )
        return snapshot
