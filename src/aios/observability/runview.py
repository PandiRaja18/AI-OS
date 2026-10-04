"""Rebuilds a readable view of a run from its trace.

The trace is the record of what happened, but it is a flat list of events, which
is the wrong shape for a reader. This folds it back into the three things a
person actually wants: the task graph, the contradictions and how they were
settled, and the work grouped by the task it belongs to.

Nothing is stored for this. If the trace can be replayed, the view can be
rebuilt, which keeps the trace the single source of truth.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Sequence

from pydantic import BaseModel, Field


class TaskState(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    SUPERSEDED = "superseded"
    AWAITING_SIGNOFF = "awaiting_signoff"


class Claim(BaseModel):
    subject: str
    metric: str
    value: str


class TaskNode(BaseModel):
    """One task, as the console draws it."""

    task_id: str
    description: str = ""
    agent: str = ""
    depends_on: list[str] = Field(default_factory=list)
    critical: bool = True
    requires_signoff: bool = False
    state: TaskState = TaskState.PENDING
    attempts: int = 0
    confidence: float | None = None
    summary: str = ""
    claims: list[Claim] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    error: str = ""
    duration_ms: int = 0
    depth: int = 0


class ConflictView(BaseModel):
    """A contradiction, the claims behind it, and how it ended."""

    subject: str
    metric: str
    state: str = "open"
    values: dict[str, str] = Field(default_factory=dict)
    requeried: str = ""
    resolution: str = ""


class Milestone(BaseModel):
    """A moment worth marking on a timeline."""

    seq: int
    kind: str
    message: str


class RunView(BaseModel):
    """Everything the console needs to render one run."""

    run_id: str
    goal: str = ""
    tasks: list[TaskNode] = Field(default_factory=list)
    conflicts: list[ConflictView] = Field(default_factory=list)
    milestones: list[Milestone] = Field(default_factory=list)
    events_by_task: dict[str, list[Milestone]] = Field(default_factory=dict)
    tool_calls: int = 0
    tool_errors: int = 0
    retries: int = 0
    model_calls: int = 0
    plan_revisions: int = 1


MILESTONE_KINDS = frozenset(
    {
        "run_started",
        "plan",
        "replan",
        "plan_rejected",
        "conflict_detected",
        "conflict_resolved",
        "conflict_escalated",
        "task_degraded",
        "task_blocked",
        "signoff_requested",
        "signoff_recorded",
        "memory_promote",
        "report",
    }
)

_FAILURE_STATE = {
    "task_degraded": TaskState.DEGRADED,
    "task_blocked": TaskState.BLOCKED,
}


def build_run_view(run_id: str, events: Sequence) -> RunView:
    """Fold a run's trace into the shape the console draws."""
    view = RunView(run_id=run_id)
    nodes: dict[str, TaskNode] = {}

    for event in events:
        kind = str(event.kind)
        detail = event.detail or {}

        if kind == "run_started":
            view.goal = event.message.removeprefix("goal: ")
        elif kind in ("plan", "replan"):
            if kind == "replan":
                view.plan_revisions += 1
            _apply_plan(nodes, detail.get("tasks", []))
        elif kind == "dispatch":
            _apply_dispatch(nodes, event)
        elif kind == "agent_result":
            _apply_result(nodes, event, detail)
        elif kind == "tool_call":
            view.tool_calls += 1
            _note_tool(nodes, event)
        elif kind == "tool_error":
            view.tool_errors += 1
        elif kind == "task_retry":
            view.retries += 1
        elif kind == "llm_call":
            view.model_calls += 1
        elif kind in _FAILURE_STATE:
            node = nodes.get(event.task_id or "")
            if node is not None:
                node.state = _FAILURE_STATE[kind]
                node.error = event.message
        elif kind.startswith("conflict_"):
            _apply_conflict(view, kind, event, detail)
        elif kind == "signoff_requested":
            for task_id in detail.get("task_ids", []):
                if task_id in nodes:
                    nodes[task_id].state = TaskState.AWAITING_SIGNOFF
        elif kind == "signoff_recorded":
            for node in nodes.values():
                if node.state is TaskState.AWAITING_SIGNOFF:
                    node.state = TaskState.DONE

        if kind in MILESTONE_KINDS:
            view.milestones.append(
                Milestone(seq=event.seq, kind=kind, message=event.message)
            )
        if event.task_id:
            view.events_by_task.setdefault(event.task_id, []).append(
                Milestone(seq=event.seq, kind=kind, message=event.message)
            )

    _mark_superseded(nodes, view.plan_revisions)
    view.tasks = _ordered(nodes)
    return view


