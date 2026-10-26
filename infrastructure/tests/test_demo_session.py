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


def test_demo_links_all_showcase_companies_with_bank_summary(database, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("DEMO_COMPANIES", "U_FISH=Рыбный импортёр;U_MISSING=Нет в CSV;U_CHEM=Химия")
    execute("INSERT INTO companies(company_id) VALUES('U_OTHER'),('U_FISH'),('U_CHEM')")
    batch = one(
        "INSERT INTO bank_import_batches(dataset,source_filename,status) VALUES('company','c.csv','ready') "
        "RETURNING source_snapshot_id"
    )["source_snapshot_id"]
    execute(
        "INSERT INTO bank_company_data(source_snapshot_id,source_row_number,company_id,main_okved_name,"
        "registration_city,revenue_value,raw_attributes) VALUES(%s,2,'U_FISH','Торговля рыбой','г. Санкт-Петербург',"
        "54767618087,'{}')",
        (batch,),
    )
    client = TestClient(app)
    session = client.post("/api/demo/session").json()
    assert session["company_id"] == "U_FISH"
    assert session["company_ids"] == ["U_FISH", "U_CHEM"]
    rows = client.get("/api/companies", headers={"Authorization": "Bearer " + session["token"]}).json()
    by_id = {row["company_id"]: row for row in rows}
    assert set(by_id) == {"U_FISH", "U_CHEM"}
    assert by_id["U_FISH"]["legal_name"] == "Рыбный импортёр" and by_id["U_FISH"]["is_fictional"] is True
    assert by_id["U_FISH"]["registration_city"] == "г. Санкт-Петербург"
    assert by_id["U_CHEM"]["main_okved_name"] is None
    assert client.post("/api/demo/session").json() == session


def test_demo_switches_existing_account_to_showcase(database, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    execute("INSERT INTO companies(company_id) VALUES('U_1')")
    client = TestClient(app)
    first = client.post("/api/demo/session").json()
    assert first["company_id"] == "U_1"
    execute("INSERT INTO companies(company_id) VALUES('U_102920')")
    second = client.post("/api/demo/session").json()
    assert second["company_ids"] == ["U_102920"]
    rows = client.get("/api/companies", headers={"Authorization": "Bearer " + second["token"]}).json()
    assert [row["company_id"] for row in rows] == ["U_102920"]
    assert rows[0]["legal_name"] == "Нордаква Импорт"


def test_demo_prefill_answers_are_valid_and_served_per_company(database, monkeypatch):
    from agent_common.contracts import Answers
    from backend_service.demo import DEFAULT_ANSWERS, DEFAULT_DEMO_COMPANIES, DEMO_ANSWERS

    assert set(DEMO_ANSWERS) == {cid for cid, _ in DEFAULT_DEMO_COMPANIES}
    for answers in [DEFAULT_ANSWERS, *DEMO_ANSWERS.values()]:
        assert Answers.model_validate(answers).model_dump() == answers
        assert answers["kpis"]
    monkeypatch.setenv("DEMO_MODE", "true")
    execute("INSERT INTO companies(company_id) VALUES('U_102920'),('U_35210')")
    client = TestClient(app)
    session = client.post("/api/demo/session").json()
    rows = client.get("/api/companies", headers={"Authorization": "Bearer " + session["token"]}).json()
    by_id = {row["company_id"]: row["suggested_answers"] for row in rows}
    assert by_id == {"U_102920": DEMO_ANSWERS["U_102920"], "U_35210": DEMO_ANSWERS["U_35210"]}
    assert one("SELECT count(*) AS n FROM onboarding_answers")["n"] == 0
