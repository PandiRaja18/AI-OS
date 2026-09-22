"""Structured output from a self-hosted model.

The whole architecture rests on one assumption: a model call returns a valid
typed object, not prose. A hosted frontier model gets that right almost always.
A self-hosted 7B model does not, so this client does three things the Claude
client does not have to:

1. It sends the Pydantic schema to the server's constrained-decoding backend, so
   the tokens are forced to fit.
2. It flattens the schema first. Pydantic emits `$defs`/`$ref` for nested models
   and several local backends silently ignore or reject those.
3. It repairs. On a validation failure it re-asks with the error attached, a
   bounded number of times, and gives up loudly rather than returning something
   half-valid.

One client covers the common servers because most expose an OpenAI-compatible
endpoint: vLLM, llama.cpp, TGI, LM Studio, LocalAI. Ollama's native endpoint is
supported too because its `format` parameter is the simplest schema channel.
"""

from __future__ import annotations

import enum
import json
import time
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from aios.llm import LlmError, Usage
from aios.observability import EventKind, TraceStore

ModelT = TypeVar("ModelT", bound=BaseModel)

DEFAULT_TIMEOUT = 300.0
MAX_TOKENS = 4096


class ApiStyle(enum.StrEnum):
    OPENAI = "openai"
    OLLAMA = "ollama"


def flatten_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline `$ref`s and drop `$defs`, which local backends handle poorly."""
    definitions = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(item) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            name = node["$ref"].removeprefix("#/$defs/")
            resolved = definitions.get(name)
            if resolved is None:
                raise LlmError(f"schema reference {node['$ref']} is unresolvable")
            merged = {**walk(resolved)}
            merged.update({k: walk(v) for k, v in node.items() if k != "$ref"})
            return merged
        return {key: walk(value) for key, value in node.items() if key != "$defs"}

    flat = walk({key: value for key, value in schema.items() if key != "$defs"})
    return _strict(flat)


def _strict(node: Any) -> Any:
    """Close every object so the decoder cannot invent fields."""
    if isinstance(node, list):
        return [_strict(item) for item in node]
    if not isinstance(node, dict):
        return node
    closed = {key: _strict(value) for key, value in node.items()}
    if closed.get("type") == "object" and "properties" in closed:
        closed.setdefault("additionalProperties", False)
        closed.setdefault("required", sorted(closed["properties"]))
    return closed


class LocalLlmClient:
    """Calls a self-hosted model and insists on a valid typed answer."""

    def __init__(
        self,
        base_url: str,
        model: str,
        trace: TraceStore,
        api_style: ApiStyle | str = ApiStyle.OPENAI,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_repairs: int = 2,
        temperature: float = 0.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._trace = trace
        self._style = ApiStyle(api_style)
        self._timeout = timeout
        self._max_repairs = max_repairs
        self._temperature = temperature
        # An injected client is how a deployment supplies a proxy, a private CA
        # bundle or mTLS - self-hosted inference often sits behind one of those.
        self._client = http_client or httpx.Client(
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
        )
        self.name = f"{model}@{self._base_url}"
        self.last_usage: Usage | None = None

    def close(self) -> None:
        self._client.close()

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
        schema = flatten_schema(output_model.model_json_schema())
        started = time.perf_counter()
        attempt_prompt = prompt
        last_error = "no response"

        for attempt in range(1, self._max_repairs + 2):
            text, usage = self._call(system, attempt_prompt, schema, output_model)
            self.last_usage = usage
            try:
                parsed = output_model.model_validate_json(text)
            except ValidationError as error:
                last_error = _summarize(error)
            else:
                self._trace.emit(
                    EventKind.LLM_CALL,
                    f"{key} -> {output_model.__name__}"
                    + (f" (repaired x{attempt - 1})" if attempt > 1 else ""),
                    actor=actor,
                    task_id=task_id,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    model=self._model,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    repairs=attempt - 1,
                )
                return parsed

            self._trace.emit(
                EventKind.LLM_CALL,
                f"{key}: invalid output, repairing ({last_error})",
                actor=actor,
                task_id=task_id,
                model=self._model,
                attempt=attempt,
            )
            attempt_prompt = (
                f"{prompt}\n\nYour previous answer did not match the required "
                f"schema: {last_error}\nReturn only valid JSON for the schema."
            )

        raise LlmError(
            f"{key}: {self._model} could not produce valid "
            f"{output_model.__name__} after {self._max_repairs + 1} attempts "
            f"({last_error})"
        )

    def _call(
        self,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        output_model: type[BaseModel],
    ) -> tuple[str, Usage]:
        if self._style is ApiStyle.OLLAMA:
            url = f"{self._base_url}/api/chat"
            body = {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "format": schema,
                "stream": False,
                "options": {"temperature": self._temperature, "num_predict": MAX_TOKENS},
            }
        else:
            url = f"{self._base_url}/v1/chat/completions"
            body = {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": output_model.__name__,
                        "schema": schema,
                        "strict": True,
                    },
                },
                "temperature": self._temperature,
                "max_tokens": MAX_TOKENS,
            }

        try:
            response = self._client.post(url, json=body)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as error:
            raise LlmError(
                f"{self._model} returned {error.response.status_code}: "
                f"{error.response.text[:300]}"
            ) from error
        except httpx.HTTPError as error:
            raise LlmError(f"cannot reach {self._base_url}: {error}") from error

        if self._style is ApiStyle.OLLAMA:
            text = payload.get("message", {}).get("content", "")
            usage = Usage(
                input_tokens=payload.get("prompt_eval_count", 0),
                output_tokens=payload.get("eval_count", 0),
            )
        else:
            choices = payload.get("choices") or []
            if not choices:
                raise LlmError(f"{self._model} returned no choices")
            text = choices[0].get("message", {}).get("content", "")
            reported = payload.get("usage") or {}
            usage = Usage(
                input_tokens=reported.get("prompt_tokens", 0),
                output_tokens=reported.get("completion_tokens", 0),
            )
        return text, usage

    def probe(self) -> str:
        """Check the server answers and can hold a schema. Used by preflight."""

        class Probe(BaseModel):
            ok: bool
            model_name: str

        result = self.structured(
            key="probe",
            system="You answer with JSON only.",
            prompt='Reply with ok=true and model_name set to your model name.',
            output_model=Probe,
            actor="preflight",
        )
        return result.model_name


def _summarize(error: ValidationError, limit: int = 3) -> str:
    """One readable line from a validation failure, for the repair prompt."""
    parts = [
        f"{'.'.join(str(item) for item in issue['loc']) or 'root'}: {issue['msg']}"
        for issue in error.errors()[:limit]
    ]
    return "; ".join(parts)
