"""Chooses which model backs a run.

Three providers, one interface. `replay` serves recorded answers and is what the
demos and the test suite use. `claude` calls the hosted API. `local` calls a
self-hosted server - vLLM, llama.cpp, Ollama, TGI, LM Studio - through the same
structured-output contract.

Nothing above this module knows which one is in play.
"""

from __future__ import annotations

import enum

from aios.config import Settings
from aios.demo.replay import SCENARIOS
from aios.llm import ClaudeClient, LlmClient, ReplayClient
from aios.llm_local import LocalLlmClient
from aios.observability import TraceStore


class Provider(enum.StrEnum):
    REPLAY = "replay"
    CLAUDE = "claude"
    LOCAL = "local"


def model_label(settings: Settings, offline: bool) -> str:
    """The name spend is priced against. A `local:` prefix prices at zero."""
    if offline:
        return "replay"
    if Provider(settings.llm_provider) is Provider.LOCAL:
        return f"local:{settings.llm_model}"
    return settings.model


def build_llm(
    settings: Settings,
    trace: TraceStore,
    offline: bool = False,
    scenario: str = "audit",
) -> LlmClient:
    """Build the model client this run should use."""
    if offline:
        recording = SCENARIOS.get(scenario)
        if recording is None:
            raise KeyError(
                f"unknown scenario {scenario!r}; available: {sorted(SCENARIOS)}"
            )
        return ReplayClient(recording, trace)

    provider = Provider(settings.llm_provider)
    if provider is Provider.LOCAL:
        return LocalLlmClient(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            trace=trace,
            api_style=settings.llm_api_style,
            api_key=settings.llm_api_key,
            timeout=settings.llm_timeout_seconds,
            max_repairs=settings.llm_max_repairs,
        )
    return ClaudeClient(settings.model, trace)
