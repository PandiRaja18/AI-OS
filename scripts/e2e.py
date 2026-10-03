"""End-to-end check of the hosted platform, over real HTTP.

Boots the API and a worker pool in one process against a throwaway workspace,
then drives the whole story as an ordinary client: submit, watch, pause, review,
resume, read the report. Every step asserts something, and the script exits
non-zero if any of them fails.

    python scripts/e2e.py            # run everything
    python scripts/e2e.py --keep     # leave the server up to poke at the console
"""

from __future__ import annotations

import argparse
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aios.config import Settings  # noqa: E402
from aios.demo import build_fixtures  # noqa: E402
from aios.platform import Platform, TenantPolicy, start_pool  # noqa: E402
from aios.platform.api import create_app  # noqa: E402
from aios.platform.budget import Budget  # noqa: E402

GOAL = "Prepare the FY26-Q3 quarterly audit review"

GREEN, RED, DIM, BOLD, OFF = "\033[32m", "\033[31m", "\033[90m", "\033[1m", "\033[0m"

passed = 0
failed = 0


def check(label: str, condition: bool, detail: str = "") -> bool:
    """Record one assertion."""
    global passed, failed
    if condition:
        passed += 1
        print(f"  {GREEN}PASS{OFF}  {label}" + (f" {DIM}{detail}{OFF}" if detail else ""))
    else:
        failed += 1
        print(f"  {RED}FAIL{OFF}  {label}" + (f" {DIM}{detail}{OFF}" if detail else ""))
    return condition


