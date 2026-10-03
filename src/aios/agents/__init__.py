"""Worker agents and the registry the coordinator dispatches through."""

from __future__ import annotations

from aios.agents.base import Agent, ToolCall, ToolPlan
from aios.agents.data import DataAgent
from aios.agents.reporting import ReportDraft, ReportingAgent
from aios.agents.research import ResearchAgent
from aios.llm import LlmClient
from aios.mcp_gateway import McpGateway
from aios.observability import TraceStore
from aios.orchestration.state import AgentType

AGENT_CLASSES: tuple[type[Agent], ...] = (
    ResearchAgent,
    DataAgent,
    ReportingAgent,
)


def build_agents(
    llm: LlmClient, gateway: McpGateway, trace: TraceStore
) -> dict[AgentType, Agent]:
    """Instantiate one agent per worker type."""
    return {
        agent_class.agent_type: agent_class(llm, gateway, trace)
        for agent_class in AGENT_CLASSES
    }


__all__ = [
    "AGENT_CLASSES",
    "Agent",
    "DataAgent",
    "ReportDraft",
    "ReportingAgent",
    "ResearchAgent",
    "ToolCall",
    "ToolPlan",
    "build_agents",
]
