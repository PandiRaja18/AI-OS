"""Console for the platform: run goals, inspect traces, review sign-offs."""

from __future__ import annotations

import sys
from pathlib import Path
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
    return text if len(text) <= width else text[: width - 1] + "..."


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


# --- platform: the hosted plane ----------------------------------------------


@app.command()
def provision(
    tenant: Annotated[str, typer.Argument(help="Tenant id to create.")],
    name: Annotated[str, typer.Option(help="Display name.")] = "",
    reviewer: Annotated[
        list[str] | None, typer.Option(help="Reviewer who may sign off.")
    ] = None,
    max_runs: Annotated[int, typer.Option(help="Concurrent run cap.")] = 4,
    ttl_hours: Annotated[int, typer.Option(help="Hours before a review expires.")] = 24,
) -> None:
    """Create a tenant, its budget and its tool grants."""
    from aios.platform import Platform, TenantPolicy

    platform = Platform(_settings())
    policy = platform.provision_tenant(
        TenantPolicy(
            tenant_id=tenant,
            name=name or tenant,
            max_concurrent_runs=max_runs,
            review_ttl_hours=ttl_hours,
            reviewers=tuple(reviewer or ()),
        )
    )
    grants = platform.policy.grants(tenant)
    console.print(f"tenant:    {policy.tenant_id} ({policy.name})")
    console.print(f"reviewers: {', '.join(policy.reviewers) or 'anyone'}")
    console.print(f"grants:    {', '.join(grant.tool for grant in grants)}")
    console.print(f"budget:    ${policy.default_budget.max_currency} per run")


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port.")] = 8000,
    workers: Annotated[int, typer.Option(help="In-process worker count.")] = 2,
    lease_seconds: Annotated[int, typer.Option(help="Lease length.")] = 120,
) -> None:
    """Run the API, the console and a worker pool in one process."""
    import uvicorn

    from aios.platform import Platform, start_pool
    from aios.platform.api import create_app

    platform = Platform(_settings())
    pool, _, _ = start_pool(platform, size=workers, lease_seconds=lease_seconds)
    console.print(
        f"[bold]AI-OS[/bold] console at http://{host}:{port}  "
        f"({len(pool)} worker(s): {', '.join(w.worker_id for w in pool)})"
    )
    uvicorn.run(create_app(platform), host=host, port=port, log_level="warning")


@app.command()
def worker(
    worker_id: Annotated[str, typer.Option("--id", help="Worker identity.")] = "worker-1",
    lease_seconds: Annotated[int, typer.Option(help="Lease length.")] = 120,
    once: Annotated[bool, typer.Option("--once", help="Drain the queue and exit.")] = False,
) -> None:
    """Run a standalone worker against the shared queue."""
    from aios.platform import Platform, Worker

    platform = Platform(_settings())
    runner = Worker(platform, worker_id, lease_seconds=lease_seconds)
    if once:
        stats = runner.drain()
        console.print(
            f"{worker_id}: leased {stats.leased}, completed {stats.completed}, "
            f"paused {stats.paused}, failed {stats.failed}"
        )
        return
    console.print(f"{worker_id} polling for work; ctrl-c to stop")
    try:
        runner.run_forever()
    except KeyboardInterrupt:
        console.print(f"\n{worker_id} stopped after {runner.stats.leased} run(s)")


