"""Database access that is portable between SQLite and Postgres.

Statements are written once with `?` placeholders and translated for the driver.
Values that differ between engines - timestamps, money, JSON - are stored as text
in a canonical form, so a table written by one engine reads the same on the
other. The only genuinely dialect-specific statement is the queue's atomic claim,
which each dialect spells differently and which `queue.py` selects on.
"""

from __future__ import annotations

import enum
import json
import sqlite3
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

Row = dict[str, Any]


class Dialect(enum.StrEnum):
    SQLITE = "sqlite"
    POSTGRES = "postgres"


class Database:
    """A connection factory with a small, portable query surface."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.dialect = (
            Dialect.POSTGRES
            if url.startswith(("postgres://", "postgresql://"))
            else Dialect.SQLITE
        )
        self._local = threading.local()
        if self.dialect is Dialect.SQLITE:
            path = url.removeprefix("sqlite:///") or ":memory:"
            self._path = path
            if path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)

    def _new_connection(self):
        if self.dialect is Dialect.SQLITE:
            connection = sqlite3.connect(
                self._path, timeout=15.0, check_same_thread=False
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=15000")
            connection.execute("PRAGMA foreign_keys=ON")
            return connection
        import psycopg  # imported lazily so SQLite deployments need no driver

        return psycopg.connect(self.url, autocommit=False)

    @property
    def connection(self):
        """One connection per thread, opened on first use."""
        existing = getattr(self._local, "connection", None)
        if existing is None:
            existing = self._new_connection()
            self._local.connection = existing
        return existing

    def close(self) -> None:
        existing = getattr(self._local, "connection", None)
        if existing is not None:
            existing.close()
            self._local.connection = None

    def translate(self, statement: str) -> str:
        """Adapt placeholder style to the driver."""
        if self.dialect is Dialect.POSTGRES:
            return statement.replace("?", "%s")
        return statement

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """Run a unit of work. SQLite takes the write lock up front."""
        connection = self.connection
        if self.dialect is Dialect.SQLITE:
            connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def execute(self, statement: str, params: Sequence[Any] = ()) -> None:
        with self.transaction() as connection:
            connection.execute(self.translate(statement), tuple(params))

    def execute_many(self, statement: str, rows: Sequence[Sequence[Any]]) -> None:
        with self.transaction() as connection:
            connection.executemany(self.translate(statement), [tuple(r) for r in rows])

    def script(self, statements: Sequence[str]) -> None:
        with self.transaction() as connection:
            for statement in statements:
                connection.execute(self.translate(statement))

    def query(self, statement: str, params: Sequence[Any] = ()) -> list[Row]:
        cursor = self.connection.execute(self.translate(statement), tuple(params))
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def one(self, statement: str, params: Sequence[Any] = ()) -> Row | None:
        rows = self.query(statement, params)
        return rows[0] if rows else None


# --- canonical encodings, so both engines round-trip identically ---------------


def to_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def from_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value))


def to_json(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)


def from_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def to_money(value: Decimal) -> str:
    return f"{Decimal(value):.6f}"


def from_money(value: Any) -> Decimal:
    return Decimal(str(value or "0"))


def to_list(values: Sequence[str]) -> str:
    return to_json(list(values))


def from_list(value: Any) -> tuple[str, ...]:
    return tuple(from_json(value) or ())


def model_json(model: Any) -> str:
    """Serialize a Pydantic model for a text column."""
    return model.model_dump_json()


def parse_model(model_type: Any, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return model_type.model_validate(value)
    return model_type.model_validate_json(value)
