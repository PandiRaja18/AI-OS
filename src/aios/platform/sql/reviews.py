"""The human gate as a record.

In v1 the gate was a terminal prompt and the process sat there. Here it is a row:
the worker opens a review and releases the run, so a pause costs no compute and
can outlast the process, the machine and the working day. A decision or an expiry
is what puts the run back on the queue.
"""

from __future__ import annotations

from datetime import datetime

from aios.orchestration.state import Signoff
from aios.platform.models import (
    Principal,
    PrincipalKind,
    ReviewRequest,
    ReviewStatus,
    now,
)
from aios.platform.sql.engine import (
    Database,
    from_list,
    from_time,
    model_json,
    parse_model,
    to_list,
    to_time,
)


class NotAHumanDecision(PermissionError):
    """Only a human principal may decide a review."""


class ReviewNotPending(RuntimeError):
    """The review was already decided or has expired."""


class SqlReviewInbox:
    """Pending human decisions, with assignment and a TTL."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def open(self, request: ReviewRequest) -> str:
        self._db.execute(
            """
            INSERT INTO reviews (
                review_id, run_id, tenant_id, goal, task_ids, draft, report_uri,
                open_conflicts, degraded_tasks, assigned_to, status, decision,
                created_at, expires_at, decided_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,NULL)
            """,
            (
                request.review_id,
                request.run_id,
                request.tenant_id,
                request.goal,
                to_list(request.task_ids),
                request.draft,
                request.report_uri,
                request.open_conflicts,
                request.degraded_tasks,
                to_list(request.assigned_to),
                request.status.value,
                to_time(request.created_at),
                to_time(request.expires_at),
            ),
        )
        return request.review_id

    def get(self, review_id: str, tenant_id: str | None = None) -> ReviewRequest | None:
        statement = "SELECT * FROM reviews WHERE review_id = ?"
        params: list = [review_id]
        if tenant_id is not None:
            statement += " AND tenant_id = ?"
            params.append(tenant_id)
        row = self._db.one(statement, params)
        return _row_to_review(row) if row else None

    def for_run(self, run_id: str) -> ReviewRequest | None:
        row = self._db.one("SELECT * FROM reviews WHERE run_id = ?", (run_id,))
        return _row_to_review(row) if row else None

    def pending(
        self, tenant_id: str, reviewer: str | None = None
    ) -> list[ReviewRequest]:
        rows = self._db.query(
            """
            SELECT * FROM reviews
            WHERE tenant_id = ? AND status = 'pending'
            ORDER BY created_at ASC
            """,
            (tenant_id,),
        )
        reviews = [_row_to_review(row) for row in rows]
        if reviewer is None:
            return reviews
        return [
            review
            for review in reviews
            if not review.assigned_to or reviewer in review.assigned_to
        ]

    def decide(
        self, review_id: str, decision: Signoff, principal: Principal
    ) -> ReviewRequest:
        """Record a human decision. Agents cannot approve their own work."""
        if principal.kind is not PrincipalKind.HUMAN:
            raise NotAHumanDecision(f"{principal} is not a human principal")
        review = self.get(review_id, principal.tenant_id)
        if review is None:
            raise ReviewNotPending(f"unknown review {review_id}")
        if review.status is not ReviewStatus.PENDING:
            raise ReviewNotPending(
                f"review {review_id} is already {review.status.value}"
            )

        decided_at = now()
        self._db.execute(
            """
            UPDATE reviews
            SET status = 'decided', decision = ?, decided_at = ?
            WHERE review_id = ? AND status = 'pending'
            """,
            (model_json(decision), to_time(decided_at), review_id),
        )
        return review.model_copy(
            update={
                "status": ReviewStatus.DECIDED,
                "decision": decision,
                "decided_at": decided_at,
            }
        )

    def expire(self, at: datetime | None = None) -> list[ReviewRequest]:
        """Close reviews nobody answered in time."""
        moment = at or now()
        rows = self._db.query(
            "SELECT * FROM reviews WHERE status = 'pending' AND expires_at <= ?",
            (to_time(moment),),
        )
        expired = [_row_to_review(row) for row in rows]
        for review in expired:
            self._db.execute(
                "UPDATE reviews SET status = 'expired', decided_at = ? WHERE review_id = ?",
                (to_time(moment), review.review_id),
            )
        return expired


def _row_to_review(row: dict) -> ReviewRequest:
    return ReviewRequest(
        review_id=row["review_id"],
        run_id=row["run_id"],
        tenant_id=row["tenant_id"],
        goal=row["goal"],
        task_ids=from_list(row["task_ids"]),
        draft=row["draft"],
        report_uri=row["report_uri"],
        open_conflicts=row["open_conflicts"],
        degraded_tasks=row["degraded_tasks"],
        assigned_to=from_list(row["assigned_to"]),
        status=ReviewStatus(row["status"]),
        decision=parse_model(Signoff, row["decision"]),
        created_at=from_time(row["created_at"]),
        expires_at=from_time(row["expires_at"]),
        decided_at=from_time(row["decided_at"]),
    )
