import json
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from agent_common.contracts import FetchArgs, ProfileDraft, ReadArgs
from agent_common.core import execute, j, settings
from agent_common.runtime import Runtime
from insight_service import service as insights
from profiler_service import service as profiler
from tool_gateway_service import tools
from test_integration import demo as demo, fake_embeddings as fake_embeddings, fake_profile_llm, seed_news


def test_public_tools_cannot_send_private_queries_or_arbitrary_urls(demo, monkeypatch):
    runtime = Runtime("company_profiler", **demo)

    def forbidden(*args, **kwargs):
        raise AssertionError("Private data must be rejected before network access")

    monkeypatch.setattr(httpx, "get", forbidden)
    monkeypatch.setattr(tools, "fetch_public", forbidden)
    for query in (
        "U_123 5000000",
        "Секретная стратегия из документа компании",
        "https://attacker.test/private",
    ):
        with pytest.raises(ValueError):
            tools.dispatch("web.search", runtime.scope, {"query": query})
    with pytest.raises(ValueError, match="unchanged result"):
        tools.web_fetch(runtime.scope, FetchArgs(url="https://attacker.test/collect?amount=5000000"))


def test_only_unchanged_search_result_can_be_fetched(demo, monkeypatch):
    runtime = Runtime("company_profiler", **demo)
    url = "https://public.example/news"
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **kw: httpx.Response(
            200, request=httpx.Request("GET", url), json={"results": [{"title": "Ставка", "url": url}]}
        ),
    )
    tools.dispatch("web.search", runtime.scope, {"query": "ключевая ставка Банка России"})
    monkeypatch.setattr(tools, "fetch_public", lambda value, **kwargs: (value, "Публичный текст"))
    assert tools.web_fetch(runtime.scope, FetchArgs(url=url))["sources"]
    with pytest.raises(ValueError):
        tools.web_fetch(runtime.scope, FetchArgs(url=url + "?secret=private"))
    another = Runtime("company_profiler", **demo)
    with pytest.raises(ValueError):
        tools.web_fetch(another.scope, FetchArgs(url=url))


def test_note_survives_context_trimming_and_tool_timeout_is_capped(demo, fake_embeddings, monkeypatch):
    note = "Не предлагать увеличение валютной позиции"
    execute(
        "INSERT INTO onboarding_answers(company_id,user_id,answers) VALUES(%s,%s,%s)",
        (demo["company_id"], demo["user_id"], j({"role": "Казначей", "note": note})),
    )
    complete_json = Runtime.complete_json
    monkeypatch.setattr(Runtime, "complete_json", fake_profile_llm)
    profile = profiler.profile_job(demo)
    monkeypatch.setattr(Runtime, "complete_json", complete_json)
    from agent_common.core import encode

    runtime = Runtime("insight_agent", **demo, **profile)
    runtime.messages = [{"role": "system", "content": "policy"}, {"role": "user", "content": "task"}]
    original_started = runtime.started
    runtime.started = time.monotonic() - settings.agent_timeout_seconds + 2

    def post(url, **kwargs):
        assert 0 < kwargs["timeout"] <= 2
        return httpx.Response(
            200, request=httpx.Request("POST", url), json=encode(tools.context(runtime.scope))
        )

    monkeypatch.setattr(httpx, "post", post)
    runtime.tool("company_context.get", {})
    runtime.started = original_started
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            usage=None,
            choices=[
                SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content='{"action":"plan"}'))
            ],
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    client.with_options = lambda **kw: client
    runtime.client = client
    runtime.messages += [{"role": "user", "content": "x" * 100000}, {"role": "user", "content": "recent"}]
    monkeypatch.setattr(settings, "agent_max_context_bytes", 8000)
    Runtime.complete_json(runtime, runtime.messages)
    assert note in json.dumps(captured["messages"], ensure_ascii=False)
    assert "x" * 10000 not in json.dumps(captured["messages"])


def test_new_auxiliary_revision_is_rejected_but_pinned_old_primary_is_readable(demo):
    seed_news()
    old = insights.article_evidence([{"article_id": "a1", "content_version": "v1"}])[0]
    runtime = Runtime(
        "insight_agent",
        **demo,
        snapshot={"ranking_input": {"articles": [{"article_id": "a1", "content_version": "v1"}]}},
    )
    execute(
        "INSERT INTO news_revisions(article_id,content_version,title,published_at) SELECT article_id,'v2','Changed',published_at FROM news WHERE article_id='a1'"
    )
    new = insights.article_evidence([{"article_id": "a1", "content_version": "v2"}])[0]
    with pytest.raises(HTTPException):
        tools.read(runtime.scope, "news.read", ReadArgs(source_id=new["source_id"]))
    assert tools.read(runtime.scope, "news.read", ReadArgs(source_id=old["source_id"]))["sources"]


def test_real_bank_quote_cannot_support_invented_debt_balance(demo, fake_embeddings):
    runtime = Runtime("company_profiler", **demo)
    runtime.tool("company_bank_data.get", {})
    draft = ProfileDraft.model_validate(fake_profile_llm(runtime, [{"content": ""}])["result"])
    profiler.validate_profile(draft, runtime)
    draft.facts[0].field = "debt_balance"
    draft.facts[0].value = "999999999"
    with pytest.raises(ValueError, match="extractive"):
        profiler.validate_profile(draft, runtime)
