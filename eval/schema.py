"""Typed shapes for evaluation scenarios and their ground truth.

A scenario is one question put to the orchestrator, the sources it may consult,
and the answer we already know to be correct. Everything the harness measures is
a comparison against the ground truth recorded here, so this module is the
contract between dataset builders and metrics.

Two fields carry most of the research weight. `ConflictClass` labels *why* two
sources differ, which is what lets detection be scored per class rather than in
aggregate. `SourceRef.derived_from` records that one source was produced from
another, so agreement between them can be discounted: dependent sources that
agree are not corroboration, they are the same observation counted twice.
"""

from __future__ import annotations

import enum
import hashlib
from datetime import date

from pydantic import BaseModel, Field, model_validator


class ConflictClass(enum.StrEnum):
    """Why two sources report different values for the same thing."""

    NONE = "none"
    TRUE_DISAGREEMENT = "true_disagreement"
    STALE_SNAPSHOT = "stale_snapshot"
    SCOPE_MISMATCH = "scope_mismatch"
    UNIT_MISMATCH = "unit_mismatch"
    FORMATTING_ONLY = "formatting_only"
    PARAPHRASED_METRIC = "paraphrased_metric"


RESOLVABLE_WITHOUT_REQUERY = frozenset(
    {ConflictClass.FORMATTING_ONLY, ConflictClass.PARAPHRASED_METRIC}
)


class SourceKind(enum.StrEnum):
    LIVE_TABLE = "live_table"
    SNAPSHOT_TABLE = "snapshot_table"
    DOCUMENT = "document"


class SourceRef(BaseModel):
    """One place a claim came from."""

    source_id: str
    kind: SourceKind
    as_of: date | None = None
    tier: int = 1
    derived_from: str | None = None

    @property
    def independent(self) -> bool:
        """False when this source is a copy or summary of another one."""
        return self.derived_from is None


class ScenarioClaim(BaseModel):
    """A value a source reports, and whether it is the correct one."""

    source_id: str
    subject: str
    metric: str
    value: str
    unit: str = ""
    period: str = ""
    correct: bool = False


class GroundTruth(BaseModel):
    """What a correct run would conclude."""

    subject: str
    metric: str
    value: str
    unit: str = ""
    period: str = ""
    conflict_class: ConflictClass
    authoritative_source: str
    rationale: str = ""

    @property
    def is_conflict(self) -> bool:
        return self.conflict_class is not ConflictClass.NONE


class Scenario(BaseModel):
    """One evaluation case: a question, its sources, and the known answer."""

    scenario_id: str
    domain: str
    goal: str
    sources: list[SourceRef]
    claims: list[ScenarioClaim]
    truth: GroundTruth
    provenance: str = ""
    split: str = ""

    @model_validator(mode="after")
    def claims_reference_declared_sources(self) -> "Scenario":
        declared = {source.source_id for source in self.sources}
        unknown = {claim.source_id for claim in self.claims} - declared
        if unknown:
            raise ValueError(f"{self.scenario_id}: undeclared sources {sorted(unknown)}")
        if self.truth.authoritative_source not in declared:
            raise ValueError(
                f"{self.scenario_id}: authoritative source "
                f"{self.truth.authoritative_source} is not declared"
            )
        return self

    @property
    def has_dependent_sources(self) -> bool:
        """True when some source is a copy or summary of another.

        Agreement between dependent sources is not corroboration, so a scenario
        with this property can distinguish a system that knows the difference
        from one that counts agreement naively.
        """
        return any(not source.independent for source in self.sources)

    @property
    def distinct_values(self) -> int:
        return len({claim.value for claim in self.claims})

    def correct_claim(self) -> ScenarioClaim | None:
        return next((claim for claim in self.claims if claim.correct), None)


class ScenarioSet(BaseModel):
    """A named collection of scenarios, with the split written down."""

    name: str
    domain: str
    scenarios: list[Scenario] = Field(default_factory=list)

    def by_class(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for scenario in self.scenarios:
            key = str(scenario.truth.conflict_class)
            counts[key] = counts.get(key, 0) + 1
        return counts

    def split(self, name: str) -> list[Scenario]:
        return [scenario for scenario in self.scenarios if scenario.split == name]


def stable_split(scenario_id: str, dev_fraction: float = 0.3) -> str:
    """Assign a scenario to dev or test, the same way on every machine.

    Hashing the id rather than shuffling means the split survives regeneration,
    reordering and parallel builds, which is what makes "test was touched once"
    a claim anyone can check.
    """
    digest = hashlib.sha256(scenario_id.encode()).hexdigest()
    bucket = int(digest[:8], 16) / 0xFFFFFFFF
    return "dev" if bucket < dev_fraction else "test"
