from uuid import uuid4

import pytest

from agent_common.contracts import InsightDraft
from agent_common.core import ServiceBusy, execute, one
from backend_service.worker import claim, finish
from insight_service.service import evidence_pack, resolve_refs


def test_evidence_refs_copy_quotes_without_fabricating_support():
    fid, sid = str(uuid4()), str(uuid4())
    source = {
        "source_id": sid,
        "source_type": "news",
        "article_id": "n1",
        "excerpt": "Банк России сохранил ключевую ставку на уровне четырнадцати процентов.",
        "source_ref": {"content_version": "v1"},
    }
    refs, text = evidence_pack(
        [
            {
                "facts": [{"fact_id": fid, "field": "loan_mark", "value": True}],
                "preferences": {"business_role": "Казначей", "raw_answers": {"kpis": ["Ликвидность"]}},
            },
            {"sources": [source]},
            {"sources": []},
        ],
        1,
    )
    assert "Казначей" in text and "Ликвидность" in text
    draft = InsightDraft(
        summary="Обзор",
        cards=[
            {
                "title": "Ставка",
                "event_summary": "Решение ЦБ",
                "business_impact": "Условия нового кредита",
                "mechanism": "Стоимость финансирования",
                "role_action": "Проверить условия",
                "article_ids": ["A1"],
                "claims": [
                    {
                        "kind": "risk",
                        "statement": "Новые кредиты могут оставаться дорогими",
                        "refs": ["F1", "A1"],
                    },
                    {"kind": "recommendation", "statement": "Сравнить предложения", "refs": ["F1", "A1"]},
                ],
            }
        ],
    )
    resolve_refs(draft, refs, {fid})
    card = draft.cards[0]
    assert card.article_ids == ["n1"]
    assert card.claims[0].kind == "verified_fact" and card.claims[0].statement == source["excerpt"]
    assert str(card.claims[0].source_ids[0]) == sid
    assert str(card.claims[1].fact_ids[0]) == fid
    # Normalization is idempotent.
    resolve_refs(draft, refs, {fid})
    assert len(card.claims) == 3
    card.claims[1].fact_ids = [uuid4()]
    with pytest.raises(ValueError, match="outside the pinned profile"):
        resolve_refs(draft, refs, {fid})


def test_ranker_busy_does_not_burn_job_attempt(database):
    execute(
        "INSERT INTO jobs(kind,idempotency_key,payload,max_attempts) VALUES('dispatch_ranking','busy','{}',1)"
    )
    job = claim(["dispatch_ranking"])
    assert job["attempts"] == 1
    finish(job, {"ok": False, "busy": True, "error": str(ServiceBusy("Ranker is busy"))})
    row = one("SELECT * FROM jobs WHERE job_id=%s", (job["job_id"],))
    assert row["status"] == "pending" and row["attempts"] == 0
    assert row["lease_until"] is None
