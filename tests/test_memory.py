"""Durable memory: recall, promotion policy and supersession."""

from __future__ import annotations

from aios.config import Settings
from aios.memory import PromotionPolicy, RunIndex, SemanticMemory
from aios.orchestration.state import Claim

EXPOSURE = Claim(
    subject="Northwind Logistics", metric="unapproved_exposure_usd", value="55700.00"
)


def store(settings: Settings, **policy) -> SemanticMemory:
    return SemanticMemory(settings.semantic_db, PromotionPolicy(**policy))


def test_promotion_requires_human_approval_by_default(settings: Settings):
    memory = store(settings)
    assert memory.promote([EXPOSURE], run_id="r1", confidence=0.99, human_approved=False) == []
    assert memory.facts() == []


def test_promotion_requires_the_confidence_bar(settings: Settings):
    memory = store(settings, min_confidence=0.8)
    assert memory.promote([EXPOSURE], run_id="r1", confidence=0.5, human_approved=True) == []


def test_an_approved_confident_claim_is_promoted_with_provenance(settings: Settings):
    memory = store(settings)
    promoted = memory.promote(
        [EXPOSURE], run_id="r1", confidence=0.9, human_approved=True
    )
    assert len(promoted) == 1
    assert promoted[0].provenance == "r1"
    assert memory.facts()[0].value == "55700.00"


def test_promoting_the_same_value_twice_does_not_duplicate(settings: Settings):
    memory = store(settings)
    memory.promote([EXPOSURE], run_id="r1", confidence=0.9, human_approved=True)
    again = memory.promote([EXPOSURE], run_id="r2", confidence=0.9, human_approved=True)
    assert again == []
    assert len(memory.facts()) == 1


def test_a_contradicting_fact_supersedes_rather_than_overwrites(settings: Settings):
    memory = store(settings)
    memory.promote([EXPOSURE], run_id="r1", confidence=0.9, human_approved=True)
    revised = EXPOSURE.model_copy(update={"value": "61300.00"})
    memory.promote([revised], run_id="r2", confidence=0.9, human_approved=True)

    active = memory.facts()
    assert [fact.value for fact in active] == ["61300.00"]

    history = memory.facts(include_superseded=True)
    superseded = [fact for fact in history if fact.superseded_by]
    assert len(superseded) == 1
    assert superseded[0].value == "55700.00"
    assert superseded[0].superseded_by == active[0].fact_id


def test_recall_scores_on_shared_terms(settings: Settings):
    memory = store(settings)
    memory.seed(EXPOSURE, provenance="seed", confidence=0.9)
    memory.seed(
        Claim(subject="Atlas Facilities", metric="risk_tier", value="low"),
        provenance="seed",
        confidence=0.9,
    )
    recalled = memory.recall("Northwind Logistics unapproved exposure")
    assert [fact.subject for fact in recalled] == ["Northwind Logistics"]


def test_recall_ignores_superseded_facts(settings: Settings):
    memory = store(settings)
    memory.promote([EXPOSURE], run_id="r1", confidence=0.9, human_approved=True)
    memory.promote(
        [EXPOSURE.model_copy(update={"value": "61300.00"})],
        run_id="r2",
        confidence=0.9,
        human_approved=True,
    )
    assert [fact.value for fact in memory.recall("Northwind exposure")] == ["61300.00"]


def test_the_run_index_tracks_status_and_report(settings: Settings):
    index = RunIndex(settings.index_db)
    index.start("r1", "goal one")
    index.update("r1", "awaiting_signoff")
    index.update("r1", "completed", report_path="reports/r1.md")

    record = index.get("r1")
    assert record.status == "completed"
    assert record.report_path == "reports/r1.md"

    index.update("r1", "completed")
    assert index.get("r1").report_path == "reports/r1.md"
    assert [record.run_id for record in index.list()] == ["r1"]
