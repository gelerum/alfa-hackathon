import httpx
from fastapi.testclient import TestClient

from agent_common.core import execute, j, one, settings
from backend_service import observability
from backend_service.api import app
from backend_service.worker import claim, finish_logged, heartbeat

ADMIN = {"Authorization": "Bearer test-admin-token"}


def _fake_get(url, timeout):
    if "tool-gateway" in url:
        return httpx.Response(200, request=httpx.Request("GET", url))
    if "embeddings" in url:
        return httpx.Response(503, request=httpx.Request("GET", url))
    raise httpx.ConnectError("refused", request=httpx.Request("GET", url))


def _seed(monkeypatch):
    monkeypatch.setattr(settings, "gateway_url", "http://tool-gateway:8000")
    monkeypatch.setattr(settings, "embedding_base_url", "http://embeddings:8000")
    monkeypatch.setattr(settings, "ranker_url", "")
    monkeypatch.setattr(observability.httpx, "get", _fake_get)
    execute("INSERT INTO companies(company_id) VALUES('U_1')")
    user = one("INSERT INTO app_users(auth_subject) VALUES('dev') RETURNING user_id")["user_id"]
    execute("INSERT INTO company_memberships(company_id,user_id) VALUES('U_1',%s)", (user,))
    execute(
        "INSERT INTO jobs(kind,idempotency_key,payload) VALUES('profile','p1',%s),('profile','p2',%s)",
        (j({"company_id": "U_1"}), j({"company_id": "U_1"})),
    )
    counters = {"processed": 0, "failed": 0}
    first = claim(["profile"])
    assert first["started_at"] is not None
    heartbeat("profiler:host:1", "profiler", ["profile"], first["started_at"], [first], counters)
    finish_logged(first, {"ok": True, "result": {}}, 0, counters)
    second = claim(["profile"])
    finish_logged(second, {"ok": False, "error": "ValueError: Configure approved LLM base URL and API key"}, 0, counters)
    heartbeat("profiler:host:1", "profiler", ["profile"], first["started_at"], [], counters)
    run = one(
        "INSERT INTO agent_runs(agent_name,company_id,user_id,status,llm_tokens,tool_calls,finished_at) "
        "VALUES('company_profiler','U_1',%s,'completed',1200,3,now()) RETURNING agent_run_id",
        (user,),
    )["agent_run_id"]
    execute(
        "INSERT INTO agent_steps(agent_run_id,step_no,step_kind,tool_name,arguments,status) VALUES"
        "(%s,1,'tool_call','company_bank_data.get','{}','ok'),(%s,2,'validation',NULL,NULL,'error')",
        (run, run),
    )
    execute(
        "INSERT INTO tool_audit(agent_run_id,company_id,user_id,tool_name,arguments,status,result_count) "
        "VALUES(%s,'U_1',%s,'company_bank_data.get','{}','ok',1)",
        (run, user),
    )
    return run, counters


def test_snapshot_reports_workers_queues_failures_agents_and_services(database, monkeypatch):
    run, counters = _seed(monkeypatch)
    assert counters == {"processed": 2, "failed": 1}
    client = TestClient(app)
    assert client.get("/api/admin/observability").status_code == 401
    snap = client.get("/api/admin/observability", headers=ADMIN).json()
    services = {s["name"]: s["status"] for s in snap["services"]}
    assert services["postgres"] == "ok" and services["tool-gateway"] == "ok"
    assert services["embeddings"] == "error" and services["bank-materials-source"] == "down"
    assert services["retrieval-ranking"] == "not_configured"
    worker = snap["workers"]["items"][0]
    assert worker["alive"] is True and worker["processed"] == 2 and worker["failed"] == 1
    assert set(snap["workers"]["missing_roles"]) == {"backend", "documents", "insight"}
    queue = next(q for q in snap["queues"] if q["kind"] == "profile")
    assert queue["completed_1h"] == 1 and queue["retry_scheduled"] == 1
    assert "Configure approved LLM" in snap["failures"][0]["last_error"]
    agent = snap["agents"][0]
    assert agent["agent_name"] == "company_profiler" and agent["tokens"] == 1200 and agent["validation_errors"] == 1
    assert snap["tools"][0] == {**snap["tools"][0], "tool_name": "company_bank_data.get", "calls": 1, "errors": 0}
    assert snap["data"]["news"]["total"] == 0


def test_prometheus_and_agent_trace(database, monkeypatch):
    run, _ = _seed(monkeypatch)
    client = TestClient(app)
    assert client.get("/api/admin/metrics/prometheus").status_code == 401
    text = client.get("/api/admin/metrics/prometheus", headers=ADMIN).text
    assert 'alpha_service_up{service="tool-gateway"} 1.0' in text
    assert 'alpha_worker_alive{role="profiler",worker="profiler:host:1"} 1.0' in text
    assert 'alpha_jobs_retry_scheduled{kind="profile"} 1.0' in text
    assert 'alpha_agent_tokens{agent="company_profiler"} 1200.0' in text
    assert "alpha_news_last_fetch_age_seconds -1.0" in text
    runs = client.get("/api/admin/agent-runs?agent=company_profiler", headers=ADMIN).json()
    assert runs[0]["agent_run_id"] == str(run) and runs[0]["steps"] == 2 and runs[0]["errors"] == 1
    assert client.get("/api/admin/agent-runs?agent=insight_agent", headers=ADMIN).json() == []
    trace = client.get(f"/api/admin/agent-runs/{run}", headers=ADMIN).json()
    assert [s["step_kind"] for s in trace["steps"]] == ["tool_call", "validation"]
    assert trace["tool_audit"][0]["tool_name"] == "company_bank_data.get"
    missing = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/api/admin/agent-runs/{missing}", headers=ADMIN).status_code == 404


def test_dev_console_page_is_served():
    page = TestClient(app).get("/dev")
    assert page.status_code == 200 and "/frontend/dev.js" in page.text
