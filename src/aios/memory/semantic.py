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

# A fact counts as current while nothing has replaced it and its own validity
# period, if it declared one, has not run out.
ACTIVE_FACT = "superseded_by IS NULL AND (valid_to IS NULL OR valid_to > ?)"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    fact_id       TEXT PRIMARY KEY,
    subject       TEXT NOT NULL,
    metric        TEXT NOT NULL,
    value         TEXT NOT NULL,
    confidence    REAL NOT NULL,
    provenance    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    valid_to      TEXT,
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
    valid_to: str | None = None
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
            _add_missing_columns(connection)

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
        """Facts still believed, most recent first.

        A fact leaves this set two ways: a later fact supersedes it, or its own
        validity period ends.
        """
        clause = "" if include_superseded else f"WHERE {ACTIVE_FACT}"
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM facts {clause} ORDER BY created_at DESC",
                () if include_superseded else (_now(),),
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
                valid_to=claim.valid_to,
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
            valid_to=claim.valid_to,
        )
        self._insert(fact)
        return fact

    def _active(self, subject: str, metric: str) -> Fact | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM facts
                WHERE subject = ? AND metric = ? AND {active}
                ORDER BY created_at DESC LIMIT 1
                """.format(active=ACTIVE_FACT),
                (subject, metric, _now()),
            ).fetchone()
        return Fact(**dict(row)) if row else None

    def _insert(self, fact: Fact) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO facts (
                    fact_id, subject, metric, value, confidence, provenance,
                    created_at, valid_to, superseded_by
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    fact.fact_id,
                    fact.subject,
                    fact.metric,
                    fact.value,
                    fact.confidence,
                    fact.provenance,
                    fact.created_at,
                    fact.valid_to,
                    fact.superseded_by,
                ),
            )

    def _supersede(self, old_fact_id: str, new_fact_id: str) -> None:
        """Point a fact at the one that replaced it.

        `valid_to` is untouched: a corrected fact was never true, so it did not
        expire. Only the claim that asserted an expiry sets that column.
        """
        with self._connect() as connection:
            connection.execute(
                "UPDATE facts SET superseded_by = ? WHERE fact_id = ?",
                (new_fact_id, old_fact_id),
            )


def _add_missing_columns(connection: sqlite3.Connection) -> None:
    """Widen a store written before `valid_to` existed."""
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(facts)")}
    if "valid_to" not in columns:
        connection.execute("ALTER TABLE facts ADD COLUMN valid_to TEXT")


def _tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", text.lower()) if len(token) > 2}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
