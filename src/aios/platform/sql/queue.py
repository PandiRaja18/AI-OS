"""Durable run queue with leases.

A worker claims a run for a bounded time and must keep extending that claim. If
it stops - crash, pause, network partition - the lease lapses and the run goes
back on the queue for someone else. Because a run resumes from its checkpoint,
nothing already finished runs twice.

A run that keeps outliving its workers is poison: after `max_lease_attempts`
handovers it is failed rather than requeued, so one bad run cannot occupy the
pool forever.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from aios.platform.models import Lane, Lease, RunRecord, RunStatus, now
from aios.platform.sql.engine import Database, Dialect, to_time
from aios.platform.sql.runs import RUN_COLUMNS, row_to_run, run_to_params

LEASABLE = (RunStatus.PLANNING, RunStatus.RUNNING)


class SqlRunQueue:
    """Priority-lane queue with atomic claim and lease expiry."""

    def __init__(self, database: Database, max_lease_attempts: int = 3) -> None:
        self._db = database
        self.max_lease_attempts = max_lease_attempts

    def submit(self, record: RunRecord) -> str:
        placeholders = ",".join("?" * len(RUN_COLUMNS.split(",")))
        self._db.execute(
            f"INSERT INTO runs ({RUN_COLUMNS}) VALUES ({placeholders})",
            run_to_params(record),
        )
        return record.run_id

    def requeue(self, run_id: str) -> None:
        """Put an existing run back on the queue, e.g. after a review decision."""
        self._db.execute(
            """
            UPDATE runs
            SET status = 'queued', worker_id = NULL, lease_expires_at = NULL,
                updated_at = ?
            WHERE run_id = ?
            """,
            (to_time(now()), run_id),
        )

    def lease(
        self,
        worker_id: str,
        lanes: tuple[Lane, ...] = (Lane.INTERACTIVE, Lane.BATCH, Lane.BACKFILL),
        for_seconds: int = 120,
    ) -> RunRecord | None:
        """Claim the highest-priority queued run in one of `lanes`."""
        lane_values = [lane.value for lane in lanes]
        placeholders = ",".join("?" * len(lane_values))
        expires = now() + timedelta(seconds=for_seconds)

        select = (
            f"SELECT {RUN_COLUMNS} FROM runs "
            f"WHERE status = 'queued' AND lane IN ({placeholders}) "
            "ORDER BY priority DESC, created_at ASC LIMIT 1"
        )
        if self._db.dialect is Dialect.POSTGRES:
            select += " FOR UPDATE SKIP LOCKED"

        with self._db.transaction() as connection:
            cursor = connection.execute(self._db.translate(select), tuple(lane_values))
            row = cursor.fetchone()
            if row is None:
                return None
            columns = [column[0] for column in cursor.description]
            record = row_to_run(dict(zip(columns, row)))
            cursor = connection.execute(
                self._db.translate(
                    """
                    UPDATE runs
                    SET worker_id = ?, lease_expires_at = ?, status = 'planning',
                        updated_at = ?
                    WHERE run_id = ? AND status = 'queued'
                    """
                ),
                (worker_id, to_time(expires), to_time(now()), record.run_id),
            )
            if cursor.rowcount != 1:
                return None

        record.lease = Lease(worker_id=worker_id, expires_at=expires)
        record.status = RunStatus.PLANNING
        return record

    def heartbeat(self, run_id: str, worker_id: str, for_seconds: int = 120) -> bool:
        """Extend a lease. False means the lease was lost and work must stop."""
        expires = now() + timedelta(seconds=for_seconds)
        with self._db.transaction() as connection:
            cursor = connection.execute(
                self._db.translate(
                    """
                    UPDATE runs SET lease_expires_at = ?, updated_at = ?
                    WHERE run_id = ? AND worker_id = ? AND status IN ('planning','running')
                    """
                ),
                (to_time(expires), to_time(now()), run_id, worker_id),
            )
            return cursor.rowcount == 1

    def mark_running(self, run_id: str, worker_id: str) -> None:
        self._db.execute(
            """
            UPDATE runs SET status = 'running', updated_at = ?
            WHERE run_id = ? AND worker_id = ? AND status = 'planning'
            """,
            (to_time(now()), run_id, worker_id),
        )

    def release(
        self,
        run_id: str,
        worker_id: str,
        status: RunStatus,
        failure_reason: str | None = None,
    ) -> None:
        """Give up the lease and record where the run landed."""
        self._db.execute(
            """
            UPDATE runs
            SET status = ?, worker_id = NULL, lease_expires_at = NULL,
                updated_at = ?, failure_reason = COALESCE(?, failure_reason)
            WHERE run_id = ? AND worker_id = ?
            """,
            (status.value, to_time(now()), failure_reason, run_id, worker_id),
        )

    def requeue_expired(self, at: datetime | None = None) -> list[str]:
        """Return lapsed runs to the queue, failing the ones that keep lapsing."""
        moment = at or now()
        rows = self._db.query(
            """
            SELECT run_id, lease_attempts FROM runs
            WHERE status IN ('planning','running')
              AND lease_expires_at IS NOT NULL
              AND lease_expires_at <= ?
            """,
            (to_time(moment),),
        )
        requeued: list[str] = []
        for row in rows:
            attempts = int(row["lease_attempts"]) + 1
            if attempts > self.max_lease_attempts:
                self._db.execute(
                    """
                    UPDATE runs
                    SET status = 'failed', worker_id = NULL, lease_expires_at = NULL,
                        lease_attempts = ?, updated_at = ?,
                        failure_reason = 'lease lapsed more than the handover limit'
                    WHERE run_id = ?
                    """,
                    (attempts, to_time(moment), row["run_id"]),
                )
                continue
            self._db.execute(
                """
                UPDATE runs
                SET status = 'queued', worker_id = NULL, lease_expires_at = NULL,
                    lease_attempts = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (attempts, to_time(moment), row["run_id"]),
            )
            requeued.append(row["run_id"])
        return requeued

    def depth(self, tenant_id: str | None = None) -> int:
        statement = "SELECT COUNT(*) AS n FROM runs WHERE status = 'queued'"
        params: list = []
        if tenant_id is not None:
            statement += " AND tenant_id = ?"
            params.append(tenant_id)
        row = self._db.one(statement, params)
        return int(row["n"]) if row else 0
