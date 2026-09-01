"""Shared fixtures. Every test runs against an isolated workspace."""

from __future__ import annotations

from pathlib import Path

import pytest

from aios.config import Settings
from aios.demo import build_fixtures
from aios.observability import TraceStore
from aios.orchestration.state import AgentResult, Claim, Task, TaskStatus


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    configured = Settings(
        _env_file=None,
        workspace=tmp_path / ".aios",
        data_dir=tmp_path / "data",
        retry_backoff_seconds=0.0,
    )
    configured.ensure_dirs()
    build_fixtures(configured)
    return configured


@pytest.fixture
def trace(settings: Settings) -> TraceStore:
    return TraceStore("run-test", settings.trace_dir)


def make_task(task_id: str, **overrides) -> Task:
    """Build a task with sensible defaults for scheduling tests."""
    fields = {
        "task_id": task_id,
        "description": f"do {task_id}",
        "agent": "data",
        "success_criteria": "it is done",
    }
    fields.update(overrides)
    return Task(**fields)


def make_result(
    *claims: tuple[str, str, str], confidence: float = 0.9
) -> AgentResult:
    """Build an agent result from (subject, metric, value) triples."""
    return AgentResult(
        summary="result",
        claims=[
            Claim(subject=subject, metric=metric, value=value)
            for subject, metric, value in claims
        ],
        confidence=confidence,
        evidence=["fixture"],
    )


def done(task: Task, result: AgentResult) -> Task:
    return task.model_copy(
        update={"status": TaskStatus.DONE, "attempts": 1, "result": result}
    )
