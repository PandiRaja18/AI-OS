"""Durable memory, tenant-scoped and versioned.

Two things change from v1. Facts carry a tenant and a validity window, so recall
can answer "what did we believe when that report was signed" rather than only
"what do we believe now". And recall is a three-stage pipeline - keyword filter,
optional vector search, blended ranking - instead of a single lexical pass.

The vector leg is pluggable and defaults to a null implementation. Ranking is a
blend of lexical overlap, semantic score, recency and the fact's own confidence,
because the most similar fact is not always the most useful one.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Protocol

from aios.memory.semantic import ACTIVE_FACT, Fact, PromotionPolicy
from aios.orchestration.state import Claim
from aios.platform.models import new_id, now
from aios.platform.sql.engine import Database, from_time, to_time


class VectorIndex(Protocol):
    """Semantic leg of recall. Swap in pgvector or a managed index."""

    def search(self, tenant_id: str, query: str, limit: int) -> dict[str, float]:
        """Fact id -> similarity in [0, 1]."""

    def add(self, tenant_id: str, fact: Fact) -> None: ...


class NullVectorIndex:
    """No semantic leg configured. Recall falls back to lexical and recency."""

    def search(self, tenant_id: str, query: str, limit: int) -> dict[str, float]:
        return {}

    def add(self, tenant_id: str, fact: Fact) -> None:
        return None


class RecallWeights:
    """Per-domain tuning. Compliance leans on confidence, operations on recency."""

    def __init__(
        self,
        lexical: float = 0.45,
        semantic: float = 0.25,
        recency: float = 0.15,
        confidence: float = 0.15,
    ) -> None:
        self.lexical = lexical
        self.semantic = semantic
        self.recency = recency
        self.confidence = confidence


class SqlFactStore:
    """Versioned fact store with promotion and supersession policies."""

    def __init__(
        self,
        database: Database,
        policy: PromotionPolicy | None = None,
        vectors: VectorIndex | None = None,
        weights: RecallWeights | None = None,
    ) -> None:
        self._db = database
        self.policy = policy or PromotionPolicy()
        self._vectors = vectors or NullVectorIndex()
        self._weights = weights or RecallWeights()

    def recall(self, tenant_id: str, query: str, limit: int = 5) -> list[Fact]:
        """Rank this tenant's active facts against the goal."""
        candidates = self.facts(tenant_id)
        if not candidates:
            return []
        terms = _tokens(query)
        semantic = self._vectors.search(tenant_id, query, limit * 4)
        newest = max(fact.created_at for fact in candidates)

        scored: list[tuple[float, Fact]] = []
        for fact in candidates:
            haystack = _tokens(f"{fact.subject} {fact.metric} {fact.value}")
            overlap = len(terms & haystack) / max(len(terms), 1)
            if overlap == 0 and fact.fact_id not in semantic:
                continue
            score = (
                self._weights.lexical * overlap
                + self._weights.semantic * semantic.get(fact.fact_id, 0.0)
                + self._weights.recency * _recency(fact.created_at, newest)
                + self._weights.confidence * fact.confidence
            )
            scored.append((score, fact))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [fact for _, fact in scored[:limit]]

    def facts(self, tenant_id: str, include_superseded: bool = False) -> list[Fact]:
        """This tenant's facts, newest first; current ones unless asked for all."""
        statement = "SELECT * FROM facts WHERE tenant_id = ?"
        params: list = [tenant_id]
        if not include_superseded:
            statement += f" AND {ACTIVE_FACT}"
            params.append(to_time(now()))
        statement += " ORDER BY valid_from DESC"
        return [_row_to_fact(row) for row in self._db.query(statement, tuple(params))]

    def history(self, tenant_id: str, subject: str, metric: str) -> list[Fact]:
        """Every version of one fact, newest first."""
        rows = self._db.query(
            """
            SELECT * FROM facts
            WHERE tenant_id = ? AND subject = ? AND metric = ?
            ORDER BY valid_from DESC
            """,
            (tenant_id, subject, metric),
        )
        return [_row_to_fact(row) for row in rows]

    def as_of(self, tenant_id: str, moment: datetime) -> list[Fact]:
        """What this tenant believed at a point in time.

        Two windows have to hold at once. A fact was believed until its
        replacement was written, and it asserted something that was true until
        its own `valid_to`. Only the first is a correction.
        """
        when = to_time(moment)
        rows = self._db.query(
            """
            SELECT fact.* FROM facts AS fact
            LEFT JOIN facts AS replacement
                   ON replacement.fact_id = fact.superseded_by
            WHERE fact.tenant_id = ? AND fact.valid_from <= ?
              AND (fact.valid_to IS NULL OR fact.valid_to > ?)
              AND (replacement.valid_from IS NULL OR replacement.valid_from > ?)
            """,
            (tenant_id, when, when, when),
        )
        return [_row_to_fact(row) for row in rows]

    def promote(
        self,
        tenant_id: str,
        claims: list[Claim],
        run_id: str,
        confidence: float,
        human_approved: bool,
    ) -> list[Fact]:
        """Promote claims that satisfy the policy, superseding what they contradict."""
        if not self.policy.admits(confidence, human_approved):
            return []
        promoted: list[Fact] = []
        for claim in claims:
            existing = self._active(tenant_id, claim.subject, claim.metric)
            if existing is not None and existing.value == claim.value:
                continue
            fact = Fact(
                fact_id=new_id("fact"),
                subject=claim.subject,
                metric=claim.metric,
                value=claim.value,
                confidence=confidence,
                provenance=run_id,
                created_at=to_time(now()),
                valid_to=claim.valid_to,
            )
            self._insert(tenant_id, fact)
            if existing is not None:
                self._supersede(existing.fact_id, fact.fact_id)
            self._vectors.add(tenant_id, fact)
            promoted.append(fact)
        return promoted

    def seed(
        self, tenant_id: str, claim: Claim, provenance: str, confidence: float
    ) -> Fact:
        """Insert a fact directly, bypassing the promotion policy."""
        fact = Fact(
            fact_id=new_id("fact"),
            subject=claim.subject,
            metric=claim.metric,
            value=claim.value,
            confidence=confidence,
            provenance=provenance,
            created_at=to_time(now()),
            valid_to=claim.valid_to,
        )
        self._insert(tenant_id, fact)
        self._vectors.add(tenant_id, fact)
        return fact

    def _active(self, tenant_id: str, subject: str, metric: str) -> Fact | None:
        row = self._db.one(
            """
            SELECT * FROM facts
            WHERE tenant_id = ? AND subject = ? AND metric = ? AND {active}
            ORDER BY valid_from DESC LIMIT 1
            """.format(active=ACTIVE_FACT),
            (tenant_id, subject, metric, to_time(now())),
        )
        return _row_to_fact(row) if row else None

    def _insert(self, tenant_id: str, fact: Fact) -> None:
        self._db.execute(
            """
            INSERT INTO facts (
                fact_id, tenant_id, subject, metric, value, confidence,
                provenance_run, valid_from, valid_to, superseded_by
            ) VALUES (?,?,?,?,?,?,?,?,?,NULL)
            """,
            (
                fact.fact_id,
                tenant_id,
                fact.subject,
                fact.metric,
                fact.value,
                fact.confidence,
                fact.provenance,
                fact.created_at,
                fact.valid_to,
            ),
        )

    def _supersede(self, old_fact_id: str, new_fact_id: str) -> None:
        """Point a fact at the one that replaced it.

        `valid_to` is untouched: a corrected fact was never true, so it did not
        expire. Only the claim that asserted an expiry sets that column. The
        replacement's `valid_from` is what ends the belief window `as_of` reads.
        """
        self._db.execute(
            "UPDATE facts SET superseded_by = ? WHERE fact_id = ?",
            (new_fact_id, old_fact_id),
        )


def _row_to_fact(row: dict) -> Fact:
    return Fact(
        fact_id=row["fact_id"],
        subject=row["subject"],
        metric=row["metric"],
        value=row["value"],
        confidence=row["confidence"],
        provenance=row["provenance_run"],
        created_at=str(row["valid_from"]),
        valid_to=row["valid_to"],
        superseded_by=row["superseded_by"],
    )


def _tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", text.lower()) if len(token) > 2}


def _recency(created_at: str, newest: str) -> float:
    """1.0 for the newest fact, decaying to 0 over 90 days."""
    created = from_time(created_at)
    latest = from_time(newest)
    if created is None or latest is None:
        return 0.0
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    age_days = max((latest - created).total_seconds() / 86_400, 0.0)
    return max(0.0, 1.0 - age_days / 90.0)
