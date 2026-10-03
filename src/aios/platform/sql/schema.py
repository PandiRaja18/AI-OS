"""Schema for the platform stores.

Every table carries `tenant_id` and every read path filters on it. SQLite has no
row-level security, so isolation is enforced in the store layer; on Postgres the
same predicate becomes an RLS policy and the store code is unchanged.
"""

from __future__ import annotations

from aios.platform.sql.engine import Database, Dialect

TABLES = (
    """
    CREATE TABLE IF NOT EXISTS tenants (
        tenant_id            TEXT PRIMARY KEY,
        name                 TEXT NOT NULL DEFAULT '',
        max_concurrent_runs  INTEGER NOT NULL DEFAULT 4,
        default_budget       TEXT NOT NULL,
        monthly_currency_cap TEXT NOT NULL DEFAULT '500.000000',
        review_ttl_hours     INTEGER NOT NULL DEFAULT 24,
        reviewers            TEXT NOT NULL DEFAULT '[]'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
        run_id            TEXT PRIMARY KEY,
        tenant_id         TEXT NOT NULL,
        submitted_by      TEXT NOT NULL,
        goal              TEXT NOT NULL,
        domain            TEXT NOT NULL DEFAULT 'audit',
        scenario          TEXT NOT NULL DEFAULT 'audit',
        offline           INTEGER NOT NULL DEFAULT 1,
        status            TEXT NOT NULL,
        lane              TEXT NOT NULL DEFAULT 'interactive',
        priority          INTEGER NOT NULL DEFAULT 100,
        budget            TEXT NOT NULL,
        spend             TEXT NOT NULL,
        worker_id         TEXT,
        lease_expires_at  TEXT,
        lease_attempts    INTEGER NOT NULL DEFAULT 0,
        report_uri        TEXT,
        failure_reason    TEXT,
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL,
        deadline_at       TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS task_attempts (
        attempt_id    TEXT PRIMARY KEY,
        run_id        TEXT NOT NULL,
        tenant_id     TEXT NOT NULL,
        task_id       TEXT NOT NULL,
        attempt       INTEGER NOT NULL,
        agent         TEXT NOT NULL,
        outcome       TEXT NOT NULL,
        error         TEXT,
        result_uri    TEXT,
        confidence    REAL,
        input_tokens  INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        duration_ms   INTEGER NOT NULL DEFAULT 0,
        at            TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trace_events (
        event_id    TEXT PRIMARY KEY,
        run_id      TEXT NOT NULL,
        tenant_id   TEXT NOT NULL,
        seq         INTEGER NOT NULL,
        kind        TEXT NOT NULL,
        actor       TEXT NOT NULL,
        task_id     TEXT,
        message     TEXT NOT NULL,
        duration_ms INTEGER,
        detail      TEXT NOT NULL DEFAULT '{}',
        at          TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS facts (
        fact_id        TEXT PRIMARY KEY,
        tenant_id      TEXT NOT NULL,
        subject        TEXT NOT NULL,
        metric         TEXT NOT NULL,
        value          TEXT NOT NULL,
        confidence     REAL NOT NULL,
        provenance_run TEXT NOT NULL,
        valid_from     TEXT NOT NULL,
        valid_to       TEXT,
        superseded_by  TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reviews (
        review_id       TEXT PRIMARY KEY,
        run_id          TEXT NOT NULL,
        tenant_id       TEXT NOT NULL,
        goal            TEXT NOT NULL,
        task_ids        TEXT NOT NULL DEFAULT '[]',
        draft           TEXT NOT NULL DEFAULT '',
        report_uri      TEXT,
        open_conflicts  INTEGER NOT NULL DEFAULT 0,
        degraded_tasks  INTEGER NOT NULL DEFAULT 0,
        assigned_to     TEXT NOT NULL DEFAULT '[]',
        status          TEXT NOT NULL,
        decision        TEXT,
        created_at      TEXT NOT NULL,
        expires_at      TEXT NOT NULL,
        decided_at      TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tool_grants (
        tenant_id            TEXT NOT NULL,
        tool                 TEXT NOT NULL,
        allowed_agents       TEXT NOT NULL DEFAULT '[]',
        allowed_kinds        TEXT NOT NULL DEFAULT '["agent"]',
        rate_limit_per_minute INTEGER,
        timeout_seconds      REAL NOT NULL DEFAULT 20.0,
        secret_ref           TEXT,
        PRIMARY KEY (tenant_id, tool)
    )
    """,
)

INDEXES = (
    "CREATE INDEX IF NOT EXISTS runs_queue ON runs (status, priority, created_at)",
    "CREATE INDEX IF NOT EXISTS runs_tenant ON runs (tenant_id, status)",
    "CREATE INDEX IF NOT EXISTS runs_lease ON runs (status, lease_expires_at)",
    "CREATE INDEX IF NOT EXISTS attempts_run ON task_attempts (run_id, at)",
    "CREATE INDEX IF NOT EXISTS trace_run ON trace_events (run_id, seq)",
    "CREATE INDEX IF NOT EXISTS trace_tenant ON trace_events (tenant_id, kind)",
    "CREATE INDEX IF NOT EXISTS facts_lookup ON facts (tenant_id, subject, metric)",
    "CREATE INDEX IF NOT EXISTS facts_active ON facts (tenant_id, superseded_by)",
    "CREATE INDEX IF NOT EXISTS reviews_inbox ON reviews (tenant_id, status)",
    "CREATE UNIQUE INDEX IF NOT EXISTS reviews_run ON reviews (run_id)",
)


def create_schema(database: Database) -> None:
    """Create every table and index. Safe to call on every start."""
    statements = list(TABLES) + list(INDEXES)
    if database.dialect is Dialect.POSTGRES:
        statements = [
            statement.replace("INTEGER NOT NULL DEFAULT 1", "BOOLEAN NOT NULL DEFAULT TRUE")
            for statement in statements
        ]
    database.script(statements)
