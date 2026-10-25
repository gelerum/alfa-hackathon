import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from agent_common.core import settings


@pytest.fixture
def database(monkeypatch, tmp_path):
    """An isolated schema, only on explicitly opted-in TEST_DATABASE_URL (must have pgvector)."""
    dsn = os.getenv("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL + pgvector integration tests")
    schema = "test_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    scoped = make_conninfo(dsn, options=f"-csearch_path={schema},public")
    monkeypatch.setattr(settings, "database_url", scoped)
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")
    monkeypatch.setattr(settings, "internal_token", "test-internal-token")
    monkeypatch.setattr(settings, "admin_token", "test-admin-token")
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(settings, "openai_base_url", "http://llm.test/v1")
    with psycopg.connect(scoped, autocommit=True) as conn:
        conn.execute((Path(__file__).resolve().parents[2] / "database/schema.sql").read_text())
        conn.execute((Path(__file__).resolve().parents[2] / "database/application.sql").read_text())
    yield scoped
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
