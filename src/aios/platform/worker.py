"""The worker loop.

A worker holds nothing between runs. It leases a run, carries it as far as it
goes, and releases it. Everything needed to continue is in the checkpoint, so a
worker can be killed at any point and another picks the run up from the last
completed wave.

The distinction that matters: a crash is covered by lease expiry, and an
exception is a bug. A crashed worker's run goes back on the queue; a run that
raises is failed with the reason, because retrying deterministic breakage just
burns the pool.
"""

from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass, field

from aios.platform.models import Lane, RunRecord, RunStatus
from aios.platform.service import Platform

ALL_LANES = (Lane.INTERACTIVE, Lane.BATCH, Lane.BACKFILL)


@dataclass
class WorkerStats:
    leased: int = 0
    completed: int = 0
    paused: int = 0
    failed: int = 0
    run_ids: list[str] = field(default_factory=list)


class Worker:
    """Leases runs and advances them until they finish or need a human."""

    def __init__(
        self,
        platform: Platform,
        worker_id: str,
        lanes: tuple[Lane, ...] = ALL_LANES,
        lease_seconds: int = 120,
        poll_seconds: float = 0.25,
    ) -> None:
        self._platform = platform
        self.worker_id = worker_id
        self._lanes = lanes
        self._lease_seconds = lease_seconds
        self._poll_seconds = poll_seconds
        self._orchestrator = platform.orchestrator()
        self.stats = WorkerStats()

    def run_once(self) -> RunRecord | None:
        """Take one run off the queue and carry it forward. None if idle."""
        record = self._platform.queue.lease(
            self.worker_id, self._lanes, self._lease_seconds
        )
        if record is None:
            return None

        self.stats.leased += 1
        self.stats.run_ids.append(record.run_id)
        self._platform.queue.mark_running(record.run_id, self.worker_id)

        try:
            result = self._orchestrator.advance(record)
        except Exception as error:  # a bug, not a crash: fail with the reason
            self._platform.queue.release(
                record.run_id,
                self.worker_id,
                RunStatus.FAILED,
                failure_reason=f"{type(error).__name__}: {error}",
            )
            self.stats.failed += 1
            traceback.print_exc()
            return self._platform.runs.get(record.run_id)

        self._platform.queue.release(
            record.run_id,
            self.worker_id,
            result.status,
            failure_reason=result.failure_reason,
        )
        if result.report_uri:
            self._platform.runs.update_status(
                record.run_id, result.status, report_uri=result.report_uri
            )
        if result.status is RunStatus.COMPLETED:
            self.stats.completed += 1
        elif result.status is RunStatus.AWAITING_SIGNOFF:
            self.stats.paused += 1
        elif result.status in (RunStatus.FAILED, RunStatus.EXPIRED):
            self.stats.failed += 1
        return self._platform.runs.get(record.run_id)

    def drain(self, limit: int = 100) -> WorkerStats:
        """Process runs until the queue is empty. Used by tests and one-shot jobs."""
        for _ in range(limit):
            if self.run_once() is None:
                break
        return self.stats

    def run_forever(self, stop: threading.Event | None = None) -> WorkerStats:
        """Poll until told to stop."""
        stop = stop or threading.Event()
        while not stop.is_set():
            if self.run_once() is None:
                stop.wait(self._poll_seconds)
        return self.stats


class Janitor:
    """Sweeps lapsed leases and expired reviews on a fixed cadence."""

    def __init__(self, platform: Platform, interval_seconds: float = 5.0) -> None:
        self._platform = platform
        self._interval = interval_seconds

    def run_forever(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        while not stop.is_set():
            self._platform.sweep()
            stop.wait(self._interval)


def start_pool(
    platform: Platform,
    size: int = 2,
    lanes: tuple[Lane, ...] = ALL_LANES,
    lease_seconds: int = 120,
    with_janitor: bool = True,
) -> tuple[list[Worker], threading.Event, list[threading.Thread]]:
    """Start a worker pool in background threads and return the stop handle."""
    stop = threading.Event()
    workers = [
        Worker(platform, f"worker-{index + 1}", lanes, lease_seconds)
        for index in range(size)
    ]
    threads = [
        threading.Thread(target=worker.run_forever, args=(stop,), daemon=True)
        for worker in workers
    ]
    if with_janitor:
        threads.append(
            threading.Thread(
                target=Janitor(platform).run_forever, args=(stop,), daemon=True
            )
        )
    for thread in threads:
        thread.start()
    return workers, stop, threads


def wait_for(predicate, timeout: float = 30.0, interval: float = 0.1) -> bool:
    """Poll a condition. Returns False on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False
