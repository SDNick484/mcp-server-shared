"""A scripted `ModelClient` for the routing eval's tests: no network, every request recorded.

An importable helper, not a test module: the runner, example-home and CLI tests all use it.
"""

from __future__ import annotations

import copy
import itertools
from collections.abc import Callable
from typing import Any

import anyio
from evals.routing_eval.client import ModelResponse, ToolUse

_ids = itertools.count(1)

Responder = Callable[[dict[str, Any]], ModelResponse]


def text_response(text: str = "Done.") -> ModelResponse:
    """A response that ends the turn with text and no tool call."""
    return ModelResponse(text=text, tool_uses=(), stop_reason="end_turn", raw_content=[{"type": "text", "text": text}])


def tool_response(*calls: tuple[str, dict[str, Any]], text: str = "") -> ModelResponse:
    """A response asking for one or more tool calls, given as `(namespaced tool, input)` pairs."""
    uses = tuple(ToolUse(id=f"toolu_{next(_ids):04d}", name=name, input=dict(args)) for name, args in calls)
    raw: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
    raw += [{"type": "tool_use", "id": u.id, "name": u.name, "input": u.input} for u in uses]
    return ModelResponse(text=text, tool_uses=uses, stop_reason="tool_use", raw_content=raw)


class FakeClient:
    """Answers `create` from `script`: a list consumed in order (an `Exception` item is raised instead of
    returned), or a callable given the request. Each request is recorded as a deep copy in `requests`.
    With `delay`, each call sleeps first, and `max_in_flight` records the most calls running at once."""

    def __init__(self, script: list[ModelResponse | Exception] | Responder, *, delay: float = 0.0) -> None:
        self.script = script
        self.delay = delay
        self.requests: list[dict[str, Any]] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def create(
        self, *, model: str, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelResponse:
        request = copy.deepcopy({"model": model, "system": system, "messages": messages, "tools": tools})
        self.requests.append(request)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay:
                await anyio.sleep(self.delay)
            if callable(self.script):
                return self.script(request)
            if not self.script:
                raise AssertionError("FakeClient script exhausted")
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        finally:
            self.in_flight -= 1
