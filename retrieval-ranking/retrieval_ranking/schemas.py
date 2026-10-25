from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .domain import RankingQuery


class RankRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_id: str = Field(min_length=1, max_length=128)
    user_id: UUID | None = None
    top_k: int = Field(default=10, ge=1, le=1000)
    as_of: datetime = Field(default_factory=lambda: datetime.now(UTC))
    published_after: datetime | None = None
    profile_id: UUID | None = None
    user_context_id: UUID | None = None
    embedding_config_id: UUID | None = None
    knowledge_as_of: datetime | None = None
    request_id: UUID | None = None

    def to_query(self) -> RankingQuery:
        return RankingQuery(**self.model_dump())


class HitResponse(BaseModel):
    method: str
    query_id: str
    rank: int
    score: float
    score_type: str
    facet_id: UUID | None
    facet_type: str | None


class NewsResponse(BaseModel):
    article_id: str
    content_version: str
    title: str
    text: str
    description: str | None
    published_at: datetime | None
    source_url: str
    source_name: str
    event_id: UUID | None
    rank: int
    score: float
    score_type: str
    retrieval_hits: list[HitResponse]
    document_truncated: bool
    token_count: int


class RankResponse(BaseModel):
    ranking_run_id: UUID
    company_id: str
    user_id: UUID | None
    profile_id: UUID
    user_context_id: UUID | None
    embedding_config_id: UUID
    as_of: datetime
    candidate_count: int
    context_truncated: bool
    model_id: str
    model_revision: str
    score_type: str
    items: list[NewsResponse]
