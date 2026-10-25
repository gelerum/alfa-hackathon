from __future__ import annotations

import hashlib
import json
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import psycopg
from fastapi import Header, HTTPException
from fastapi.encoders import jsonable_encoder
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql://alfa:alfa_dev_password@localhost:5432/alfa_hack"
    internal_token: str = ""
    admin_token: str = ""
    storage_dir: Path = Path("/tmp/alpha-storage")
    embedding_base_url: str = "http://localhost:8001"
    embedding_config_key: str = "qwen3-embedding-0.6b@97b0c614:v1"
    gateway_url: str = "http://localhost:8090"
    searxng_base_url: str = "http://localhost:8080"
    bank_source_url: str = "http://localhost:8091"
    ranker_url: str = ""
    ranker_timeout_seconds: int = 900
    insight_callback_url: str = "http://insight-agent:8000/runs"
    openai_base_url: str = ""
    openai_api_key: str = ""
    openai_model: str = "qwen3.6-35b-a3b"
    openai_max_tokens: int = 6000
    agent_max_steps: int = 8
    agent_max_tool_calls: int = 140
    agent_max_total_tokens: int = 80000
    agent_max_context_bytes: int = 90000
    agent_timeout_seconds: int = 600
    timezone: str = "Europe/Moscow"
    max_upload_bytes: int = 64 * 1024 * 1024
    max_pages: int = 150
    max_extracted_chars: int = 2_000_000
    service_role: str = "backend"
    worker_poll_seconds: float = 5


settings = Settings()


def now() -> datetime:
    return datetime.now(timezone.utc)


def encode(value: Any):
    # Preserve exact bank NUMERIC values instead of silently rounding through binary floats.
    return jsonable_encoder(value, custom_encoder={Decimal: str})


def digest(value: Any) -> str:
    if not isinstance(value, bytes):
        value = json.dumps(encode(value), sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(value).hexdigest()


def j(value: Any) -> Jsonb:
    return Jsonb(encode(value))


@contextmanager
def db():
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        yield conn


def one(sql: str, args=()):
    with db() as conn:
        return conn.execute(sql, args).fetchone()


def all_rows(sql: str, args=()):
    with db() as conn:
        return conn.execute(sql, args).fetchall()


def execute(sql: str, args=()):
    with db() as conn:
        return conn.execute(sql, args).rowcount


def internal(authorization: str = Header(default="")):
    expected = f"Bearer {settings.internal_token}"
    if not settings.internal_token or not secrets.compare_digest(authorization, expected):
        raise HTTPException(401, "Internal authentication required")


def admin(authorization: str = Header(default="")):
    if not settings.admin_token or not secrets.compare_digest(
        authorization, f"Bearer {settings.admin_token}"
    ):
        raise HTTPException(401, "Administrator authentication required")


def current_user(authorization: str = Header(default="")) -> UUID:
    token = authorization.removeprefix("Bearer ")
    row = one("SELECT user_id FROM api_tokens WHERE token_hash=%s AND revoked_at IS NULL", (digest(token),))
    if not row or not authorization.startswith("Bearer "):
        raise HTTPException(401, "User authentication required")
    return row["user_id"]


def membership(company_id: str, user_id: UUID | str, write=False):
    row = one(
        "SELECT * FROM company_memberships WHERE company_id=%s AND user_id=%s AND is_active",
        (company_id, user_id),
    )
    if not row or (write and row["access_role"] not in ("admin", "analyst")):
        raise HTTPException(403, "No access to this company")
    return row


def enqueue(kind: str, key: str, payload: dict, available_at=None):
    with db() as conn:
        conn.execute(
            "INSERT INTO jobs(kind,idempotency_key,payload,available_at) VALUES(%s,%s,%s,%s) "
            "ON CONFLICT(idempotency_key) DO NOTHING",
            (kind, key, j(payload), available_at or now()),
        )
        row = conn.execute("SELECT * FROM jobs WHERE idempotency_key=%s", (key,)).fetchone()
        if row["kind"] != kind or row["payload"] != encode(payload):
            raise HTTPException(409, "Idempotency key reused with different input")
        return row


def monday_window(at: datetime, upcoming: bool = False):
    """Seven elapsed days ending Monday 08:00 Moscow by default; aware timestamps only."""
    if at.tzinfo is None:
        raise ValueError("Timezone-aware datetime required")
    local = at.astimezone(ZoneInfo(settings.timezone))
    end = (local - timedelta(days=local.weekday())).replace(hour=8, minute=0, second=0, microsecond=0)
    if upcoming and end < local:
        end += timedelta(days=7)
    elif not upcoming and end > local:
        end -= timedelta(days=7)
    return end - timedelta(days=7), end
