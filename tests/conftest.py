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


def _platform(settings: Settings, tmp_path: Path, name: str = "platform"):
    """A fully wired platform with two provisioned tenants."""
    from aios.platform import Platform, TenantPolicy

    platform = Platform(settings, database_url=f"sqlite:///{tmp_path / name}.db")
    platform.provision_tenant(
        TenantPolicy(
            tenant_id="acme",
            name="Acme",
            max_concurrent_runs=2,
            reviewers=("cfo",),
        )
    )
    platform.provision_tenant(
        TenantPolicy(
            tenant_id="globex",
            name="Globex",
            max_concurrent_runs=2,
            reviewers=("dana",),
        )
    )
    return platform


@pytest.fixture
def platform(settings: Settings, tmp_path: Path):
    instance = _platform(settings, tmp_path)
    yield instance
    instance.close()


@pytest.fixture
def outage_platform(settings: Settings, tmp_path: Path):
    """A platform whose ledger tool is down for good."""
    from aios.platform import TenantPolicy

    configured = settings.model_copy(update={"outage_tools": ("ledger_lookup",)})
    instance = _platform(configured, tmp_path, "outage")
    yield instance
    instance.close()


@pytest.fixture
def alice(platform):
    return platform.tokens.resolve(platform.token_for("acme", "alice"))


@pytest.fixture
def cfo(platform):
    return platform.tokens.resolve(platform.token_for("acme", "cfo"))


@pytest.fixture
def bob(platform):
    return platform.tokens.resolve(platform.token_for("globex", "bob"))


@pytest.fixture
def dana(platform):
    return platform.tokens.resolve(platform.token_for("globex", "dana"))


@pytest.fixture
def outage_caller(outage_platform):
    return outage_platform.tokens.resolve(outage_platform.token_for("acme", "alice"))


@pytest.fixture
def outage_reviewer(outage_platform):
    return outage_platform.tokens.resolve(outage_platform.token_for("acme", "cfo"))


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
