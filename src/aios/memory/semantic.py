"""Semantic memory: facts that outlive a single run.

The interesting part is not the store, it is the two policies around it.
Promotion decides what earns a place in durable memory; supersession keeps the
audit trail intact when a later run contradicts an earlier fact.

Retrieval is lexical scoring over a fact table. That is a deliberate v1 choice:
it is small, deterministic and debuggable, and the interface (`recall`) is the
seam a vector or graph backend slots into later.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from aios.orchestration.state import Claim

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    fact_id       TEXT PRIMARY KEY,
    subject       TEXT NOT NULL,
    metric        TEXT NOT NULL,
    value         TEXT NOT NULL,
    confidence    REAL NOT NULL,
    provenance    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    superseded_by TEXT
)
"""


class Fact(BaseModel):
    """A durable claim, with a pointer back to the run that produced it."""

    fact_id: str
    subject: str
    metric: str
    value: str
    confidence: float
    provenance: str
    created_at: str
    superseded_by: str | None = None

    def as_context(self) -> str:
        return f"{self.subject}.{self.metric} = {self.value} (run {self.provenance})"


class PromotionPolicy(BaseModel):
    """What may cross from episodic into durable memory."""

    min_confidence: float = 0.8
    require_human_approval: bool = True

    def admits(self, confidence: float, human_approved: bool) -> bool:
        if self.require_human_approval and not human_approved:
            return False
        return confidence >= self.min_confidence


class SemanticMemory:
    """Durable fact store with promotion and supersession policies."""

    def __init__(self, db_path: Path, policy: PromotionPolicy | None = None) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self.policy = policy or PromotionPolicy()
        with self._connect() as connection:
            connection.execute(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def recall(self, query: str, limit: int = 5) -> list[Fact]:
        """Return the active facts most lexically related to `query`."""
        terms = _tokens(query)
        scored: list[tuple[int, Fact]] = []
        for fact in self.facts():
            haystack = _tokens(f"{fact.subject} {fact.metric} {fact.value}")
            score = len(terms & haystack)
            if score:
                scored.append((score, fact))
        scored.sort(key=lambda item: (item[0], item[1].created_at), reverse=True)
        return [fact for _, fact in scored[:limit]]

    def facts(self, include_superseded: bool = False) -> list[Fact]:
        """All facts, most recent first."""
        clause = "" if include_superseded else "WHERE superseded_by IS NULL"
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM facts {clause} ORDER BY created_at DESC"
            ).fetchall()
        return [Fact(**dict(row)) for row in rows]

    def promote(
        self,
        claims: list[Claim],
        *,
        run_id: str,
        confidence: float,
        human_approved: bool,
    ) -> list[Fact]:
        """Promote claims that satisfy the policy; supersede what they contradict."""
        if not self.policy.admits(confidence, human_approved):
            return []
        promoted: list[Fact] = []
        for claim in claims:
            existing = self._active(claim.subject, claim.metric)
            if existing is not None and existing.value == claim.value:
                continue
            fact = Fact(
                fact_id=uuid.uuid4().hex[:12],
                subject=claim.subject,
                metric=claim.metric,
                value=claim.value,
                confidence=confidence,
                provenance=run_id,
                created_at=_now(),
            )
            self._insert(fact)
            if existing is not None:
                self._supersede(existing.fact_id, fact.fact_id)
            promoted.append(fact)
        return promoted

    def seed(self, claim: Claim, *, provenance: str, confidence: float) -> Fact:
        """Insert a fact directly, bypassing the promotion policy."""
        fact = Fact(
            fact_id=uuid.uuid4().hex[:12],
            subject=claim.subject,
            metric=claim.metric,
            value=claim.value,
            confidence=confidence,
            provenance=provenance,
            created_at=_now(),
        )
        self._insert(fact)
        return fact

    def _active(self, subject: str, metric: str) -> Fact | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM facts
                WHERE subject = ? AND metric = ? AND superseded_by IS NULL
                ORDER BY created_at DESC LIMIT 1
                """,
                (subject, metric),
            ).fetchone()
        return Fact(**dict(row)) if row else None

    def _insert(self, fact: Fact) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO facts VALUES (?,?,?,?,?,?,?,?)",
                (
                    fact.fact_id,
                    fact.subject,
                    fact.metric,
                    fact.value,
                    fact.confidence,
                    fact.provenance,
                    fact.created_at,
                    fact.superseded_by,
                ),
            )

    def _supersede(self, old_fact_id: str, new_fact_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE facts SET superseded_by = ? WHERE fact_id = ?",
                (new_fact_id, old_fact_id),
            )


def _tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", text.lower()) if len(token) > 2}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
