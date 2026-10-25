"""Validate database input boundaries independently of PostgreSQL integration tests."""

import json
from datetime import UTC, datetime
from uuid import UUID

import pytest

from retrieval_ranking.config import Settings
from retrieval_ranking.db.context import ContextReader, parse_vector
from retrieval_ranking.db.runs import RunStore, json_safe
from retrieval_ranking.db.search import entity_names, lexical_terms
from retrieval_ranking.domain import (
    Candidate,
    CompanyContext,
    Facet,
    Preferences,
    RankingQuery,
    ScoredCandidate,
)
from retrieval_ranking.errors import ContextNotReady, RankingError

ID = UUID("00000000-0000-0000-0000-000000000001")


def context(*, preferences=None, keywords=None, entities=None, text="secret full profile"):
    return CompanyContext(
        company_id="U_TEST",
        profile_id=ID,
        profile_version=1,
        embedding_config_id=ID,
        embedding_metadata={},
        facets=[
            Facet(
                facet_id=ID,
                facet_type="activity",
                text=text,
                keywords=keywords or [],
                entities=entities or [],
                embedding=[1.0] + [0.0] * 1023,
            )
        ],
        preferences=preferences,
    )


@pytest.mark.parametrize(
    "value",
    [
        "null",
        "{}",
        "[]",
        "bad-json",
        json.dumps([0] * 1024),
        json.dumps([float("nan")] + [0] * 1023),
        json.dumps([float("inf")] + [0] * 1023),
    ],
)
def test_invalid_query_vector_fails_closed(value):
    with pytest.raises(ContextNotReady):
        parse_vector(value)


def test_finite_nonzero_vector_keeps_all_dimensions():
    vector = [-1.0] + [0.0] * 1023
    assert parse_vector(json.dumps(vector)) == vector


def test_fts_uses_declared_terms_not_context_prose():
    settings = Settings(database_url="postgresql://unused", max_fts_terms=4)
    preferences = Preferences(
        user_context_id=ID,
        user_id=ID,
        context_version=1,
        watchlist=["  ACME  ", "acme"],
        focus_topics=["Industrial   growth"],
        interest_markets=["Asia"],
    )
    value = context(
        preferences=preferences,
        keywords=["Supply", "industrial growth"],
        entities=[{"name": "Other", "description": "not a declared entity name"}],
    )
    assert lexical_terms(value, settings) == ["ACME", "Industrial growth", "Asia", "Supply"]


def test_fts_rejects_unbounded_or_unstructured_values():
    settings = Settings(database_url="postgresql://unused", max_term_characters=8)
    value = context(
        keywords=["x" * 9, "good"],
        entities=[
            {"name": "entity", "aliases": ["alias", 5], "description": "body"},
            {"id": "not-an-entity-name"},
            17,
            None,
        ],
    )
    assert lexical_terms(value, settings) == ["good", "entity", "alias"]
    assert entity_names({"name": "x", "aliases": "y"}) == ["x", None, "y"]


def test_personal_context_cannot_be_requested_without_user():
    settings = Settings(database_url="postgresql://unused")
    request = RankingQuery(
        company_id="U_TEST",
        as_of=datetime.now(UTC),
        top_k=1,
        user_context_id=ID,
    )
    with pytest.raises(RankingError, match="requires user_id"):
        ContextReader(settings).read(None, request)


def scored(article_id, score=1.0, rank=None):
    return ScoredCandidate(
        Candidate(
            article_id=article_id,
            content_version="v1",
            title="title",
            text="text",
            published_at=None,
            source_url="https://example.test",
            source_name="test",
        ),
        score=score,
        rank=rank,
    )


@pytest.mark.parametrize(
    "items",
    [
        [scored("a", rank=1), scored("a")],
        [scored("a", score=float("nan"), rank=1)],
        [scored("a", rank=2)],
        [scored("a")],
    ],
)
def test_run_validation_rejects_unreproducible_results(items):
    with pytest.raises(RankingError):
        RunStore._validate_items(items, top_k=1)


def test_all_scored_candidates_allow_null_rank_outside_top_k():
    RunStore._validate_items([scored("a", rank=1), scored("b")], top_k=1)
    RunStore._validate_items([], top_k=1)


def test_json_snapshot_converts_versions_and_rejects_nan():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert json_safe({"id": ID, "as_of": now}) == {"id": str(ID), "as_of": now.isoformat()}
    with pytest.raises(ValueError):
        json_safe({"score": float("nan")})
