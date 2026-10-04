"""Picks the tool registry for a run: a configured domain, or the demo.

Keeping this choice in one function means the runtime and the platform
orchestrator cannot drift apart on which tools a run actually gets.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from aios.config import Settings
from aios.domain import DomainPack
from aios.mcp_gateway.registry import ToolSpec


def build_run_tools(
    settings: Settings,
    run_id: str,
    report_writer: Callable[[str, Sequence[dict[str, str]]], str] | None = None,
    pack: DomainPack | None = None,
) -> list[ToolSpec]:
    """Domain tools when a pack is configured, the demo registry otherwise."""
    if pack is not None:
        from aios.mcp_gateway.domain_tools import build_domain_tools

        return build_domain_tools(pack, settings, run_id, report_writer)

    from aios.mcp_gateway.tools import build_tools

    return build_tools(settings, run_id, report_writer)
