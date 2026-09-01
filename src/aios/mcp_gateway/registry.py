"""Tool registry and principals.

A tool declares which principals may call it; the gateway checks that on every
call. A principal is built from an authenticated identity (an agent's own type,
or the reviewer a CLI session logged in as) and is never read from call
arguments.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from aios.orchestration.state import AgentType


class PrincipalKind(enum.StrEnum):
    HUMAN = "human"
    AGENT = "agent"
    SERVICE = "service"


@dataclass(frozen=True)
class Principal:
    """The authenticated caller of a tool."""

    kind: PrincipalKind
    name: str

    @classmethod
    def for_agent(cls, agent: AgentType) -> "Principal":
        return cls(PrincipalKind.AGENT, agent.value)

    @classmethod
    def human(cls, reviewer: str) -> "Principal":
        return cls(PrincipalKind.HUMAN, reviewer)

    def __str__(self) -> str:
        return f"{self.kind.value}:{self.name}"


ToolHandler = Callable[..., Any]


@dataclass(frozen=True)
class ToolSpec:
    """A tool exposed through the gateway."""

    name: str
    description: str
    handler: ToolHandler
    allowed_agents: frozenset[str] = field(default_factory=frozenset)
    allowed_kinds: frozenset[PrincipalKind] = field(
        default_factory=lambda: frozenset({PrincipalKind.AGENT})
    )
    timeout_seconds: float | None = None

    def permits(self, principal: Principal) -> bool:
        """Whether `principal` may call this tool."""
        if principal.kind not in self.allowed_kinds:
            return False
        if principal.kind is PrincipalKind.AGENT:
            return principal.name in self.allowed_agents
        return True