@app.command()
def use(
    folder: Annotated[
        Path | None, typer.Argument(help="Folder holding your files.")
    ] = None,
    demo: Annotated[
        bool, typer.Option("--demo", help="Go back to the built-in demo data.")
    ] = False,
    name: Annotated[str | None, typer.Option(help="Name for this domain.")] = None,
    tenant: Annotated[str, typer.Option(help="Tenant to grant the tools to.")] = "me",
    reviewer: Annotated[str, typer.Option(help="Who signs runs off.")] = "me",
    force: Annotated[
        bool, typer.Option("--force", help="Regenerate an existing pack.")
    ] = False,
) -> None:
    """Point everything at a folder of your files. One command, then `aios serve`.

    Inspects the folder, writes a domain pack for it, records it as the active
    domain, and grants your tenant the tools that domain publishes.
    """
    from aios.config import write_active_domain
    from aios.domain import DomainPack, scaffold_from_folder
    from aios.files import inventory
    from aios.platform import Platform, TenantPolicy

    settings = Settings()
    settings.ensure_dirs()

    if demo:
        write_active_domain(settings.workspace, None)
        platform = Platform(Settings())
        platform.provision_tenant(
            TenantPolicy(tenant_id=tenant, name=tenant, reviewers=(reviewer,)),
            from_domain=False,
        )
        console.print("[bold]using the built-in demo data[/bold]")
        console.print("  run [bold]aios seed[/bold] if you have not already, "
                      "then [bold]aios serve[/bold]")
        return

    if folder is None:
        current = settings.domain_file
        if current is None:
            console.print("using the built-in [bold]demo[/bold] data")
        else:
            console.print(f"using [bold]{DomainPack.load(current).name}[/bold] "
                          f"[dim]{current}[/dim]")
        console.print("\npass a folder to switch: [bold]aios use C:/work/my-files[/bold]")
        return

    folder = folder.resolve()
    if not folder.exists():
        console.print(f"[red]no folder at {folder}[/red]")
        raise typer.Exit(code=1)

    found = inventory(folder)
    if not found["tabular"] and not found["documents"]:
        console.print(f"[red]nothing readable in {folder}[/red]")
        console.print("  data:      .csv .tsv .xlsx")
        console.print("  documents: .md .txt .pdf .docx")
        raise typer.Exit(code=1)

    domain_name = name or folder.name.replace(" ", "-").lower() or "mydomain"
    pack_path = settings.workspace / "domains" / f"{domain_name}.toml"
    pack_path.parent.mkdir(parents=True, exist_ok=True)

    if pack_path.exists() and not force:
        console.print(f"[dim]keeping your edits in {pack_path}[/dim]")
    else:
        pack_path.unlink(missing_ok=True)
        scaffold_from_folder(domain_name, pack_path, folder, settings.workspace)

    pack = DomainPack.load(pack_path)
    problems = pack.check()
    for problem in problems:
        console.print(f"[red]{problem}[/red]")
    if problems:
        raise typer.Exit(code=1)

    write_active_domain(settings.workspace, pack_path)
    platform = Platform(Settings())
    platform.provision_tenant(
        TenantPolicy(tenant_id=tenant, name=tenant, reviewers=(reviewer,))
    )
    granted = [grant.tool for grant in platform.policy.grants(tenant)]

    console.print(f"[bold]{domain_name}[/bold] is now active")
    console.print(f"  files:  {len(found['tabular'])} spreadsheet(s), "
                  f"{len(found['documents'])} document(s)"
                  + (f", {len(found['ignored'])} ignored" if found["ignored"] else ""))
    console.print(f"  tools:  {', '.join(granted)}")
    console.print(f"  tenant: {tenant}, reviewer {reviewer}")
    console.print(f"  pack:   {pack_path} [dim](edit to tune queries and prompts)[/dim]")
    console.print("\nNext: [bold]aios serve[/bold], then sign in as "
                  f"[bold]{tenant}[/bold]")


# --- domain packs: pointing the engine at your own data ----------------------

domain_app = typer.Typer(
    no_args_is_help=True,
    help="Describe your own data so runs use it instead of the demo fixtures.",
)
app.add_typer(domain_app, name="domain")

DEFAULT_PACK = Path("domain.toml")


def _pack_path(file: Path | None) -> Path:
    settings = _settings()
    return file or settings.domain_file or DEFAULT_PACK


