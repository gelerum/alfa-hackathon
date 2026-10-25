import json
from datetime import timedelta
from uuid import uuid4

import httpx
import psycopg
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from profiler_service import service as profiler
from insight_service import service as insights
from tool_gateway_service import tools
from backend_service.api import app as backend_app
from agent_common.contracts import AgentScope, InsightRequest, SearchArgs
from agent_common.core import all_rows, db, enqueue, execute, j, now, one
from backend_service.documents import embed_chunks_batch, index_chunks
from tool_gateway_service.api import app as gateway_app
from backend_service.reports import create_report, publish_deadline_fallback, request_ranking
from agent_common.runtime import Runtime
from backend_service.worker import claim, finish


@pytest.fixture
def demo(database):
    with db() as conn:
        conn.execute(
            "INSERT INTO companies(company_id,legal_name,is_fictional) VALUES('U_demo','Вымышленный завод',true)"
        )
        user = conn.execute(
            "INSERT INTO app_users(auth_subject) VALUES('demo') RETURNING user_id"
        ).fetchone()["user_id"]
        conn.execute(
            "INSERT INTO company_memberships(company_id,user_id,access_role) VALUES('U_demo',%s,'admin')",
            (user,),
        )
        snapshot = conn.execute(
            "INSERT INTO bank_import_batches(dataset,source_filename,status) VALUES('company','test.csv','ready') "
            "RETURNING source_snapshot_id"
        ).fetchone()["source_snapshot_id"]
        conn.execute(
            "INSERT INTO bank_company_data(source_snapshot_id,source_row_number,company_id,main_okved_name,"
            "loan_mark,raw_attributes) VALUES(%s,2,'U_demo',%s,true,%s)",
            (snapshot, "Производство оборудования", j({"MAINOKVED_NAME": "Производство оборудования"})),
        )
        conn.execute(
            "INSERT INTO onboarding_answers(company_id,user_id,answers) VALUES(%s,%s,%s)",
            (
                "U_demo",
                user,
                j(
                    {
                        "role": "Финансовый директор",
                        "responsibilities": ["Финансы"],
                        "goals": ["Снизить стоимость финансирования"],
                        "note": "",
                    }
                ),
            ),
        )
    return {"company_id": "U_demo", "user_id": str(user)}


