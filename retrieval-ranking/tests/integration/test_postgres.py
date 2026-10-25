from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from retrieval_ranking.config import SCORE_TYPE, Settings
from retrieval_ranking.db import PostgresRepository
from retrieval_ranking.db.search import FTS_SQL
from retrieval_ranking.domain import RankingQuery, ScoredCandidate
from retrieval_ranking.errors import ContextNotReady, RankingError

pytestmark = pytest.mark.integration


def settings(dsn, **overrides):
    values = dict(
        database_url=dsn,
        vector_top_l=1,
        fts_top_b=20,
        max_facets=16,
        max_candidates=100,
    )
    return Settings(_env_file=None, **(values | overrides))


def query(seed, **overrides):
    return RankingQuery(**(dict(company_id=seed.COMPANY, as_of=seed.AS_OF, top_k=2) | overrides))


def test_hybrid_union_preserves_individual_hits(database_dsn, seed):
    repository = PostgresRepository(settings(database_dsn))
    repository.check_ready()
    batch = repository.retrieve(query(seed))
    candidates = {item.article_id: item for item in batch.candidates}

    assert set(candidates) == {"activity", "counterparty", "lexical", "lexical-without-vector"}
    assert batch.context.profile_id == seed.PROFILE_ID
    assert batch.context.embedding_config_id == seed.CONFIG_ID
    assert {facet.facet_id for facet in batch.context.facets} == {
        seed.ACTIVITY_FACET_ID,
        seed.COUNTERPARTY_FACET_ID,
    }
    assert {hit.method for hit in candidates["activity"].hits} == {"vector", "fts"}
    assert candidates["counterparty"].hits[0].facet_id == seed.COUNTERPARTY_FACET_ID
    vector_hit = next(hit for hit in candidates["activity"].hits if hit.method == "vector")
    assert vector_hit.score == pytest.approx(1)
    assert vector_hit.score_type == "cosine_similarity"
    assert vector_hit.rank == 1
    assert candidates["lexical-without-vector"].hits[0].method == "fts"
    assert "archived body" in candidates["activity"].text
    assert all(item.content_version == "v1" for item in batch.candidates)


def test_vector_filters_config_revision_duplicates_and_time(database_dsn, seed):
    repository = PostgresRepository(
        settings(
            database_dsn,
            vector_top_l=20,
            fts_top_b=0,
            max_candidates=400,
        )
    )
    batch = repository.retrieve(query(seed))
    assert {item.article_id for item in batch.candidates} == {
        "activity",
        "counterparty",
        "lexical",
        "personal",
    }
    assert all(len(item.hits) == 2 for item in batch.candidates)


def test_undated_policy_is_explicit(database_dsn, seed):
    repository = PostgresRepository(
        settings(
            database_dsn,
            vector_top_l=20,
            fts_top_b=0,
            max_candidates=400,
            include_undated=True,
        )
    )
    ids = {item.article_id for item in repository.retrieve(query(seed)).candidates}
    assert "undated" in ids
    assert "future" not in ids


def test_publication_lower_bound_can_yield_empty_valid_batch(database_dsn, seed):
    repository = PostgresRepository(settings(database_dsn))
    batch = repository.retrieve(
        query(
            seed,
            published_after=datetime(2026, 1, 2, tzinfo=UTC),
        )
    )
    assert batch.candidates == []


def test_personal_context_is_pinned_and_adds_a_query(database_dsn, seed):
    repository = PostgresRepository(settings(database_dsn))
    batch = repository.retrieve(
        query(
            seed,
            user_id=seed.USER_ID,
            user_context_id=seed.USER_CONTEXT_ID,
        )
    )
    assert batch.context.preferences.user_context_id == seed.USER_CONTEXT_ID
    assert batch.context.preferences.goals[0].goal_id == seed.PERSONAL_GOAL_ID
    assert len(batch.context.facets) == 3
    personal = next(item for item in batch.candidates if item.article_id == "personal")
    assert any(hit.facet_id == seed.PERSONAL_FACET_ID for hit in personal.hits)
    assert "АрктическийПорт" in batch.lexical_terms


