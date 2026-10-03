"""Run records and the append-only execution history."""

from __future__ import annotations

from datetime import datetime

from aios.platform.models import (
    Budget,
    Lane,
    Lease,
    RunRecord,
    RunStatus,
    Spend,
    TaskAttempt,
    now,
)
from aios.platform.sql.engine import (
    Database,
    Row,
    from_time,
    model_json,
    parse_model,
    to_time,
)

RUN_COLUMNS = (
    "run_id, tenant_id, submitted_by, goal, domain, scenario, offline, status, "
    "lane, priority, budget, spend, worker_id, lease_expires_at, lease_attempts, "
    "report_uri, failure_reason, created_at, updated_at, deadline_at"
)


def row_to_run(row: Row) -> RunRecord:
    """Rebuild a run record from its row."""
    lease = None
    if row["worker_id"] and row["lease_expires_at"]:
        lease = Lease(
            worker_id=row["worker_id"],
            expires_at=from_time(row["lease_expires_at"]),
        )
    return RunRecord(
        run_id=row["run_id"],
        tenant_id=row["tenant_id"],
        submitted_by=row["submitted_by"],
        goal=row["goal"],
        domain=row["domain"],
        scenario=row["scenario"],
        offline=bool(row["offline"]),
        status=RunStatus(row["status"]),
        lane=Lane(row["lane"]),
        priority=row["priority"],
        budget=parse_model(Budget, row["budget"]),
        spend=parse_model(Spend, row["spend"]),
        lease=lease,
        lease_attempts=row["lease_attempts"],
        report_uri=row["report_uri"],
        failure_reason=row["failure_reason"],
        created_at=from_time(row["created_at"]),
        updated_at=from_time(row["updated_at"]),
        deadline_at=from_time(row["deadline_at"]),
    )


def run_to_params(record: RunRecord) -> tuple:
    return (
        record.run_id,
        record.tenant_id,
        record.submitted_by,
        record.goal,
        record.domain,
        record.scenario,
        int(record.offline),
        record.status.value,
        record.lane.value,
        record.priority,
        model_json(record.budget),
        model_json(record.spend),
        record.lease.worker_id if record.lease else None,
        to_time(record.lease.expires_at) if record.lease else None,
        record.lease_attempts,
        record.report_uri,
        record.failure_reason,
        to_time(record.created_at),
        to_time(record.updated_at),
        to_time(record.deadline_at),
    )


class SqlRunStore:
    """Queryable record of runs, their cost and their execution history."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def get(self, run_id: str, tenant_id: str | None = None) -> RunRecord | None:
        statement = f"SELECT {RUN_COLUMNS} FROM runs WHERE run_id = ?"
        params: list = [run_id]
        if tenant_id is not None:
            statement += " AND tenant_id = ?"
            params.append(tenant_id)
        row = self._db.one(statement, params)
        return row_to_run(row) if row else None

    def list(
        self,
        tenant_id: str | None = None,
        status: RunStatus | None = None,
        limit: int = 50,
    ) -> list[RunRecord]:
        statement = f"SELECT {RUN_COLUMNS} FROM runs WHERE 1=1"
        params: list = []
        if tenant_id is not None:
            statement += " AND tenant_id = ?"
            params.append(tenant_id)
        if status is not None:
            statement += " AND status = ?"
            params.append(status.value)
        statement += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [row_to_run(row) for row in self._db.query(statement, params)]

    def update_status(
        self,
        run_id: str,
        status: RunStatus,
        report_uri: str | None = None,
        failure_reason: str | None = None,
    ) -> None:
        self._db.execute(
            """
            UPDATE runs
            SET status = ?,
                updated_at = ?,
                report_uri = COALESCE(?, report_uri),
                failure_reason = COALESCE(?, failure_reason)
            WHERE run_id = ?
            """,
            (status.value, to_time(now()), report_uri, failure_reason, run_id),
        )

    def add_spend(self, run_id: str, delta: Spend) -> Spend:
        """Apply spend inside one transaction so concurrent writers cannot race."""
        with self._db.transaction() as connection:
            cursor = connection.execute(
                self._db.translate("SELECT spend FROM runs WHERE run_id = ?"),
                (run_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return delta
            current = parse_model(Spend, row[0])
            total = current.plus(delta)
            connection.execute(
                self._db.translate(
                    "UPDATE runs SET spend = ?, updated_at = ? WHERE run_id = ?"
                ),
                (model_json(total), to_time(now()), run_id),
            )
        return total

    def append_attempt(self, attempt: TaskAttempt) -> None:
        self._db.execute(
            """
            INSERT INTO task_attempts (
                attempt_id, run_id, tenant_id, task_id, attempt, agent, outcome,
                error, result_uri, confidence, input_tokens, output_tokens,
                duration_ms, at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                attempt.attempt_id,
                attempt.run_id,
                attempt.tenant_id,
                attempt.task_id,
                attempt.attempt,
                attempt.agent,
                attempt.outcome.value,
                attempt.error,
                attempt.result_uri,
                attempt.confidence,
                attempt.input_tokens,
                attempt.output_tokens,
                attempt.duration_ms,
                to_time(attempt.at),
            ),
        )

    def attempts(self, run_id: str) -> list[TaskAttempt]:
        rows = self._db.query(
            "SELECT * FROM task_attempts WHERE run_id = ? ORDER BY at", (run_id,)
        )
        return [
            TaskAttempt(
                attempt_id=row["attempt_id"],
                run_id=row["run_id"],
                tenant_id=row["tenant_id"],
                task_id=row["task_id"],
                attempt=row["attempt"],
                agent=row["agent"],
                outcome=row["outcome"],
                error=row["error"],
                result_uri=row["result_uri"],
                confidence=row["confidence"],
                input_tokens=row["input_tokens"],
                output_tokens=row["output_tokens"],
                duration_ms=row["duration_ms"],
                at=from_time(row["at"]),
            )
            for row in rows
        ]

    def active_run_count(self, tenant_id: str) -> int:
        row = self._db.one(
            """
            SELECT COUNT(*) AS n FROM runs
            WHERE tenant_id = ?
              AND status IN ('queued','planning','running','awaiting_signoff')
            """,
            (tenant_id,),
        )
        return int(row["n"]) if row else 0

    def spend_since(self, tenant_id: str, since: datetime) -> Spend:
        rows = self._db.query(
            "SELECT spend FROM runs WHERE tenant_id = ? AND created_at >= ?",
            (tenant_id, to_time(since)),
        )
        total = Spend()
        for row in rows:
            total = total.plus(parse_model(Spend, row["spend"]))
        return total