def step(title: str) -> None:
    print(f"\n{BOLD}{title}{OFF}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_until(predicate, timeout: float = 60.0, interval: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class Client:
    """Thin HTTP client that carries one principal's token."""

    def __init__(self, base: str, token: str) -> None:
        self._http = httpx.Client(
            base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=30.0
        )

    def get(self, path: str, **params):
        return self._http.get(path, params=params)

    def post(self, path: str, body: dict | None = None):
        return self._http.post(path, json=body or {})

    def close(self) -> None:
        self._http.close()


def build_platform(root: Path) -> Platform:
    settings = Settings(
        _env_file=None,
        workspace=root / ".aios",
        data_dir=root / "data",
        retry_backoff_seconds=0.0,
    )
    settings.ensure_dirs()
    build_fixtures(settings)

    platform = Platform(settings, database_url=f"sqlite:///{root / 'platform.db'}")
    platform.provision_tenant(
        TenantPolicy(
            tenant_id="acme", name="Acme Corp", max_concurrent_runs=3, reviewers=("cfo",)
        )
    )
    platform.provision_tenant(
        TenantPolicy(
            tenant_id="globex", name="Globex", max_concurrent_runs=2, reviewers=("dana",)
        )
    )
    return platform


def serve(app, port: int) -> uvicorn.Server:
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    return server


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="leave the server running")
    args = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix="aios-e2e-"))
    port = free_port()
    base = f"http://127.0.0.1:{port}"

    platform = build_platform(root)
    pool, stop, _ = start_pool(platform, size=2, lease_seconds=60)
    server = serve(create_app(platform), port)

    print(f"{BOLD}AI-OS end-to-end check{OFF}")
    print(f"{DIM}workspace {root}{OFF}")
    print(f"{DIM}console   {base}{OFF}")
    print(f"{DIM}workers   {', '.join(w.worker_id for w in pool)}{OFF}")

    ready = wait_until(
        lambda: httpx.get(f"{base}/healthz", timeout=2.0).status_code == 200, 30
    )
    if not ready:
        print(f"{RED}server never came up{OFF}")
        return 1

    alice = Client(base, platform.token_for("acme", "alice"))
    cfo = Client(base, platform.token_for("acme", "cfo"))
    bob = Client(base, platform.token_for("globex", "bob"))

    try:
        run_id = scenario_happy_path(platform, alice, cfo, bob)
        scenario_isolation(alice, bob, run_id)
        scenario_crash_recovery(platform, alice, cfo)
        scenario_limits(platform, alice)
        scenario_review_expiry(platform, alice)
        scenario_metrics(alice)
    finally:
        if not args.keep:
            stop.set()
            server.should_exit = True

    print(f"\n{BOLD}{passed} passed, {failed} failed{OFF}")
    if args.keep:
        print(f"\nConsole still up at {base} - ctrl-c to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            stop.set()
            server.should_exit = True
    return 1 if failed else 0


def scenario_happy_path(platform: Platform, alice: Client, cfo: Client, bob: Client) -> str:
    step("1. Submit a goal and let the pool carry it to the gate")
    response = alice.post("/v1/runs", {"goal": GOAL, "scenario": "audit"})
    check("run accepted", response.status_code == 202, f"HTTP {response.status_code}")
    run_id = response.json()["run_id"]
    print(f"  {DIM}run {run_id}{OFF}")

    reached = wait_until(
        lambda: alice.get(f"/v1/runs/{run_id}").json()["status"] == "awaiting_signoff"
    )
    check("run reached the sign-off gate", reached)

    detail = alice.get(f"/v1/runs/{run_id}").json()
    check("worker released the run while it waits", detail["worker_id"] is None)
    check("spend was metered", detail["spend"]["llm_calls"] >= 13,
          f'{detail["spend"]["llm_calls"]} model calls')
    check("every task execution recorded", len(detail["attempts"]) >= 11,
          f'{len(detail["attempts"])} attempts')

    trace = alice.get(f"/v1/runs/{run_id}/trace").json()
    kinds = {event["kind"] for event in trace}
    check("trace shows parallel dispatch", any(
        e["kind"] == "run_status" and e["message"].count(",") == 2 for e in trace))
    check("trace shows a retried tool", "task_retry" in kinds)
    check("trace shows a degraded task", "task_degraded" in kinds)
    check("trace shows a contradiction found and settled",
          {"conflict_detected", "conflict_resolved"} <= kinds)

    step("2. The review inbox")
    reviews = cfo.get("/v1/reviews").json()
    check("review is waiting for the assigned reviewer", len(reviews) == 1)
    review = reviews[0]
    check("draft carries the headline figure", "86,900 USD" in review["draft"])
    check("gaps are surfaced, not hidden", review["degraded_tasks"] == 1)
    check("an unassigned tenant sees nothing", bob.get("/v1/reviews").json() == [])

    step("3. Approve, and let the run finish")
    decided = cfo.post(f"/v1/reviews/{review['review_id']}/decide",
                       {"approved": True, "note": "signed off"})
    check("decision accepted", decided.status_code == 200)

    done = wait_until(
        lambda: alice.get(f"/v1/runs/{run_id}").json()["status"] == "completed"
    )
    check("run completed after approval", done)

    final = alice.get(f"/v1/runs/{run_id}").json()
    check("report stored outside the worker", str(final["report_uri"]).startswith("aios://"))
    report = alice.get("/v1/reports", uri=final["report_uri"])
    check("report is readable", report.status_code == 200
          and report.json()["markdown"].startswith("# FY26-Q3"))

    facts = alice.get("/v1/memory").json()
    check("approved findings promoted to durable memory", len(facts) >= 4,
          f"{len(facts)} facts")
    return run_id


def scenario_isolation(alice: Client, bob: Client, run_id: str) -> None:
    step("4. Tenant isolation")
    check("another tenant cannot read the run", bob.get(f"/v1/runs/{run_id}").status_code == 404)
    check("another tenant cannot read its trace",
          bob.get(f"/v1/runs/{run_id}/trace").status_code == 404)
    uri = alice.get(f"/v1/runs/{run_id}").json()["report_uri"]
    check("another tenant cannot read its report",
          bob.get("/v1/reports", uri=uri).status_code == 403)
    check("another tenant's memory is empty", bob.get("/v1/memory").json() == [])
    check("an unsigned request is refused",
          httpx.get(f"{alice._http.base_url}/v1/runs").status_code == 401)


def scenario_crash_recovery(platform: Platform, alice: Client, cfo: Client) -> None:
    step("5. A worker dies mid-run")
    response = alice.post("/v1/runs", {"goal": GOAL})
    run_id = response.json()["run_id"]
    wait_until(lambda: alice.get(f"/v1/runs/{run_id}").json()["status"] == "awaiting_signoff")

    review = [r for r in cfo.get("/v1/reviews").json() if r["run_id"] == run_id][0]
    before = len(alice.get(f"/v1/runs/{run_id}").json()["attempts"])
    cfo.post(f"/v1/reviews/{review['review_id']}/decide", {"approved": True})

    # Kill whichever worker picks it up, by letting its lease lapse.
    platform.database.execute(
        "UPDATE runs SET lease_expires_at = ? WHERE run_id = ?",
        ("2000-01-01T00:00:00.000000+00:00", run_id),
    )
    recovered = wait_until(
        lambda: alice.get(f"/v1/runs/{run_id}").json()["status"] == "completed", 60
    )
    check("another worker finished the run", recovered)
    after = alice.get(f"/v1/runs/{run_id}").json()
    check("no finished task was re-run", len(after["attempts"]) == before,
          f"{before} attempts before, {len(after['attempts'])} after")


def scenario_limits(platform: Platform, alice: Client) -> None:
    step("6. Limits bind")
    platform.policy.upsert_tenant(
        platform.policy.tenant("acme").model_copy(update={"max_concurrent_runs": 1})
    )
    first = alice.post("/v1/runs", {"goal": GOAL})
    second = alice.post("/v1/runs", {"goal": GOAL})
    check("concurrency cap rejects at the edge", second.status_code == 429,
          f"HTTP {second.status_code}")
    check("rejection tells the caller when to retry", "Retry-After" in second.headers)
    platform.policy.upsert_tenant(
        platform.policy.tenant("acme").model_copy(update={"max_concurrent_runs": 5})
    )

    principal = platform.tokens.resolve(platform.token_for("acme", "alice"))
    record = platform.submit(principal, GOAL, budget=Budget(max_input_tokens=200))
    halted = wait_until(
        lambda: platform.runs.get(record.run_id).status.value == "failed", 60
    )
    stored = platform.runs.get(record.run_id)
    check("a run that crosses its budget halts", halted, stored.failure_reason or "")
    check("it halted mid-run, not before starting", stored.spend.llm_calls > 0,
          f"{stored.spend.llm_calls} calls before the ceiling")
    if first.status_code == 202:
        platform.cancel(principal, first.json()["run_id"])


def scenario_review_expiry(platform: Platform, alice: Client) -> None:
    step("7. A review nobody answers")
    principal = platform.tokens.resolve(platform.token_for("acme", "alice"))
    record = platform.submit(principal, GOAL)
    reached = wait_until(
        lambda: platform.runs.get(record.run_id).status.value == "awaiting_signoff", 60
    )
    if not check("run parked at the gate", reached):
        return

    review = platform.reviews.for_run(record.run_id)
    platform.database.execute(
        "UPDATE reviews SET expires_at = ? WHERE review_id = ?",
        ("2000-01-01T00:00:00.000000+00:00", review.review_id),
    )
    expired = wait_until(
        lambda: platform.runs.get(record.run_id).status.value == "expired", 30
    )
    check("the TTL closed it out", expired,
          platform.runs.get(record.run_id).failure_reason or "")


def scenario_metrics(alice: Client) -> None:
    step("8. What the platform can tell you afterwards")
    metrics = alice.get("/v1/metrics").json()
    quality = metrics["decision_quality"]
    check("conflicts are counted", quality["conflicts_detected"] >= 2)
    check("every conflict was resolved, none escalated",
          quality["conflicts_resolved"] == quality["conflicts_detected"]
          and quality["conflicts_escalated"] == 0)
    check("degradation is visible", quality["tasks_degraded"] >= 2)
    check("promotions are counted", quality["promotions"] >= 1)
    check("cost per completed run is known", float(metrics["spend"]["per_completed_run"]) >= 0)
    print(f"  {DIM}runs by status: {metrics['runs_by_status']}{OFF}")
    print(f"  {DIM}model calls: {metrics['spend']['llm_calls']}, "
          f"tokens in/out: {metrics['spend']['input_tokens']}/"
          f"{metrics['spend']['output_tokens']}{OFF}")


if __name__ == "__main__":
    raise SystemExit(main())
