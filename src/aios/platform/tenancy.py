"""Tenant provisioning and tool grants.

A grant is data, not code. Two tenants can run the same domain with different
tools, different limits and different credentials, and the gateway checks the
grant on every call. A tool with no grant is simply not reachable - there is no
implicit default, because an implicit default is how an agent ends up with access
nobody decided to give it.
"""

from __future__ import annotations

from aios.mcp_gateway.registry import PrincipalKind, ToolSpec
from aios.orchestration.state import AgentType
from aios.platform.models import TenantPolicy, ToolGrant
from aios.platform.protocols import PolicyStore

RESEARCH = AgentType.RESEARCH.value
DATA = AgentType.DATA.value
REPORTING = AgentType.REPORTING.value

# What a tenant gets when it is provisioned for the audit domain.
DEFAULT_GRANTS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("sql_query", (DATA,), ("agent",)),
    ("ledger_lookup", (DATA,), ("agent",)),
    ("doc_search", (RESEARCH, REPORTING), ("agent",)),
    ("peer_benchmark", (RESEARCH,), ("agent",)),
    ("render_report", (REPORTING,), ("agent",)),
    ("record_signoff", (), ("human",)),
)


def grants_from_tools(tenant_id: str, tools: list[ToolSpec]) -> list[ToolGrant]:
    """Derive a tenant's grants from the tools a domain actually publishes.

    A tool already declares which agents may call it; provisioning records that
    as the starting grant, which an administrator can then tighten. Without
    this, a domain that introduces a new tool would publish it and the gateway
    would refuse it, because no grant names it.
    """
    return [
        ToolGrant(
            tenant_id=tenant_id,
            tool=tool.name,
            allowed_agents=tuple(sorted(tool.allowed_agents)),
            allowed_kinds=tuple(sorted(kind.value for kind in tool.allowed_kinds)),
            timeout_seconds=tool.timeout_seconds or 20.0,
        )
        for tool in tools
    ]


def provision(
    policy: PolicyStore,
    tenant: TenantPolicy,
    grants: tuple[str, ...] | None = None,
    tools: list[ToolSpec] | None = None,
) -> TenantPolicy:
    """Create a tenant and install its tool grants.

    With `tools`, grants are derived from that registry - which is how a domain
    pack's own tools become reachable. Otherwise the built-in demo grants apply.
    """
    policy.upsert_tenant(tenant)
    if tools is not None:
        for grant in grants_from_tools(tenant.tenant_id, tools):
            if grants is not None and grant.tool not in set(grants):
                continue
            policy.upsert_grant(grant)
        return tenant

    wanted = set(grants) if grants is not None else None
    for tool, agents, kinds in DEFAULT_GRANTS:
        if wanted is not None and tool not in wanted:
            continue
        policy.upsert_grant(
            ToolGrant(
                tenant_id=tenant.tenant_id,
                tool=tool,
                allowed_agents=agents,
                allowed_kinds=kinds,
            )
        )
    return tenant


def apply_grants(tools: list[ToolSpec], grants: list[ToolGrant]) -> list[ToolSpec]:
    """Rebuild the tool registry from the tenant's grants.

    A tool the tenant has no grant for is dropped, so it cannot be called at all
    rather than being merely denied per principal.
    """
    by_name = {grant.tool: grant for grant in grants}
    granted: list[ToolSpec] = []
    for tool in tools:
        grant = by_name.get(tool.name)
        if grant is None:
            continue
        granted.append(
            ToolSpec(
                name=tool.name,
                description=tool.description,
                handler=tool.handler,
                allowed_agents=frozenset(grant.allowed_agents),
                allowed_kinds=frozenset(
                    PrincipalKind(kind) for kind in grant.allowed_kinds
                ),
                timeout_seconds=grant.timeout_seconds,
            )
        )
    return granted
