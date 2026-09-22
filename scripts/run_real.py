"""Run one real goal, against real data, on a self-hosted model.

This is the script you hand to someone taking the project past the demo. It
refuses to run until the preflight passes, because the two ways this goes wrong
quietly are leaving the demo's sabotage switches on and pointing at a model that
cannot hold a schema.

    python scripts/run_real.py --check
    python scripts/run_real.py "Prepare the Q3 supplier review" --tenant acme
    python scripts/run_real.py "..." --tenant acme --approve   # sign off inline
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aios.config import Settings  # noqa: E402
from aios.llm import LlmError  # noqa: E402
from aios.llm_local import LocalLlmClient  # noqa: E402
from aios.mcp_gateway.tools import NAMED_QUERIES, build_tools  # noqa: E402
from aios.observability import TraceStore  # noqa: E402
from aios.orchestration.state import Signoff  # noqa: E402
from aios.platform import Platform, TenantPolicy  # noqa: E402
from aios.platform.budget import Budget  # noqa: E402
from aios.platform.models import RunStatus  # noqa: E402
from aios.platform.worker import Worker, wait_for  # noqa: E402
from aios.providers import Provider  # noqa: E402

GREEN, RED, YELLOW, DIM, BOLD, OFF = (
    "\033[32m", "\033[31m", "\033[33m", "\033[90m", "\033[1m", "\033[0m"
)

DEMO_TOOLS = {"peer_benchmark", "ledger_lookup"}
DEFAULT_SECRET = "dev-secret-change-me"


class Preflight:
    """Refuses to let a demo configuration touch real data."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.blockers: list[str] = []
        self.warnings: list[str] = []

    def ok(self, label: str, detail: str = "") -> None:
        print(f"  {GREEN}ok{OFF}    {label}" + (f" {DIM}{detail}{OFF}" if detail else ""))

    def block(self, label: str, fix: str) -> None:
        self.blockers.append(label)
        print(f"  {RED}STOP{OFF}  {label}\n        {DIM}fix: {fix}{OFF}")

    def warn(self, label: str, detail: str = "") -> None:
        self.warnings.append(label)
        print(f"  {YELLOW}warn{OFF}  {label}" + (f" {DIM}{detail}{OFF}" if detail else ""))

    def run(self) -> bool:
        settings = self.settings
        print(f"{BOLD}Preflight{OFF}")

        if settings.injected_ledger_failures:
            self.block(
                f"demo sabotage is on: ledger calls fail "
                f"{settings.injected_ledger_failures}x per run",
                "AIOS_INJECTED_LEDGER_FAILURES=0",
            )
        else:
            self.ok("injected tool failures are off")

        if settings.outage_tools:
            self.block(
                f"tools forced offline: {', '.join(settings.outage_tools)}",
                "unset AIOS_OUTAGE_TOOLS",
            )
        else:
            self.ok("no tools forced offline")

        registered = {tool.name for tool in build_tools(settings, "preflight")}
        leftover = registered & DEMO_TOOLS
        if leftover:
            self.warn(
                f"demo tools still registered: {', '.join(sorted(leftover))}",
                "replace them with real connectors or drop them",
            )
        else:
            self.ok("no demo tools registered")

        if settings.demo_db.exists() and "demo" in str(settings.demo_db):
            self.warn(
                f"sql_query still points at the demo database ({settings.demo_db})",
                "repoint it at your warehouse, read-only",
            )
        self.ok(f"{len(NAMED_QUERIES)} named quer(y/ies) registered", ", ".join(NAMED_QUERIES))

        if not settings.policy_dir.exists() or not any(settings.policy_dir.glob("*.md")):
            self.block(
                f"no documents at {settings.policy_dir}",
                "point doc_search at your corpus",
            )
        else:
            count = len(list(settings.policy_dir.glob("*.md")))
            self.ok(f"{count} document(s) in the corpus", str(settings.policy_dir))

        self._check_model()
        print()
        return not self.blockers

    def _check_model(self) -> None:
        settings = self.settings
        provider = Provider(settings.llm_provider)
        if provider is Provider.CLAUDE:
            import os

            if os.environ.get("ANTHROPIC_API_KEY"):
                self.ok("model: hosted Claude", settings.model)
            else:
                self.block("ANTHROPIC_API_KEY is not set", "export it, or use --local")
            return

        label = f"{settings.llm_model} at {settings.llm_base_url} ({settings.llm_api_style})"
        probe = LocalLlmClient(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            trace=TraceStore("preflight"),
            api_style=settings.llm_api_style,
            api_key=settings.llm_api_key,
            timeout=min(settings.llm_timeout_seconds, 60.0),
            max_repairs=1,
        )
        try:
            served = probe.probe()
        except LlmError as error:
            self.block(f"model not usable: {label}", str(error)[:200])
        else:
            self.ok("model answers with valid JSON for a schema", f"{label} -> {served}")
        finally:
            probe.close()


