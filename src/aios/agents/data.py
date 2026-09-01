"""Data agent: warehouse and ledger queries."""

from __future__ import annotations

from typing import ClassVar

from aios.agents.base import Agent
from aios.orchestration.state import AgentType

ROLE_PROMPT = """You are the Data agent of an enterprise audit platform.

You answer questions from the transaction warehouse and the vendor ledger using
named, parameterized queries. Report only figures the query results support, and
state the quarter every figure covers. If a threshold matters to the question,
take it from the task context rather than assuming one.
"""


class DataAgent(Agent):
    agent_type: ClassVar[AgentType] = AgentType.DATA
    role_prompt: ClassVar[str] = ROLE_PROMPT
