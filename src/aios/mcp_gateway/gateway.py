"""The gateway that mediates every tool call.

Responsibilities, in order: authorize the principal, bound the call with a
timeout, and trace the outcome. Failures are raised as `ToolError` so the
coordinator - not the agent - decides whether to retry, degrade or replan.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any

from aios.mcp_gateway.registry import Principal, ToolSpec
from aios.observability import EventKind, TraceStore


class ToolError(RuntimeError):
    """A tool call failed in a way the caller may be able to recover from."""


class ToolTimeout(ToolError):
    """A tool call exceeded its time budget."""


class ToolAccessDenied(PermissionError):
    """The principal is not allowed to call the tool."""


class McpGateway:
    """Authorizing, timeout-bounded, fully traced tool access."""

    def __init__(
        self,
        tools: Iterable[ToolSpec],
        trace: TraceStore,
        default_timeout_seconds: float = 20.0,
    ) -> None:
        self._tools: Mapping[str, ToolSpec] = {tool.name: tool for tool in tools}
        self._trace = trace
        self._default_timeout = default_timeout_seconds
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="mcp")

    def tools_for(self, principal: Principal) -> list[str]:
        """Tool names `principal` is allowed to call."""
        return sorted(
            name for name, spec in self._tools.items() if spec.permits(principal)
        )

    def describe(self, principal: Principal) -> str:
        """Human-readable tool catalogue for an agent's prompt."""
        return "\n".join(
            f"- {name}: {self._tools[name].description}"
            for name in self.tools_for(principal)
        )

    def call(
        self,
        principal: Principal,
        tool: str,
        *,
        task_id: str | None = None,
        **arguments: Any,
    ) -> Any:
        """Invoke a tool on behalf of `principal`."""
        spec = self._tools.get(tool)
        if spec is None:
            raise ToolError(f"unknown tool: {tool}")
        if not spec.permits(principal):
            self._trace.emit(
                EventKind.ACCESS_DENIED,
                f"{principal} denied access to {tool}",
                actor=str(principal),
                task_id=task_id,
                tool=tool,
            )
            raise ToolAccessDenied(f"{principal} may not call {tool}")

        timeout = spec.timeout_seconds or self._default_timeout
        started = time.perf_counter()
        try:
            result = self._pool.submit(spec.handler, **arguments).result(timeout)
        except FutureTimeout as exc:
            self._emit_failure(principal, tool, task_id, started, "timeout")
            raise ToolTimeout(f"{tool} exceeded {timeout:.1f}s") from exc
        except Exception as exc:
            self._emit_failure(principal, tool, task_id, started, str(exc))
            raise ToolError(f"{tool} failed: {exc}") from exc

        self._trace.emit(
            EventKind.TOOL_CALL,
            f"{tool}({_render_args(arguments)})",
            actor=str(principal),
            task_id=task_id,
            duration_ms=_elapsed_ms(started),
            tool=tool,
        )
        return result

    def _emit_failure(
        self,
        principal: Principal,
        tool: str,
        task_id: str | None,
        started: float,
        reason: str,
    ) -> None:
        self._trace.emit(
            EventKind.TOOL_ERROR,
            f"{tool} failed: {reason}",
            actor=str(principal),
            task_id=task_id,
            duration_ms=_elapsed_ms(started),
            tool=tool,
        )


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _render_args(arguments: Mapping[str, Any], width: int = 60) -> str:
    """Render call arguments for the trace, elided so a log line stays a line."""
    parts = []
    for key, value in arguments.items():
        text = repr(value)
        if len(text) > width:
            text = f"{text[:width]}... ({len(text)} chars)"
        parts.append(f"{key}={text}")
    return ", ".join(parts)
