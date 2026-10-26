from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_common.contracts import InsightDraft
from insight_service import service


def make_card(source_ids):
    return InsightDraft(
        summary="Выводы недели",
        cards=[
            {
                "title": "Условия финансирования",
                "event_summary": "Решение ЦБ",
                "business_impact": "Проверить сценарии новых заимствований",
                "mechanism": "Стоимость фондирования",
                "role_action": "Сравнить предложения",
                "article_ids": ["n1"],
                "claims": [
                    {"kind": "verified_fact", "statement": "Ставка сохранена", "source_ids": [source_ids[0]]},
                    {
                        "kind": "risk",
                        "statement": "Новые кредиты могут оставаться дорогими",
                        "fact_ids": [str(uuid4())],
                        "source_ids": source_ids,
                    },
                ],
            }
        ],
    )


@pytest.mark.parametrize(
    "second_url,second_text,expected",
    [
        ("https://one.ru/other", "Другой материал", False),
        ("https://two.ru/post", "Первый материал", False),
        ("https://two.ru/post", "Независимая оценка стоимости фондирования", True),
    ],
)
def test_conclusion_needs_independent_publishers_and_nonduplicate_text(
    monkeypatch, second_url, second_text, expected
):
    ids = [str(uuid4()), str(uuid4())]
    sources = [
        {
            "source_type": "news",
            "source_url": "https://one.ru/post",
            "excerpt": "Заголовок\nПервый материал",
            "article_id": "n1",
        },
        {
            "source_type": "news",
            "source_url": second_url,
            "excerpt": "Другой заголовок\n" + second_text,
            "article_id": "n2",
        },
    ]
    monkeypatch.setattr(service, "retrieved_sources", lambda cited, rt: sources)
    draft = make_card(ids)
    service.retain_supported_cards(draft, SimpleNamespace())
    assert bool(draft.cards) is expected
    if not expected:
        assert "Недостаточно независимых" in draft.summary
        assert "двух независимых источников" in draft.limitations[0]


def test_two_sources_elsewhere_in_card_do_not_support_single_source_conclusion(monkeypatch):
    ids = [str(uuid4()), str(uuid4())]
    source = {
        "source_type": "news",
        "source_url": "https://one.ru/post",
        "excerpt": "Материал",
        "article_id": "n1",
    }
    monkeypatch.setattr(service, "retrieved_sources", lambda cited, rt: [source])
    draft = make_card(ids)
    draft.cards[0].claims[1].source_ids = draft.cards[0].claims[1].source_ids[:1]
    service.retain_supported_cards(draft, SimpleNamespace())
    assert draft.cards == []


def test_publisher_identity():
    assert service.source_publisher({"source_url": "https://www.example.ru/1"}) == "example.ru"
    assert service.source_publisher({"source_url": "https://t.me/BankChannel/123"}) == "telegram:bankchannel"
    assert (
        service.source_publisher({"source_type": "bank_analytics", "source_url": "https://file-store/1"})
        == "alfa-bank"
    )
