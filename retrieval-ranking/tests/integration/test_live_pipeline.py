"""Opt-in full path: real SQL candidates, real pinned Qwen CPU, HTTP and persisted trace."""

import json
import os
from pathlib import Path
from time import perf_counter

import psycopg
import pytest
from fastapi.testclient import TestClient

from retrieval_ranking.api import create_app
from retrieval_ranking.config import MODEL_ID, MODEL_REVISION, Settings

pytestmark = [pytest.mark.integration, pytest.mark.model]


def test_real_sql_qwen_http_pipeline(database_dsn, seed, monkeypatch):
    if os.environ.get("RETR_RANK_RUN_MODEL_TESTS") != "1":
        pytest.skip("Set RETR_RANK_RUN_MODEL_TESTS=1 to execute the real CPU model")
    monkeypatch.setenv("HF_HOME", ".cache/huggingface")
    monkeypatch.setenv("HF_XET_CACHE", ".cache/huggingface/xet")
    monkeypatch.setenv("TOKENIZERS_PARALLELISM", "false")
    settings = Settings(database_url=database_dsn)
    app = create_app(settings=settings)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 200
        assert "/rank" in client.get("/openapi.json").json()["paths"]
        started = perf_counter()
        response = client.post(
            "/rank",
            json={
                "company_id": seed.COMPANY,
                "user_id": str(seed.USER_ID),
                "top_k": 3,
                "as_of": seed.AS_OF.isoformat(),
            },
        )
        elapsed = perf_counter() - started
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["model_id"] == MODEL_ID
    assert body["model_revision"] == MODEL_REVISION
    assert body["candidate_count"] >= len(body["items"]) == 3
    assert [item["rank"] for item in body["items"]] == [1, 2, 3]
    assert [item["score"] for item in body["items"]] == sorted(
        (item["score"] for item in body["items"]),
        reverse=True,
    )
    with psycopg.connect(database_dsn) as connection:
        run = connection.execute(
            "SELECT status, context_snapshot FROM ranking_runs WHERE ranking_run_id=%s",
            (body["ranking_run_id"],),
        ).fetchone()
        counts = connection.execute(
            """SELECT count(*), count(rank) FROM ranking_items WHERE ranking_run_id=%s""",
            (body["ranking_run_id"],),
        ).fetchone()
    assert run[0] == "completed"
    assert counts == (body["candidate_count"], 3)
    assert run[1]["profile_id"] == str(seed.PROFILE_ID)
    assert run[1]["preferences"]["user_context_id"] == str(seed.USER_CONTEXT_ID)
    assert all("embedding" not in facet for facet in run[1]["facets"])
    evidence = {
        "synthetic_vectors": True,
        "technical_smoke_only": True,
        "request_seconds_after_model_startup": elapsed,
        "response": body,
        "persisted_candidate_count": counts[0],
    }
    target = Path(".cache/live-pipeline-smoke.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8")
