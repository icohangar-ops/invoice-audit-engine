"""SQLite helpers shared by all apps. Each app owns its tables; migrations are idempotent."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from auditengine.config import settings


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path or settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_schema(conn: sqlite3.Connection, ddl: str) -> None:
    conn.executescript(ddl)