def build_settings(args) -> Settings:
    """Read the real configuration.

    The demo switches are deliberately *not* overridden here - preflight has to
    see the configuration the operator actually has, or it is checking itself.
    """
    overrides: dict = {}
    if args.local:
        overrides["llm_provider"] = "local"
    if args.base_url:
        overrides["llm_base_url"] = args.base_url
    if args.model:
        overrides["llm_model"] = args.model
    if args.api_style:
        overrides["llm_api_style"] = args.api_style
    settings = Settings(**overrides)
    settings.ensure_dirs()
    return settings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("goal", nargs="?", help="what you want done")
    parser.add_argument("--check", action="store_true", help="preflight only")
    parser.add_argument("--tenant", default="acme")
    parser.add_argument("--reviewer", default="reviewer")
    parser.add_argument("--submitter", default="operator")
    parser.add_argument("--local", action="store_true", help="use the self-hosted model")
    parser.add_argument("--base-url", help="e.g. http://localhost:11434")
    parser.add_argument("--model", help="e.g. qwen2.5:32b-instruct")
    parser.add_argument("--api-style", choices=("ollama", "openai"))
    parser.add_argument("--budget-tokens", type=int, default=400_000)
    parser.add_argument("--approve", action="store_true", help="sign off inline")
    parser.add_argument(
        "--force", action="store_true", help="run even if preflight blocks"
    )
    args = parser.parse_args()

    settings = build_settings(args)
    passed = Preflight(settings).run()
    if not passed and not args.force:
        print(f"{RED}Preflight failed. Fix the items above, or pass --force.{OFF}")
        return 1
    if args.check:
        return 0 if passed else 1
    if not args.goal:
        parser.error("a goal is required unless --check is given")

    platform = Platform(settings)
    if platform.tokens._secret.decode() == DEFAULT_SECRET:  # noqa: SLF001
        print(f"{YELLOW}warn{OFF}  token secret is the built-in default "
              f"{DIM}set AIOS_TOKEN_SECRET before exposing the API{OFF}\n")

    if platform.policy.tenant(args.tenant) is None:
        platform.provision_tenant(
            TenantPolicy(
                tenant_id=args.tenant,
                name=args.tenant,
                reviewers=(args.reviewer,),
            )
        )
        print(f"{DIM}provisioned tenant {args.tenant}, reviewer {args.reviewer}{OFF}")

    operator = platform.tokens.resolve(
        platform.token_for(args.tenant, args.submitter)
    )
    record = platform.submit(
        operator,
        args.goal,
        offline=False,
        budget=Budget(max_input_tokens=args.budget_tokens),
    )
    print(f"{BOLD}run {record.run_id}{OFF}  {DIM}{args.goal}{OFF}\n")

    worker = Worker(platform, "worker-local")
    seen = 0
    import threading

    stop = threading.Event()

    def follow() -> None:
        nonlocal seen
        while not stop.is_set():
            for event in platform.trace.query(
                args.tenant, record.run_id, since_seq=seen
            ):
                seen = max(seen, event.seq)
                took = f" {DIM}{event.duration_ms}ms{OFF}" if event.duration_ms else ""
                print(
                    f"{DIM}{event.seq:>3}{OFF} {event.kind:<19} "
                    f"{DIM}{event.actor:<15}{OFF} {event.message[:110]}{took}"
                )
            stop.wait(0.3)

    watcher = threading.Thread(target=follow, daemon=True)
    watcher.start()
    worker.run_once()
    stop.set()
    watcher.join(timeout=2)

    stored = platform.runs.get(record.run_id)
    print(f"\n{BOLD}status: {stored.status}{OFF}  "
          f"{DIM}{stored.spend.llm_calls} model calls, "
          f"{stored.spend.input_tokens} in / {stored.spend.output_tokens} out{OFF}")

    if stored.status is RunStatus.AWAITING_SIGNOFF:
        review = platform.reviews.for_run(record.run_id)
        print(f"\n{BOLD}Awaiting sign-off{OFF} {DIM}({review.review_id}){OFF}")
        print(f"  degraded tasks: {review.degraded_tasks}   "
              f"unresolved conflicts: {review.open_conflicts}")
        print(f"\n{review.draft[:900]}\n")
        if not args.approve:
            print(f"Approve in the console (`aios serve`), or rerun with --approve.")
            return 0
        reviewer = platform.tokens.resolve(
            platform.token_for(args.tenant, args.reviewer)
        )
        platform.decide(reviewer, review.review_id, approved=True, note="approved via script")
        worker.run_once()
        stored = platform.runs.get(record.run_id)
        print(f"{BOLD}status: {stored.status}{OFF}")

    if stored.report_uri:
        print(f"\n{BOLD}Report{OFF} {DIM}{stored.report_uri}{OFF}\n")
        print(platform.objects.get(stored.report_uri))
    if stored.failure_reason:
        print(f"{RED}{stored.failure_reason}{OFF}")

    return 0 if stored.status is RunStatus.COMPLETED else 1


if __name__ == "__main__":
    raise SystemExit(main())
