"""One entry point, two modes: the built-in demo, or your own files.

    python scripts/start.py demo
    python scripts/start.py mine --folder C:/work/my-files

`demo` needs nothing installed - no API key, no model - because it replays
recorded answers against the real engine. Use it to confirm the platform works.

`mine` points the same engine at a folder of your spreadsheets and documents,
driven by a model you host. Use it to confirm it works on data you care about.

Each step prints what it is doing and why, so this doubles as the runbook.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aios.config import Settings  # noqa: E402
from aios.domain import DomainPack, scaffold_from_folder  # noqa: E402
from aios.files import inventory  # noqa: E402
from aios.llm import LlmError  # noqa: E402
from aios.llm_local import LocalLlmClient  # noqa: E402
from aios.observability import TraceStore  # noqa: E402
from aios.orchestration.state import Signoff  # noqa: E402
from aios.platform import Platform, TenantPolicy  # noqa: E402
from aios.platform.models import RunStatus  # noqa: E402
from aios.platform.worker import Worker  # noqa: E402

GREEN, RED, YELLOW, DIM, BOLD, OFF = (
    "\033[32m", "\033[31m", "\033[33m", "\033[90m", "\033[1m", "\033[0m"
)

DEMO_GOAL = "Prepare the FY26-Q3 quarterly audit review"
step_number = 0


def step(title: str, why: str = "") -> None:
    global step_number
    step_number += 1
    print(f"\n{BOLD}{step_number}. {title}{OFF}" + (f"\n   {DIM}{why}{OFF}" if why else ""))


def ok(message: str) -> None:
    print(f"   {GREEN}ok{OFF}  {message}")


def warn(message: str) -> None:
    print(f"   {YELLOW}!{OFF}   {message}")


def fail(message: str, fix: str = "") -> None:
    print(f"   {RED}x{OFF}   {message}" + (f"\n       {DIM}fix: {fix}{OFF}" if fix else ""))


def follow(platform: Platform, tenant: str, run_id: str, stop) -> None:
    """Print the trace as the worker produces it."""
    seen = 0
    while not stop.is_set():
        for event in platform.trace.query(tenant, run_id, since_seq=seen):
            seen = max(seen, event.seq)
            took = f" {DIM}{event.duration_ms}ms{OFF}" if event.duration_ms else ""
            print(f"   {DIM}{event.seq:>3}{OFF} {event.kind:<19}"
                  f"{DIM}{event.actor:<15}{OFF} {event.message[:100]}{took}")
        stop.wait(0.3)


def execute(platform: Platform, tenant: str, reviewer: str, goal: str,
            offline: bool, approve: bool) -> int:
    """Submit, watch, pause at the gate, optionally approve, show the report."""
    import threading

    operator = platform.tokens.resolve(platform.token_for(tenant, "operator"))
    record = platform.submit(operator, goal, offline=offline)
    print(f"   {DIM}run {record.run_id}{OFF}\n")

    worker = Worker(platform, "worker-1")
    stop = threading.Event()
    watcher = threading.Thread(
        target=follow, args=(platform, tenant, record.run_id, stop), daemon=True
    )
    watcher.start()
    worker.run_once()
    stop.set()
    watcher.join(timeout=2)
    time.sleep(0.4)

    stored = platform.runs.get(record.run_id)
    spend = stored.spend
    print(f"\n   {BOLD}{stored.status}{OFF}  {DIM}{spend.llm_calls} model calls, "
          f"{spend.input_tokens} in / {spend.output_tokens} out{OFF}")

    if stored.status is RunStatus.AWAITING_SIGNOFF:
        review = platform.reviews.for_run(record.run_id)
        step("A human decision is required",
             "the run is parked at a checkpoint and holds no worker")
        print(f"   {review.draft[:600]}\n")
        print(f"   degraded tasks: {review.degraded_tasks}   "
              f"unresolved conflicts: {review.open_conflicts}")
        if not approve:
            print(f"\n   Approve in the console, or re-run with --approve.")
            return 0
        decider = platform.tokens.resolve(platform.token_for(tenant, reviewer))
        platform.decide(decider, review.review_id, approved=True, note="approved by start.py")
        ok(f"{reviewer} approved; the run goes back on the queue")
        worker.run_once()
        stored = platform.runs.get(record.run_id)
        print(f"   {BOLD}{stored.status}{OFF}")

    if stored.report_uri:
        step("The report", "written to object storage, not the worker's disk")
        print(platform.objects.get(stored.report_uri))
    if stored.failure_reason:
        fail(stored.failure_reason)

    facts = platform.facts.facts(tenant)
    if facts:
        step("What it will remember next time",
             "only because a human approved it and it cleared the confidence bar")
        for fact in facts:
            print(f"   {fact.subject}.{fact.metric} = {fact.value}  {DIM}({fact.provenance}){OFF}")

    return 0 if stored.status is RunStatus.COMPLETED else 1


def run_demo(args) -> int:
    print(f"{BOLD}AI-OS — demo mode{OFF}")
    print(f"{DIM}Recorded model answers, real engine. No API key or model needed.{OFF}")

    step("Build the demo dataset", "synthetic vendors, transactions and policies")
    subprocess.run([sys.executable, "-m", "aios.cli", "seed"], check=True)

    settings = Settings(injected_ledger_failures=2)
    settings.ensure_dirs()
    platform = Platform(settings)

    step("Create a tenant", "so the run has an owner, a budget and a reviewer")
    platform.provision_tenant(
        TenantPolicy(tenant_id="demo", name="Demo", max_concurrent_runs=4,
                     reviewers=("cfo",)),
        from_domain=False,
    )
    ok("tenant 'demo', reviewer 'cfo'")

    step("Run the goal", DEMO_GOAL)
    code = execute(platform, "demo", "cfo", DEMO_GOAL, offline=True, approve=True)

    step("See it in the browser")
    print(f"   aios serve        {DIM}then open http://127.0.0.1:8000{OFF}")
    print(f"   python scripts/e2e.py --keep   {DIM}runs 36 checks, leaves the UI up{OFF}")
    return code


def run_mine(args) -> int:
    print(f"{BOLD}AI-OS — your own data{OFF}")
    folder = Path(args.folder).resolve()
    pack_path = Path(args.domain) if args.domain else folder.parent / "domain.toml"

    step("Look at your folder", str(folder))
    if not folder.exists():
        fail(f"no folder at {folder}")
        return 1
    found = inventory(folder)
    ok(f"{len(found['tabular'])} spreadsheet(s): {', '.join(found['tabular'][:4]) or '-'}")
    ok(f"{len(found['documents'])} document(s): {', '.join(found['documents'][:4]) or '-'}")
    if found["ignored"]:
        warn(f"{len(found['ignored'])} file(s) ignored: {', '.join(found['ignored'][:4])}")
    if not found["tabular"] and not found["documents"]:
        fail("nothing readable here",
             "add .csv/.xlsx for data, or .md/.pdf/.docx for documents")
        return 1

    settings = Settings(domain_file=pack_path, injected_ledger_failures=0)
    settings.ensure_dirs()

    step("Describe it as a domain pack", str(pack_path))
    if pack_path.exists():
        ok(f"using the existing pack at {pack_path}")
    else:
        scaffold_from_folder(args.name, pack_path, folder, settings.workspace)
        ok(f"generated {pack_path} with your real tables and columns")
        warn("the starter queries just list rows; edit them to ask real questions")

    pack = DomainPack.load(pack_path)
    problems = pack.check()
    for problem in problems:
        fail(problem, "aios domain check")
    if problems:
        return 1
    ok(f"pack '{pack.name}' is usable, {len(pack.queries)} named quer(y/ies)")

    step("Check the model", f"{args.model} at {args.base_url}")
    settings = settings.model_copy(update={
        "llm_provider": "local", "llm_base_url": args.base_url,
        "llm_model": args.model, "llm_api_style": args.api_style,
    })
    probe = LocalLlmClient(args.base_url, args.model, TraceStore("preflight"),
                           args.api_style, timeout=60.0, max_repairs=1)
    try:
        served = probe.probe()
    except LlmError as error:
        fail(f"the model cannot return valid JSON for a schema: {str(error)[:160]}",
             "ollama serve, then ollama pull qwen2.5:32b-instruct")
        return 1
    finally:
        probe.close()
    ok(f"answering, and holds a schema: {served}")

    platform = Platform(settings)
    step("Create a tenant", "granting exactly the tools your domain publishes")
    platform.provision_tenant(
        TenantPolicy(tenant_id=args.tenant, name=args.tenant,
                     max_concurrent_runs=4, reviewers=(args.reviewer,))
    )
    ok(f"tenant '{args.tenant}', tools: "
       f"{', '.join(g.tool for g in platform.policy.grants(args.tenant))}")

    step("Run your goal", args.goal)
    return execute(platform, args.tenant, args.reviewer, args.goal,
                   offline=False, approve=args.approve)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)

    demo = modes.add_parser("demo", help="run the built-in demo, no setup needed")
    demo.set_defaults(handler=run_demo)

    mine = modes.add_parser("mine", help="run against your own folder of files")
    mine.add_argument("--folder", required=True, help="where your files are")
    mine.add_argument("--goal", default="Review this data and report what needs attention")
    mine.add_argument("--name", default="mydomain", help="a name for this domain")
    mine.add_argument("--domain", help="an existing domain.toml to use")
    mine.add_argument("--model", default="qwen2.5:32b-instruct")
    mine.add_argument("--base-url", default="http://localhost:11434")
    mine.add_argument("--api-style", choices=("ollama", "openai"), default="ollama")
    mine.add_argument("--tenant", default="acme")
    mine.add_argument("--reviewer", default="cfo")
    mine.add_argument("--approve", action="store_true", help="sign off inline")
    mine.set_defaults(handler=run_mine)

    args = parser.parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
