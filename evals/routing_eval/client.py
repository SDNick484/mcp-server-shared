"""The model client: what the runner needs from a model API, and the Anthropic implementation.

The runner talks only to `ModelClient`, so tests drive it with a scripted fake (tests/routing_eval_fake.py)
and never reach the network. `anthropic` is imported inside `AnthropicClient.__init__`, so this module (and
the runner) import without the `[eval]` extra installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolUse:
    """One tool call the model asked for (a `tool_use` content block)."""

    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ModelResponse:
    """One model response. `raw_content` is the content blocks as plain dicts, sent back unchanged as the
    assistant turn (thinking blocks included, which the API requires to be passed back as they came)."""

    text: str
    tool_uses: tuple[ToolUse, ...]
    stop_reason: str
    raw_content: list[dict[str, Any]]


class ModelClient(Protocol):
    async def create(
        self, *, model: str, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelResponse: ...


class AnthropicClient:  # pragma: no cover - needs the network; the runner is tested with a fake client
    """`ModelClient` over the Messages API. The SDK retries 408/409/429/5xx and connection errors with
    exponential backoff (`max_retries=5`); an error that survives the retries propagates to the runner,
    which records it on the trial. The system prompt and the tool list are cache breakpoints: both are
    identical across a case's trials, so later trials read them from the prompt cache."""

    def __init__(self, api_key: str | None = None) -> None:
        import anthropic

        self._client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=5)

    async def create(
        self, *, model: str, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelResponse:
        cache = {"type": "ephemeral"}
        cached_tools = [*tools[:-1], {**tools[-1], "cache_control": cache}] if tools else []
        create: Any = self._client.messages.create  # plain dicts, not the SDK's TypedDict params
        message = await create(
            model=model,
            max_tokens=4096,
            system=[{"type": "text", "text": system, "cache_control": cache}],
            messages=messages,
            tools=cached_tools,
        )
        uses = tuple(ToolUse(b.id, b.name, dict(b.input)) for b in message.content if b.type == "tool_use")
        text = "".join(b.text for b in message.content if b.type == "text")
        raw = [block.to_dict() for block in message.content]
        return ModelResponse(text=text, tool_uses=uses, stop_reason=str(message.stop_reason), raw_content=raw)
