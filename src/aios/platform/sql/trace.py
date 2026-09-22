"""Trace events, queryable across runs.

v1 wrote one JSONL file per run, which answers "what happened in this run" and
nothing else. The same events in a table answer the questions that matter once
there are many runs: how often do conflicts escalate, which tool fails most, what
does a run cost.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from aios.observability import TraceEvent
from aios.platform.models import new_id, now
from aios.platform.sql.engine import Database, from_json, from_time, to_json, to_time


class TraceRecord(BaseModel):
    """A stored trace event, scoped to its tenant."""

    event_id: str
    run_id: str
    tenant_id: str
    seq: int
    kind: str
    actor: str
    task_id: str | None = None
    message: str
    duration_ms: int | None = None
    detail: dict = Field(default_factory=dict)
    at: datetime


class SqlTraceSink:
    """Append-only event log, scoped to one tenant on the write path."""

    def __init__(self, database: Database, tenant_id: str | None = None) -> None:
        self._db = database
        self._tenant_id = tenant_id

    def for_tenant(self, tenant_id: str) -> "SqlTraceSink":
        return SqlTraceSink(self._db, tenant_id)

    def emit(self, event: TraceEvent) -> None:
        self._db.execute(
            """
            INSERT INTO trace_events (
                event_id, run_id, tenant_id, seq, kind, actor, task_id, message,
                duration_ms, detail, at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                new_id("evt"),
                event.run_id,
                self._tenant_id or "unknown",
                event.seq,
                str(event.kind),
                event.actor,
                event.task_id,
                event.message,
                event.duration_ms,
                to_json(event.detail),
                to_time(from_time(event.at) or now()),
            ),
        )

    def max_seq(self, run_id: str) -> int:
        """Highest sequence already recorded, so a resumed run continues it."""
        row = self._db.one(
            "SELECT MAX(seq) AS s FROM trace_events WHERE run_id = ?", (run_id,)
        )
        return int(row["s"]) if row and row["s"] is not None else 0

    def query(
        self,
        tenant_id: str | None = None,
        run_id: str | None = None,
        kinds: tuple[str, ...] = (),
        since_seq: int = 0,
        limit: int = 500,
    ) -> list[TraceRecord]:
        statement = "SELECT * FROM trace_events WHERE seq > ?"
        params: list = [since_seq]
        if tenant_id is not None:
            statement += " AND tenant_id = ?"
            params.append(tenant_id)
        if run_id is not None:
            statement += " AND run_id = ?"
            params.append(run_id)
        if kinds:
            statement += f" AND kind IN ({','.join('?' * len(kinds))})"
            params.extend(kinds)
        statement += " ORDER BY seq ASC LIMIT ?"
        params.append(limit)
        return [_row_to_record(row) for row in self._db.query(statement, params)]

    def counts_by_kind(self, tenant_id: str | None = None) -> dict[str, int]:
        statement = "SELECT kind, COUNT(*) AS n FROM trace_events WHERE 1=1"
        params: list = []
        if tenant_id is not None:
            statement += " AND tenant_id = ?"
            params.append(tenant_id)
        statement += " GROUP BY kind ORDER BY n DESC"
        return {row["kind"]: int(row["n"]) for row in self._db.query(statement, params)}

    def run_ids(self, tenant_id: str | None = None, limit: int = 50) -> list[str]:
        statement = "SELECT DISTINCT run_id FROM trace_events WHERE 1=1"
        params: list = []
        if tenant_id is not None:
            statement += " AND tenant_id = ?"
            params.append(tenant_id)
        statement += " LIMIT ?"
        params.append(limit)
        return [row["run_id"] for row in self._db.query(statement, params)]


def _row_to_record(row: dict) -> TraceRecord:
    return TraceRecord(
        event_id=row["event_id"],
        run_id=row["run_id"],
        tenant_id=row["tenant_id"],
        seq=row["seq"],
        kind=row["kind"],
        actor=row["actor"],
        task_id=row["task_id"],
        message=row["message"],
        duration_ms=row["duration_ms"],
        detail=from_json(row["detail"]) or {},
        at=from_time(row["at"]),
    )
