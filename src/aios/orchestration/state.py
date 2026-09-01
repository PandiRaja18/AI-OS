"""Run state: the typed data model checkpointed by LangGraph.

Everything here is plain data. Scheduling policy lives in `coordinator`, graph
wiring in `graph`; keeping the state pure is what makes a run resumable from a
checkpoint alone.
"""

from __future__ import annotations

import enum
from typing import Annotated, Any, TypedDict

from pydantic import BaseModel, Field


class AgentType(enum.StrEnum):
    PLANNER = "planner"
    RESEARCH = "research"
    DATA = "data"
    REPORTING = "reporting"


WORKER_AGENTS = frozenset(
    {AgentType.RESEARCH, AgentType.DATA, AgentType.REPORTING}
)


class TaskStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    AWAITING_SIGNOFF = "awaiting_signoff"
    SUPERSEDED = "superseded"


class RunStatus(enum.StrEnum):
    PLANNING = "planning"
    RUNNING = "running"
    AWAITING_SIGNOFF = "awaiting_signoff"
    COMPLETED = "completed"
    FAILED = "failed"


class ConflictStatus(enum.StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    ESCALATED = "escalated"


class Claim(BaseModel):
    """A single asserted fact. Conflict detection compares claims, not prose."""

    subject: str
    metric: str
    value: str


class AgentResult(BaseModel):
    """The typed contract every worker agent returns."""

    summary: str
    claims: list[Claim] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)


class Task(BaseModel):
    """One node of the plan DAG."""

    task_id: str
    description: str
    agent: AgentType
    depends_on: list[str] = Field(default_factory=list)
    success_criteria: str
    critical: bool = True
    requires_signoff: bool = False
    context: dict[str, str] = Field(default_factory=dict)

    status: TaskStatus = TaskStatus.PENDING
    attempts: int = 0
    result: AgentResult | None = None
    error: str | None = None

    @property
    def resolved(self) -> bool:
        """True once the task no longer blocks its dependents."""
        return self.status in (
            TaskStatus.DONE,
            TaskStatus.DEGRADED,
            TaskStatus.SUPERSEDED,
        )


class Conflict(BaseModel):
    """Two agents asserting different values for the same subject and metric."""

    conflict_id: str
    subject: str
    metric: str
    values: dict[str, str]
    status: ConflictStatus = ConflictStatus.OPEN
    resolution: str | None = None
    requeried_task: str | None = None
    attempts: int = 0


class Signoff(BaseModel):
    """A human decision on the tasks that requested one."""

    approved: bool
    reviewer: str
    note: str | None = None


class Dispatch(TypedDict):
    """Payload sent to the execute node, one per fan-out branch.

    The task travels as a plain dict so the payload survives checkpointing.
    """

    run_id: str
    goal: str
    task: dict[str, Any]
    attempt: int
    context: dict[str, str]


def merge_tasks(left: dict[str, Task], right: dict[str, Task]) -> dict[str, Task]:
    """Reducer for concurrent task writes; the newer value per id wins."""
    merged = dict(left)
    merged.update(right)
    return merged


def merge_conflicts(
    left: dict[str, Conflict], right: dict[str, Conflict]
) -> dict[str, Conflict]:
    """Reducer for concurrent conflict writes; the newer value per id wins."""
    merged = dict(left)
    merged.update(right)
    return merged


class RunState(TypedDict):
    """The checkpointed state of one run."""

    run_id: str
    goal: str
    status: RunStatus
    tasks: Annotated[dict[str, Task], merge_tasks]
    conflicts: Annotated[dict[str, Conflict], merge_conflicts]
    precedents: list[str]
    plan_revision: int
    replan_reason: str | None
    signoff: Signoff | None
    report: str | None
    report_path: str | None


def new_run_state(run_id: str, goal: str, precedents: list[str]) -> RunState:
    """Build the initial state for a fresh run."""
    return RunState(
        run_id=run_id,
        goal=goal,
        status=RunStatus.PLANNING,
        tasks={},
        conflicts={},
        precedents=precedents,
        plan_revision=0,
        replan_reason=None,
        signoff=None,
        report=None,
        report_path=None,
    )
