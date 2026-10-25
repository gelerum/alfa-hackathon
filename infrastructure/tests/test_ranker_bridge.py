from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_common.core import all_rows, digest, execute, now, one, settings
from agent_common.runtime import Runtime
from backend_service.reports import create_report, dispatch_ranking, request_ranking
from backend_service.api import app as backend_app
from insight_service.api import create_insight
from insight_service import service as insights
from profiler_service import service as profiler
from retrieval_ranking.api import create_app
from retrieval_ranking.config import Settings as RankSettings
from retrieval_ranking.db import PostgresRepository
from retrieval_ranking.domain import RerankScores
from test_integration import demo as demo, fake_embeddings as fake_embeddings, fake_profile_llm, seed_news


class TestScorer:
    __test__ = False

    def score(self, context, documents):
        assert "Производство" in context
        return RerankScores(
            list(range(len(documents), 0, -1)), False, [False] * len(documents), [50] * len(documents)
        )


@pytest.fixture
def bridge(fake_embeddings, monkeypatch):
    config = RankSettings(database_url=settings.database_url, _env_file=None)
    gateway_post = httpx.post
    state = {"rank_calls": 0, "callback_calls": 0, "fail_callback": False, "lose_rank_response": False}
    monkeypatch.setattr(settings, "ranker_url", "http://ranker.test/rank")
    with TestClient(create_app(config, PostgresRepository(config), TestScorer())) as ranker:
        with TestClient(create_insight()) as insight:

            def post(url, **kwargs):
                if url == settings.ranker_url:
                    state["rank_calls"] += 1
                    response = ranker.post("/rank", json=kwargs["json"])
                    if state["lose_rank_response"]:
                        state["lose_rank_response"] = False
                        assert response.status_code == 200, response.text
                        raise httpx.ReadTimeout(
                            "Lost successful response", request=httpx.Request("POST", url)
                        )
                elif url == settings.insight_callback_url:
                    state["callback_calls"] += 1
                    if state["fail_callback"]:
                        state["fail_callback"] = False
                        raise httpx.ConnectError("Insight unavailable", request=httpx.Request("POST", url))
                    response = insight.post("/runs", json=kwargs["json"], headers=kwargs["headers"])
                else:
                    return gateway_post(url, **kwargs)
                return httpx.Response(
                    response.status_code, json=response.json(), request=httpx.Request("POST", url)
                )

            monkeypatch.setattr(httpx, "post", post)
            yield state


def add_vector(article, config):
    execute(
        "INSERT INTO news_embeddings(article_id,content_version,embedding_config_id,embedding) VALUES(%s,'v1',%s,%s::vector)",
        (article, config["embedding_config_id"], "[1," + ",".join(["0"] * 1023) + "]"),
    )


def prepare(demo, fake_embeddings, monkeypatch):
    monkeypatch.setattr(Runtime, "complete_json", fake_profile_llm)
    profiler.profile_job(demo)
    end = now() - timedelta(hours=1)
    for article, date in (
        ("inside", end - timedelta(hours=1)),
        ("boundary", end),
        ("too-old", end - timedelta(days=8)),
    ):
        seed_news(article, date)
        add_vector(article, fake_embeddings)
    report = create_report(**demo, trigger_type="on_demand", key="bridge:" + str(uuid4()), end=end)
    return report, request_ranking(report)


def draft_insight(runtime, messages):
    if "Review this candidate" in messages[-1]["content"]:
        return {"valid": True, "issues": []}
    sources = all_rows(
        "SELECT * FROM evidence_sources WHERE source_id=ANY(%s::uuid[]) AND source_type='news'",
        (list(runtime.sources),),
    )
    if '"answer"' in messages[0]["content"]:
        return {
            "action": "finish",
            "result": {
                "answer": "Условия долга неизвестны; проверьте договоры.",
                "source_ids": [str(sources[0]["source_id"])],
            },
        }
    cards = []
    for source in sources:
        cards.append(
            {
                "title": "Проверить условия финансирования",
                "event_summary": "Ставка сохранена.",
                "business_impact": "Влияние на бюджет зависит от условий договоров.",
                "mechanism": "При использовании кредитов проверьте чувствительность расходов к ставке.",
                "role_action": "Финансовому директору — проверить сценарии бюджета.",
                "article_ids": [source["article_id"]],
                "assumptions": ["Условия долга неизвестны"],
                "claims": [
                    {
                        "kind": "verified_fact",
                        "statement": "Ключевая ставка сохранена",
                        "source_ids": [str(source["source_id"])],
                    },
                    {
                        "kind": "ai_inference",
                        "statement": "Следует проверить сценарии бюджета.",
                        "source_ids": [str(source["source_id"])],
                        "fact_ids": [next(iter(runtime.fact_ids))],
                    },
                ],
            }
        )
    return {"action": "finish", "result": {"summary": "Проверьте чувствительность бюджета.", "cards": cards}}


