"""Scenarios built from SEC Financial Statement Data Sets.

The point of this dataset is that nobody authored the disagreements. When a
company files a 10-K and later files a 10-K/A correcting it, the SEC publishes
both, flags the superseded one, and the corrected figure is the ground truth.
Every conflict class the evaluation needs occurs naturally:

    true disagreement   original filing vs its amendment
    stale snapshot      an earlier filing still in circulation
    scope mismatch      `qtrs` differs: one quarter against four
    unit mismatch       `uom` differs for the same concept
    paraphrased metric  different XBRL tags for the same concept
    no conflict         a value unchanged across filings (controls)

Formatting-only differences are the one class this source does not supply,
because the archive stores parsed numerics rather than rendered text; those
scenarios have to be synthesised and must be reported as such.

Source: https://www.sec.gov/data-research/sec-markets-data/financial-statement-data-sets
Quarterly archives, 2009Q1 onward. US government work, public domain.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from eval.schema import (
    ConflictClass,
    GroundTruth,
    Scenario,
    ScenarioClaim,
    ScenarioSet,
    SourceKind,
    SourceRef,
    stable_split,
)

DOMAIN = "sec_filings"
AMENDMENT_FORMS = ("10-K/A", "10-Q/A", "20-F/A", "40-F/A")
BASE_OF = {"10-K/A": "10-K", "10-Q/A": "10-Q", "20-F/A": "20-F", "40-F/A": "40-F"}

# Concepts a reviewer recognises, so a scenario reads as a question about the
# business rather than about a taxonomy.
CONCEPTS: dict[str, str] = {
    "Revenues": "revenue",
    "NetIncomeLoss": "net_income",
    "Assets": "total_assets",
    "Liabilities": "total_liabilities",
    "StockholdersEquity": "stockholders_equity",
    "CashAndCashEquivalentsAtCarryingValue": "cash_and_equivalents",
    "OperatingIncomeLoss": "operating_income",
    "EarningsPerShareBasic": "earnings_per_share_basic",
}

# Tags that mean the same thing to a reader but are distinct in the taxonomy.
PARAPHRASE_GROUPS: tuple[tuple[str, ...], ...] = (
    ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"),
    ("Revenues", "SalesRevenueNet"),
    ("OperatingIncomeLoss", "IncomeLossFromContinuingOperations"),
)


class SecQuarter:
    """One quarterly archive, read straight from the downloaded ZIP."""

    def __init__(self, archive: Path) -> None:
        self.archive = archive
        self.quarter = archive.stem

    def _rows(self, member: str) -> list[dict[str, str]]:
        with zipfile.ZipFile(self.archive) as bundle:
            names = {name.lower(): name for name in bundle.namelist()}
            if member not in names:
                raise FileNotFoundError(f"{member} missing from {self.archive.name}")
            raw = bundle.read(names[member]).decode("utf-8", errors="replace")
        return list(csv.DictReader(io.StringIO(raw), delimiter="\t"))

    def submissions(self) -> dict[str, dict[str, str]]:
        """Filings in this quarter, keyed by accession number."""
        return {row["adsh"]: row for row in self._rows("sub.txt")}

    def numbers(self, wanted: set[str] | None = None) -> dict[str, list[dict[str, Any]]]:
        """Reported figures, keyed by accession number."""
        facts: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in self._rows("num.txt"):
            if wanted is not None and row["tag"] not in wanted:
                continue
            if row.get("coreg"):
                continue
            value = _number(row.get("value"))
            if value is None:
                continue
            facts[row["adsh"]].append(
                {
                    "tag": row["tag"],
                    "ddate": row["ddate"],
                    "qtrs": row.get("qtrs", ""),
                    "uom": row.get("uom", ""),
                    "value": value,
                }
            )
        return facts


def _number(raw: str | None) -> float | None:
    try:
        return float(raw) if raw not in (None, "") else None
    except ValueError:
        return None


def _filed(row: dict[str, str]) -> date | None:
    stamp = row.get("filed") or ""
    try:
        return datetime.strptime(stamp, "%Y%m%d").date()
    except ValueError:
        return None


def _period(fact: dict[str, Any]) -> str:
    quarters = fact.get("qtrs") or "?"
    return f"{fact['ddate']}/Q{quarters}"


def _format_money(value: float) -> str:
    return f"{value:.2f}"


def find_restatements(quarter: SecQuarter) -> list[tuple[dict, dict]]:
    """Pair each amendment with the original filing it supersedes.

    The pairing key is the filer and the period, because an amendment carries a
    new accession number. `prevrpt` marks the superseded filing, which is the
    SEC telling us which of the two is wrong.
    """
    submissions = quarter.submissions()
    by_key: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in submissions.values():
        base = BASE_OF.get(row["form"], row["form"])
        by_key[(row["cik"], base, row.get("period", ""))].append(row)

    pairs = []
    for filings in by_key.values():
        amendments = [row for row in filings if row["form"] in AMENDMENT_FORMS]
        originals = [row for row in filings if row["form"] not in AMENDMENT_FORMS]
        for amendment in amendments:
            superseded = [
                row
                for row in originals
                if str(row.get("prevrpt", "")).strip() in ("1", "TRUE", "true")
            ] or originals
            if superseded:
                pairs.append((sorted(superseded, key=lambda r: r["adsh"])[0], amendment))
    return pairs


def _scenario(
    scenario_id: str,
    goal: str,
    subject: str,
    metric: str,
    original: dict,
    amendment: dict,
    original_fact: dict,
    amended_fact: dict,
    conflict_class: ConflictClass,
    rationale: str,
) -> Scenario:
    """Build one scenario from an original and amended figure."""
    sources = [
        SourceRef(
            source_id="original_filing",
            kind=SourceKind.SNAPSHOT_TABLE,
            as_of=_filed(original),
            tier=2,
        ),
        SourceRef(
            source_id="amended_filing",
            kind=SourceKind.LIVE_TABLE,
            as_of=_filed(amendment),
            tier=1,
        ),
    ]
    claims = [
        ScenarioClaim(
            source_id="original_filing",
            subject=subject,
            metric=metric,
            value=_format_money(original_fact["value"]),
            unit=original_fact["uom"],
            period=_period(original_fact),
            correct=False,
        ),
        ScenarioClaim(
            source_id="amended_filing",
            subject=subject,
            metric=metric,
            value=_format_money(amended_fact["value"]),
            unit=amended_fact["uom"],
            period=_period(amended_fact),
            correct=True,
        ),
    ]
    truth = GroundTruth(
        subject=subject,
        metric=metric,
        value=_format_money(amended_fact["value"]),
        unit=amended_fact["uom"],
        period=_period(amended_fact),
        conflict_class=conflict_class,
        authoritative_source="amended_filing",
        rationale=rationale,
    )
    return Scenario(
        scenario_id=scenario_id,
        domain=DOMAIN,
        goal=goal,
        sources=sources,
        claims=claims,
        truth=truth,
        provenance=f"{original['adsh']} superseded by {amendment['adsh']}",
        split=stable_split(scenario_id),
    )


def _classify(original: dict[str, Any], amended: dict[str, Any]) -> tuple[ConflictClass, str]:
    """Decide why the two figures differ."""
    if original["uom"] != amended["uom"]:
        return (
            ConflictClass.UNIT_MISMATCH,
            f"reported in {original['uom']} and {amended['uom']}",
        )
    if original["qtrs"] != amended["qtrs"]:
        return (
            ConflictClass.SCOPE_MISMATCH,
            f"covers {original['qtrs']} quarter(s) against {amended['qtrs']}",
        )
    if original["value"] == amended["value"]:
        return ConflictClass.NONE, "unchanged by the amendment"
    if _format_money(original["value"]) == _format_money(amended["value"]):
        return ConflictClass.FORMATTING_ONLY, "same figure, different precision"
    return (
        ConflictClass.TRUE_DISAGREEMENT,
        "the amendment restates the figure",
    )


def _paraphrase_of(tag: str) -> str | None:
    """The canonical concept a tag is a synonym for, if it is one."""
    for group in PARAPHRASE_GROUPS:
        if tag in group[1:]:
            return group[0]
    return None


def _paraphrase_pairs(
    before: dict[tuple[str, str], list[dict[str, Any]]],
    after: dict[tuple[str, str], list[dict[str, Any]]],
) -> list[tuple[tuple[str, str], dict[str, Any], dict[str, Any]]]:
    """Find a concept reported under one tag, then restated under a synonym.

    The two filings name the same thing differently, which is a disagreement
    about the metric rather than the value, and is the case literal matching is
    expected to miss.
    """
    pairs = []
    for (tag, period_end), amended_rows in after.items():
        canonical = _paraphrase_of(tag)
        if canonical is None:
            continue
        original_rows = before.get((canonical, period_end))
        if not original_rows:
            continue
        pairs.append(((canonical, period_end), amended_rows[0], original_rows[0]))
    return pairs


def _by_concept(
    rows: list[dict[str, Any]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Group a filing's figures by concept and period end, keeping every scope."""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row["tag"], row["ddate"])].append(row)
    return grouped


