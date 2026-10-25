"""Explicit limits are versioned inputs, not learned relevance thresholds."""

import hashlib
import json
from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MODEL_ID = "Qwen/Qwen3-Reranker-0.6B"
MODEL_REVISION = "e61197ed45024b0ed8a2d74b80b4d909f1255473"
SCORE_TYPE = "qwen_yes_no_logit_margin"
FORMAT_VERSION = "context-news-v1"
INSTRUCTION = (
    "Judge whether the news describes an event relevant to the company's activities, "
    "known dependencies, or explicitly stated user goals. Consider the user's priorities. "
    "A generic topic overlap alone is insufficient. Use only relationships given in the context; "
    "do not assume unknown suppliers, financial terms, or corporate identities."
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RETR_RANK_", env_file=".env", extra="ignore")

    database_url: SecretStr
    vector_top_l: int = Field(default=5, ge=1, le=100)
    fts_top_b: int = Field(default=20, ge=0, le=200)
    max_facets: int = Field(default=16, ge=1, le=64)
    max_candidates: int = Field(default=100, ge=1, le=1000)
    max_fts_terms: int = Field(default=32, ge=1, le=100)
    max_term_characters: int = Field(default=120, ge=1, le=500)
    max_top_k: int = Field(default=100, ge=1, le=1000)
    include_undated: bool = False
    connect_timeout_seconds: int = Field(default=5, ge=1, le=60)
    statement_timeout_ms: int = Field(default=10000, ge=100, le=120000)
    reranker_batch_size: int = Field(default=2, ge=1, le=8)
    reranker_max_tokens: int = Field(default=2048, ge=512, le=8192)
    context_max_tokens: int = Field(default=512, ge=64, le=4096)
    torch_threads: int = Field(default=4, ge=1, le=32)
    model_cache_dir: Path = Path(".cache/huggingface/hub")
    model_local_files_only: bool = False
    instruction: str = INSTRUCTION

    @model_validator(mode="after")
    def validate_budgets(self):
        # Bound CPU work without silently dropping a whole query aspect.
        if self.max_facets * self.vector_top_l + self.fts_top_b > self.max_candidates:
            raise ValueError("max_facets * vector_top_l + fts_top_b exceeds max_candidates")
        if self.context_max_tokens + 256 >= self.reranker_max_tokens:
            raise ValueError("Reserve room for the instruction, news and model suffix")
        if not self.instruction.strip():
            raise ValueError("instruction must not be blank")
        return self

    def retrieval_version(self) -> str:
        return _version(
            "hybrid-v1",
            {
                "l": self.vector_top_l,
                "b": self.fts_top_b,
                "max_facets": self.max_facets,
                "max_candidates": self.max_candidates,
                "fts_terms": self.max_fts_terms,
                "term_chars": self.max_term_characters,
                "include_undated": self.include_undated,
                "ann_underfill": "exact_sql_fallback",
                "fusion": "candidate_union",
            },
        )

    def ranking_version(self) -> str:
        return _version(
            "context-v1",
            {
                "model": MODEL_ID,
                "revision": MODEL_REVISION,
                "format": FORMAT_VERSION,
                "instruction": self.instruction,
                "batch": self.reranker_batch_size,
                "max_tokens": self.reranker_max_tokens,
                "context_tokens": self.context_max_tokens,
                "dtype": "float32",
                "device": "cpu",
            },
        )


def _version(prefix: str, values: dict) -> str:
    encoded = json.dumps(values, sort_keys=True, ensure_ascii=False).encode()
    return f"{prefix}:{hashlib.sha256(encoded).hexdigest()[:16]}"
