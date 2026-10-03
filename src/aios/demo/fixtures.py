"""Builds the synthetic audit dataset the demo runs against.

All data here is invented. The numbers are chosen so that a live walkthrough
exercises the paths worth showing: a policy threshold breach, a cross-source
figure conflict, a flaky tool, and an unreachable one.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from aios.config import Settings

VENDORS = [
    ("V-101", "Northwind Logistics", "freight", "2023-04-11", "medium"),
    ("V-102", "Cobalt Cloud Services", "saas", "2024-01-08", "low"),
    ("V-103", "Meridian Consulting", "professional_services", "2022-09-30", "high"),
    ("V-104", "Atlas Facilities", "facilities", "2021-06-15", "low"),
]

# (txn_id, vendor_id, amount, booked_on, quarter, approval_ref)
TRANSACTIONS = [
    ("T-2901", "V-101", 26900.00, "2025-11-19", "FY26-Q2", None),
    ("T-2902", "V-102", 14300.00, "2025-12-02", "FY26-Q2", "AP-7702"),
    ("T-2903", "V-103", 33100.00, "2025-12-21", "FY26-Q2", "AP-7715"),
    ("T-3001", "V-101", 28400.00, "2026-01-14", "FY26-Q3", None),
    ("T-3002", "V-101", 27300.00, "2026-02-03", "FY26-Q3", None),
    ("T-3003", "V-101", 19800.00, "2026-02-19", "FY26-Q3", None),
    ("T-3004", "V-102", 12500.00, "2026-01-22", "FY26-Q3", "AP-7790"),
    ("T-3005", "V-103", 31200.00, "2026-02-27", "FY26-Q3", None),
    ("T-3006", "V-103", 8900.00, "2026-03-02", "FY26-Q3", "AP-7801"),
    ("T-3007", "V-104", 26050.00, "2026-03-18", "FY26-Q3", "AP-7808"),
    ("T-3008", "V-101", 20000.00, "2026-03-25", "FY26-Q3", "AP-7815"),
]

LEDGER_BALANCES = [
    ("V-101", "FY26-Q3", 95500.00),
    ("V-102", "FY26-Q3", 12500.00),
    ("V-103", "FY26-Q3", 40100.00),
    ("V-104", "FY26-Q3", 26050.00),
]

SCHEMA = """
CREATE TABLE vendors (
    vendor_id    TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    category     TEXT NOT NULL,
    onboarded_on TEXT NOT NULL,
    risk_tier    TEXT NOT NULL
);
CREATE TABLE transactions (
    txn_id       TEXT PRIMARY KEY,
    vendor_id    TEXT NOT NULL REFERENCES vendors(vendor_id),
    amount_usd   REAL NOT NULL,
    booked_on    TEXT NOT NULL,
    quarter      TEXT NOT NULL,
    approval_ref TEXT
);
CREATE TABLE ledger_balances (
    vendor_id  TEXT NOT NULL REFERENCES vendors(vendor_id),
    quarter    TEXT NOT NULL,
    amount_usd REAL NOT NULL,
    PRIMARY KEY (vendor_id, quarter)
);
"""

POLICIES: dict[str, str] = {
    "expense-approval.md": """# Expense Approval Policy (POL-014)

## Dual approval threshold
Any single vendor transaction of **25,000 USD or more** requires dual approval.
An approved transaction carries an approval reference (`AP-nnnn`) in the ledger.
A transaction at or above the threshold with no approval reference is a control
exception and must be listed in the quarterly audit review.

## Exception handling
Control exceptions are reported per vendor as *unapproved exposure*: the sum of
that vendor's unapproved transactions at or above the threshold, for the period
under review. Exceptions above 50,000 USD for a single vendor require CFO
sign-off before the review is closed.
""",
    "fiscal-calendar.md": """# Fiscal Calendar FY26 (POL-002)

| Quarter | Period |
|---|---|
| FY26-Q1 | 2025-07-01 to 2025-09-30 |
| FY26-Q2 | 2025-10-01 to 2025-12-31 |
| FY26-Q3 | 2026-01-01 to 2026-03-31 |
| FY26-Q4 | 2026-04-01 to 2026-06-30 |

Figures labelled *FY-to-date* are cumulative from FY26-Q1 and are not
comparable with single-quarter figures.
""",
    "vendor-risk.md": """# Vendor Risk Tiering (POL-021)

Vendors are tiered `low`, `medium` or `high`. High-tier vendors require a
documented review every quarter regardless of transaction volume. A medium-tier
vendor with two or more control exceptions in one quarter is escalated to high
tier at the next review.
""",
    "audit-memo-fy26-q2.md": """# Audit Memo - FY26-Q2 close

Carried-forward items for the next review:

- Northwind Logistics (V-101): unapproved exposure of **82,600 USD**,
  FY-to-date. Freight invoices are routinely booked before the approval
  workflow completes.
- Meridian Consulting (V-103): high-tier vendor, quarterly review outstanding.
""",
}


def build_fixtures(settings: Settings) -> Path:
    """Create the demo database and policy corpus. Idempotent."""
    settings.policy_dir.mkdir(parents=True, exist_ok=True)
    for name, body in POLICIES.items():
        (settings.policy_dir / name).write_text(body, encoding="utf-8")

    settings.demo_db.parent.mkdir(parents=True, exist_ok=True)
    settings.demo_db.unlink(missing_ok=True)
    with sqlite3.connect(settings.demo_db) as connection:
        connection.executescript(SCHEMA)
        connection.executemany("INSERT INTO vendors VALUES (?,?,?,?,?)", VENDORS)
        connection.executemany(
            "INSERT INTO transactions VALUES (?,?,?,?,?,?)", TRANSACTIONS
        )
        connection.executemany(
            "INSERT INTO ledger_balances VALUES (?,?,?)", LEDGER_BALANCES
        )
    return settings.demo_db
