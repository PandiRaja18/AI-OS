"""Per-tenant policy: budgets, concurrency and tool grants.

In v1 the tool registry was hard-coded in `build_tools`. Here a grant is a row,
so two tenants can run the same domain with different tools, different rate
limits and different credentials, and the gateway checks the tenant's grant on
every call rather than trusting a process-wide table.
"""

from __future__ import annotations

from decimal import Decimal

from aios.platform.models import Budget, TenantPolicy, ToolGrant
from aios.platform.sql.engine import (
    Database,
    from_list,
    from_money,
    model_json,
    parse_model,
    to_list,
    to_money,
)


class SqlPolicyStore:
    """Tenant limits and tool grants."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def tenant(self, tenant_id: str) -> TenantPolicy | None:
        row = self._db.one(
            "SELECT * FROM tenants WHERE tenant_id = ?", (tenant_id,)
        )
        if row is None:
            return None
        return TenantPolicy(
            tenant_id=row["tenant_id"],
            name=row["name"],
            max_concurrent_runs=row["max_concurrent_runs"],
            default_budget=parse_model(Budget, row["default_budget"]),
            monthly_currency_cap=from_money(row["monthly_currency_cap"]),
            review_ttl_hours=row["review_ttl_hours"],
            reviewers=from_list(row["reviewers"]),
        )

    def tenants(self) -> list[TenantPolicy]:
        rows = self._db.query("SELECT tenant_id FROM tenants ORDER BY tenant_id")
        return [self.tenant(row["tenant_id"]) for row in rows]

    def upsert_tenant(self, policy: TenantPolicy) -> None:
        self._db.execute("DELETE FROM tenants WHERE tenant_id = ?", (policy.tenant_id,))
        self._db.execute(
            """
            INSERT INTO tenants (
                tenant_id, name, max_concurrent_runs, default_budget,
                monthly_currency_cap, review_ttl_hours, reviewers
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (
                policy.tenant_id,
                policy.name,
                policy.max_concurrent_runs,
                model_json(policy.default_budget),
                to_money(policy.monthly_currency_cap),
                policy.review_ttl_hours,
                to_list(policy.reviewers),
            ),
        )

    def grants(self, tenant_id: str) -> list[ToolGrant]:
        rows = self._db.query(
            "SELECT * FROM tool_grants WHERE tenant_id = ? ORDER BY tool", (tenant_id,)
        )
        return [
            ToolGrant(
                tenant_id=row["tenant_id"],
                tool=row["tool"],
                allowed_agents=from_list(row["allowed_agents"]),
                allowed_kinds=from_list(row["allowed_kinds"]),
                rate_limit_per_minute=row["rate_limit_per_minute"],
                timeout_seconds=row["timeout_seconds"],
                secret_ref=row["secret_ref"],
            )
            for row in rows
        ]

    def upsert_grant(self, grant: ToolGrant) -> None:
        self._db.execute(
            "DELETE FROM tool_grants WHERE tenant_id = ? AND tool = ?",
            (grant.tenant_id, grant.tool),
        )
        self._db.execute(
            """
            INSERT INTO tool_grants (
                tenant_id, tool, allowed_agents, allowed_kinds,
                rate_limit_per_minute, timeout_seconds, secret_ref
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (
                grant.tenant_id,
                grant.tool,
                to_list(grant.allowed_agents),
                to_list(grant.allowed_kinds),
                grant.rate_limit_per_minute,
                grant.timeout_seconds,
                grant.secret_ref,
            ),
        )

    def revoke_grant(self, tenant_id: str, tool: str) -> None:
        self._db.execute(
            "DELETE FROM tool_grants WHERE tenant_id = ? AND tool = ?",
            (tenant_id, tool),
        )

    def default_budget(self, tenant_id: str) -> Budget:
        policy = self.tenant(tenant_id)
        return policy.default_budget if policy else Budget()

    def monthly_cap(self, tenant_id: str) -> Decimal:
        policy = self.tenant(tenant_id)
        return policy.monthly_currency_cap if policy else Decimal("0")
