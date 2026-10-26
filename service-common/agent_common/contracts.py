from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Answers(StrictModel):
    role: str = Field(min_length=1, max_length=200)
    responsibilities: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list, max_length=8
    )
    goals: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        default_factory=list, max_length=3
    )
    kpis: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list, max_length=3
    )
    note: str = Field(default="", max_length=1000)

    @field_validator("kpis")
    @classmethod
    def normalize_kpis(cls, values):
        result, seen = [], set()
        for value in values:
            value = value.strip()
            if not value:
                raise ValueError("KPI must not be blank")
            if value.casefold() not in seen:
                result.append(value)
                seen.add(value.casefold())
        return result


class AgentScope(StrictModel):
    company_id: str = Field(min_length=1, max_length=128)
    user_id: UUID
    agent_run_id: UUID
    profile_id: UUID | None = None
    user_context_id: UUID | None = None
    as_of: datetime


class ToolCall(StrictModel):
    scope: AgentScope
    arguments: dict = Field(default_factory=dict)


class SearchArgs(StrictModel):
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=6, ge=1, le=12)
    published_after: datetime | None = None


class ReadArgs(StrictModel):
    source_id: UUID


PUBLIC_RESEARCH_TOPICS = (
    "ключевая ставка Банка России",
    "инфляция в России",
    "валютное регулирование России",
    "российская промышленность",
    "рынок металлов",
    "рынок нефти и газа",
    "рынок золота",
    "международная торговля и санкции",
)


class WebArgs(StrictModel):
    query: Literal[
        "ключевая ставка Банка России",
        "инфляция в России",
        "валютное регулирование России",
        "российская промышленность",
        "рынок металлов",
        "рынок нефти и газа",
        "рынок золота",
        "международная торговля и санкции",
    ]
    limit: int = Field(default=5, ge=1, le=8)


class FetchArgs(StrictModel):
    url: str = Field(max_length=2048)


class FactDraft(StrictModel):
    field: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=2000)
    source_ids: list[UUID] = Field(min_length=1, max_length=8)
    evidence_quote: str = Field(min_length=1, max_length=2000)


class FacetDraft(StrictModel):
    facet_type: Literal[
        "activity",
        "counterparty_network",
        "corporate_group",
        "financial_exposure",
        "foreign_trade",
        "strategic_goals",
        "other",
    ]
    text: str = Field(min_length=1, max_length=1800)
    fact_indexes: list[int] = Field(min_length=1, max_length=20)
    keywords: list[str] = Field(default_factory=list, max_length=20)


class ProfileFocusDraft(StrictModel):
    """What the LLM writes: interpretation only. Bank facts are extracted deterministically."""

    summary: str = Field(min_length=1, max_length=1500)
    facets: list[FacetDraft] = Field(min_length=1, max_length=8)
    document_facts: list[FactDraft] = Field(default_factory=list, max_length=12)
    missing_data: list[str] = Field(default_factory=list, max_length=15)
    conflicts: list[str] = Field(default_factory=list, max_length=10)
    clarification: str | None = Field(default=None, max_length=500)


class ProfileDraft(StrictModel):
    summary: str = Field(min_length=1, max_length=4000)
    facts: list[FactDraft] = Field(min_length=1, max_length=60)
    facets: list[FacetDraft] = Field(min_length=1, max_length=12)
    missing_data: list[str] = Field(default_factory=list, max_length=30)
    conflicts: list[str] = Field(default_factory=list, max_length=20)
    clarification: str | None = Field(default=None, max_length=500)


class ClaimDraft(StrictModel):
    kind: Literal["verified_fact", "bank_position", "ai_inference", "risk", "opportunity", "recommendation"]
    # Empty statement is allowed only with an S-ref: the system copies the exact quote.
    statement: str = Field(default="", max_length=3000)
    certainty: Literal["confirmed", "qualified", "unverified"] = "qualified"
    source_ids: list[UUID] = Field(default_factory=list, max_length=12)
    fact_ids: list[UUID] = Field(default_factory=list, max_length=12)
    refs: list[str] = Field(default_factory=list, max_length=12)

    @model_validator(mode="before")
    @classmethod
    def move_short_refs(cls, data):
        # Models often put "S1"/"F3" into source_ids/fact_ids; treat them as refs instead of failing.
        if isinstance(data, dict):
            refs = list(data.get("refs") or [])
            for key in ("source_ids", "fact_ids"):
                kept = []
                for value in data.get(key) or []:
                    try:
                        UUID(str(value))
                        kept.append(value)
                    except ValueError:
                        refs.append(str(value))
                data = {**data, key: kept}
            data["refs"] = list(dict.fromkeys(refs))
        return data

    @model_validator(mode="after")
    def statement_or_refs(self):
        if not self.statement.strip() and not self.refs:
            raise ValueError("Claim needs a statement or evidence refs")
        return self


class CardDraft(StrictModel):
    title: str = Field(min_length=1, max_length=300)
    event_summary: str = Field(min_length=1, max_length=2000)
    business_impact: str = Field(min_length=1, max_length=3000)
    mechanism: str = Field(min_length=1, max_length=3000)
    role_action: str = Field(min_length=1, max_length=2000)
    article_ids: list[str] = Field(min_length=1, max_length=20)
    claims: list[ClaimDraft] = Field(min_length=2, max_length=30)
    assumptions: list[str] = Field(default_factory=list, max_length=15)


class InsightDraft(StrictModel):
    summary: str = Field(min_length=1, max_length=5000)
    cards: list[CardDraft] = Field(default_factory=list, max_length=10)
    limitations: list[str] = Field(default_factory=list, max_length=30)
    changes: list[str] = Field(default_factory=list, max_length=20)


class EvidenceClaimDraft(StrictModel):
    @model_validator(mode="before")
    @classmethod
    def legacy_ids(cls, data):
        if isinstance(data, dict):
            data = dict(data)
            data["refs"] = (
                list(data.get("refs") or [])
                + list(data.pop("source_ids", []) or [])
                + list(data.pop("fact_ids", []) or [])
            )
        return data

    kind: Literal["verified_fact", "bank_position", "ai_inference", "risk", "opportunity", "recommendation"]
    statement: str = Field(default="", max_length=1500)
    certainty: Literal["confirmed", "qualified", "unverified"] = "qualified"
    refs: list[str] = Field(min_length=1, max_length=12)


class EvidenceCardDraft(CardDraft):
    claims: list[EvidenceClaimDraft] = Field(min_length=2, max_length=12)


class EvidenceInsightDraft(InsightDraft):
    cards: list[EvidenceCardDraft] = Field(default_factory=list, max_length=4)


class QuestionDraft(StrictModel):
    answer: str = Field(min_length=1, max_length=6000)
    source_ids: list[UUID] = Field(default_factory=list, max_length=20)


class ProfileRequest(StrictModel):
    request_id: UUID
    company_id: str
    user_id: UUID


class InsightRequest(StrictModel):
    request_id: UUID  # ranking_requests.request_id, also idempotency key
    ranking_run_id: UUID | None = None
    article_ids: list[str] | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def exactly_one(self):
        if (self.ranking_run_id is None) == (self.article_ids is None):
            raise ValueError("Provide exactly one of ranking_run_id or article_ids")
        return self
