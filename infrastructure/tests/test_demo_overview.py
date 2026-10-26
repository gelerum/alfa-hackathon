import json
from uuid import UUID

from fastapi.testclient import TestClient

from agent_common.core import current_user
from backend_service import api, observability


def test_demo_overview_needs_explicit_demo_mode(monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    client = TestClient(api.app)
    assert client.get("/api/dev/overview").status_code == 404
    assert client.get("/api/admin/observability").status_code == 401


def test_demo_overview_is_passwordless_but_admin_stays_protected(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setattr(observability, "public_snapshot", lambda: {"mode": "demo", "services": []})
    client = TestClient(api.app)
    assert client.get("/api/dev/overview").json() == {"mode": "demo", "services": []}
    assert client.get("/api/admin/observability").status_code == 401
    assert client.get("/api/admin/agent-runs").status_code == 401
    assert client.post("/api/admin/demo", json={}).status_code == 401


def test_public_overview_excludes_private_diagnostics(monkeypatch):
    monkeypatch.setattr(observability, "services", lambda: [
        {"name": "embeddings", "status": "down", "url": "https://secret:password@internal", "detail": "private", "latency_ms": 10},
        {"name": "llm", "status": "ok", "url": "https://provider", "detail": "model=private"},
    ])
    monkeypatch.setattr(observability, "workers", lambda: {"items": [
        {"role": "profiler", "worker_id": "private-host", "alive": True, "running_jobs": [{"company_id": "secret-company"}], "processed": 3, "failed": 1},
    ], "missing_roles": ["insight"]})
    monkeypatch.setattr(observability, "queues", lambda: [
        {"kind": "profile", "ready": 1, "running": 2, "last_error": "secret-value"},
    ])
    result = observability.public_snapshot()
    body = json.dumps(result, default=str)
    assert "secret" not in body and "private" not in body and "provider" not in body
    assert result["services"][1]["status"] == "configured"
    worker = next(row for row in result["workers"] if row["role"] == "profiler")
    assert worker == {"role": "profiler", "alive": True, "running": 1, "processed": 3, "failed": 1}
    assert len(result["workers"]) == 4


def test_discussions_include_card_and_scoped_source_details(monkeypatch):
    report_id = UUID("00000000-0000-0000-0000-000000000001")
    user_id = UUID("00000000-0000-0000-0000-000000000002")
    messages = [
        {"speaker": "user", "sources": [], "insight_id": "card-1", "content": "Почему?"},
        {"speaker": "assistant", "sources": ["source-1"], "insight_id": "card-1", "content": "Ответ"},
    ]
    calls = []

    def rows(query, args):
        calls.append((query, args))
        return messages if "FROM discussions" in query else [{"source_id": "source-1", "excerpt": "Цитата"}]

    monkeypatch.setattr(api, "owned_report", lambda report, user: {"company_id": "U_1"})
    monkeypatch.setattr(api, "all_rows", rows)
    api.app.dependency_overrides[current_user] = lambda: user_id
    try:
        response = TestClient(api.app).get(f"/api/reports/{report_id}/discussions")
    finally:
        api.app.dependency_overrides.pop(current_user, None)
    assert response.status_code == 200
    result = response.json()
    assert result[0]["source_details"] == []
    assert result[1]["source_details"][0]["excerpt"] == "Цитата"
    assert result[0]["insight_id"] == result[1]["insight_id"] == "card-1"
    assert calls[1][1] == (["source-1"], "U_1")
