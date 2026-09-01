"""Console for the platform: run goals, inspect traces, review sign-offs."""

from __future__ import annotations

import sys
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from aios.config import Settings
from aios.demo import build_fixtures
from aios.memory import SemanticMemory
from aios.observability import EventKind, TraceEvent, read_trace
from aios.orchestration.state import Claim, RunStatus, Signoff, Task, TaskStatus
from aios.runtime import RunOutcome, Runtime

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Enterprise AI-OS - multi-agent orchestration console.",
)
console = Console()

EVENT_STYLE: dict[EventKind, str] = {
    EventKind.RUN_STARTED: "bold cyan",
    EventKind.PLAN: "bold magenta",
    EventKind.PLAN_REJECTED: "yellow",
    EventKind.REPLAN: "bold magenta",
    EventKind.DISPATCH: "cyan",
    EventKind.TOOL_CALL: "blue",
    EventKind.TOOL_ERROR: "yellow",
    EventKind.ACCESS_DENIED: "bold red",
    EventKind.AGENT_RESULT: "green",
    EventKind.TASK_RETRY: "yellow",
    EventKind.TASK_DEGRADED: "bold yellow",
    EventKind.TASK_BLOCKED: "bold red",
    EventKind.CONFLICT_DETECTED: "bold red",
    EventKind.CONFLICT_RESOLVED: "bold green",
    EventKind.CONFLICT_ESCALATED: "bold red",
    EventKind.SIGNOFF_REQUESTED: "bold yellow",
    EventKind.SIGNOFF_RECORDED: "bold green",
    EventKind.MEMORY_PROMOTE: "bold blue",
    EventKind.REPORT: "bold green",
}

STATUS_STYLE: dict[TaskStatus, str] = {
    TaskStatus.PENDING: "dim",
    TaskStatus.RUNNING: "cyan",
    TaskStatus.DONE: "green",
    TaskStatus.DEGRADED: "yellow",
    TaskStatus.BLOCKED: "red",
    TaskStatus.AWAITING_SIGNOFF: "bold yellow",
    TaskStatus.SUPERSEDED: "dim strike",
}


def _print_event(event: TraceEvent) -> None:
    style = EVENT_STYLE.get(event.kind, "white")
    took = f" [dim]{event.duration_ms}ms[/dim]" if event.duration_ms else ""
    console.print(
        f"[dim]{event.seq:>3}[/dim] [{style}]{event.kind.value:<19}[/{style}] "
        f"[dim]{event.actor:<12}[/dim] {event.message}{took}",
        highlight=False,
    )


def _short(task_id: str) -> str:
    """Leading id of a task, e.g. t2_exception_scan -> t2."""
    return task_id.split("_", 1)[0]


