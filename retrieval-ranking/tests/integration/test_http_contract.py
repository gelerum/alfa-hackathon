import json
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from retrieval_ranking.api import create_app
from retrieval_ranking.config import Settings
from retrieval_ranking.db import PostgresRepository
from retrieval_ranking.domain import RerankScores

pytestmark = pytest.mark.integration


class DeterministicScorer:
    """Exercise score ordering without importing or loading any model."""

    def __init__(self):
        self.document_count = 0

    def score(self, context_text, documents):
        assert context_text
        self.document_count = len(documents)
        return RerankScores(
            margins=[float(index) for index in range(len(documents))],
            context_truncated=False,
            document_truncated=[False] * len(documents),
            token_counts=[1] * len(documents),
        )


@pytest.mark.parametrize("personal", [False, True])
def test_real_sql_http_and_atomic_trace_without_model(database_dsn, seed, personal):
    settings = Settings(_env_file=None, database_url=database_dsn, vector_top_l=1)
    scorer = DeterministicScorer()
    request = {
        "company_id": seed.COMPANY,
        "profile_id": str(seed.PROFILE_ID),
        "embedding_config_id": str(seed.CONFIG_ID),
        "as_of": seed.AS_OF.isoformat(),
        "top_k": 2,
    }
    if personal:
        request.update(
            user_id=str(seed.USER_ID),
            user_context_id=str(seed.USER_CONTEXT_ID),
        )
    app = create_app(settings, PostgresRepository(settings), scorer)
    with TestClient(app) as client:
        assert client.get("/ready").status_code == 200
        assert "/rank" in client.get("/openapi.json").json()["paths"]
        response = client.post("/rank", json=request)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["candidate_count"] == scorer.document_count >= 2
    assert [item["rank"] for item in body["items"]] == [1, 2]
    assert [item["score"] for item in body["items"]] == [
        float(scorer.document_count - 1),
        float(scorer.document_count - 2),
    ]
    assert all(item["retrieval_hits"] for item in body["items"])
    assert all(item["content_version"] == "v1" for item in body["items"])
    assert '"embedding":' not in response.text

    with psycopg.connect(database_dsn) as connection:
        run = connection.execute(
            "SELECT status, context_snapshot FROM ranking_runs WHERE ranking_run_id=%s",
            (body["ranking_run_id"],),
        ).fetchone()
        counts = connection.execute(
            "SELECT count(*), count(rank) FROM ranking_items WHERE ranking_run_id=%s",
            (body["ranking_run_id"],),
        ).fetchone()
    assert run[0] == "completed"
    assert counts == (scorer.document_count, 2)
    assert run[1]["profile_id"] == str(seed.PROFILE_ID)
    assert bool(run[1]["preferences"]) == personal
    assert all("embedding" not in facet for facet in run[1]["facets"])

    # This evidence is a technical contract check, never a Qwen quality/latency result.
    evidence = {
        "synthetic_vectors": True,
        "synthetic_scorer": True,
        "real_qwen": False,
        "technical_smoke_only": True,
        "personal": personal,
        "response": body,
        "persisted_candidate_count": counts[0],
        "persisted_visible_count": counts[1],
    }
    target = Path(".cache/release-readiness") / (
        "sql-http-personal.json" if personal else "sql-http-company.json"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8")
