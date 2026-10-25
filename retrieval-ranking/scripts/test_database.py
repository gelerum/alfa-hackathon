"""Manage the service's isolated Docker PostgreSQL test database.

Uses only compose.test.yaml and project alpha-retrieval-ranking-test.
No production settings, sibling services, persistent volumes, or host changes are used.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "alpha-retrieval-ranking-test"
PORT = 55439
USER = "retr_rank_test"
PASSWORD = "retr_rank_test"
DEMO_DATABASE = "retr_rank_demo"
DEMO_MARKER = "retrieval-ranking synthetic demo only"


def docker_command() -> str:
    # Windows Docker CLI can reach Docker Desktop from WSL even before native CLI integration.
    for executable in ("docker.exe", "docker"):
        if path := shutil.which(executable):
            return path
    raise RuntimeError("Docker CLI is unavailable; start Docker Desktop or install Docker Engine")


def compose(*args: str, capture: bool = False) -> subprocess.CompletedProcess:
    # Read Compose from stdin so docker.exe never needs to resolve a Linux host file path.
    return subprocess.run(
        [
            docker_command(),
            "compose",
            "--progress",
            "quiet",
            "--project-name",
            PROJECT,
            "-f",
            "-",
            *args,
        ],
        input=(ROOT / "compose.test.yaml").read_text(),
        text=True,
        check=True,
        capture_output=capture,
        cwd=ROOT,
    )


def start() -> None:
    compose("up", "--detach", "--wait", "--wait-timeout", "90")


def stop() -> None:
    # Compose scopes deletion to this exact project. No global prune or sibling containers.
    compose("down", "--remove-orphans")


def dsn(database: str = "postgres") -> str:
    host = os.environ.get("RETR_RANK_TEST_DB_HOST", "127.0.0.1")
    return (
        f"host={host} port={PORT} dbname={database} user={USER} password={PASSWORD} "
        "connect_timeout=5"
    )


def demo(*, reset: bool = False) -> None:
    """Seed only the explicitly named disposable demo, guarding any destructive reset."""
    import psycopg
    from psycopg import sql

    with psycopg.connect(dsn(), autocommit=True) as admin:
        existing = admin.execute(
            "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname=%s",
            (DEMO_DATABASE,),
        ).fetchone()
        if existing and existing[0] != DEMO_MARKER:
            raise RuntimeError("Refusing to reuse a database not marked as our synthetic demo")
        if existing and reset:
            admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(DEMO_DATABASE))
            )
            existing = None
        if not existing:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DEMO_DATABASE)))
            admin.execute(
                sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                    sql.Identifier(DEMO_DATABASE),
                    sql.Literal(DEMO_MARKER),
                )
            )
            _seed_demo()
    print(
        json.dumps(
            {
                "database_url": dsn(DEMO_DATABASE),
                "request": {
                    "company_id": "U_TEST",
                    "user_id": "00000000-0000-0000-0000-0000000000c9",
                    "as_of": "2026-01-10T00:00:00Z",
                    "top_k": 3,
                },
                "synthetic_vectors": True,
            },
            indent=2,
        )
    )


def _seed_demo() -> None:
    import psycopg

    fixture = ROOT / "tests" / "fixtures"
    spec = importlib.util.spec_from_file_location("synthetic_demo_seed", fixture / "seed.py")
    assert spec is not None and spec.loader is not None
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)
    with psycopg.connect(dsn(DEMO_DATABASE), autocommit=True) as connection:
        connection.execute((fixture / "schema.sql").read_text(), prepare=False)
        with connection.transaction():
            seed.seed_database(connection)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop", "status", "dsn", "demo"))
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Recreate only our marked demo database",
    )
    args = parser.parse_args()
    if args.reset and args.action != "demo":
        parser.error("--reset applies only to demo")
    if args.action == "start":
        start()
    elif args.action == "stop":
        stop()
    elif args.action == "status":
        compose("ps")
    elif args.action == "demo":
        demo(reset=args.reset)
    else:
        print(dsn())


if __name__ == "__main__":
    main()
