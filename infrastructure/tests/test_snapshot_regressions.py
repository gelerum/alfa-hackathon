from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from agent_common.core import all_rows, db, execute, now, one
from agent_common.runtime import Runtime
from agent_common.evidence import retrieved_sources
from backend_service.documents import index_chunks
from tool_gateway_service.tools import read
from agent_common.contracts import ReadArgs
from fastapi import HTTPException
from backend_service.reports import create_report, dispatch_ranking, request_ranking
from profiler_service import service as profiler
from test_integration import demo as demo, fake_embeddings as fake_embeddings, fake_profile_llm
from test_ranker_bridge import bridge as bridge, add_vector


def article(name, end, title):
    with db() as conn:
        conn.execute(
            "INSERT INTO news(article_id,canonical_url,source_name,title,body,published_at,content_version) "
            "VALUES(%s,%s,'test',%s,'Изменились показатели',%s,'v1')",
            (name, "https://example.test/" + name, title, end - timedelta(minutes=20)),
        )
        conn.execute(
            "INSERT INTO news_revisions(article_id,content_version,title,body,published_at) "
            "SELECT article_id,content_version,title,body,published_at FROM news WHERE article_id=%s",
            (name,),
        )


def report_with_profile(demo, monkeypatch):
    monkeypatch.setattr(Runtime, "complete_json", fake_profile_llm)
    profiler.profile_job(demo)
    return create_report(**demo, trigger_type="on_demand", key=str(uuid4()), end=now() - timedelta(hours=1))


def test_completed_embedding_creates_new_snapshot_and_finds_vector_only_article(
    demo, fake_embeddings, bridge, monkeypatch
):
    report = report_with_profile(demo, monkeypatch)
    article("vector-only", report["window_end"], "Изменение показателей")
    first = request_ranking(report)
    dispatch_ranking({"request_id": first["request_id"]})
    assert one("SELECT payload FROM jobs WHERE kind='insight'")["payload"]["articles"] == []
    add_vector("vector-only", fake_embeddings)
    second = request_ranking(report)
    assert second["request_id"] != first["request_id"]
    assert second["created_at"] > first["created_at"]
    dispatch_ranking({"request_id": second["request_id"]})
    latest = one("SELECT payload FROM jobs WHERE kind='insight' ORDER BY created_at DESC LIMIT 1")
    assert latest["payload"]["articles"] == [{"article_id": "vector-only", "content_version": "v1"}]
    assert bridge["rank_calls"] == 2


@pytest.mark.parametrize("channel", ["vector", "fts"])
def test_queued_request_reads_revision_and_text_known_before_pointer_update(
    demo, fake_embeddings, bridge, monkeypatch, channel
):
    report = report_with_profile(demo, monkeypatch)
    article("versioned", report["window_end"], "Производство оборудования")
    if channel == "vector":
        add_vector("versioned", fake_embeddings)
    request = request_ranking(report)
    with db() as conn:
        conn.execute(
            "INSERT INTO news_revisions(article_id,content_version,title,body,published_at) "
            "SELECT article_id,'v2','UNRELATED CURRENT TEXT','Updated',published_at FROM news WHERE article_id='versioned'"
        )
        conn.execute(
            "UPDATE news SET content_version='v2',title='UNRELATED CURRENT TEXT',body='Updated',fetched_at=now() WHERE article_id='versioned'"
        )
    dispatch_ranking({"request_id": request["request_id"]})
    assert one("SELECT payload FROM jobs WHERE kind='insight'")["payload"]["articles"] == [
        {"article_id": "versioned", "content_version": "v1"}
    ]
    hits = one("SELECT retrieval_hits FROM ranking_items WHERE article_id='versioned'")["retrieval_hits"]
    assert any(hit["method"] == channel for hit in hits)
    assert one("SELECT window_end FROM reports")["window_end"] == report["window_end"]


@pytest.mark.parametrize("fetched_before_start", [False, True])
def test_new_bank_version_cannot_starve_frozen_old_evidence(demo, fake_embeddings, fetched_before_start):
    published = now() - timedelta(days=1)

    def document(version, fetched):
        with db() as conn:
            return conn.execute(
                "INSERT INTO documents(source_type,title,storage_key,external_id,external_version,published_at,fetched_at,processing_status) "
                "VALUES('bank_analytics','Ставка банка',%s,'snapshot-bank',%s,%s,%s,'ready') RETURNING document_id",
                (str(uuid4()), version, published, fetched),
            ).fetchone()["document_id"]

    old = document("v1", now() - timedelta(minutes=2))
    index_chunks(
        sections=[("Ставка: старая позиция банка", {"page": 1})],
        title="Ставка",
        kind="bank_analytics",
        document_id=old,
        published_at=published,
    )
    newer = document("v2", now() - timedelta(minutes=1)) if fetched_before_start else None
    if newer:
        execute("UPDATE documents SET processing_status='processing' WHERE document_id=%s", (newer,))
    runtime = Runtime("insight_agent", **demo)
    if newer is None:
        newer = document("v2", now())
    index_chunks(
        sections=[("Ставка: новая позиция " * 30, {"page": page}) for page in range(1, 66)],
        title="Ставка",
        kind="bank_analytics",
        document_id=newer,
        published_at=published,
    )
    execute("UPDATE documents SET processing_status='ready' WHERE document_id=%s", (newer,))
    result = runtime.tool("bank_analytics.search", {"query": "ставка", "limit": 6})
    assert {source["document_id"] for source in result["sources"]} == {str(old)}
    assert result["retrieval"]["lexical_candidates"] == 1
    source_id = result["sources"][0]["source_id"]
    assert retrieved_sources([source_id], runtime)
    assert read(runtime.scope, "bank_analytics.read", ReadArgs(source_id=source_id))["sources"]
    later = Runtime("insight_agent", **demo)
    result = later.tool("bank_analytics.search", {"query": "ставка", "limit": 6})
    assert {source["document_id"] for source in result["sources"]} == {str(newer)}
    with pytest.raises(HTTPException):
        read(later.scope, "bank_analytics.read", ReadArgs(source_id=source_id))


def test_retrieval_index_migration_is_repeatable(database):
    sql = (Path(__file__).resolve().parents[2] / "database/retrieval_indexes.sql").read_text()
    execute(sql)
    execute(sql)
    assert {
        row["indexname"]
        for row in all_rows(
            "SELECT indexname FROM pg_indexes WHERE schemaname=current_schema() AND indexname LIKE %s",
            ("news_revisions_%",),
        )
    } >= {"news_revisions_snapshot_idx", "news_revisions_fts_idx"}
