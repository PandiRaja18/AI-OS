"""Episodic memory: per-run state and the index of runs.

The task graph itself is stored by LangGraph's SQLite checkpointer, keyed by run
id, which is what makes a paused run resumable. The index adds the small amount
of metadata a console needs to list runs without replaying checkpoints.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import BaseModel

from aios.orchestration.state import (
    AgentResult,
    AgentType,
    Claim,
    Conflict,
    ConflictStatus,
    RunStatus,
    Signoff,
    Task,
    TaskStatus,
)

# Types the checkpointer is allowed to reconstruct from a checkpoint.
CHECKPOINT_TYPES = (
    AgentResult,
    AgentType,
    Claim,
    Conflict,
    ConflictStatus,
    RunStatus,
    Signoff,
    Task,
    TaskStatus,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    goal        TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    report_path TEXT
)
"""


class RunRecord(BaseModel):
    """Index entry for one run."""

    run_id: str
    goal: str
    status: str
    created_at: str
    updated_at: str
    report_path: str | None = None


@contextmanager
def checkpointer(db_path: Path) -> Iterator[SqliteSaver]:
    """Open the LangGraph checkpoint store for the lifetime of a run."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path), check_same_thread=False)
    try:
        yield SqliteSaver(
            connection,
            serde=JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_TYPES),
        )
    finally:
        connection.close()


class RunIndex:
    """Queryable list of runs and their current status."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        with self._connect() as connection:
            connection.execute(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def start(self, run_id: str, goal: str) -> None:
        """Register a new run."""
        now = _now()
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?)",
                (run_id, goal, "planning", now, now, None),
            )

    def update(
        self, run_id: str, status: str, report_path: str | None = None
    ) -> None:
        """Record the latest status, and the report path once one exists."""
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE runs
                SET status = ?,
                    updated_at = ?,
                    report_path = COALESCE(?, report_path)
                WHERE run_id = ?
                """,
                (status, _now(), report_path, run_id),
            )

    def get(self, run_id: str) -> RunRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return RunRecord(**dict(row)) if row else None

    def list(self, limit: int = 20) -> list[RunRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [RunRecord(**dict(row)) for row in rows]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