@domain_app.command("init")
def domain_init(
    name: Annotated[str, typer.Argument(help="A short name for this domain.")],
    file: Annotated[Path, typer.Option(help="Where to write the pack.")] = DEFAULT_PACK,
    from_folder: Annotated[
        Path | None,
        typer.Option("--from-folder", help="Inspect this folder and fill the pack in."),
    ] = None,
) -> None:
    """Write a domain pack, optionally generated from a folder of your files."""
    from aios.domain import scaffold, scaffold_from_folder

    if file.exists():
        console.print(f"[red]{file} already exists[/red]; delete it or choose --file")
        raise typer.Exit(code=1)

    if from_folder is None:
        scaffold(name, file)
        console.print(f"wrote {file}")
        console.print("\nNext: point [bold]data_source[/bold] and "
                      "[bold]documents[/bold] at your files, then run:")
        console.print(f"  aios domain check --file {file}")
        return

    if not from_folder.exists():
        console.print(f"[red]no folder at {from_folder}[/red]")
        raise typer.Exit(code=1)
    settings = _settings()
    settings.ensure_dirs()
    scaffold_from_folder(name, file, from_folder, settings.workspace)
    console.print(f"wrote {file} from {from_folder}")
    console.print("\nIt already knows your tables and columns. Review the "
                  "queries, then run:")
    console.print(f"  aios domain check --file {file}")


@domain_app.command("check")
def domain_check(
    file: Annotated[Path | None, typer.Option(help="Pack to validate.")] = None,
) -> None:
    """Validate a domain pack and show what the agents would see."""
    from aios.domain import DomainPack
    from aios.files import inventory

    path = _pack_path(file)
    try:
        pack = DomainPack.load(path)
    except Exception as error:
        console.print(f"[red]{path}: {error}[/red]")
        raise typer.Exit(code=1) from error

    console.print(f"[bold]{pack.name}[/bold] - {pack.description or 'no description'}")
    problems = pack.check()

    if pack.data_source is not None:
        source = pack.data_source
        detail = source.path if source.kind == "files" else source.dsn
        console.print(f"data:      {source.kind} [dim]{detail}[/dim]")
        if source.kind == "files" and Path(source.path).exists():
            found = inventory(Path(source.path))
            console.print(
                f"           {len(found['tabular'])} spreadsheet(s), "
                f"{len(found['documents'])} document(s), "
                f"{len(found['ignored'])} ignored"
            )
        if source.allow_adhoc_queries:
            console.print("           [yellow]ad-hoc SQL enabled[/yellow] "
                          "[dim](ingested copy only)[/dim]")
    if pack.documents is not None:
        console.print(f"documents: {pack.documents.path} [dim]{pack.documents.glob}[/dim]")

    table = Table(title="Named queries", header_style="bold", expand=True)
    table.add_column("name", no_wrap=True)
    table.add_column("params", no_wrap=True)
    table.add_column("description")
    for name, query in sorted(pack.queries.items()):
        table.add_row(name, ", ".join(query.params) or "-", query.description)
    if pack.queries:
        console.print(table)

    if problems:
        console.print("\n[red]Problems[/red]")
        for problem in problems:
            console.print(f"  - {problem}")
        raise typer.Exit(code=1)
    console.print("\n[green]pack is usable[/green]")


@domain_app.command("add")
def domain_add(
    paths: Annotated[list[Path], typer.Argument(help="Files or folders to copy in.")],
    file: Annotated[Path | None, typer.Option(help="Pack to add them to.")] = None,
    into: Annotated[
        str, typer.Option(help="Which source: data or documents.")
    ] = "data",
) -> None:
    """Copy files into a pack's folder so the agents can reach them."""
    import shutil

    from aios.domain import DomainPack
    from aios.files import READABLE

    pack = DomainPack.load(_pack_path(file))
    source = pack.data_source if into == "data" else pack.documents
    if source is None or not getattr(source, "path", ""):
        console.print(f"[red]the pack has no folder-based '{into}' source[/red]")
        raise typer.Exit(code=1)

    target = Path(source.path)
    target.mkdir(parents=True, exist_ok=True)
    copied, skipped = 0, 0
    for origin in paths:
        candidates = (
            [p for p in origin.rglob("*") if p.is_file()] if origin.is_dir() else [origin]
        )
        for candidate in candidates:
            if candidate.suffix.lower() not in READABLE:
                skipped += 1
                continue
            shutil.copy2(candidate, target / candidate.name)
            copied += 1
    console.print(f"copied {copied} file(s) into {target}"
                  + (f", skipped {skipped} unreadable" if skipped else ""))


if __name__ == "__main__":
    app()