@pytest.fixture
def fake_embeddings(demo, monkeypatch):
    with db() as conn:
        cfg = conn.execute(
            "INSERT INTO embedding_configs(config_key,model_id,model_revision) "
            "VALUES('test','Qwen/Qwen3-Embedding-0.6B','rev') RETURNING *"
        ).fetchone()
    monkeypatch.setattr(profiler, "config", lambda **kw: cfg)
    monkeypatch.setattr(tools, "config", lambda **kw: cfg)
    monkeypatch.setattr("backend_service.documents.config", lambda **kw: cfg)
    monkeypatch.setattr(
        "agent_common.embeddings.embed", lambda texts, role, config: [[1.0] + [0.0] * 1023 for _ in texts]
    )
    monkeypatch.setattr(tools, "embed", lambda texts, role, config: [[1.0] + [0.0] * 1023 for _ in texts])
    monkeypatch.setattr(
        "backend_service.documents.embed", lambda texts, role, config: [[1.0] + [0.0] * 1023 for _ in texts]
    )
    gateway = TestClient(gateway_app)

    def gateway_post(url, **kwargs):
        assert "/tools/" in url
        result = gateway.post(
            "/tools/" + url.rsplit("/", 1)[1], json=kwargs["json"], headers=kwargs["headers"]
        )
        return httpx.Response(result.status_code, json=result.json(), request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", gateway_post)
    return cfg


def fake_profile_llm(self, messages):
    if "Review this candidate" in messages[-1]["content"]:
        return {"valid": True, "issues": []}
    source = one(
        "SELECT * FROM evidence_sources WHERE source_id=ANY(%s::uuid[]) AND source_type='bank_import' LIMIT 1",
        (list(self.sources),),
    )
    return {
        "action": "finish",
        "result": {
            "summary": "Компания производит оборудование.",
            "facts": [
                {
                    "field": "activity",
                    "value": "Производство оборудования",
                    "source_ids": [str(source["source_id"])],
                    "evidence_quote": "Производство оборудования",
                }
            ],
            "facets": [
                {
                    "facet_type": "activity",
                    "text": "Производство оборудования и промышленные рынки",
                    "fact_indexes": [0],
                    "keywords": ["оборудование"],
                }
            ],
            "missing_data": ["Условия кредитования неизвестны"],
            "conflicts": [],
        },
    }


def seed_news(article="a1", published_at=None):
    published_at = published_at or now() - timedelta(hours=3)
    with db() as conn:
        conn.execute(
            "INSERT INTO news(article_id,canonical_url,source_name,title,body,published_at,content_version) "
            "VALUES(%s,%s,'test','Ключевая ставка сохранена','Стоимость финансирования сохраняется.',%s,'v1')",
            (article, "https://example.org/" + article, published_at),
        )
        conn.execute(
            "INSERT INTO news_revisions(article_id,content_version,title,body,published_at) VALUES(%s,'v1',%s,%s,%s)",
            (article, "Ключевая ставка сохранена", "Стоимость финансирования сохраняется.", published_at),
        )


def test_profile_publishes_evidence_facets_and_private_preferences(demo, fake_embeddings, monkeypatch):
    monkeypatch.setattr(Runtime, "complete_json", fake_profile_llm)
    result = profiler.profile_job(demo)
    assert (
        one("SELECT status FROM company_profiles WHERE profile_id=%s", (result["profile_id"],))["status"]
        == "ready"
    )
    facets = all_rows("SELECT * FROM company_context_facets ORDER BY scope")
    assert {f["scope"] for f in facets} == {"company", "user_company"}
    profile = one("SELECT payload FROM company_profiles WHERE profile_id=%s", (result["profile_id"],))[
        "payload"
    ]
    assert "Снизить стоимость" not in json.dumps(profile, ensure_ascii=False)
    assert len(all_rows("SELECT * FROM user_preference_goals")) == 1
    # Side effects are idempotent even if a worker dies after the database commit.
    second = profiler.profile_job(demo)
    assert str(second["profile_id"]) == str(result["profile_id"])
    assert len(all_rows("SELECT * FROM user_preferences")) == 1
    with pytest.raises(psycopg.Error):
        execute("UPDATE company_profiles SET payload='{}' WHERE profile_id=%s", (result["profile_id"],))


def test_second_user_reuses_company_profile_without_reusing_personal_goals(
    demo, fake_embeddings, monkeypatch
):
    monkeypatch.setattr(Runtime, "complete_json", fake_profile_llm)
    first = profiler.profile_job(demo)
    with db() as conn:
        second_user = conn.execute(
            "INSERT INTO app_users(auth_subject) VALUES('second-user') RETURNING user_id"
        ).fetchone()["user_id"]
        conn.execute(
            "INSERT INTO company_memberships(company_id,user_id,access_role) VALUES('U_demo',%s,'analyst')",
            (second_user,),
        )
        conn.execute(
            "INSERT INTO onboarding_answers(company_id,user_id,answers) VALUES(%s,%s,%s)",
            (
                "U_demo",
                second_user,
                j(
                    {
                        "role": "Директор по закупкам",
                        "responsibilities": ["Закупки"],
                        "goals": ["Проверить поставщиков"],
                        "note": "",
                    }
                ),
            ),
        )
    second = profiler.profile_job({"company_id": "U_demo", "user_id": str(second_user)})
    assert str(first["profile_id"]) == str(second["profile_id"])
    assert first["user_context_id"] != second["user_context_id"]
    assert one("SELECT count(*) AS n FROM company_profiles")["n"] == 1
    scope = AgentScope(
        company_id="U_demo",
        user_id=second_user,
        agent_run_id=uuid4(),
        profile_id=second["profile_id"],
        user_context_id=second["user_context_id"],
        as_of=now(),
    )
    result = tools.context(scope)
    assert result["preferences"]["business_role"] == "Директор по закупкам"
    assert [g["text"] for g in result["user_goals"]] == ["Проверить поставщиков"]


def test_full_report_and_late_arrival_revision(demo, fake_embeddings, monkeypatch):
    monkeypatch.setattr(Runtime, "complete_json", fake_profile_llm)
    profiler.profile_job(demo)
    seed_news()
    report = create_report(**demo, trigger_type="on_demand", key="request-1")
    first_request = request_ranking(report)
    fallback_before = now()
    publish_deadline_fallback(report)
    assert len(all_rows("SELECT * FROM report_versions")) == 1

    def insight_llm(self, messages):
        if "Review this candidate" in messages[-1]["content"]:
            return {"valid": True, "issues": []}
        articles = one("SELECT input_snapshot FROM agent_runs WHERE agent_run_id=%s", (self.run_id,))[
            "input_snapshot"
        ]["ranking_input"]["articles"]
        ids = [a["article_id"] for a in articles]
        source = one(
            "SELECT * FROM evidence_sources WHERE source_id=ANY(%s::uuid[]) AND source_type='news' LIMIT 1",
            (list(self.sources),),
        )
        fact_id = str(next(iter(self.fact_ids)))
        return {
            "action": "finish",
            "result": {
                "summary": "Проверьте чувствительность финансового плана к стоимости заёмных средств.",
                "cards": [
                    {
                        "title": "Стоимость финансирования",
                        "event_summary": "Ставка сохранена.",
                        "business_impact": "Для производителя важно проверить бюджет финансирования.",
                        "mechanism": "Если компания использует заёмные средства, их стоимость зависит от условий договора.",
                        "role_action": "Финансовому директору — проверить условия и сценарии.",
                        "article_ids": ids,
                        "claims": [
                            {
                                "kind": "verified_fact",
                                "statement": "Ключевая ставка сохранена",
                                "source_ids": [str(source["source_id"])],
                            },
                            {
                                "kind": "ai_inference",
                                "statement": "Следует проверить чувствительность бюджета.",
                                "source_ids": [str(source["source_id"])],
                                "fact_ids": [fact_id],
                            },
                        ],
                        "assumptions": ["Условия долга неизвестны"],
                    }
                ],
                "limitations": ["Аналитика Альфа-Банка по событию отсутствует."],
                "changes": ["Обработаны доступные новости."],
            },
        }

    monkeypatch.setattr(Runtime, "complete_json", insight_llm)
    accepted = insights.accept_insight(
        InsightRequest(request_id=first_request["request_id"], article_ids=["a1"])
    )
    job = one("SELECT * FROM jobs WHERE job_id=%s", (accepted["job_id"],))
    first = insights.insight_job(job["payload"])
    assert first["version"] == 2
    assert one("SELECT count(*) AS n FROM insight_claim_sources")["n"] == 2
    assert one("SELECT count(*) AS n FROM insight_claim_facts")["n"] == 1
    assert one("SELECT input_snapshot FROM report_versions WHERE version=1")["input_snapshot"][
        "deadline_fallback"
    ]
    assert insights.insight_job(job["payload"])["report_version_id"] == first["report_version_id"]
    # Published before cutoff, fetched only after first report: same fixed window, new version.
    seed_news("a2", report["window_end"] - timedelta(hours=2))
    second_request = request_ranking(report)
    assert second_request["request_id"] != first_request["request_id"]
    accepted2 = insights.accept_insight(
        InsightRequest(request_id=second_request["request_id"], article_ids=["a1", "a2"])
    )
    job2 = one("SELECT * FROM jobs WHERE job_id=%s", (accepted2["job_id"],))
    second = insights.insight_job(job2["payload"])
    assert second["version"] == 3
    assert one("SELECT window_end FROM reports")["window_end"] == report["window_end"]
    assert one("SELECT published_at FROM report_versions WHERE version=1")["published_at"] >= fallback_before
    with pytest.raises(psycopg.Error):
        execute(
            "UPDATE report_versions SET summary='changed' WHERE report_version_id=%s",
            (first["report_version_id"],),
        )
    claim = one("SELECT * FROM insight_claims LIMIT 1")
    with pytest.raises(psycopg.Error):
        execute("UPDATE insight_claims SET statement='changed' WHERE claim_id=%s", (claim["claim_id"],))
    # An irrelevant late article changes the retrieval corpus, but not the selected evidence.
    seed_news("irrelevant", report["window_end"] - timedelta(hours=1))
    third_request = request_ranking(report)
    accepted3 = insights.accept_insight(
        InsightRequest(request_id=third_request["request_id"], article_ids=["a1", "a2"])
    )
    job3 = one("SELECT * FROM jobs WHERE job_id=%s", (accepted3["job_id"],))

    def forbidden_llm(*args):
        raise AssertionError("Unchanged selected evidence must not consume LLM tokens")

    monkeypatch.setattr(Runtime, "complete_json", forbidden_llm)
    reused = insights.insight_job(job3["payload"])
    assert reused["unchanged"] is True and reused["version"] == 3
    assert len(all_rows("SELECT * FROM report_versions")) == 3


def test_scheduler_publishes_honest_deadline_bulletin_and_recovers_idempotently(demo, monkeypatch):
    from agent_common.core import monday_window
    from backend_service.reports import scheduler_tick

    end = monday_window(now(), upcoming=True)[1]
    monkeypatch.setattr("backend_service.reports.now", lambda: end)
    scheduler_tick()
    report = one("SELECT * FROM reports")
    assert report["window_end"] == end
    assert report["window_start"] == end - timedelta(days=7)
    version = one("SELECT * FROM report_versions")
    assert version["input_snapshot"]["deadline_fallback"] is True
    assert not all_rows("SELECT * FROM insights")
    scheduler_tick()
    assert len(all_rows("SELECT * FROM reports")) == 1
    assert len(all_rows("SELECT * FROM report_versions")) == 1


def test_queue_leases_and_idempotency(database):
    job = enqueue("test", "key", {"a": 1})
    assert enqueue("test", "key", {"a": 1})["job_id"] == job["job_id"]
    with pytest.raises(HTTPException):
        enqueue("test", "key", {"a": 2})
    running = claim(["test"])
    assert running["attempts"] == 1
    assert claim(["test"]) is None
    execute("UPDATE jobs SET lease_until=now()-interval '1 second' WHERE job_id=%s", (job["job_id"],))
    recovered = claim(["test"])
    assert recovered["lease_token"] != running["lease_token"]
    finish(running, {"ok": True, "result": "stale"})
    assert one("SELECT status FROM jobs")["status"] == "running"
    finish(recovered, {"ok": True, "result": "correct"})
    assert one("SELECT result FROM jobs")["result"] == "correct"


def test_rag_tenant_isolation_and_deferred_embeddings(demo, fake_embeddings):
    with db() as conn:
        conn.execute("INSERT INTO companies(company_id) VALUES('U_other')")
        document = conn.execute(
            "INSERT INTO documents(company_id,source_type,title) VALUES('U_demo','user_upload','Завод') "
            "RETURNING document_id"
        ).fetchone()["document_id"]
        other = conn.execute(
            "INSERT INTO documents(company_id,source_type,title) VALUES('U_other','user_upload','Секрет') "
            "RETURNING document_id"
        ).fetchone()["document_id"]
    index_chunks(
        sections=[("Производство оборудования", {"page": 1})],
        title="Завод",
        kind="company_document",
        company_id="U_demo",
        document_id=document,
    )
    index_chunks(
        sections=[("Производство секретного оборудования", {"page": 1})],
        title="Секрет",
        kind="company_document",
        company_id="U_other",
        document_id=other,
    )
    scope = AgentScope(**demo, agent_run_id=uuid4(), as_of=now())
    result = tools.search(scope, "company_documents.search", SearchArgs(query="оборудование"))
    assert len(result["sources"]) == 1
    assert result["sources"][0]["company_id"] == "U_demo"
    embed_chunks_batch()
    assert one("SELECT count(*) AS n FROM rag_chunks WHERE embedding IS NOT NULL")["n"] == 2
    with pytest.raises(psycopg.Error):
        execute("UPDATE rag_chunks SET content='tampered'")


def test_original_documents_are_pinned_to_profile_not_latest_uploads(demo, fake_embeddings, monkeypatch):
    from agent_common.contracts import ReadArgs

    monkeypatch.setattr(Runtime, "complete_json", fake_profile_llm)

    def add_document(title):
        with db() as conn:
            document_id = conn.execute(
                "INSERT INTO documents(company_id,source_type,title,processing_status) "
                "VALUES('U_demo','user_upload',%s,'ready') RETURNING document_id",
                (title,),
            ).fetchone()["document_id"]
        index_chunks(
            sections=[("Производство оборудования " + title, {"page": 1})],
            title=title,
            kind="company_document",
            company_id="U_demo",
            document_id=document_id,
        )
        return document_id

    old_doc = add_document("Original")
    published = profiler.profile_job(demo)
    new_doc = add_document("Later upload")
    scope = AgentScope(
        **demo,
        agent_run_id=uuid4(),
        profile_id=published["profile_id"],
        user_context_id=published["user_context_id"],
        as_of=now(),
    )
    found = tools.search(scope, "company_documents.search", SearchArgs(query="оборудование"))["sources"]
    assert {s["document_id"] for s in found} == {old_doc}
    new_source = one("SELECT source_id FROM rag_chunks WHERE document_id=%s", (new_doc,))["source_id"]
    with pytest.raises(HTTPException):
        tools.read(scope, "company_documents.read", ReadArgs(source_id=new_source))


def test_gateway_scope_is_server_pinned_and_capabilities_are_separate(demo, monkeypatch):
    from agent_common.core import settings

    monkeypatch.setattr(settings, "agent_max_tool_calls", 2)
    runtime = Runtime("company_profiler", **demo)
    client = TestClient(gateway_app)
    headers = {"Authorization": "Bearer test-internal-token"}
    body = {"scope": runtime.scope.model_dump(mode="json"), "arguments": {}}
    assert client.post("/tools/company_context.get", headers=headers, json=body).status_code == 200
    changed = {**body, "scope": {**body["scope"], "profile_id": str(uuid4())}}
    assert client.post("/tools/company_context.get", headers=headers, json=changed).status_code == 403
    assert (
        client.post(
            "/tools/news.search", headers=headers, json={**body, "arguments": {"query": "ставка"}}
        ).status_code
        == 403
    )
    assert client.post("/tools/company_context.get", headers=headers, json=body).status_code == 429
    assert (
        one("SELECT tool_calls FROM agent_runs WHERE agent_run_id=%s", (runtime.run_id,))["tool_calls"] == 2
    )
    execute("UPDATE company_memberships SET is_active=false")
    assert client.post("/tools/company_context.get", headers=headers, json=body).status_code == 403
    runtime.finish(False)


def test_gateway_run_capability_expires(demo):
    runtime = Runtime("company_profiler", **demo)
    execute(
        "UPDATE agent_runs SET started_at=now()-interval '11 minutes' WHERE agent_run_id=%s",
        (runtime.run_id,),
    )
    response = TestClient(gateway_app).post(
        "/tools/company_context.get",
        headers={"Authorization": "Bearer test-internal-token"},
        json={"scope": runtime.scope.model_dump(mode="json"), "arguments": {}},
    )
    assert response.status_code == 403
    runtime.finish(False)


def test_bank_payment_analytics_preserves_unknown_units_and_excludes_self(demo):
    with db() as conn:
        snapshot = conn.execute(
            "INSERT INTO bank_import_batches(dataset,source_filename,status) VALUES('payments','p.csv','ready') "
            "RETURNING source_snapshot_id"
        ).fetchone()["source_snapshot_id"]
        for n, beneficiary, amount in [
            (2, "counterparty-a", "900.01"),
            (3, "counterparty-b", "100.00"),
            (4, "U_demo", "5000"),
        ]:
            conn.execute(
                "INSERT INTO bank_payment_edges(source_snapshot_id,source_row_number,payer,beneficiary,pay_amt,pay_cnt) "
                "VALUES(%s,%s,'U_demo',%s,%s,1)",
                (snapshot, n, beneficiary, amount),
            )
    scope = AgentScope(**demo, agent_run_id=uuid4(), as_of=now())
    result = tools.bank_data(scope)
    assert result["payment_aggregates"][0]["currency"] is None
    assert result["payment_aggregates"][0]["aggregation_from"] is None
    assert str(result["payment_aggregates"][0]["amount"]) == "1000.01"
    assert len(result["top_counterparties"]) == 2
    derived = next(s for s in result["sources"] if s["source_type"] == "derived")
    assert "1000.01" in derived["excerpt"]


def test_person_links_are_network_signals_not_identity_and_inputs_wait_for_ready(demo):
    from agent_common.core import digest

    with db() as conn:
        before = digest(profiler.company_inputs(conn, "U_demo"))
        snapshot = conn.execute(
            "INSERT INTO bank_import_batches(dataset,source_filename,status) VALUES('people','people.csv','loading') "
            "RETURNING source_snapshot_id"
        ).fetchone()["source_snapshot_id"]
        for row, company in [(2, "U_demo"), (3, "U_peer"), (4, "U_peer")]:
            conn.execute(
                "INSERT INTO bank_person_links(source_snapshot_id,source_row_number,synthetic_f,synthetic_u) "
                "VALUES(%s,%s,'F_shared',%s)",
                (snapshot, row, company),
            )
        assert digest(profiler.company_inputs(conn, "U_demo")) == before
        conn.execute("UPDATE bank_import_batches SET status='ready' WHERE source_snapshot_id=%s", (snapshot,))
        assert digest(profiler.company_inputs(conn, "U_demo")) != before
    scope = AgentScope(**demo, agent_run_id=uuid4(), as_of=now())
    result = tools.bank_data(scope)
    assert result["person_network"] == {"linked_people": 1, "linked_companies": 1}
    assert any("do not establish ownership" in limitation for limitation in result["limitations"])
    assert len(all_rows("SELECT * FROM app_users")) == 1


def test_backend_upload_extraction_and_job_status_are_connected(demo):
    import io
    from docx import Document
    from agent_common.core import digest
    from backend_service.worker import handle

    execute(
        "INSERT INTO api_tokens(token_hash,user_id) VALUES(%s,%s)", (digest("upload-user"), demo["user_id"])
    )
    client = TestClient(backend_app)
    headers = {"Authorization": "Bearer upload-user"}
    doc = Document()
    doc.add_paragraph("Компания производит промышленное оборудование.")
    buffer = io.BytesIO()
    doc.save(buffer)
    response = client.post(
        "/api/companies/U_demo/documents",
        headers=headers,
        files={"file": ("company.docx", buffer.getvalue())},
    )
    assert response.status_code == 202, response.text
    uploaded = response.json()
    job = one("SELECT * FROM jobs WHERE job_id=%s", (uploaded["job_id"],))
    assert client.get("/api/jobs/" + uploaded["job_id"], headers=headers).status_code == 200
    handle("extract_document", job["payload"])
    assert (
        one("SELECT processing_status FROM documents WHERE document_id=%s", (uploaded["document_id"],))[
            "processing_status"
        ]
        == "ready"
    )
    assert one("SELECT count(*) AS n FROM rag_chunks")["n"] > 0
    assert client.get("/api/documents/" + uploaded["document_id"] + "/content").status_code == 401
    assert (
        client.get("/api/documents/" + uploaded["document_id"] + "/content", headers=headers).content
        == buffer.getvalue()
    )
    execute(
        "UPDATE jobs SET status='failed',attempts=max_attempts,finished_at=now() WHERE job_id=%s",
        (uploaded["job_id"],),
    )
    retried = client.post("/api/jobs/" + uploaded["job_id"] + "/retry", headers=headers)
    assert retried.status_code == 202
    recovered = client.get("/api/jobs/" + uploaded["job_id"], headers=headers).json()
    assert recovered["status"] == "pending" and recovered["attempts"] == 0
    answer = client.post(
        "/api/companies/U_demo/onboarding",
        headers=headers,
        json={"role": "CFO", "responsibilities": ["Финансы"], "goals": ["Контроль затрат"]},
    )
    assert answer.status_code == 202, answer.text
    assert client.get("/api/jobs/" + answer.json()["job_id"], headers=headers).json()["status"] == "pending"


def test_public_api_auth_and_cross_tenant(demo):
    client = TestClient(backend_app)
    assert client.get("/api/companies").status_code == 401
    from agent_common.core import digest

    execute(
        "INSERT INTO api_tokens(token_hash,user_id) VALUES(%s,%s)", (digest("user-secret"), demo["user_id"])
    )
    headers = {"Authorization": "Bearer user-secret"}
    assert client.get("/api/companies", headers=headers).status_code == 200
    assert client.get("/api/companies/U_other/reports", headers=headers).status_code == 403
    assert client.post("/api/admin/demo", headers=headers, json={}).status_code == 401


def test_parser_revisits_full_seven_days_instead_of_latest_watermark(database):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "tested_master_parser", (Path(__file__).resolve().parents[2] / "parser/master_parser.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    seed_news("already-latest", now() - timedelta(hours=1))
    seen = []

    class Source:
        name = "test"

        def fetch(self, since):
            seen.append(since)
            return [
                {
                    "source": "test",
                    "title": "Late recovered article",
                    "link": "https://example.org/recovered",
                    "date": now() - timedelta(days=6),
                    "text": "Recovered material.",
                }
            ]

    parser = module.Master_Parser(database, [Source()])
    assert parser.run()["test"] == 1
    assert seen[0] < now() - timedelta(days=6, hours=23)
    assert parser.run()["test"] == 0
    assert one("SELECT count(*) AS n FROM news")["n"] == 2


def test_callback_rejects_future_and_out_of_window(demo, fake_embeddings, monkeypatch):
    monkeypatch.setattr(Runtime, "complete_json", fake_profile_llm)
    profiler.profile_job(demo)
    report = create_report(**demo, trigger_type="on_demand", key="callback-test")
    request = request_ranking(report)
    seed_news("future", report["window_end"] + timedelta(seconds=1))
    with pytest.raises(HTTPException) as exc:
        insights.accept_insight(InsightRequest(request_id=request["request_id"], article_ids=["future"]))
    assert exc.value.status_code == 422


def test_primary_citations_must_use_the_pinned_revision(demo, fake_embeddings, monkeypatch):
    from types import SimpleNamespace
    from agent_common.contracts import InsightDraft

    monkeypatch.setattr(Runtime, "complete_json", fake_profile_llm)
    profile = profiler.profile_job(demo)
    seed_news()
    pinned = [{"article_id": "a1", "content_version": "v1"}]
    old_source = insights.article_evidence(pinned)[0]
    with db() as conn:
        conn.execute(
            "INSERT INTO news_revisions(article_id,content_version,title,body,published_at) "
            "SELECT article_id,'v2','Изменённое событие','Обновлённые сведения.',published_at FROM news WHERE article_id='a1'"
        )
        conn.execute("UPDATE news SET content_version='v2' WHERE article_id='a1'")
    new_source = insights.article_evidence([{"article_id": "a1", "content_version": "v2"}])[0]
    fact_id = str(
        one("SELECT fact_id FROM company_profile_facts WHERE profile_id=%s", (profile["profile_id"],))[
            "fact_id"
        ]
    )
    runtime = SimpleNamespace(
        sources={str(old_source["source_id"]), str(new_source["source_id"])},
        fact_ids={fact_id},
        scope=SimpleNamespace(company_id="U_demo", as_of=now()),
    )
    draft = InsightDraft.model_validate(
        {
            "summary": "Проверка источников",
            "cards": [
                {
                    "title": "Событие",
                    "event_summary": "Описание",
                    "business_impact": "Влияние",
                    "mechanism": "Условный механизм",
                    "role_action": "Проверить условия",
                    "article_ids": ["a1"],
                    "claims": [
                        {
                            "kind": "verified_fact",
                            "statement": "Ключевая ставка сохранена",
                            "source_ids": [new_source["source_id"]],
                        },
                        {
                            "kind": "ai_inference",
                            "statement": "Условное влияние",
                            "fact_ids": [fact_id],
                            "source_ids": [old_source["source_id"]],
                        },
                    ],
                }
            ],
        }
    )
    with pytest.raises(ValueError, match="content_version"):
        insights.validate_insight(draft, runtime, pinned)
    draft.cards[0].claims[0].source_ids = [old_source["source_id"]]
    insights.validate_insight(draft, runtime, pinned)
    # The database is a second guard against accidentally bypassing application validation.
    with db() as conn:
        insight = conn.execute(
            "INSERT INTO insights(company_id,profile_id,user_context_id,title) VALUES(%s,%s,%s,%s) RETURNING insight_id",
            ("U_demo", profile["profile_id"], profile["user_context_id"], "Draft"),
        ).fetchone()["insight_id"]
        conn.execute(
            "INSERT INTO insight_articles(insight_id,article_id,content_version) VALUES(%s,'a1','v1')",
            (insight,),
        )
        claim = conn.execute(
            "INSERT INTO insight_claims(insight_id,kind,statement) VALUES(%s,'verified_fact','Fact') RETURNING claim_id",
            (insight,),
        ).fetchone()["claim_id"]
        conn.execute(
            "INSERT INTO insight_claim_sources(claim_id,source_id,relation) VALUES(%s,%s,'supports')",
            (claim, new_source["source_id"]),
        )
    with pytest.raises(psycopg.Error, match="citation revision"):
        execute("UPDATE insights SET status='published',published_at=now() WHERE insight_id=%s", (insight,))


def test_configuring_ranker_after_waiting_dispatch_does_not_strand_request(
    demo, fake_embeddings, monkeypatch
):
    from agent_common.core import settings
    from backend_service.reports import dispatch_ranking

    monkeypatch.setattr(Runtime, "complete_json", fake_profile_llm)
    profiler.profile_job(demo)
    monkeypatch.setattr(settings, "ranker_url", "")
    report = create_report(**demo, trigger_type="on_demand", key="configure-ranker")
    request = request_ranking(report)
    waiting = claim(["dispatch_ranking"])
    finish(waiting, {"ok": True, "result": dispatch_ranking(waiting["payload"])})
    assert one("SELECT status FROM jobs WHERE job_id=%s", (waiting["job_id"],))["status"] == "completed"
    monkeypatch.setattr(settings, "ranker_url", "https://ranker.test/runs")
    calls = []

    def accept(url, **kwargs):
        calls.append(kwargs["json"])
        if url == settings.ranker_url:
            body = {"ranking_run_id": str(uuid4())}
        else:
            body = enqueue("insight", "insight:" + str(request["request_id"]), {"test": True})
            body = {"job_id": str(body["job_id"])}
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", accept)
    assert request_ranking(report)["request_id"] == request["request_id"]
    active = claim(["dispatch_ranking"])
    assert active["job_id"] != waiting["job_id"]
    finish(active, {"ok": True, "result": dispatch_ranking(active["payload"])})
    assert len(calls) == 2 and calls[0]["request_id"] == str(request["request_id"])
    assert "callback_url" not in calls[0] and "report_id" not in calls[0]
    assert calls[1]["ranking_run_id"]
    assert one("SELECT status FROM ranking_requests")["status"] == "sent"
    request_ranking(report)
    assert claim(["dispatch_ranking"]) is None


def test_terminal_ranker_dispatch_recovers_once_after_cooldown(demo, fake_embeddings, monkeypatch):
    from agent_common.core import settings
    from backend_service.reports import dispatch_ranking

    monkeypatch.setattr(Runtime, "complete_json", fake_profile_llm)
    profiler.profile_job(demo)
    monkeypatch.setattr(settings, "ranker_url", "https://ranker.test/runs")
    report = create_report(**demo, trigger_type="on_demand", key="recover-ranker")
    request = request_ranking(report)
    initial = one("SELECT * FROM jobs WHERE kind='dispatch_ranking'")
    execute(
        "UPDATE jobs SET status='failed',attempts=max_attempts,finished_at=now(),last_error='Ranker offline' WHERE job_id=%s",
        (initial["job_id"],),
    )
    request_ranking(report)
    assert one("SELECT count(*) AS n FROM jobs WHERE kind='dispatch_ranking'")["n"] == 1
    assert one("SELECT status,last_error FROM ranking_requests")["last_error"] == "Ranker offline"
    execute("UPDATE jobs SET finished_at=now()-interval '16 minutes' WHERE job_id=%s", (initial["job_id"],))
    request_ranking(report)
    request_ranking(report)  # Multiple ticks must not multiply recovery jobs.
    assert one("SELECT count(*) AS n FROM jobs WHERE kind='dispatch_ranking'")["n"] == 2
    recovered = claim(["dispatch_ranking"])
    assert recovered["attempts"] == 1 and recovered["job_id"] != initial["job_id"]
    assert recovered["payload"]["request_id"] == str(request["request_id"])
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kw: httpx.Response(
            200,
            json={"ranking_run_id": str(uuid4()), "job_id": str(uuid4())},
            request=httpx.Request("POST", url),
        ),
    )
    finish(recovered, {"ok": True, "result": dispatch_ranking(recovered["payload"])})
    final = one("SELECT status,last_error FROM ranking_requests")
    assert final == {"status": "sent", "last_error": None}


def test_shutdown_restores_final_attempt_without_releasing_new_owner(database):
    from backend_service.worker import release_on_shutdown

    job = enqueue("shutdown-test", "shutdown-regression", {})
    execute("UPDATE jobs SET max_attempts=1 WHERE job_id=%s", (job["job_id"],))
    first = claim(["shutdown-test"])
    assert first["attempts"] == 1
    assert release_on_shutdown(first) == 1
    assert release_on_shutdown(first) == 0
    resumed = claim(["shutdown-test"])
    assert resumed is not None and resumed["attempts"] == 1
    assert release_on_shutdown(first) == 0  # The old lease must not release a new owner.
    finish(resumed, {"ok": True, "result": "done"})
    assert release_on_shutdown(resumed) == 0
    finish(resumed, {"ok": False, "error": "late duplicate failure"})
    assert one("SELECT status FROM jobs")["status"] == "completed"


def test_old_onboarding_job_cannot_overwrite_newer_user_context(demo, fake_embeddings, monkeypatch):
    monkeypatch.setattr(Runtime, "complete_json", fake_profile_llm)
    first = one("SELECT answer_id FROM onboarding_answers")["answer_id"]
    with db() as conn:
        second = conn.execute(
            "INSERT INTO onboarding_answers(company_id,user_id,answers) VALUES(%s,%s,%s) RETURNING answer_id",
            (
                "U_demo",
                demo["user_id"],
                j(
                    {
                        "role": "Казначей",
                        "responsibilities": ["Казначейство"],
                        "goals": ["Контроль ликвидности"],
                        "note": "",
                    }
                ),
            ),
        ).fetchone()["answer_id"]
    profiler.profile_job({**demo, "answer_id": str(second), "_operation_key": "new-onboarding"})
    stale = profiler.profile_job({**demo, "answer_id": str(first), "_operation_key": "old-onboarding"})
    assert stale["status"] == "superseded"
    assert one("SELECT business_role FROM current_user_preferences")["business_role"] == "Казначей"
    assert one("SELECT count(*) AS n FROM user_preferences")["n"] == 1


def test_answers_changing_during_generation_block_stale_publication(demo, fake_embeddings, monkeypatch):
    first = one("SELECT answer_id FROM onboarding_answers")["answer_id"]
    inserted = []

    def change_answers_once(runtime, messages):
        if not inserted:
            with db() as conn:
                inserted.append(
                    conn.execute(
                        "INSERT INTO onboarding_answers(company_id,user_id,answers) "
                        "VALUES(%s,%s,%s) RETURNING answer_id",
                        (
                            "U_demo",
                            demo["user_id"],
                            j(
                                {
                                    "role": "Новый директор",
                                    "responsibilities": [],
                                    "goals": ["Новая цель"],
                                    "note": "",
                                }
                            ),
                        ),
                    ).fetchone()["answer_id"]
                )
        return fake_profile_llm(runtime, messages)

    monkeypatch.setattr(Runtime, "complete_json", change_answers_once)
    with pytest.raises(RuntimeError, match="User answers changed"):
        profiler.profile_job({**demo, "answer_id": str(first), "_operation_key": "racing-old-answer"})
    assert one("SELECT count(*) AS n FROM user_preferences")["n"] == 0
    monkeypatch.setattr(Runtime, "complete_json", fake_profile_llm)
    profiler.profile_job({**demo, "answer_id": str(inserted[0]), "_operation_key": "racing-new-answer"})
    assert one("SELECT business_role FROM current_user_preferences")["business_role"] == "Новый директор"


def test_onboarding_job_insert_failure_rolls_back_answer(demo):
    from agent_common.core import digest

    execute(
        "INSERT INTO api_tokens(token_hash,user_id) VALUES(%s,%s)", (digest("atomic-user"), demo["user_id"])
    )
    with db() as conn:
        conn.execute(
            "CREATE FUNCTION reject_test_profile_job() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
            "IF NEW.kind='profile' THEN RAISE EXCEPTION 'injected queue failure'; END IF; RETURN NEW; END $$"
        )
        conn.execute(
            "CREATE TRIGGER reject_test_job BEFORE INSERT ON jobs FOR EACH ROW EXECUTE FUNCTION reject_test_profile_job()"
        )
    before = one("SELECT count(*) AS n FROM onboarding_answers")["n"]
    client = TestClient(backend_app, raise_server_exceptions=False)
    response = client.post(
        "/api/companies/U_demo/onboarding",
        headers={"Authorization": "Bearer atomic-user"},
        json={"role": "CFO", "goals": ["Новая цель"]},
    )
    assert response.status_code == 500
    assert one("SELECT count(*) AS n FROM onboarding_answers")["n"] == before
    assert one("SELECT count(*) AS n FROM jobs WHERE kind='profile'")["n"] == 0
