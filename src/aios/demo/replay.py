"""Recorded model responses for the offline demo scenarios.

`ReplayClient` serves these instead of calling Claude, so the walkthrough runs
with no credentials and produces the same trace every time. The tools, gateway,
scheduler and graph are the real ones - only the model calls are recorded.

Two scenarios:
  audit         the standard run: conflicting figures, a flaky ledger, an
                unreachable benchmark provider, then human sign-off.
  audit_outage  the same goal with `ledger_lookup` down for good, which blocks a
                critical task and forces the planner to route around it.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

AUDIT_PLAN: dict[str, Any] = {
    "rationale": (
        "Establish the policy baseline first, scan the quarter for control "
        "exceptions, then verify and contextualise the exceptions in parallel "
        "before reporting."
    ),
    "tasks": [
        {
            "task_id": "t1_policy_baseline",
            "description": (
                "Establish the dual-approval threshold and the exception "
                "reporting rules that apply to the FY26-Q3 audit review."
            ),
            "agent": "research",
            "depends_on": [],
            "success_criteria": (
                "The approval threshold and the definition of unapproved "
                "exposure are quoted from policy."
            ),
            "critical": True,
            "requires_signoff": False,
        },
        {
            "task_id": "t2_exception_scan",
            "description": (
                "List FY26-Q3 transactions at or above the approval threshold "
                "with no approval reference, and total unapproved exposure per "
                "vendor."
            ),
            "agent": "data",
            "depends_on": ["t1_policy_baseline"],
            "success_criteria": (
                "Every exception transaction is listed with a per-vendor total."
            ),
            "critical": True,
            "requires_signoff": False,
        },
        {
            "task_id": "t3_vendor_precedent",
            "description": (
                "Check the flagged vendors against prior-period audit memos and "
                "durable findings."
            ),
            "agent": "research",
            "depends_on": ["t2_exception_scan"],
            "success_criteria": (
                "Each flagged vendor is matched to prior findings or explicitly "
                "reported as new."
            ),
            "critical": True,
            "requires_signoff": False,
        },
        {
            "task_id": "t4_ledger_verification",
            "description": (
                "Verify the largest flagged vendor's exposure against the "
                "booked ledger balance for FY26-Q3."
            ),
            "agent": "data",
            "depends_on": ["t2_exception_scan"],
            "success_criteria": (
                "The exception total is reconciled against the ledger balance."
            ),
            "critical": True,
            "requires_signoff": False,
        },
        {
            "task_id": "t5_peer_benchmark",
            "description": (
                "Benchmark the flagged vendor category spend against peer "
                "organisations."
            ),
            "agent": "research",
            "depends_on": ["t2_exception_scan"],
            "success_criteria": "A peer benchmark is reported for the category.",
            "critical": False,
            "requires_signoff": False,
        },
        {
            "task_id": "t6_audit_report",
            "description": (
                "Synthesise the FY26-Q3 audit review for reviewer sign-off."
            ),
            "agent": "reporting",
            "depends_on": [
                "t1_policy_baseline",
                "t2_exception_scan",
                "t3_vendor_precedent",
                "t4_ledger_verification",
                "t5_peer_benchmark",
            ],
            "success_criteria": (
                "The report states every exception, its policy basis, and every "
                "open item."
            ),
            "critical": True,
            "requires_signoff": True,
        },
    ],
}

AUDIT: dict[str, Any] = {
    "planner.plan": AUDIT_PLAN,
    "research.t1_policy_baseline.plan": {
        "calls": [
            {
                "tool": "doc_search",
                "arguments": [
                    {
                        "name": "query",
                        "value": (
                            "dual approval threshold unapproved exposure "
                            "control exception reporting"
                        ),
                    },
                    {"name": "limit", "value": "4"},
                ],
                "purpose": "Find the approval threshold and exception rules.",
            }
        ]
    },
    "research.t1_policy_baseline.result": {
        "summary": (
            "POL-014 requires dual approval for any single vendor transaction of "
            "25,000 USD or more; such a transaction without an AP reference is a "
            "control exception. Exceptions are reported per vendor as unapproved "
            "exposure for the period under review, and a single vendor above "
            "50,000 USD requires CFO sign-off before the review closes."
        ),
        "claims": [
            {
                "subject": "POL-014",
                "metric": "dual_approval_threshold_usd",
                "value": "25000.00",
            },
            {
                "subject": "POL-014",
                "metric": "cfo_signoff_threshold_usd",
                "value": "50000.00",
            },
        ],
        "confidence": 0.94,
        "evidence": ["expense-approval.md", "fiscal-calendar.md"],
    },
    "data.t2_exception_scan.plan": {
        "calls": [
            {
                "tool": "sql_query",
                "arguments": [
                    {"name": "query", "value": "unapproved_above_threshold"},
                    {"name": "quarter", "value": "FY26-Q3"},
                    {"name": "threshold", "value": "25000"},
                ],
                "purpose": "List the FY26-Q3 exception transactions.",
            },
            {
                "tool": "sql_query",
                "arguments": [
                    {"name": "query", "value": "vendor_unapproved_exposure"},
                    {"name": "quarter", "value": "FY26-Q3"},
                    {"name": "threshold", "value": "25000"},
                ],
                "purpose": "Total the exceptions per vendor.",
            },
        ]
    },
    "data.t2_exception_scan.result": {
        "summary": (
            "FY26-Q3 has three control exceptions at or above 25,000 USD with no "
            "approval reference: T-3001 (28,400) and T-3002 (27,300) for "
            "Northwind Logistics, and T-3005 (31,200) for Meridian Consulting. "
            "Unapproved exposure is 55,700 USD for Northwind and 31,200 USD for "
            "Meridian, 86,900 USD for the quarter."
        ),
        "claims": [
            {
                "subject": "Northwind Logistics",
                "metric": "unapproved_exposure_usd",
                "value": "55700.00",
            },
            {
                "subject": "Meridian Consulting",
                "metric": "unapproved_exposure_usd",
                "value": "31200.00",
            },
            {
                "subject": "FY26-Q3",
                "metric": "total_unapproved_exposure_usd",
                "value": "86900.00",
            },
        ],
        "confidence": 0.93,
        "evidence": [
            "sql:unapproved_above_threshold",
            "sql:vendor_unapproved_exposure",
            "T-3001",
            "T-3002",
            "T-3005",
        ],
    },
    "research.t3_vendor_precedent.plan": {
        "calls": [
            {
                "tool": "doc_search",
                "arguments": [
                    {
                        "name": "query",
                        "value": (
                            "Northwind Logistics Meridian Consulting unapproved "
                            "exposure carried forward memo review"
                        ),
                    },
                    {"name": "limit", "value": "3"},
                ],
                "purpose": "Find prior-period findings for the flagged vendors.",
            }
        ]
    },
    "research.t3_vendor_precedent.result": {
        "summary": (
            "Both flagged vendors appear in the FY26-Q2 audit memo. It records "
            "unapproved exposure of 82,600 USD for Northwind Logistics and an "
            "outstanding quarterly review for Meridian Consulting, a high-tier "
            "vendor."
        ),
        "claims": [
            {
                "subject": "Northwind Logistics",
                "metric": "unapproved_exposure_usd",
                "value": "82600.00",
            },
            {
                "subject": "Meridian Consulting",
                "metric": "quarterly_review_status",
                "value": "outstanding",
            },
        ],
        "confidence": 0.62,
        "evidence": ["audit-memo-fy26-q2.md", "vendor-risk.md"],
    },
    "research.t3_vendor_precedent.plan@2": {
        "calls": [
            {
                "tool": "doc_search",
                "arguments": [
                    {
                        "name": "query",
                        "value": (
                            "fiscal calendar FY26 quarter periods cumulative "
                            "fy-to-date figures not comparable"
                        ),
                    },
                    {"name": "limit", "value": "3"},
                ],
                "purpose": "Check which period the memo figure covers.",
            }
        ]
    },
    "research.t3_vendor_precedent.result@2": {
        "summary": (
            "The 82,600 USD in the FY26-Q2 memo is an FY-to-date cumulative "
            "figure, which POL-002 states is not comparable with a "
            "single-quarter figure. For FY26-Q3 alone the Northwind exposure is "
            "55,700 USD, with 26,900 USD carried forward from FY26-Q2. The "
            "Meridian quarterly review remains outstanding."
        ),
        "claims": [
            {
                "subject": "Northwind Logistics",
                "metric": "unapproved_exposure_usd",
                "value": "55700.00",
            },
            {
                "subject": "Northwind Logistics",
                "metric": "prior_period_carryforward_usd",
                "value": "26900.00",
            },
            {
                "subject": "Meridian Consulting",
                "metric": "quarterly_review_status",
                "value": "outstanding",
            },
        ],
        "confidence": 0.88,
        "evidence": [
            "audit-memo-fy26-q2.md",
            "fiscal-calendar.md",
            "vendor-risk.md",
        ],
    },
    "data.t4_ledger_verification.plan": {
        "calls": [
            {
                "tool": "ledger_lookup",
                "arguments": [
                    {"name": "vendor", "value": "Northwind Logistics"},
                    {"name": "quarter", "value": "FY26-Q3"},
                ],
                "purpose": "Reconcile the exception total against the ledger.",
            }
        ]
    },
    "data.t4_ledger_verification.result": {
        "summary": (
            "Northwind Logistics has 95,500 USD booked for FY26-Q3. The 55,700 "
            "USD of unapproved exposure is 58.3 percent of the booked balance, "
            "so the exceptions are consistent with the ledger rather than a "
            "duplicate posting."
        ),
        "claims": [
            {
                "subject": "Northwind Logistics",
                "metric": "ledger_balance_usd",
                "value": "95500.00",
            },
            {
                "subject": "Northwind Logistics",
                "metric": "unapproved_share_of_booked_pct",
                "value": "58.3",
            },
        ],
        "confidence": 0.90,
        "evidence": ["ledger:V-101:FY26-Q3"],
    },
    "research.t5_peer_benchmark.plan": {
        "calls": [
            {
                "tool": "peer_benchmark",
                "arguments": [{"name": "category", "value": "freight"}],
                "purpose": "Compare freight spend against peers.",
            }
        ]
    },
    "reporting.t6_audit_report.plan": {
        "calls": [
            {
                "tool": "doc_search",
                "arguments": [
                    {
                        "name": "query",
                        "value": "CFO sign-off exceptions above 50000 reporting",
                    },
                    {"name": "limit", "value": "2"},
                ],
                "purpose": "Confirm the sign-off rule to cite in the report.",
            }
        ]
    },
    "reporting.t6_audit_report.draft": {
        "title": "FY26-Q3 Quarterly Audit Review - Draft for Sign-off",
        "summary": (
            "Three control exceptions totalling 86,900 USD were identified in "
            "FY26-Q3 across two vendors. Northwind Logistics at 55,700 USD "
            "exceeds the 50,000 USD single-vendor threshold and therefore "
            "requires CFO sign-off. One contradiction with the FY26-Q2 memo was "
            "resolved as a reporting-period difference; the peer benchmark could "
            "not be retrieved."
        ),
        "sections": [
            {
                "heading": "Executive summary",
                "body": (
                    "FY26-Q3 contains three transactions at or above the 25,000 "
                    "USD dual-approval threshold (POL-014) with no approval "
                    "reference, totalling 86,900 USD.\n\n"
                    "- Northwind Logistics (V-101): 55,700 USD across T-3001 "
                    "(28,400) and T-3002 (27,300). Above the 50,000 USD "
                    "single-vendor threshold, so CFO sign-off is required before "
                    "this review closes.\n"
                    "- Meridian Consulting (V-103): 31,200 USD in T-3005. "
                    "High-risk tier with a quarterly review still outstanding "
                    "under POL-021."
                ),
            },
            {
                "heading": "Findings and evidence",
                "body": (
                    "| Finding | Value | Source |\n"
                    "|---|---|---|\n"
                    "| Dual-approval threshold | 25,000 USD | expense-approval.md "
                    "(t1_policy_baseline) |\n"
                    "| Northwind unapproved exposure, FY26-Q3 | 55,700 USD | "
                    "warehouse scan (t2_exception_scan) |\n"
                    "| Northwind booked balance, FY26-Q3 | 95,500 USD | vendor "
                    "ledger (t4_ledger_verification) |\n"
                    "| Meridian unapproved exposure, FY26-Q3 | 31,200 USD | "
                    "warehouse scan (t2_exception_scan) |\n"
                    "| Northwind carry-forward from FY26-Q2 | 26,900 USD | "
                    "audit-memo-fy26-q2.md (t3_vendor_precedent) |\n\n"
                    "The unapproved exposure represents 58.3 percent of "
                    "Northwind's booked balance for the quarter, consistent with "
                    "an approval-workflow timing gap rather than duplicate "
                    "postings."
                ),
            },
            {
                "heading": "Control gaps and open items",
                "body": (
                    "1. Resolved contradiction: the FY26-Q2 memo reports 82,600 "
                    "USD for Northwind Logistics. Re-query established this is "
                    "an FY-to-date cumulative figure and not comparable with a "
                    "single quarter (POL-002); the FY26-Q3 figure is 55,700 "
                    "USD.\n"
                    "2. Degraded task: t5_peer_benchmark could not complete. The "
                    "peer-benchmark provider was unreachable across all "
                    "attempts, so no external comparison is included.\n"
                    "3. Requires human judgement: CFO sign-off on the Northwind "
                    "exposure, and scheduling of the outstanding Meridian "
                    "quarterly review."
                ),
            },
        ],
        "key_claims": [
            {
                "subject": "Northwind Logistics",
                "metric": "unapproved_exposure_usd",
                "value": "55700.00",
            },
            {
                "subject": "Northwind Logistics",
                "metric": "requires_cfo_signoff",
                "value": "true",
            },
            {
                "subject": "Meridian Consulting",
                "metric": "unapproved_exposure_usd",
                "value": "31200.00",
            },
            {
                "subject": "FY26-Q3",
                "metric": "total_unapproved_exposure_usd",
                "value": "86900.00",
            },
        ],
        "confidence": 0.87,
        "open_questions": [
            "CFO sign-off outstanding for Northwind Logistics exposure.",
            "No peer benchmark available for the freight category.",
        ],
    },
}

_OUTAGE_PLAN: dict[str, Any] = {
    "rationale": (
        "The vendor ledger is unavailable, so the exception totals are "
        "cross-checked against the prior-period memo instead of the ledger."
    ),
    "tasks": [
        deepcopy(task)
        for task in AUDIT_PLAN["tasks"]
        if task["task_id"] != "t4_ledger_verification"
    ]
    + [
        {
            "task_id": "t4b_memo_crosscheck",
            "description": (
                "Cross-check the flagged vendor exposure against the "
                "prior-period audit memo, the ledger being unavailable."
            ),
            "agent": "research",
            "depends_on": ["t2_exception_scan"],
            "success_criteria": (
                "The exception total is corroborated by a documented source and "
                "the missing ledger verification is stated."
            ),
            "critical": True,
            "requires_signoff": False,
        }
    ],
}

for _task in _OUTAGE_PLAN["tasks"]:
    if _task["task_id"] == "t6_audit_report":
        _task["depends_on"] = [
            "t1_policy_baseline",
            "t2_exception_scan",
            "t3_vendor_precedent",
            "t4b_memo_crosscheck",
            "t5_peer_benchmark",
        ]

AUDIT_OUTAGE: dict[str, Any] = {
    **AUDIT,
    "planner.replan@2": _OUTAGE_PLAN,
    "research.t4b_memo_crosscheck.plan": {
        "calls": [
            {
                "tool": "doc_search",
                "arguments": [
                    {
                        "name": "query",
                        "value": "Northwind Logistics freight invoices approval workflow memo",
                    },
                    {"name": "limit", "value": "2"},
                ],
                "purpose": "Corroborate the exception total from documents.",
            }
        ]
    },
    "research.t4b_memo_crosscheck.result": {
        "summary": (
            "The vendor ledger is unavailable, so the FY26-Q3 exception total "
            "could not be reconciled against booked balances. The FY26-Q2 memo "
            "independently documents the same pattern for Northwind Logistics - "
            "freight invoices booked before the approval workflow completes - "
            "which corroborates the exceptions without confirming the amounts."
        ),
        "claims": [
            {
                "subject": "Northwind Logistics",
                "metric": "ledger_verification_status",
                "value": "unavailable",
            },
            {
                "subject": "Northwind Logistics",
                "metric": "exception_cause",
                "value": "invoice_booked_before_approval",
            },
        ],
        "confidence": 0.71,
        "evidence": ["audit-memo-fy26-q2.md"],
    },
    "reporting.t6_audit_report.draft": {
        "title": "FY26-Q3 Quarterly Audit Review - Draft for Sign-off",
        "summary": (
            "Three control exceptions totalling 86,900 USD were identified in "
            "FY26-Q3 across two vendors. Northwind Logistics at 55,700 USD "
            "exceeds the 50,000 USD single-vendor threshold and requires CFO "
            "sign-off. The vendor ledger was unavailable, so the totals are "
            "corroborated by documentation only and remain unreconciled."
        ),
        "sections": [
            {
                "heading": "Executive summary",
                "body": (
                    "FY26-Q3 contains three transactions at or above the 25,000 "
                    "USD dual-approval threshold (POL-014) with no approval "
                    "reference, totalling 86,900 USD.\n\n"
                    "- Northwind Logistics (V-101): 55,700 USD across T-3001 "
                    "(28,400) and T-3002 (27,300). Above the 50,000 USD "
                    "single-vendor threshold, so CFO sign-off is required.\n"
                    "- Meridian Consulting (V-103): 31,200 USD in T-3005. "
                    "High-risk tier with a quarterly review still outstanding "
                    "under POL-021."
                ),
            },
            {
                "heading": "Findings and evidence",
                "body": (
                    "| Finding | Value | Source |\n"
                    "|---|---|---|\n"
                    "| Dual-approval threshold | 25,000 USD | expense-approval.md "
                    "(t1_policy_baseline) |\n"
                    "| Northwind unapproved exposure, FY26-Q3 | 55,700 USD | "
                    "warehouse scan (t2_exception_scan) |\n"
                    "| Meridian unapproved exposure, FY26-Q3 | 31,200 USD | "
                    "warehouse scan (t2_exception_scan) |\n"
                    "| Northwind carry-forward from FY26-Q2 | 26,900 USD | "
                    "audit-memo-fy26-q2.md (t3_vendor_precedent) |\n"
                    "| Exception cause | invoices booked before approval "
                    "completes | audit-memo-fy26-q2.md (t4b_memo_crosscheck) |"
                ),
            },
            {
                "heading": "Control gaps and open items",
                "body": (
                    "1. Unreconciled: the vendor ledger was unavailable for the "
                    "whole run, so the exception totals could not be checked "
                    "against booked balances. The plan was revised to "
                    "cross-check documentation instead (t4b_memo_crosscheck, "
                    "confidence 0.71), which corroborates the pattern but not "
                    "the amounts.\n"
                    "2. Resolved contradiction: the FY26-Q2 memo reports 82,600 "
                    "USD for Northwind Logistics. Re-query established this is "
                    "an FY-to-date cumulative figure, not comparable with a "
                    "single quarter (POL-002).\n"
                    "3. Degraded task: t5_peer_benchmark could not complete; the "
                    "peer-benchmark provider was unreachable.\n"
                    "4. Requires human judgement: CFO sign-off on the Northwind "
                    "exposure, and a ledger reconciliation once the service is "
                    "restored."
                ),
            },
        ],
        "key_claims": [
            {
                "subject": "Northwind Logistics",
                "metric": "unapproved_exposure_usd",
                "value": "55700.00",
            },
            {
                "subject": "Meridian Consulting",
                "metric": "unapproved_exposure_usd",
                "value": "31200.00",
            },
            {
                "subject": "FY26-Q3",
                "metric": "total_unapproved_exposure_usd",
                "value": "86900.00",
            },
        ],
        "confidence": 0.74,
        "open_questions": [
            "Ledger reconciliation outstanding: vendor ledger unavailable.",
            "CFO sign-off outstanding for Northwind Logistics exposure.",
        ],
    },
}

# The support desk is the headline demo because it needs no domain
# knowledge; audit is kept because it shows more of the machinery.
from aios.demo.support import SCENARIO as SUPPORT  # noqa: E402

SCENARIOS: dict[str, dict[str, Any]] = {
    "support": SUPPORT,
    "audit": AUDIT,
    "audit_outage": AUDIT_OUTAGE,
}