@pytest.mark.parametrize("user", [UUID(int=99999), UUID(int=202)])
def test_inactive_or_unknown_membership_never_falls_back(database_dsn, seed, user):
    with pytest.raises(RankingError) as error:
        PostgresRepository(settings(database_dsn)).retrieve(query(seed, user_id=user))
    assert error.value.status_code == 403


def test_unavailable_explicit_context_never_falls_back(database_dsn, seed):
    repository = PostgresRepository(settings(database_dsn))
    with pytest.raises(ContextNotReady):
        repository.retrieve(query(seed, profile_id=seed.FUTURE_PROFILE_ID))
    with pytest.raises(ContextNotReady):
        repository.retrieve(query(seed, embedding_config_id=seed.OTHER_CONFIG_ID))
    with pytest.raises(ContextNotReady):
        repository.retrieve(query(seed, user_id=seed.USER_ID, user_context_id=UUID(int=99999)))


def test_latest_profile_selection_respects_as_of(database_dsn, seed):
    repository = PostgresRepository(settings(database_dsn))
    old = repository.retrieve(query(seed))
    new = repository.retrieve(
        query(
            seed,
            as_of=datetime(2026, 1, 30, tzinfo=UTC),
        )
    )
    assert old.context.profile_id == seed.PROFILE_ID
    assert new.context.profile_id == seed.FUTURE_PROFILE_ID


def test_context_budget_is_an_error_instead_of_dropping_facets(database_dsn, seed):
    repository = PostgresRepository(settings(database_dsn, max_facets=1))
    with pytest.raises(RankingError) as error:
        repository.retrieve(query(seed))
    assert error.value.code == "facet_budget_exceeded"


@pytest.mark.parametrize(
    "terms",
    [
        ["и", "в", "на"],
        ['" & | ! : (*)', "вольфрам'); DROP TABLE news; --"],
    ],
)
def test_fts_stopwords_and_operator_like_terms_are_safe(database_dsn, seed, terms):
    parameters = dict(
        terms=terms,
        limit=20,
        as_of=seed.AS_OF,
        known_at=seed.AS_OF,
        published_after=None,
        include_undated=False,
    )
    with psycopg.connect(database_dsn, row_factory=dict_row) as connection:
        rows = connection.execute(FTS_SQL, parameters).fetchall()
        assert connection.execute("SELECT count(*) AS n FROM news").fetchone()["n"] > 0
    if terms == ["и", "в", "на"]:
        assert rows == []


def test_complete_run_saves_all_scores_and_only_visible_top_k(database_dsn, seed):
    repository = PostgresRepository(settings(database_dsn))
    request = query(seed, top_k=1)
    batch = repository.retrieve(request)
    run_id = uuid4()
    repository.start_run(run_id, request, batch.context, {"synthetic": True})
    items = [
        ScoredCandidate(item, 10.0 - index, 1 if index == 0 else None, token_count=42)
        for index, item in enumerate(batch.candidates)
    ]
    repository.complete_run(run_id, items, {"candidate_count": len(items)})
    with psycopg.connect(database_dsn, row_factory=dict_row) as connection:
        run = connection.execute(
            "SELECT * FROM ranking_runs WHERE ranking_run_id=%s",
            (run_id,),
        ).fetchone()
        saved = connection.execute(
            "SELECT * FROM ranking_items WHERE ranking_run_id=%s ORDER BY score DESC",
            (run_id,),
        ).fetchall()
    assert run["status"] == "completed"
    assert run["profile_id"] == seed.PROFILE_ID
    assert run["embedding_config_id"] == seed.CONFIG_ID
    assert run["context_snapshot"]["ranking_result"]["candidate_count"] == len(items)
    assert len(saved) == len(batch.candidates)
    assert [item["rank"] for item in saved] == [1] + [None] * (len(saved) - 1)
    assert all(item["score_type"] == SCORE_TYPE for item in saved)
    assert all(item["retrieval_hits"] for item in saved)
    assert saved[0]["features_snapshot"]["token_count"] == 42
    with pytest.raises(RankingError):
        repository.complete_run(run_id, items, {})
    with psycopg.connect(database_dsn) as connection:
        with pytest.raises(psycopg.Error, match="completed ranking run is immutable"):
            connection.execute(
                "UPDATE ranking_runs SET status='failed' WHERE ranking_run_id=%s",
                (run_id,),
            )


