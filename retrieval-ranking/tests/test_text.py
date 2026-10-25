from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from retrieval_ranking.domain import Candidate, CompanyContext, Facet, Fact, Goal, Preferences
from retrieval_ranking.text import render_context, render_news


def uid(number):
    return UUID(int=number)


def context():
    return CompanyContext(
        company_id="company-1",
        company_name="Example company",
        profile_id=uid(1),
        profile_version=1,
        embedding_config_id=uid(2),
        embedding_metadata={},
        facts=[
            Fact(uid(4), "activity", {"b": 2, "a": 1}),
            Fact(uid(3), "supplier", "Unconfirmed connection", assertion_type="hypothesis"),
        ],
        goals=[Goal(uid(5), "Reduce delivery risks")],
        preferences=Preferences(
            uid(6),
            uid(7),
            1,
            business_role="CFO",
            focus_topics=["Funding"],
            goals=[Goal(uid(8), "Refinance debt")],
        ),
        facets=[Facet(uid(9), "activity", "Food production", [], [], [0.5])],
        missing_data=["Counterparties"],
    )


def test_context_preserves_priorities_and_labels_hypotheses():
    text = render_context(context())
    assert "Company name: Example company" in text
    assert "Business role: CFO" in text
    assert "User goal: Refinance debt" in text
    assert text.index("User goal:") < text.index("Supplied company facts:")
    assert 'activity: {"a":1,"b":2}' in text
    assert "Company goal: Reduce delivery risks" in text
    assert "company/activity: Food production" in text
    assert "Missing information: Counterparties" in text
    assert text.index("Unconfirmed connection") > text.index("Unverified hypotheses")
    assert "0.5" not in text
    assert str(uid(9)) not in text


def test_context_format_is_stable_when_database_row_order_changes():
    source = context()
    other = replace(source, facts=list(reversed(source.facts)))
    assert render_context(source) == render_context(other)


def test_missing_optional_context_fields_are_not_invented():
    source = replace(
        context(),
        company_name=None,
        preferences=None,
        facts=[],
        goals=[],
        facets=[],
        missing_data=[],
    )
    assert render_context(source) == "Company ID: company-1"


def test_news_is_one_labeled_document_with_headline_before_body():
    article = Candidate(
        "news-1",
        "v1",
        "New factory",
        "The plant opened today.",
        datetime(2026, 10, 24, 10, 0, tzinfo=UTC),
        "https://example.test/news",
        "Example",
        description="Production expanded.",
    )
    assert render_news(article) == (
        "Title: New factory\n"
        "Published at: 2026-10-24T10:00:00+00:00\n"
        "Source: Example\n"
        "URL: https://example.test/news\n"
        "Description: Production expanded.\n"
        "Body: The plant opened today."
    )


def test_unknown_publication_date_is_explicit():
    article = Candidate("news", "v1", "Headline", "", None, "", "")
    assert render_news(article) == "Title: Headline\nPublished at: unknown"
