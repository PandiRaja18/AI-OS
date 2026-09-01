"""Plan validation and the reject-and-reprompt loop."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from aios.observability import EventKind, TraceStore
from aios.orchestration.planner import (
    InvalidPlan,
    Plan,
    PlannedTask,
    Planner,
    validate_plan,
)

VALID = [
    {
        "task_id": "t1",
        "description": "gather",
        "agent": "data",
        "depends_on": [],
        "success_criteria": "rows returned",
    },
    {
        "task_id": "t2",
        "description": "report",
        "agent": "reporting",
        "depends_on": ["t1"],
        "success_criteria": "report rendered",
        "requires_signoff": True,
    },
]


def plan_from(tasks: list[dict]) -> list[PlannedTask]:
    return [PlannedTask(**task) for task in tasks]


def test_accepts_a_well_formed_dag():
    validate_plan(plan_from(VALID))


def test_rejects_duplicate_task_ids():
    tasks = VALID + [dict(VALID[0])]
    with pytest.raises(InvalidPlan, match="duplicate"):
        validate_plan(plan_from(tasks))


def test_rejects_unknown_dependency():
    tasks = [dict(VALID[0], depends_on=["nope"]), VALID[1]]
    with pytest.raises(InvalidPlan, match="unknown dependencies"):
        validate_plan(plan_from(tasks))


def test_rejects_a_cycle():
    tasks = [dict(VALID[0], depends_on=["t2"]), VALID[1]]
    with pytest.raises(InvalidPlan, match="cycle"):
        validate_plan(plan_from(tasks))


def test_rejects_a_plan_without_a_reporting_task():
    with pytest.raises(InvalidPlan, match="no reporting task"):
        validate_plan(plan_from([VALID[0]]))


def test_rejects_a_planner_agent_assignment():
    tasks = [dict(VALID[0], agent="planner"), VALID[1]]
    with pytest.raises(InvalidPlan, match="not a worker agent"):
        validate_plan(plan_from(tasks))


class ScriptedLlm:
    """Returns queued responses, recording the prompts it was given."""

    name = "scripted"

    def __init__(self, *responses: dict) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    def structured(self, *, key, system, prompt, output_model, **_) -> BaseModel:
        self.prompts.append(prompt)
        return output_model.model_validate(self._responses.pop(0))


def test_planner_reprompts_after_an_invalid_plan(trace: TraceStore):
    cyclic = {
        "rationale": "broken",
        "tasks": [dict(VALID[0], depends_on=["t2"]), VALID[1]],
    }
    good = {"rationale": "fixed", "tasks": VALID}
    llm = ScriptedLlm(cyclic, good)

    tasks = Planner(llm, trace, max_attempts=3).plan("goal", [])

    assert sorted(tasks) == ["t1", "t2"]
    assert "was rejected" in llm.prompts[1]
    kinds = [event.kind for event in trace.events]
    assert EventKind.PLAN_REJECTED in kinds
    assert kinds[-1] is EventKind.PLAN


def test_planner_gives_up_after_the_attempt_budget(trace: TraceStore):
    cyclic = {
        "rationale": "broken",
        "tasks": [dict(VALID[0], depends_on=["t2"]), VALID[1]],
    }
    llm = ScriptedLlm(cyclic, cyclic)
    with pytest.raises(InvalidPlan, match="cycle"):
        Planner(llm, trace, max_attempts=2).plan("goal", [])


def test_replan_keeps_finished_work_and_retires_dropped_tasks(trace: TraceStore):
    from tests.conftest import done, make_result, make_task

    existing = {
        "t1": done(make_task("t1"), make_result(("a", "b", "1"))),
        "t9": make_task("t9", critical=True),
    }
    revised = Plan.model_validate({"rationale": "route around t9", "tasks": VALID})
    llm = ScriptedLlm(revised.model_dump(mode="json"))

    tasks = Planner(llm, trace, max_attempts=1).replan(
        "goal", existing, "t9 failed", revision=2
    )

    assert tasks["t1"].result is not None
    assert tasks["t9"].status.value == "superseded"
    assert "t2" in tasks
