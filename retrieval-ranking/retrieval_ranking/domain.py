"""Small immutable records shared by SQL adapters and the ranking pipeline."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class Fact:
    fact_id: UUID
    field: str
    value: Any
    unit: str | None = None
    assertion_type: str = "reported"
    source_type: str = "bank_import"


@dataclass(frozen=True)
class Goal:
    goal_id: UUID
    text: str


@dataclass(frozen=True)
class Preferences:
    user_context_id: UUID
    user_id: UUID
    context_version: int
    business_role: str | None = None
    responsibility: str | None = None
    focus_topics: list[str] = field(default_factory=list)
    interest_markets: list[str] = field(default_factory=list)
    watchlist: list[str] = field(default_factory=list)
    goals: list[Goal] = field(default_factory=list)


@dataclass(frozen=True)
class Facet:
    facet_id: UUID
    facet_type: str
    text: str
    keywords: list[str]
    entities: list[Any]
    embedding: list[float]
    scope: str = "company"


@dataclass(frozen=True)
class CompanyContext:
    company_id: str
    profile_id: UUID
    profile_version: int
    embedding_config_id: UUID
    embedding_metadata: dict[str, Any]
    facets: list[Facet]
    facts: list[Fact] = field(default_factory=list)
    goals: list[Goal] = field(default_factory=list)
    preferences: Preferences | None = None
    company_name: str | None = None
    missing_data: list[Any] = field(default_factory=list)
    conflicts: list[Any] = field(default_factory=list)


@dataclass(frozen=True)
class RetrievalHit:
    method: str
    query_id: str
    rank: int
    score: float
    score_type: str
    facet_id: UUID | None = None
    facet_type: str | None = None


@dataclass(frozen=True)
class Candidate:
    article_id: str
    content_version: str
    title: str
    text: str
    published_at: datetime | None
    source_url: str
    source_name: str
    hits: list[RetrievalHit] = field(default_factory=list)
    description: str | None = None
    event_id: UUID | None = None


@dataclass(frozen=True)
class RankingQuery:
    company_id: str
    as_of: datetime
    top_k: int
    user_id: UUID | None = None
    profile_id: UUID | None = None
    user_context_id: UUID | None = None
    embedding_config_id: UUID | None = None
    published_after: datetime | None = None
    knowledge_as_of: datetime | None = None
    request_id: UUID | None = None


@dataclass(frozen=True)
class RetrievalBatch:
    context: CompanyContext
    candidates: list[Candidate]
    lexical_terms: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RerankScores:
    margins: list[float]
    context_truncated: bool
    document_truncated: list[bool]
    token_counts: list[int]


@dataclass(frozen=True)
class ScoredCandidate:
    candidate: Candidate
    score: float
    rank: int | None
    document_truncated: bool = False
    token_count: int = 0