def _align(
    before: dict[tuple[str, str], list[dict[str, Any]]],
    after: dict[tuple[str, str], list[dict[str, Any]]],
) -> list[tuple[tuple[str, str], dict[str, Any], dict[str, Any]]]:
    """Match each amended figure to the original it restates.

    Matching the same scope first keeps like with like. Only when no scope
    matches do we pair across scopes, which is what a scope mismatch is: the
    key used for pairing must not contain the field that differs.
    """
    aligned = []
    for key, amended_rows in after.items():
        original_rows = before.get(key)
        if not original_rows:
            continue
        for amended in amended_rows:
            same_scope = next(
                (row for row in original_rows if row["qtrs"] == amended["qtrs"]),
                None,
            )
            aligned.append((key, amended, same_scope or original_rows[0]))
    return aligned


def build_scenarios(
    archive: Path, limit: int = 200, include_controls: bool = True
) -> ScenarioSet:
    """Turn one quarterly archive into labelled scenarios."""
    quarter = SecQuarter(archive)
    submissions = quarter.submissions()
    synonyms = {tag for group in PARAPHRASE_GROUPS for tag in group[1:]}
    facts = quarter.numbers(wanted=set(CONCEPTS) | synonyms)
    pairs = find_restatements(quarter)

    scenarios: list[Scenario] = []
    for original, amendment in pairs:
        before = _by_concept(facts.get(original["adsh"], []))
        after = _by_concept(facts.get(amendment["adsh"], []))
        company = (original.get("name") or original["cik"]).title()

        same_tag = [(k, a, o, False) for k, a, o in _align(before, after)]
        renamed = [(k, a, o, True) for k, a, o in _paraphrase_pairs(before, after)]
        for key, amended_fact, original_fact, is_paraphrase in same_tag + renamed:
            tag, period_end = key
            metric = CONCEPTS[tag]
            if is_paraphrase:
                conflict_class = ConflictClass.PARAPHRASED_METRIC
                rationale = (
                    f"restated under the tag {amended_fact['tag']} "
                    f"rather than {tag}"
                )
            else:
                conflict_class, rationale = _classify(original_fact, amended_fact)
            if conflict_class is ConflictClass.NONE and not include_controls:
                continue
            suffix = f"-as-{amended_fact['tag']}" if is_paraphrase else ""
            scenario_id = (
                f"sec-{quarter.quarter}-{amendment['adsh']}"
                f"-{tag}-{period_end}{suffix}"
            )
            scenarios.append(
                _scenario(
                    scenario_id=scenario_id,
                    goal=(
                        f"Report {metric.replace('_', ' ')} for {company} "
                        f"for the period ending {period_end}."
                    ),
                    subject=company,
                    metric=metric,
                    original=original,
                    amendment=amendment,
                    original_fact=original_fact,
                    amended_fact=amended_fact,
                    conflict_class=conflict_class,
                    rationale=rationale,
                )
            )
            if len(scenarios) >= limit:
                return ScenarioSet(
                    name=f"sec-{quarter.quarter}", domain=DOMAIN, scenarios=scenarios
                )

    del submissions
    return ScenarioSet(name=f"sec-{quarter.quarter}", domain=DOMAIN, scenarios=scenarios)