def test_real_rank_sql_http_callback_report_and_question(demo, fake_embeddings, bridge, monkeypatch):
    report, request = prepare(demo, fake_embeddings, monkeypatch)
    assert dispatch_ranking({"request_id": request["request_id"]})["status"] == "sent"
    run = one("SELECT * FROM ranking_runs")
    assert run["status"] == "completed"
    assert run["as_of"] == report["window_end"]
    assert run["published_after"] == report["window_start"]
    assert run["context_snapshot"]["request_id"] == str(request["request_id"])
    items = all_rows("SELECT article_id,content_version FROM ranking_items WHERE rank IS NOT NULL")
    assert items == [{"article_id": "inside", "content_version": "v1"}]
    job = one("SELECT * FROM jobs WHERE kind='insight'")
    assert job["payload"]["articles"] == items
    assert bridge["rank_calls"] == bridge["callback_calls"] == 1
    dispatch_ranking({"request_id": request["request_id"]})
    assert one("SELECT count(*) AS n FROM jobs WHERE kind='insight'")["n"] == 1
    execute(
        "INSERT INTO news_revisions(article_id,content_version,title,body,published_at) VALUES('inside','v2','Updated','Changed',%s)",
        (report["window_end"] - timedelta(hours=1),),
    )
    execute("UPDATE news SET content_version='v2',title='Updated' WHERE article_id='inside'")
    monkeypatch.setattr(Runtime, "complete_json", draft_insight)
    published = insights.insight_job(job["payload"])
    assert published["version"] == 1
    execute(
        "INSERT INTO api_tokens(token_hash,user_id) VALUES(%s,%s)", (digest("bridge-user"), demo["user_id"])
    )
    headers = {"Authorization": "Bearer bridge-user"}
    client = TestClient(backend_app)
    result = client.get(f"/api/reports/{report['report_id']}", headers=headers).json()
    assert result["status"] == "ready" and len(result["cards"]) == 1
    assert result["cards"][0]["claims"]
    assert any("после окончания" in value for value in result["version"]["limitations"])
    answer = client.post(
        f"/api/reports/{report['report_id']}/questions",
        headers=headers,
        json={
            "request_id": str(uuid4()),
            "question": "Что проверить?",
            "report_version_id": result["version"]["report_version_id"],
            "insight_id": result["cards"][0]["insight_id"],
        },
    )
    assert answer.status_code == 202, answer.text
    payload = one("SELECT payload FROM jobs WHERE job_id=%s", (answer.json()["job_id"],))["payload"]
    assert insights.question_job(payload)["sources"]
    invalid = client.post(
        f"/api/reports/{report['report_id']}/questions",
        headers=headers,
        json={"request_id": str(uuid4()), "question": "Что проверить?", "insight_id": str(uuid4())},
    )
    assert invalid.status_code == 404


@pytest.mark.parametrize("failure", ["fail_callback", "lose_rank_response"])
def test_rank_result_is_reused_after_network_failure(demo, fake_embeddings, bridge, monkeypatch, failure):
    report, request = prepare(demo, fake_embeddings, monkeypatch)
    bridge[failure] = True
    with pytest.raises(httpx.HTTPError):
        dispatch_ranking({"request_id": request["request_id"]})
    dispatch_ranking({"request_id": request["request_id"]})
    assert bridge["rank_calls"] == 1
    assert one("SELECT count(*) AS n FROM ranking_runs")["n"] == 1
    assert one("SELECT count(*) AS n FROM jobs WHERE kind='insight'")["n"] == 1


def test_future_report_is_not_dispatched(demo, fake_embeddings, monkeypatch):
    monkeypatch.setattr(Runtime, "complete_json", fake_profile_llm)
    profiler.profile_job(demo)
    report = create_report(**demo, trigger_type="scheduled", key="future", end=now() + timedelta(hours=1))
    assert request_ranking(report) is None
    assert one("SELECT count(*) AS n FROM ranking_requests")["n"] == 0


def test_late_news_keeps_report_window_and_gets_new_ranking(demo, fake_embeddings, bridge, monkeypatch):
    report, first = prepare(demo, fake_embeddings, monkeypatch)
    dispatch_ranking({"request_id": first["request_id"]})
    seed_news("late", report["window_end"] - timedelta(minutes=20))
    add_vector("late", fake_embeddings)
    second = request_ranking(report)
    assert second["request_id"] != first["request_id"]
    dispatch_ranking({"request_id": second["request_id"]})
    jobs = all_rows("SELECT payload FROM jobs WHERE kind='insight' ORDER BY created_at")
    assert len(jobs) == 2
    assert {a["article_id"] for a in jobs[1]["payload"]["articles"]} == {"inside", "late"}
    assert one("SELECT window_end FROM reports")["window_end"] == report["window_end"]
    assert bridge["rank_calls"] == 2
