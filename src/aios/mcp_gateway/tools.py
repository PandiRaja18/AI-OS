"""Concrete tools published on the gateway for the audit-review domain.

Only the data layer lives here - authorization, timeouts and tracing are the
gateway's job. Two tools deliberately misbehave (`ledger_lookup` fails a few
times, `peer_benchmark` is unreachable) so retry and degradation are observable.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from aios.config import Settings
from aios.mcp_gateway.registry import PrincipalKind, ToolSpec
from aios.orchestration.state import AgentType

_RESEARCH = AgentType.RESEARCH.value
_DATA = AgentType.DATA.value
_REPORTING = AgentType.REPORTING.value

NAMED_QUERIES: dict[str, str] = {
    "unapproved_above_threshold": """
        SELECT t.txn_id, v.name AS vendor, t.amount_usd, t.booked_on
        FROM transactions t JOIN vendors v USING (vendor_id)
        WHERE t.quarter = :quarter
          AND t.approval_ref IS NULL
          AND t.amount_usd >= :threshold
        ORDER BY t.amount_usd DESC
    """,
    "vendor_unapproved_exposure": """
        SELECT v.name AS vendor, v.vendor_id, v.risk_tier,
               COUNT(*) AS exceptions, SUM(t.amount_usd) AS exposure_usd
        FROM transactions t JOIN vendors v USING (vendor_id)
        WHERE t.quarter = :quarter
          AND t.approval_ref IS NULL
          AND t.amount_usd >= :threshold
        GROUP BY v.vendor_id
        ORDER BY exposure_usd DESC
    """,
    "quarter_totals": """
        SELECT COUNT(*) AS transactions, SUM(amount_usd) AS total_usd
        FROM transactions WHERE quarter = :quarter
    """,
    "vendor_profile": """
        SELECT vendor_id, name, category, risk_tier, onboarded_on
        FROM vendors WHERE name = :vendor OR vendor_id = :vendor
    """,
}

_STOPWORDS = frozenset(
    {"the", "a", "an", "of", "for", "and", "or", "to", "in", "is", "what", "which"}
)


def _tokenize(text: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if token not in _STOPWORDS and len(token) > 2
    ]


def sql_query(db_path: Path, query: str, **params: Any) -> list[dict[str, Any]]:
    """Run one of the registered named queries."""
    statement = NAMED_QUERIES.get(query)
    if statement is None:
        raise KeyError(
            f"unknown query {query!r}; available: {sorted(NAMED_QUERIES)}"
        )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(statement, params).fetchall()
    return [dict(row) for row in rows]


def doc_search(policy_dir: Path, query: str, limit: int = 3) -> list[dict[str, Any]]:
    """Lexical paragraph search over the policy corpus."""
    terms = set(_tokenize(query))
    hits: list[tuple[int, dict[str, Any]]] = []
    for path in sorted(policy_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        for paragraph in (part.strip() for part in text.split("\n\n")):
            if not paragraph:
                continue
            score = sum(1 for token in _tokenize(paragraph) if token in terms)
            if score:
                hits.append((score, {"document": path.name, "excerpt": paragraph}))
    hits.sort(key=lambda hit: hit[0], reverse=True)
    return [hit[1] for hit in hits[:limit]]


class _FlakyLedger:
    """Ledger client that fails a fixed number of times, then succeeds."""

    def __init__(self, db_path: Path, failures: int) -> None:
        self._db_path = db_path
        self._remaining_failures = failures

    def __call__(self, vendor: str, quarter: str) -> dict[str, Any]:
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise ConnectionError("ledger service returned 503 (upstream busy)")
        with sqlite3.connect(self._db_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT b.amount_usd FROM ledger_balances b
                JOIN vendors v USING (vendor_id)
                WHERE (v.name = :vendor OR v.vendor_id = :vendor)
                  AND b.quarter = :quarter
                """,
                {"vendor": vendor, "quarter": quarter},
            ).fetchone()
        if row is None:
            raise KeyError(f"no ledger balance for {vendor} in {quarter}")
        return {
            "vendor": vendor,
            "quarter": quarter,
            "balance_usd": row["amount_usd"],
        }


