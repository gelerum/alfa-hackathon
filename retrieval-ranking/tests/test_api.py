import pytest
from fastapi.testclient import TestClient
from retrieval_ranking.api import create_app
from retrieval_ranking.errors import ContextNotReady, RankingError
from test_service import (
    AS_OF,
    CONFIG_ID,
    PROFILE_ID,
    USER_CONTEXT_ID,
    USER_ID,
    FakeRepository,
    FakeReranker,
    settings,
)


def app_client(repository=None):
    repository = repository if repository is not None else FakeRepository()
    return TestClient(create_app(settings(), repository, FakeReranker(repository)))


def request(**changes):
    return {"company_id": "U_TEST", "as_of": AS_OF.isoformat(), "top_k": 2, **changes}


def test_corporate_response_exposes_news_and_channel_scores_not_vectors():
    with app_client() as client:
        response = client.post("/rank", json=request())
    assert response.status_code == 200
    value = response.json()
    assert value["company_id"] == "U_TEST"
    assert value["user_id"] is None
    assert value["user_context_id"] is None
    assert value["profile_id"] == str(PROFILE_ID)
    assert value["embedding_config_id"] == str(CONFIG_ID)
    assert value["candidate_count"] == 3
    assert [item["article_id"] for item in value["items"]] == ["a", "b"]
    assert [item["rank"] for item in value["items"]] == [1, 2]
    assert value["model_id"] == "Qwen/Qwen3-Reranker-0.6B"
    assert value["score_type"] == "qwen_yes_no_logit_margin"
    news = value["items"][0]
    assert news["content_version"] == "sha-v1"
    assert news["text"]
    assert [hit["score_type"] for hit in news["retrieval_hits"]] == [
        "cosine_similarity",
        "ts_rank_cd",
    ]
    assert '"embedding":' not in response.text


def test_personal_response_preserves_selected_context_identifiers():
    repository = FakeRepository(personal=True)
    with app_client(repository) as client:
        response = client.post(
            "/rank",
            json=request(
                user_id=str(USER_ID),
                user_context_id=str(USER_CONTEXT_ID),
                profile_id=str(PROFILE_ID),
                embedding_config_id=str(CONFIG_ID),
            ),
        )
    assert response.status_code == 200
    value = response.json()
    assert value["user_id"] == str(USER_ID)
    assert value["user_context_id"] == str(USER_CONTEXT_ID)
    selected = repository.retrieved[0]
    assert selected.profile_id == PROFILE_ID
    assert selected.embedding_config_id == CONFIG_ID


@pytest.mark.parametrize(
    "changes",
    [
        {"company_id": ""},
        {"company_id": " "},
        {"company_id": "x" * 129},
        {"user_id": "not-a-uuid"},
        {"user_context_id": str(USER_CONTEXT_ID)},
        {"top_k": 0},
        {"top_k": 101},
        {"unexpected": True},
        {"as_of": "2026-01-10T00:00:00"},
        {"published_after": "2026-01-11T00:00:00Z"},
        {"as_of": "2099-01-01T00:00:00Z"},
    ],
)
def test_invalid_http_requests_do_not_reach_repository(changes):
    repository = FakeRepository()
    with app_client(repository) as client:
        response = client.post("/rank", json=request(**changes))
    assert response.status_code == 422
    assert repository.retrieved == []


def test_health_and_ready_distinguish_process_and_database_readiness():
    repository = FakeRepository()
    with app_client(repository) as client:
        assert client.get("/health").json()["status"] == "ok"
        ready = client.get("/ready")
        assert ready.status_code == 200
        assert ready.json()["device"] == "cpu"
        repository.error_ready = RuntimeError("postgresql://test:SECRET@host/private")
        unavailable = client.get("/ready")
        assert unavailable.status_code == 503
        assert unavailable.json() == {"status": "not_ready"}
        assert "SECRET" not in unavailable.text
        assert client.get("/health").status_code == 200


def test_database_failure_http_response_hides_credentials():
    repository = FakeRepository()
    repository.error_read = RuntimeError("postgresql://test:SECRET@host/private")
    with app_client(repository) as client:
        response = client.post("/rank", json=request())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ranking_unavailable"
    assert "SECRET" not in response.text
    assert "host" not in response.text
    assert not repository.started


@pytest.mark.parametrize(
    "error, status, code",
    [
        (ContextNotReady(), 409, "context_not_ready"),
        (
            RankingError("company_membership_required", "An active membership is required", 403),
            403,
            "company_membership_required",
        ),
    ],
)
def test_context_and_membership_failures_have_explicit_status(error, status, code):
    repository = FakeRepository()
    repository.error_read = error
    with app_client(repository) as client:
        response = client.post("/rank", json=request())
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


def test_busy_http_response_has_retry_header_and_recovers():
    with app_client() as client:
        lock = client.app.state.service._lock
        assert lock.acquire(blocking=False)
        try:
            response = client.post("/rank", json=request())
        finally:
            lock.release()
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "1"
        assert response.json()["error"]["code"] == "ranker_busy"
        assert client.post("/rank", json=request()).status_code == 200


def test_openapi_documents_json_rank_contract():
    with app_client() as client:
        document = client.get("/openapi.json").json()
    assert set(document["paths"]) == {"/health", "/ready", "/rank"}
    assert "application/json" in document["paths"]["/rank"]["post"]["requestBody"]["content"]
    fields = document["components"]["schemas"]["RankRequest"]["properties"]
    assert "company_id" in fields and "top_k" in fields
