from datetime import timedelta

from fastapi.testclient import TestClient

from agent_common.core import execute, j, now, one, settings
from backend_service.api import app


def _session(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("DEMO_COMPANIES", "U_FISH=Нордаква Импорт")
    execute("INSERT INTO companies(company_id) VALUES('U_FISH'),('U_PARTNER')")
    batch = one(
        "INSERT INTO bank_import_batches(dataset,source_filename,status) VALUES('company','c.csv','ready') "
        "RETURNING source_snapshot_id"
    )["source_snapshot_id"]
    execute(
        "INSERT INTO bank_company_data(source_snapshot_id,source_row_number,company_id,main_okved_name,"
        "registration_city,raw_attributes) VALUES(%s,2,'U_FISH','Торговля рыбой','г. Санкт-Петербург','{}')",
        (batch,),
    )
    payments = one(
        "INSERT INTO bank_import_batches(dataset,source_filename,status) VALUES('payments','p.csv','ready') "
        "RETURNING source_snapshot_id"
    )["source_snapshot_id"]
    execute(
        "INSERT INTO bank_payment_edges(source_snapshot_id,source_row_number,payer,beneficiary,pay_amt,pay_cnt,"
        "raw_attributes) VALUES(%s,2,'U_PARTNER','U_FISH',100,1,'{}')",
        (payments,),
    )
    client = TestClient(app)
    session = client.post("/api/demo/session").json()
    return client, {"Authorization": "Bearer " + session["token"]}, session["user_id"]


def _stages(client, headers):
    body = client.get("/api/companies/U_FISH/pipeline", headers=headers)
    assert body.status_code == 200, body.text
    return body.json(), {stage["key"]: stage for stage in body.json()["stages"]}


def test_pipeline_explains_each_stage_and_known_failures(database, monkeypatch):
    monkeypatch.setattr(settings, "agent_max_steps", 8)
    client, headers, user_id = _session(monkeypatch)
    body, stages = _stages(client, headers)
    assert stages["focus"]["state"] == "idle"
    assert stages["bank"]["state"] == "done" and "контрагентов в платежах: 1" in stages["bank"]["detail"]
    assert stages["documents"]["state"] == "skipped"
    assert stages["profile"]["detail"] == "Начнётся после сохранения фокуса"
    assert stages["news"]["state"] == "error" and "parser" in stages["news"]["detail"]
    assert stages["report"]["state"] == "idle"
    assert body["profile"] is None and body["busy"] is False

    saved = client.post(
        "/api/companies/U_FISH/onboarding",
        headers=headers,
        json={"role": "Финансовый директор", "kpis": ["Маржа"]},
    )
    assert saved.status_code == 202
    body, stages = _stages(client, headers)
    assert stages["focus"]["state"] == "done" and "Финансовый директор" in stages["focus"]["detail"]
    assert stages["profile"]["state"] == "waiting" and body["busy"] is True

    execute("UPDATE jobs SET created_at=now()-interval '5 minutes' WHERE kind='profile'")
    _, stages = _stages(client, headers)
    assert "обработчик подготовки профиля не запущен" in stages["profile"]["detail"]

    execute(
        "UPDATE jobs SET attempts=1,last_error='ValueError: Configure approved LLM base URL and API key' "
        "WHERE kind='profile'"
    )
    body, stages = _stages(client, headers)
    assert stages["profile"]["state"] == "error" and "OPENAI_API_KEY" in stages["profile"]["detail"]
    assert stages["profile"]["retrying"] is True and body["busy"] is True

    execute("UPDATE jobs SET status='running',attempts=2 WHERE kind='profile'")
    run = one(
        "INSERT INTO agent_runs(agent_name,company_id,user_id) VALUES('company_profiler','U_FISH',%s) "
        "RETURNING agent_run_id",
        (user_id,),
    )["agent_run_id"]
    for number, (kind, tool, arguments, status) in enumerate(
        [
            ("plan", None, None, "ok"),
            ("tool_call", "company_bank_data.get", {}, "ok"),
            ("tool_result", "company_bank_data.get", None, "ok"),
            ("tool_call", "company_documents.search", {"query": "деятельность"}, "ok"),
            ("validation", None, None, "error"),
            (
                "validation",
                None,
                {"error": "Fact evidence quote must be an exact retrieved excerpt"},
                "error",
            ),
        ],
        start=1,
    ):
        execute(
            "INSERT INTO agent_steps(agent_run_id,step_no,step_kind,tool_name,arguments,result_summary,status) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (
                run,
                number,
                kind,
                tool,
                j(arguments if kind == "tool_call" else None),
                j(arguments if kind == "validation" else None),
                status,
            ),
        )
    _, stages = _stages(client, headers)
    assert stages["profile"]["state"] == "active"
    assert [step["text"] for step in stages["profile"]["steps"]] == [
        "Планирует шаги анализа",
        "Читает банковские данные компании",
        "Ищет в документах компании: «деятельность»",
        "Проверка не пройдена (попытка 1 из 8) — агент исправляет результат",
        "Проверка не пройдена (попытка 2 из 8): цитата к факту не совпадает дословно с источником"
        " — агент исправляет результат",
    ]

    execute(
        "INSERT INTO news(article_id,canonical_url,source_name,title,published_at,fetched_at,content_version) "
        "VALUES('a','https://n/a','rss','Квоты на вылов',now(),now(),'v1'),"
        "('b','https://n/b','rss','Старое',now()-interval '30 days',%s,'v1')",
        (now() - timedelta(hours=5),),
    )
    _, stages = _stages(client, headers)
    assert stages["news"]["state"] == "done" and "В базе 2 новостей, за 7 дней 1" in stages["news"]["detail"]


def test_pipeline_requires_membership(database, monkeypatch):
    client, headers, _ = _session(monkeypatch)
    assert client.get("/api/companies/U_PARTNER/pipeline", headers=headers).status_code in (403, 404)