def peer_benchmark(category: str) -> dict[str, Any]:
    """External benchmark provider. Unreachable in this environment."""
    raise ConnectionError(f"peer-benchmark provider unreachable (category={category})")


def report_markdown(title: str, sections: Sequence[dict[str, str]]) -> str:
    """Render the report body. Separated so a host can store it anywhere."""
    lines = [f"# {title}", ""]
    for section in sections:
        heading = section.get("heading", "Section")
        lines += [f"## {heading}", "", section.get("body", ""), ""]
    return "\n".join(lines)


def render_report(
    report_dir: Path, run_id: str, title: str, sections: Sequence[dict[str, str]]
) -> str:
    """Write the report markdown to local disk and return its path."""
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{run_id}.md"
    path.write_text(report_markdown(title, sections), encoding="utf-8")
    return str(path)


def record_signoff(
    workspace: Path, run_id: str, reviewer: str, approved: bool, note: str | None
) -> dict[str, Any]:
    """Append a human decision to the append-only sign-off log."""
    entry = {
        "run_id": run_id,
        "reviewer": reviewer,
        "approved": approved,
        "note": note,
    }
    workspace.mkdir(parents=True, exist_ok=True)
    with (workspace / "signoffs.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
    return entry


def _with_outage(tool: ToolSpec) -> ToolSpec:
    """Replace a tool's handler with one that always reports an outage."""

    def unavailable(**_: Any) -> Any:
        raise ConnectionError(f"{tool.name} is unavailable (simulated outage)")

    return ToolSpec(
        name=tool.name,
        description=tool.description,
        handler=unavailable,
        allowed_agents=tool.allowed_agents,
        allowed_kinds=tool.allowed_kinds,
        timeout_seconds=tool.timeout_seconds,
    )


def build_tools(
    settings: Settings,
    run_id: str,
    report_writer: Callable[[str, Sequence[dict[str, str]]], str] | None = None,
) -> list[ToolSpec]:
    """Assemble the tool registry for one run.

    `report_writer` replaces the local-disk renderer, so a hosted run can put
    the report in object storage instead of on the worker that produced it.
    """
    ledger = _FlakyLedger(settings.demo_db, settings.injected_ledger_failures)
    render = report_writer or (
        lambda title, sections: render_report(
            settings.report_dir, run_id, title, sections
        )
    )
    tools = [
        ToolSpec(
            name="sql_query",
            description=(
                "Run a named, parameterized warehouse query. Arguments: "
                f"query (one of {sorted(NAMED_QUERIES)}), then any of "
                "quarter, threshold, vendor."
            ),
            handler=lambda query, **params: sql_query(settings.demo_db, query, **params),
            allowed_agents=frozenset({_DATA}),
        ),
        ToolSpec(
            name="ledger_lookup",
            description=(
                "Look up a vendor's booked ledger balance. Arguments: vendor, quarter."
            ),
            handler=lambda vendor, quarter: ledger(vendor, quarter),
            allowed_agents=frozenset({_DATA}),
        ),
        ToolSpec(
            name="doc_search",
            description=(
                "Search the policy and memo corpus. Arguments: query, limit."
            ),
            handler=lambda query, limit=3: doc_search(
                settings.policy_dir, query, limit
            ),
            allowed_agents=frozenset({_RESEARCH, _REPORTING}),
        ),
        ToolSpec(
            name="peer_benchmark",
            description="Fetch peer spend benchmarks. Arguments: category.",
            handler=peer_benchmark,
            allowed_agents=frozenset({_RESEARCH}),
        ),
        ToolSpec(
            name="render_report",
            description=(
                "Render the final report. Arguments: title, sections "
                "(list of {heading, body})."
            ),
            handler=lambda title, sections: render(title, sections),
            allowed_agents=frozenset({_REPORTING}),
        ),
        ToolSpec(
            name="record_signoff",
            description="Record a reviewer decision. Human principals only.",
            handler=lambda reviewer, approved, note=None: record_signoff(
                settings.workspace, run_id, reviewer, approved, note
            ),
            allowed_agents=frozenset(),
            allowed_kinds=frozenset({PrincipalKind.HUMAN}),
        ),
    ]
    outages = set(settings.outage_tools)
    return [_with_outage(tool) if tool.name in outages else tool for tool in tools]
