from fastapi.testclient import TestClient

from agent_common.core import digest, execute, one
from backend_service.api import app


def test_demo_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    assert TestClient(app).post("/api/demo/session").status_code == 404


def test_demo_automatically_creates_account_and_company(database, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    client = TestClient(app)
    response = client.post("/api/demo/session")
    assert response.status_code == 200, response.text
    session = response.json()
    assert session["demo"] is True
    headers = {"Authorization": "Bearer " + session["token"]}
    companies = client.get("/api/companies", headers=headers)
    assert companies.status_code == 200
    assert companies.json()[0]["company_id"] == session["company_id"]
    assert companies.json()[0]["is_fictional"] is True
    assert client.get(f"/api/companies/{session['company_id']}/context", headers=headers).status_code == 200


def test_demo_reuses_account_token_and_imported_company(database, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    execute("INSERT INTO companies(company_id) VALUES('U_IMPORTED')")
    client = TestClient(app)
    first = client.post("/api/demo/session").json()
    second = client.post("/api/demo/session").json()
    assert first == second
    assert first["company_id"] == "U_IMPORTED"
    assert one("SELECT count(*) AS n FROM app_users")["n"] == 1
    assert one("SELECT count(*) AS n FROM api_tokens")["n"] == 1
    assert one("SELECT count(*) AS n FROM company_memberships")["n"] == 1


def test_revoked_demo_token_is_not_reactivated(database, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    client = TestClient(app)
    session = client.post("/api/demo/session").json()
    execute("UPDATE api_tokens SET revoked_at=now() WHERE token_hash=%s", (digest(session["token"]),))
    assert client.post("/api/demo/session").status_code == 403
