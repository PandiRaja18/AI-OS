"""HTTP surface: the run API and the review console.

Every request resolves a principal from its token before anything else happens,
and every query is scoped to that principal's tenant. There is no endpoint that
takes a tenant id from the caller, because that is the one thing a caller must
never be able to choose.
"""

# No `from __future__ import annotations` here: FastAPI resolves dependency
# annotations at import time, and a stringified local alias like `Caller` is not
# resolvable from module globals - it silently becomes a query parameter.

import json
import time
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from aios.platform.models import (
    Lane,
    Principal,
    PrincipalKind,
    RunStatus,
    TenantPolicy,
)
from aios.platform.objects import ObjectNotFound
from aios.platform.principal import InvalidToken
from aios.platform.service import Platform, Rejected
from aios.platform.sql.reviews import NotAHumanDecision, ReviewNotPending

UI_PATH = Path(__file__).parent / "ui" / "console.html"


class SubmitRequest(BaseModel):
    goal: str = Field(min_length=3)
    domain: str = "audit"
    scenario: str = "audit"
    offline: bool = True
    lane: Lane = Lane.INTERACTIVE
    deadline_seconds: int | None = None


class DecideRequest(BaseModel):
    approved: bool
    note: str | None = None


class TokenRequest(BaseModel):
    tenant_id: str
    subject: str
    kind: PrincipalKind = PrincipalKind.HUMAN


