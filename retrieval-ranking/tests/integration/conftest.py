import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location("synthetic_seed", ROOT / "tests/fixtures/seed.py")
assert _SPEC is not None and _SPEC.loader is not None
SEED = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(SEED)


@pytest.fixture
def seed():
    return SEED


@pytest.fixture
def database_dsn():
    if os.environ.get("RETR_RANK_RUN_DB_TESTS") != "1":
        pytest.skip("Set RETR_RANK_RUN_DB_TESTS=1 after starting scripts/test_database.py start")
    host = os.environ.get("RETR_RANK_TEST_DB_HOST", "127.0.0.1")
    base = (
        os.environ.get("RETR_RANK_TEST_DATABASE_URL")
        or f"host={host} port=55439 user=retr_rank_test password=retr_rank_test connect_timeout=5"
    )
    name = "retr_rank_test_" + uuid4().hex
    # A fresh database keeps append-only schema constraints active without cross-test mutation.
    with psycopg.connect(make_conninfo(base, dbname="postgres"), autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    target = make_conninfo(base, dbname=name)
    try:
        with psycopg.connect(target, autocommit=True) as connection:
            connection.execute((ROOT.parent / "database/schema.sql").read_text(), prepare=False)
            with connection.transaction():
                SEED.seed_database(connection)
        yield target
    finally:
        assert name.startswith("retr_rank_test_") and len(name) == 47
        with psycopg.connect(make_conninfo(base, dbname="postgres"), autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