def test_failed_item_write_rolls_back_before_run_is_marked_failed(database_dsn, seed):
    repository = PostgresRepository(settings(database_dsn))
    request = query(seed, top_k=1)
    batch = repository.retrieve(request)
    run_id = uuid4()
    repository.start_run(run_id, request, batch.context, {})
    good = batch.candidates[0]
    missing = replace(batch.candidates[1], content_version="never-existed")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        repository.complete_run(
            run_id,
            [
                ScoredCandidate(good, 2.0, 1),
                ScoredCandidate(missing, 1.0, None),
            ],
            {},
        )
    with psycopg.connect(database_dsn) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM ranking_items WHERE ranking_run_id=%s",
                (run_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT status FROM ranking_runs WHERE ranking_run_id=%s",
                (run_id,),
            ).fetchone()[0]
            == "running"
        )
    repository.fail_run(run_id)
    with psycopg.connect(database_dsn) as connection:
        assert (
            connection.execute(
                "SELECT status FROM ranking_runs WHERE ranking_run_id=%s",
                (run_id,),
            ).fetchone()[0]
            == "failed"
        )


def test_empty_run_completes_without_items(database_dsn, seed):
    repository = PostgresRepository(settings(database_dsn))
    request = query(seed, top_k=1)
    batch = repository.retrieve(request)
    run_id = uuid4()
    repository.start_run(run_id, request, batch.context, {})
    repository.complete_run(run_id, [], {"candidate_count": 0})
    with psycopg.connect(database_dsn) as connection:
        assert (
            connection.execute(
                "SELECT status FROM ranking_runs WHERE ranking_run_id=%s",
                (run_id,),
            ).fetchone()[0]
            == "completed"
        )


@pytest.mark.parametrize("kind", ["zero-vector", "hash-mismatch"])
def test_inconsistent_ready_query_embedding_is_rejected(database_dsn, seed, kind):
    profile_id = UUID(int=1001)
    facet_id = UUID(int=1002)
    embedding_id = UUID(int=1003)
    digest = sha256(kind.encode()).hexdigest()
    query_vector = seed.vector(0) if kind == "zero-vector" else seed.vector(1)
    with psycopg.connect(database_dsn) as connection:
        connection.execute(
            """INSERT INTO company_profiles
            (profile_id,company_id,profile_version,generator_version,created_at)
            VALUES (%s,%s,3,'synthetic-invalid-vector',%s)""",
            (profile_id, seed.COMPANY, seed.KNOWN),
        )
        connection.execute(
            """INSERT INTO search_embeddings
            (embedding_id,embedding_config_id,encoding_role,text_sha256,embedding,created_at)
            VALUES (%s,%s,'query',%s,%s::vector,%s)""",
            (embedding_id, seed.CONFIG_ID, digest, query_vector, seed.KNOWN),
        )
        connection.execute(
            """INSERT INTO company_context_facets
            (facet_id,company_id,profile_id,scope,facet_type,text,embedding_config_id,
             embedding_id,text_hash,generation_version,status,created_at,published_at)
            VALUES (%s,%s,%s,'company','activity','Invalid fixture facet',%s,%s,%s,
                    'synthetic-invalid-vector','ready',%s,%s)""",
            (
                facet_id,
                seed.COMPANY,
                profile_id,
                seed.CONFIG_ID,
                embedding_id,
                "wrong-hash" if kind == "hash-mismatch" else digest,
                seed.KNOWN,
                seed.KNOWN,
            ),
        )
        connection.execute(
            "UPDATE company_profiles SET status='ready',published_at=%s WHERE profile_id=%s",
            (seed.KNOWN, profile_id),
        )
    with pytest.raises(ContextNotReady):
        PostgresRepository(settings(database_dsn)).retrieve(query(seed, profile_id=profile_id))
