import json
from datetime import UTC
from typing import Any

from .domain import Candidate, CompanyContext


def render_context(context: CompanyContext) -> str:
    """Place explicit user priorities early because the model has a context token cap."""
    lines = [f"Company ID: {context.company_id}"]
    if context.company_name:
        lines.append(f"Company name: {context.company_name}")

    preferences = context.preferences
    if preferences is not None:
        lines.append("\nUser priorities:")
        _add_optional(lines, "Business role", preferences.business_role)
        _add_optional(lines, "Responsibility", preferences.responsibility)
        _add_list(lines, "Focus topics", preferences.focus_topics)
        _add_list(lines, "Interest markets", preferences.interest_markets)
        _add_list(lines, "Watchlist", preferences.watchlist)
        for goal in sorted(preferences.goals, key=lambda item: str(item.goal_id)):
            lines.append(f"- User goal: {goal.text}")

    for goal in sorted(context.goals, key=lambda item: str(item.goal_id)):
        lines.append(f"Company goal: {goal.text}")

    facts = sorted(context.facts, key=lambda item: (item.field, str(item.fact_id)))
    verified = [fact for fact in facts if fact.assertion_type != "hypothesis"]
    if verified:
        lines.append("\nSupplied company facts:")
    for fact in verified:
        unit = f" {fact.unit}" if fact.unit else ""
        lines.append(
            f"- {fact.field}: {_value(fact.value)}{unit}"
            f" [assertion={fact.assertion_type}; source={fact.source_type}]"
        )

    if context.facets:
        lines.append("\nPrepared context aspects:")
    for facet in sorted(
        context.facets, key=lambda item: (item.scope, item.facet_type, str(item.facet_id))
    ):
        lines.append(f"- {facet.scope}/{facet.facet_type}: {facet.text}")

    # Hypotheses remain explicitly unverified rather than becoming asserted facts.
    hypotheses = [fact for fact in facts if fact.assertion_type == "hypothesis"]
    if hypotheses:
        lines.append("\nUnverified hypotheses (not established facts):")
        for fact in hypotheses:
            lines.append(f"- {fact.field}: {_value(fact.value)}")
    _add_list(lines, "Missing information", context.missing_data)
    _add_list(lines, "Recorded conflicts", context.conflicts)
    return "\n".join(lines)


def render_news(candidate: Candidate) -> str:
    """Put the headline first so head truncation retains the event's main subject."""
    lines = [f"Title: {candidate.title}"]
    if candidate.published_at is not None:
        lines.append(f"Published at: {candidate.published_at.astimezone(UTC).isoformat()}")
    else:
        lines.append("Published at: unknown")
    _add_optional(lines, "Source", candidate.source_name)
    _add_optional(lines, "URL", candidate.source_url)
    _add_optional(lines, "Description", candidate.description)
    _add_optional(lines, "Body", candidate.text)
    return "\n".join(lines)


def _value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _add_optional(lines: list[str], label: str, value: str | None) -> None:
    if value:
        lines.append(f"{label}: {value}")


def _add_list(lines: list[str], label: str, values: list[Any]) -> None:
    if values:
        lines.append(f"{label}: " + "; ".join(_value(value) for value in values))
