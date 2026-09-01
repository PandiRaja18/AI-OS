"""Research agent: policy, precedent and external context."""

from __future__ import annotations

from typing import ClassVar

from aios.agents.base import Agent
from aios.orchestration.state import AgentType

ROLE_PROMPT = """You are the Research agent of an enterprise audit platform.

You establish what the rules say and what prior periods already found. You read
policy documents, audit memos and external benchmarks. You never compute figures
from the warehouse yourself - that is the Data agent's job - but you do report
figures stated in the documents you read, and you always say which period a
stated figure covers.
"""


class ResearchAgent(Agent):
    agent_type: ClassVar[AgentType] = AgentType.RESEARCH
    role_prompt: ClassVar[str] = ROLE_PROMPT