def _apply_plan(nodes: dict[str, TaskNode], outlines: Iterable[dict]) -> None:
    planned = set()
    for outline in outlines:
        task_id = outline.get("task_id")
        if not task_id:
            continue
        planned.add(task_id)
        node = nodes.get(task_id) or TaskNode(task_id=task_id)
        node.description = outline.get("description", node.description)
        node.agent = outline.get("agent", node.agent)
        node.depends_on = list(outline.get("depends_on", node.depends_on))
        node.critical = outline.get("critical", node.critical)
        node.requires_signoff = outline.get("requires_signoff", node.requires_signoff)
        nodes[task_id] = node
    for task_id, node in nodes.items():
        if task_id not in planned and node.state is TaskState.PENDING:
            node.state = TaskState.SUPERSEDED


def _apply_dispatch(nodes: dict[str, TaskNode], event) -> None:
    task_id = event.task_id
    if not task_id:
        return
    node = nodes.setdefault(task_id, TaskNode(task_id=task_id))
    node.attempts += 1
    node.state = TaskState.RUNNING
    if " -> " in event.message and not node.agent:
        node.agent = event.message.split(" -> ")[1].split(" ")[0]


def _apply_result(nodes: dict[str, TaskNode], event, detail: dict) -> None:
    task_id = event.task_id
    if not task_id:
        return
    node = nodes.setdefault(task_id, TaskNode(task_id=task_id))
    node.state = TaskState.DONE
    node.summary = event.message.rsplit(" (confidence ", 1)[0]
    node.claims = [Claim(**claim) for claim in detail.get("claims", [])]
    node.evidence = list(detail.get("evidence", []))
    node.error = ""
    if " (confidence " in event.message:
        try:
            node.confidence = float(event.message.rsplit(" (confidence ", 1)[1].rstrip(")"))
        except ValueError:
            node.confidence = None


def _note_tool(nodes: dict[str, TaskNode], event) -> None:
    task_id = event.task_id
    if not task_id:
        return
    node = nodes.setdefault(task_id, TaskNode(task_id=task_id))
    name = event.message.split("(")[0]
    if name not in node.tools:
        node.tools.append(name)
    node.duration_ms += event.duration_ms or 0


def _apply_conflict(view: RunView, kind: str, event, detail: dict) -> None:
    subject = detail.get("subject", "")
    metric = detail.get("metric", "")
    existing = next(
        (c for c in view.conflicts if c.subject == subject and c.metric == metric), None
    )
    if existing is None:
        existing = ConflictView(subject=subject or event.message, metric=metric)
        view.conflicts.append(existing)

    if kind == "conflict_detected":
        existing.state = "open"
        existing.values = _parse_values(event.message)
    elif kind == "conflict_resolved":
        existing.state = "resolved"
        existing.resolution = event.message.split(": ", 1)[-1]
        if "re-query of " in event.message:
            existing.requeried = event.message.split("re-query of ", 1)[1].split(":")[0]
    elif kind == "conflict_escalated":
        existing.state = "escalated"
        existing.resolution = event.message.split(": ", 1)[-1]


def _parse_values(message: str) -> dict[str, str]:
    """Pull the contested values out of the detection message."""
    if "contested: " not in message:
        return {}
    try:
        import ast

        return {str(k): str(v) for k, v in ast.literal_eval(
            message.split("contested: ", 1)[1]
        ).items()}
    except (ValueError, SyntaxError):
        return {}


def _mark_superseded(nodes: dict[str, TaskNode], revisions: int) -> None:
    if revisions < 2:
        return
    for node in nodes.values():
        if node.state is TaskState.BLOCKED and node.attempts:
            node.state = TaskState.SUPERSEDED


def _ordered(nodes: dict[str, TaskNode]) -> list[TaskNode]:
    """Depth-first by dependency, so the console can lay the graph out in rows."""
    depth: dict[str, int] = {}

    def level(task_id: str, seen: frozenset[str] = frozenset()) -> int:
        if task_id in depth:
            return depth[task_id]
        node = nodes.get(task_id)
        if node is None or task_id in seen:
            return 0
        parents = [
            level(parent, seen | {task_id})
            for parent in node.depends_on
            if parent in nodes
        ]
        depth[task_id] = max(parents, default=-1) + 1
        return depth[task_id]

    for task_id in nodes:
        level(task_id)
    for task_id, node in nodes.items():
        node.depth = depth.get(task_id, 0)
    return sorted(nodes.values(), key=lambda node: (node.depth, node.task_id))