def _task_table(tasks: dict[str, Task]) -> Table:
    table = Table(title="Task graph", header_style="bold", expand=True)
    table.add_column("task", no_wrap=True)
    table.add_column("agent", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("try", justify="right", no_wrap=True)
    table.add_column("conf", justify="right", no_wrap=True)
    table.add_column("after", no_wrap=True)
    for task in tasks.values():
        style = STATUS_STYLE.get(task.status, "white")
        table.add_row(
            task.task_id,
            task.agent.value,
            f"[{style}]{task.status.value}[/{style}]",
            str(task.attempts),
            f"{task.result.confidence:.2f}" if task.result else "-",
            ", ".join(_short(dep) for dep in task.depends_on) or "-",
        )
    return table


def _elide(text: str, width: int = 160) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _report_outcome(outcome: RunOutcome) -> None:
    state = outcome.state
    tasks: dict[str, Task] = state.get("tasks", {})
    console.print()
    console.print(_task_table(tasks))

    conflicts = state.get("conflicts", {})
    if conflicts:
        table = Table(title="Conflicts", header_style="bold", expand=True)
        table.add_column("subject.metric", no_wrap=True)
        table.add_column("status", no_wrap=True)
        table.add_column("resolution")
        for conflict in conflicts.values():
            table.add_row(
                f"{conflict.subject}.{conflict.metric}",
                conflict.status.value,
                _elide(conflict.resolution or "-"),
            )
        console.print(table)

    console.print(
        f"\nrun [bold]{outcome.run_id}[/bold] -> [bold]{outcome.status.value}[/bold]"
    )
    if state.get("report_path"):
        console.print(f"report: {state['report_path']}")
    console.print(f"trace:  {outcome.trace_path}")


def _signoff_panel(pending: dict[str, Any]) -> None:
    lines = [f"[bold]{line}[/bold]" for line in pending.get("draft", [])]
    for conflict in pending.get("open_conflicts", []):
        lines.append(
            f"[red]{conflict['status']} conflict[/red] "
            f"{conflict['subject']}.{conflict['metric']}: "
            f"{conflict.get('resolution') or conflict['values']}"
        )
    for degraded in pending.get("degraded", []):
        lines.append(
            f"[yellow]degraded[/yellow] {degraded['task_id']}: {degraded['error']}"
        )
    if pending.get("report_path"):
        lines.append(f"\nreport draft: {pending['report_path']}")
    console.print(
        Panel(
            "\n".join(lines) or "no details",
            title="Human sign-off required",
            border_style="yellow",
        )
    )


def _settings(outage: list[str] | None = None) -> Settings:
    settings = Settings()
    if outage:
        settings = settings.model_copy(update={"outage_tools": tuple(outage)})
    return settings


@app.command()
def seed() -> None:
    """Build the demo dataset and seed durable memory with prior findings."""
    settings = _settings()
    settings.ensure_dirs()
    db_path = build_fixtures(settings)
    memory = SemanticMemory(settings.semantic_db)
    seeded = [
        Claim(
            subject="Northwind Logistics",
            metric="prior_audit_flag",
            value="late invoice approvals raised in the FY25-Q4 audit",
        ),
        Claim(
            subject="quarterly audit review",
            metric="standing_scope_note",
            value="high-tier vendors require a documented quarterly review",
        ),
    ]
    for claim in seeded:
        memory.seed(claim, provenance="seed-fy25q4", confidence=0.9)
    console.print(f"dataset:        {db_path}")
    console.print(f"policy corpus:  {settings.policy_dir}")
    console.print(f"durable memory: {len(seeded)} fact(s) seeded")


@app.command()
def run(
    goal: Annotated[str, typer.Argument(help="The goal to execute.")],
    offline: Annotated[
        bool, typer.Option("--offline", help="Use recorded model responses.")
    ] = False,
    scenario: Annotated[
        str, typer.Option(help="Recorded scenario to replay when offline.")
    ] = "audit",
    reviewer: Annotated[str, typer.Option(help="Reviewer identity for sign-off.")] = "reviewer",
    approve: Annotated[
        bool | None,
        typer.Option(
            "--approve/--reject",
            help="Answer the sign-off gate without prompting.",
        ),
    ] = None,
    outage: Annotated[
        list[str] | None, typer.Option(help="Tool name to force offline.")
    ] = None,
) -> None:
    """Plan and execute a goal, pausing at the human sign-off gate."""
    settings = _settings(outage)
    runtime = Runtime(
        settings, offline=offline, scenario=scenario, listener=_print_event
    )
    outcome = runtime.start(goal)

    if outcome.paused:
        console.print()
        _signoff_panel(outcome.pending_signoff or {})
        decision = approve
        if decision is None:
            if not sys.stdin.isatty():
                console.print(
                    f"\nrun [bold]{outcome.run_id}[/bold] is awaiting sign-off. "
                    f"Resume with:\n  aios resume {outcome.run_id} --approve"
                    + (" --offline" if offline else "")
                )
                return
            decision = typer.confirm("Approve this run?", default=True)
        console.print()
        outcome = runtime.resume(
            outcome.run_id,
            Signoff(approved=decision, reviewer=reviewer, note=None),
        )

    _report_outcome(outcome)
    if outcome.status is not RunStatus.COMPLETED:
        raise typer.Exit(code=1)


@app.command()
def resume(
    run_id: Annotated[str, typer.Argument(help="Run awaiting sign-off.")],
    approve: Annotated[
        bool, typer.Option("--approve/--reject", help="The reviewer decision.")
    ] = True,
    reviewer: Annotated[str, typer.Option(help="Reviewer identity.")] = "reviewer",
    note: Annotated[str | None, typer.Option(help="Reviewer note.")] = None,
    offline: Annotated[
        bool, typer.Option("--offline", help="Use recorded model responses.")
    ] = False,
    scenario: Annotated[str, typer.Option(help="Recorded scenario.")] = "audit",
) -> None:
    """Deliver a sign-off decision to a paused run."""
    runtime = Runtime(
        _settings(), offline=offline, scenario=scenario, listener=_print_event
    )
    outcome = runtime.resume(
        run_id, Signoff(approved=approve, reviewer=reviewer, note=note)
    )
    _report_outcome(outcome)


@app.command()
def runs(
    limit: Annotated[int, typer.Option(help="How many runs to list.")] = 20,
) -> None:
    """List recent runs."""
    runtime = Runtime(_settings())
    table = Table(title="Runs", header_style="bold", expand=True)
    table.add_column("run_id", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("started", no_wrap=True)
    table.add_column("goal")
    for record in runtime.index.list(limit):
        table.add_row(record.run_id, record.status, record.created_at, record.goal)
    console.print(table)


@app.command()
def trace(
    run_id: Annotated[str, typer.Argument(help="Run to replay the trace of.")],
) -> None:
    """Replay a persisted run trace."""
    settings = _settings()
    events = list(read_trace(settings.trace_dir, run_id))
    if not events:
        console.print(f"no trace found for {run_id}")
        raise typer.Exit(code=1)
    for event in events:
        _print_event(event)


@app.command()
def memory(
    history: Annotated[
        bool, typer.Option("--history", help="Include superseded facts.")
    ] = False,
) -> None:
    """Show durable memory."""
    settings = _settings()
    store = SemanticMemory(settings.semantic_db)
    table = Table(title="Durable memory", header_style="bold", expand=True)
    table.add_column("subject", no_wrap=True)
    table.add_column("metric", no_wrap=True)
    table.add_column("value")
    table.add_column("conf", justify="right", no_wrap=True)
    table.add_column("from run", no_wrap=True)
    table.add_column("state", no_wrap=True)
    for fact in store.facts(include_superseded=history):
        table.add_row(
            fact.subject,
            fact.metric,
            fact.value,
            f"{fact.confidence:.2f}",
            fact.provenance,
            "superseded" if fact.superseded_by else "active",
        )
    console.print(table)


if __name__ == "__main__":
    app()
