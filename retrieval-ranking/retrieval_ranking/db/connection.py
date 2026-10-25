"""Open short-lived connections; inference never retains a database transaction."""

from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row

from ..config import Settings


@contextmanager
def connect(settings: Settings):
    with psycopg.connect(
        settings.database_url.get_secret_value(),
        connect_timeout=settings.connect_timeout_seconds,
        application_name="retrieval-ranking",
        autocommit=True,
        row_factory=dict_row,
    ) as connection:
        yield connection


def set_timeout(connection: psycopg.Connection[dict[str, Any]], settings: Settings) -> None:
    connection.execute(
        "SELECT set_config('statement_timeout', %s, true)",
        (str(settings.statement_timeout_ms),),
    )
