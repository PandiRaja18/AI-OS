"""Append-only trace of every decision the platform makes during a run.

The task-graph view, the run report and post-mortem inspection all read from
this log, so there is exactly one place where a run's history lives.
"""

from __future__ import annotations

import enum
import json
import threading
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class EventKind(enum.StrEnum):
    RUN_STARTED = "run_started"
    RUN_STATUS = "run_status"
    MEMORY_RECALL = "memory_recall"
    MEMORY_PROMOTE = "memory_promote"
    PLAN = "plan"
    PLAN_REJECTED = "plan_rejected"
    REPLAN = "replan"
    DISPATCH = "dispatch"
    TOOL_CALL = "tool_call"
    TOOL_ERROR = "tool_error"
    ACCESS_DENIED = "access_denied"
    LLM_CALL = "llm_call"
    AGENT_RESULT = "agent_result"
    TASK_RETRY = "task_retry"
    TASK_DEGRADED = "task_degraded"
    TASK_BLOCKED = "task_blocked"
    CONFLICT_DETECTED = "conflict_detected"
    CONFLICT_RESOLVED = "conflict_resolved"
    CONFLICT_ESCALATED = "conflict_escalated"
    SIGNOFF_REQUESTED = "signoff_requested"
    SIGNOFF_RECORDED = "signoff_recorded"
    REPORT = "report"


class TraceEvent(BaseModel):
    """One traced decision, tool call or message."""

    seq: int
    run_id: str
    kind: EventKind
    actor: str
    message: str
    task_id: str | None = None
    duration_ms: int | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(
            timespec="milliseconds"
        )
    )


Listener = Callable[[TraceEvent], None]


class TraceStore:
    """Writes a run's events to JSONL and forwards them to a live listener."""

    def __init__(
        self, run_id: str, trace_dir: Path, listener: Listener | None = None
    ) -> None:
        self._run_id = run_id
        self._path = trace_dir / f"{run_id}.jsonl"
        self._listener = listener
        self._lock = threading.Lock()
        self._events: list[TraceEvent] = []
        # A run resumed in a later process appends to the same log, so the
        # sequence continues from what is already on disk.
        self._seq = _last_seq(self._path)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def events(self) -> list[TraceEvent]:
        return list(self._events)

    def emit(
        self,
        kind: EventKind,
        message: str,
        *,
        actor: str = "coordinator",
        task_id: str | None = None,
        duration_ms: int | None = None,
        **detail: Any,
    ) -> TraceEvent:
        """Record an event, persist it, and notify the listener."""
        with self._lock:
            self._seq += 1
            event = TraceEvent(
                seq=self._seq,
                run_id=self._run_id,
                kind=kind,
                actor=actor,
                message=message,
                task_id=task_id,
                duration_ms=duration_ms,
                detail=detail,
            )
            self._events.append(event)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(event.model_dump_json() + "\n")
        if self._listener is not None:
            self._listener(event)
        return event


def _last_seq(path: Path) -> int:
    """Highest sequence number already recorded in a trace log."""
    if not path.exists():
        return 0
    with path.open(encoding="utf-8") as handle:
        return max(
            (json.loads(line)["seq"] for line in handle if line.strip()), default=0
        )


def read_trace(trace_dir: Path, run_id: str) -> Iterator[TraceEvent]:
    """Replay a persisted trace."""
    path = trace_dir / f"{run_id}.jsonl"
    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield TraceEvent.model_validate(json.loads(line))
