"""MCP gateway: the only path from an agent to the outside world."""

from aios.mcp_gateway.gateway import (
    McpGateway,
    ToolAccessDenied,
    ToolError,
    ToolTimeout,
)
from aios.mcp_gateway.registry import Principal, PrincipalKind, ToolSpec
from aios.mcp_gateway.run_tools import build_run_tools

__all__ = [
    "McpGateway",
    "Principal",
    "PrincipalKind",
    "ToolAccessDenied",
    "ToolError",
    "ToolSpec",
    "ToolTimeout",
    "build_run_tools",
]
