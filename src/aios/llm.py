"""Typed LLM access.

Every model call in the platform goes through `LlmClient.structured`, which
returns a validated Pydantic object rather than free text. Two implementations
exist: `ClaudeClient` for live runs, and `ReplayClient`, which serves recorded
responses so the demo is reproducible offline and the graph is testable without
network access.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from aios.observability import EventKind, TraceStore

ModelT = TypeVar("ModelT", bound=BaseModel)

MAX_TOKENS = 8000


class Usage(BaseModel):
    """Tokens consumed by one call, when the client can report them."""

    input_tokens: int = 0
    output_tokens: int = 0


class LlmError(RuntimeError):
    """The model could not produce a usable structured response."""


class LlmClient(Protocol):
    """Structured-output model client.

    `last_usage` carries the tokens of the most recent call when the client can
    report them. Clients that cannot leave it None and the meter estimates.
    """

    name: str
    last_usage: "Usage | None"

    def structured(
        self,
        *,
        key: str,
        system: str,
        prompt: str,
        output_model: type[ModelT],
        task_id: str | None = None,
        actor: str = "planner",
    ) -> ModelT:
        """Return a validated instance of `output_model`."""


class ClaudeClient:
    """Live client backed by the Anthropic Messages API."""

    def __init__(self, model: str, trace: TraceStore) -> None:
        import anthropic  # imported lazily so offline runs need no credentials

        self._anthropic = anthropic
        self._client = anthropic.Anthropic()
        self._model = model
        self._trace = trace
        self.name = model
        self.last_usage: Usage | None = None

    def structured(
        self,
        *,
        key: str,
        system: str,
        prompt: str,
        output_model: type[ModelT],
        task_id: str | None = None,
        actor: str = "planner",
    ) -> ModelT:
        started = time.perf_counter()
        try:
            response = self._client.messages.parse(
                model=self._model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                thinking={"type": "adaptive"},
                output_format=output_model,
            )
        except self._anthropic.APIError as exc:
            raise LlmError(f"{key}: {exc}") from exc

        if response.stop_reason == "refusal":
            raise LlmError(f"{key}: model declined the request")
        parsed = response.parsed_output
        if parsed is None:
            raise LlmError(f"{key}: no structured output returned")

        self.last_usage = Usage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        self._trace.emit(
            EventKind.LLM_CALL,
            f"{key} -> {output_model.__name__}",
            actor=actor,
            task_id=task_id,
            duration_ms=int((time.perf_counter() - started) * 1000),
            model=self._model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        return parsed


class ReplayClient:
    """Serves recorded responses, keyed by call site.

    Lookup falls back from the most specific key to the least: an attempt-scoped
    key (`data.t2.plan@2`), then the call site (`data.t2.plan`), then the stage
    (`data.plan`). That lets a recording override one retry without restating
    everything else.
    """

    def __init__(self, responses: Mapping[str, Any], trace: TraceStore) -> None:
        self._responses = responses
        self._trace = trace
        self.name = "replay"
        self.last_usage: Usage | None = None

    def structured(
        self,
        *,
        key: str,
        system: str,
        prompt: str,
        output_model: type[ModelT],
        task_id: str | None = None,
        actor: str = "planner",
    ) -> ModelT:
        started = time.perf_counter()
        payload = self._lookup(key)
        if payload is None:
            raise LlmError(f"no recorded response for {key!r}")
        self._trace.emit(
            EventKind.LLM_CALL,
            f"{key} -> {output_model.__name__} (replay)",
            actor=actor,
            task_id=task_id,
            duration_ms=int((time.perf_counter() - started) * 1000),
            model=self.name,
        )
        return output_model.model_validate(payload)

    def _lookup(self, key: str) -> Any:
        candidates = [key]
        base = key.split("@", 1)[0]
        candidates.append(base)
        parts = base.split(".")
        if len(parts) == 3:
            candidates.append(f"{parts[0]}.{parts[2]}")
        for candidate in candidates:
            if candidate in self._responses:
                return self._responses[candidate]
        return None


def call_key(actor: str, stage: str, task_id: str | None = None, attempt: int = 1) -> str:
    """Build the canonical key for one model call site."""
    key = f"{actor}.{task_id}.{stage}" if task_id else f"{actor}.{stage}"
    return key if attempt <= 1 else f"{key}@{attempt}"