def create_app(platform: Platform, allow_dev_tokens: bool = True) -> FastAPI:
    """Build the API around an already-wired platform."""
    app = FastAPI(title="AI-OS", version="1.0.0")

    def principal(
        authorization: Annotated[str | None, Header()] = None,
        token: str | None = None,
    ) -> Principal:
        """Resolve the caller.

        The header is the real channel. `token` exists because EventSource
        cannot set headers, so the read-only stream endpoint accepts it as a
        query parameter; it is the only endpoint that should be reached that
        way, and query strings end up in access logs.
        """
        try:
            return platform.tokens.resolve(authorization or token)
        except InvalidToken as error:
            raise HTTPException(401, str(error)) from error

    Caller = Annotated[Principal, Depends(principal)]

    # --- runs -----------------------------------------------------------------

    @app.post("/v1/runs", status_code=202)
    def submit(request: SubmitRequest, caller: Caller) -> dict[str, Any]:
        try:
            record = platform.submit(
                caller,
                request.goal,
                domain=request.domain,
                scenario=request.scenario,
                offline=request.offline,
                lane=request.lane,
                deadline_seconds=request.deadline_seconds,
            )
        except Rejected as rejected:
            headers = {}
            if rejected.decision.retry_after_seconds:
                headers["Retry-After"] = str(rejected.decision.retry_after_seconds)
            raise HTTPException(429, rejected.decision.reason, headers=headers)
        return {"run_id": record.run_id, "status": record.status}

    @app.get("/v1/runs")
    def list_runs(
        caller: Caller,
        status: RunStatus | None = None,
        limit: int = Query(50, le=200),
    ) -> list[dict[str, Any]]:
        return [
            _run_summary(record)
            for record in platform.runs.list(caller.tenant_id, status, limit)
        ]

    @app.get("/v1/runs/{run_id}")
    def get_run(run_id: str, caller: Caller) -> dict[str, Any]:
        record = platform.runs.get(run_id, caller.tenant_id)
        if record is None:
            raise HTTPException(404, "no such run")
        review = platform.reviews.for_run(run_id)
        return {
            **_run_summary(record),
            "budget": record.budget.model_dump(mode="json"),
            "attempts": [
                attempt.model_dump(mode="json")
                for attempt in platform.runs.attempts(run_id)
            ],
            "review": review.model_dump(mode="json") if review else None,
        }

    @app.post("/v1/runs/{run_id}/cancel")
    def cancel(run_id: str, caller: Caller) -> dict[str, Any]:
        record = platform.cancel(caller, run_id)
        if record is None:
            raise HTTPException(404, "no such run")
        return _run_summary(record)

    @app.get("/v1/runs/{run_id}/trace")
    def trace(
        run_id: str,
        caller: Caller,
        since_seq: int = 0,
        limit: int = Query(500, le=2000),
    ) -> list[dict[str, Any]]:
        if platform.runs.get(run_id, caller.tenant_id) is None:
            raise HTTPException(404, "no such run")
        return [
            event.model_dump(mode="json")
            for event in platform.trace.query(
                caller.tenant_id, run_id, since_seq=since_seq, limit=limit
            )
        ]

    @app.get("/v1/runs/{run_id}/stream")
    def stream(run_id: str, caller: Caller, since_seq: int = 0) -> StreamingResponse:
        """Server-sent events, so the console follows a run as it happens."""
        if platform.runs.get(run_id, caller.tenant_id) is None:
            raise HTTPException(404, "no such run")

        def events():
            seq = since_seq
            idle = 0
            while idle < 600:
                batch = platform.trace.query(caller.tenant_id, run_id, since_seq=seq)
                for event in batch:
                    seq = max(seq, event.seq)
                    yield f"data: {event.model_dump_json()}\n\n"
                record = platform.runs.get(run_id, caller.tenant_id)
                if batch:
                    idle = 0
                else:
                    idle += 1
                    if record is not None and record.status in (
                        RunStatus.COMPLETED,
                        RunStatus.FAILED,
                        RunStatus.CANCELLED,
                        RunStatus.EXPIRED,
                        RunStatus.AWAITING_SIGNOFF,
                    ):
                        yield f"event: end\ndata: {json.dumps({'status': record.status})}\n\n"
                        return
                time.sleep(0.4)

        return StreamingResponse(events(), media_type="text/event-stream")

    # --- reviews --------------------------------------------------------------

    @app.get("/v1/reviews")
    def pending(caller: Caller) -> list[dict[str, Any]]:
        return [
            review.model_dump(mode="json")
            for review in platform.pending_reviews(caller)
        ]

    @app.post("/v1/reviews/{review_id}/decide")
    def decide(review_id: str, request: DecideRequest, caller: Caller) -> dict[str, Any]:
        if not caller.may("reviews:decide"):
            raise HTTPException(403, "principal may not decide reviews")
        try:
            review = platform.decide(caller, review_id, request.approved, request.note)
        except NotAHumanDecision as error:
            raise HTTPException(403, str(error)) from error
        except ReviewNotPending as error:
            raise HTTPException(409, str(error)) from error
        return review.model_dump(mode="json")

    # --- artefacts and metrics ------------------------------------------------

    @app.get("/v1/reports")
    def report(uri: str, caller: Caller) -> dict[str, Any]:
        if not uri.startswith(f"aios://{caller.tenant_id}/"):
            raise HTTPException(403, "report belongs to another tenant")
        try:
            return {"uri": uri, "markdown": platform.objects.get(uri)}
        except ObjectNotFound as error:
            raise HTTPException(404, str(error)) from error

    @app.get("/v1/metrics")
    def metrics(caller: Caller) -> dict[str, Any]:
        counts = platform.trace.counts_by_kind(caller.tenant_id)
        runs = platform.runs.list(caller.tenant_id, limit=200)
        completed = [r for r in runs if r.status is RunStatus.COMPLETED]
        return {
            "queue_depth": platform.queue.depth(caller.tenant_id),
            "runs_total": len(runs),
            "runs_by_status": {
                status.value: sum(1 for r in runs if r.status is status)
                for status in RunStatus
            },
            "trace_counts": counts,
            "decision_quality": {
                "conflicts_detected": counts.get("conflict_detected", 0),
                "conflicts_resolved": counts.get("conflict_resolved", 0),
                "conflicts_escalated": counts.get("conflict_escalated", 0),
                "tasks_retried": counts.get("task_retry", 0),
                "tasks_degraded": counts.get("task_degraded", 0),
                "plans_rejected": counts.get("plan_rejected", 0),
                "replans": counts.get("replan", 0),
                "promotions": counts.get("memory_promote", 0),
            },
            "spend": {
                "currency": str(sum((r.spend.currency for r in runs), start=0)),
                "input_tokens": sum(r.spend.input_tokens for r in runs),
                "output_tokens": sum(r.spend.output_tokens for r in runs),
                "llm_calls": sum(r.spend.llm_calls for r in runs),
                "per_completed_run": (
                    str(sum((r.spend.currency for r in completed), start=0) / len(completed))
                    if completed
                    else "0"
                ),
            },
        }

    @app.get("/v1/memory")
    def memory(caller: Caller, history: bool = False) -> list[dict[str, Any]]:
        return [
            fact.model_dump(mode="json")
            for fact in platform.facts.facts(caller.tenant_id, include_superseded=history)
        ]

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "queue_depth": platform.queue.depth()}

    # --- dev conveniences -----------------------------------------------------

    if allow_dev_tokens:

        @app.post("/v1/tokens")
        def mint(request: TokenRequest) -> dict[str, str]:
            """Development helper. Disable it wherever real identities exist."""
            if platform.policy.tenant(request.tenant_id) is None:
                platform.provision_tenant(
                    TenantPolicy(tenant_id=request.tenant_id, name=request.tenant_id)
                )
            return {
                "token": platform.token_for(
                    request.tenant_id, request.subject, request.kind
                )
            }

    @app.get("/", response_class=HTMLResponse)
    def console() -> str:
        return UI_PATH.read_text(encoding="utf-8")

    return app


def _run_summary(record) -> dict[str, Any]:
    return {
        "run_id": record.run_id,
        "tenant_id": record.tenant_id,
        "goal": record.goal,
        "status": record.status,
        "lane": record.lane,
        "submitted_by": record.submitted_by,
        "spend": record.spend.model_dump(mode="json"),
        "report_uri": record.report_uri,
        "failure_reason": record.failure_reason,
        "worker_id": record.lease.worker_id if record.lease else None,
        "lease_attempts": record.lease_attempts,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }
