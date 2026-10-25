"""Exercise pipeline behavior with deterministic adapters and no network/model downloads."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import UUID

import pytest

from retrieval_ranking.config import SCORE_TYPE, Settings
from retrieval_ranking.domain import (
    Candidate,
    CompanyContext,
    Facet,
    Preferences,
    RankingQuery,
    RerankScores,
    RetrievalBatch,
    RetrievalHit,
)
from retrieval_ranking.errors import BusyError, RankingError
from retrieval_ranking.service import RankingService

PROFILE_ID = UUID("10000000-0000-0000-0000-000000000001")
USER_ID = UUID("10000000-0000-0000-0000-000000000002")
USER_CONTEXT_ID = UUID("10000000-0000-0000-0000-000000000003")
CONFIG_ID = UUID("10000000-0000-0000-0000-000000000004")
FACET_ID = UUID("10000000-0000-0000-0000-000000000005")
AS_OF = datetime(2026, 1, 10, tzinfo=UTC)


def settings():
    return Settings(database_url="postgresql://test:secret@unused/test")


def context(*, personal=False):
    return CompanyContext(
        company_id="U_TEST",
        profile_id=PROFILE_ID,
        profile_version=1,
        embedding_config_id=CONFIG_ID,
        embedding_metadata={"dimensions": 1024},
        facets=[
            Facet(
                FACET_ID,
                "activity",
                "The company manufactures machinery",
                ["machinery"],
                [],
                [1.0] + [0.0] * 1023,
            )
        ],
        preferences=Preferences(
            USER_CONTEXT_ID,
            USER_ID,
            1,
            business_role="Purchasing manager",
        )
        if personal
        else None,
    )


def candidate(article_id):
    return Candidate(
        article_id=article_id,
        content_version="sha-v1",
        title=f"News {article_id}",
        text="Industrial equipment prices increased.",
        published_at=AS_OF,
        source_url=f"https://example.test/{article_id}",
        source_name="Synthetic",
        hits=[
            RetrievalHit(
                "vector", str(FACET_ID), 1, 0.2, "cosine_similarity", FACET_ID, "activity"
            ),
            RetrievalHit("fts", "fts:test", 2, 8.0, "ts_rank_cd"),
        ],
    )


def query(**overrides):
    return RankingQuery(**{"company_id": "U_TEST", "as_of": AS_OF, "top_k": 2, **overrides})


class FakeRepository:
    def __init__(self, candidates=None, *, personal=False):
        self.batch = RetrievalBatch(
            context(personal=personal),
            [candidate("c"), candidate("b"), candidate("a")] if candidates is None else candidates,
            ["machinery"],
        )
        self.started = []
        self.completed = []
        self.failed = []
        self.retrieved = []
        self.read_active = False
        self.error_read = None
        self.error_complete = None
        self.error_ready = None

    def retrieve(self, request):
        self.retrieved.append(request)
        self.read_active = True
        try:
            if self.error_read:
                raise self.error_read
            return self.batch
        finally:
            self.read_active = False

    def start_run(self, run_id, request, company, snapshot):
        assert not self.read_active
        self.started.append((run_id, request, company, snapshot))

    def complete_run(self, run_id, items, metadata):
        assert not self.read_active
        if self.error_complete:
            raise self.error_complete
        self.completed.append((run_id, items, metadata))

    def fail_run(self, run_id):
        self.failed.append(run_id)

    def check_ready(self):
        if self.error_ready:
            raise self.error_ready


class FakeReranker:
    def __init__(self, repository, scores=None):
        self.repository = repository
        self.output = (
            scores
            if scores is not None
            else RerankScores(
                [0.1, 0.9, 0.9],
                False,
                [False, True, False],
                [20, 30, 40],
            )
        )
        self.calls = []
        self.error = None

    def score(self, context_text, documents):
        # The repository read transaction must have ended and the run must already exist.
        assert not self.repository.read_active
        assert self.repository.started
        self.calls.append((context_text, documents))
        if self.error:
            raise self.error
        return self.output


def test_stable_top_k_and_all_candidate_trace_without_mixing_retrieval_scores():
    repository = FakeRepository()
    model = FakeReranker(repository)
    result = RankingService(settings(), repository, model).rank(query())
    assert [item.candidate.article_id for item in result.items] == ["a", "b"]
    assert [item.rank for item in result.items] == [1, 2]
    assert result.candidate_count == 3
    stored = repository.completed[0][1]
    assert [(item.candidate.article_id, item.rank) for item in stored] == [
        ("a", 1),
        ("b", 2),
        ("c", None),
    ]
    assert [item.score for item in stored] == [0.9, 0.9, 0.1]
    assert stored[1].document_truncated is True
    assert repository.completed[0][2]["score_type"] == SCORE_TYPE
    snapshot = repository.started[0][3]
    assert "embedding" not in snapshot["facets"][0]
    assert snapshot["lexical_terms"] == ["machinery"]
    assert snapshot["context_text"] == model.calls[0][0]
    assert len(snapshot["context_text_sha256"]) == 64
    assert not repository.failed


def test_fewer_candidates_than_top_k_are_all_returned():
    repository = FakeRepository([candidate("one")])
    model = FakeReranker(repository, RerankScores([-100.0], False, [False], [10]))
    result = RankingService(settings(), repository, model).rank(query(top_k=10))
    assert len(result.items) == 1
    assert result.items[0].score == -100.0  # No arbitrary relevance threshold.
    assert result.items[0].rank == 1


def test_empty_candidate_run_completes_without_model_inference():
    repository = FakeRepository([])
    model = FakeReranker(repository)
    result = RankingService(settings(), repository, model).rank(query())
    assert result.items == []
    assert result.candidate_count == 0
    assert not model.calls
    assert repository.completed[0][1] == []
    assert repository.completed[0][2]["returned_count"] == 0


@pytest.mark.parametrize(
    "output",
    [
        RerankScores([float("nan"), 1, 2], False, [False] * 3, [1] * 3),
        RerankScores([float("inf"), 1, 2], False, [False] * 3, [1] * 3),
        RerankScores([1, 2], False, [False] * 3, [1] * 3),
        RerankScores([1, 2, 3], False, [False] * 2, [1] * 3),
        RerankScores([1, 2, 3], False, [False] * 3, [1] * 2),
        RerankScores([1, 2, 3], False, [False] * 3, [-1, 1, 1]),
        RerankScores([1, 2, 3], False, [False] * 3, [float("nan"), 1, 1]),
        RerankScores([1, 2, 3], False, [False] * 3, [1.5, 1, 1]),
        RerankScores([1, 2, 3], False, [False] * 3, [True, 1, 1]),
        RerankScores([1, 2, 3], False, ["yes", False, False], [1] * 3),
    ],
)
def test_invalid_model_output_marks_started_run_failed(output):
    repository = FakeRepository()
    with pytest.raises(RankingError, match="Reranker"):
        RankingService(settings(), repository, FakeReranker(repository, output)).rank(query())
    assert repository.failed == [repository.started[0][0]]
    assert not repository.completed


def test_model_error_and_persistence_error_release_lock_and_mark_run_failed():
    repository = FakeRepository()
    model = FakeReranker(repository)
    service = RankingService(settings(), repository, model)
    model.error = RuntimeError("CPU model failure")
    with pytest.raises(RuntimeError, match="CPU model failure"):
        service.rank(query())
    model.error = None
    repository.error_complete = RuntimeError("transaction rolled back")
    with pytest.raises(RuntimeError, match="transaction rolled back"):
        service.rank(query())
    repository.error_complete = None
    assert service.rank(query()).candidate_count == 3
    assert len(repository.failed) == 2
    assert len(repository.completed) == 1


def test_concurrent_request_gets_busy_then_worker_becomes_available():
    repository = FakeRepository()
    model = FakeReranker(repository)
    entered = Event()
    release = Event()
    original = model.score

    def block(context_text, documents):
        entered.set()
        assert release.wait(5)
        return original(context_text, documents)

    model.score = block
    service = RankingService(settings(), repository, model)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(service.rank, query())
        try:
            assert entered.wait(5)
            with pytest.raises(BusyError):
                service.rank(query())
        finally:
            release.set()
        assert future.result(timeout=5).candidate_count == 3
    model.score = original
    assert service.rank(query()).candidate_count == 3


@pytest.mark.parametrize(
    "changes",
    [
        {"company_id": "  "},
        {"company_id": "x" * 129},
        {"top_k": 101},
        {"as_of": AS_OF.replace(tzinfo=None)},
        {"as_of": datetime.now(UTC) + timedelta(days=1)},
        {"published_after": AS_OF + timedelta(seconds=1)},
        {"published_after": AS_OF.replace(tzinfo=None)},
        {"user_context_id": USER_CONTEXT_ID},
    ],
)
def test_invalid_query_never_starts_database_or_model_work(changes):
    repository = FakeRepository()
    model = FakeReranker(repository)
    with pytest.raises(RankingError):
        RankingService(settings(), repository, model).rank(query(**changes))
    assert not repository.retrieved
    assert not repository.started
    assert not model.calls


def test_personal_identifiers_reach_repository_without_replacing_pinned_versions():
    repository = FakeRepository(personal=True)
    model = FakeReranker(repository)
    request = query(
        user_id=USER_ID,
        user_context_id=USER_CONTEXT_ID,
        profile_id=PROFILE_ID,
        embedding_config_id=CONFIG_ID,
    )
    result = RankingService(settings(), repository, model).rank(request)
    assert repository.retrieved == [request]
    assert result.context.preferences.user_context_id == USER_CONTEXT_ID
    assert repository.started[0][3]["preferences"]["user_id"] == USER_ID


@pytest.mark.parametrize(
    "company, request_changes",
    [
        (replace(context(), company_id="OTHER"), {}),
        (context(), {"profile_id": USER_CONTEXT_ID}),
        (context(), {"embedding_config_id": USER_CONTEXT_ID}),
        (context(personal=True), {}),
        (context(), {"user_id": USER_ID}),
        (context(personal=True), {"user_id": USER_ID, "user_context_id": PROFILE_ID}),
    ],
)
def test_retrieval_owner_and_versions_are_checked_before_persisting(company, request_changes):
    repository = FakeRepository()
    repository.batch = replace(repository.batch, context=company)
    with pytest.raises(RankingError, match="Retrieved context"):
        RankingService(settings(), repository, FakeReranker(repository)).rank(
            query(**request_changes),
        )
    assert not repository.started


@pytest.mark.parametrize(
    "candidates",
    [
        [candidate("duplicate"), candidate("duplicate")],
        [candidate(str(index)) for index in range(101)],
    ],
)
def test_retrieval_candidate_budget_and_uniqueness_are_checked_before_persisting(candidates):
    repository = FakeRepository(candidates)
    with pytest.raises(RankingError, match="candidate count or article uniqueness"):
        RankingService(settings(), repository, FakeReranker(repository)).rank(query())
    assert not repository.started
