"""Anthropic SDK wrapper.

One place for:
  - default model selection (Sonnet 4.5 / Opus 4.x)
  - structured output via tool-use (forces the model to return JSON
    matching a Pydantic schema instead of free-text we'd have to parse)
  - prompt caching for static system prompts (cuts repeated-input cost
    by ~90% during dev — see Anthropic docs on ephemeral cache)

Why tool-use for structured output (vs. JSON mode)? Tool-use is more
robust on Claude — the SDK enforces the schema at the API layer and
retries on schema mismatch. Free-form JSON mode loses that contract.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from src.config import get_settings


# Default model. Anthropic's latest Sonnet is the right tradeoff between
# reasoning quality and cost for our agent fleet (per design §10 row 7).
# Override per-call with the ``model`` arg of ``complete()``.
DEFAULT_MODEL = "claude-sonnet-4-5"

# When the PM synthesizer needs deeper reasoning, swap to Opus.
OPUS_MODEL = "claude-opus-4-5"


T = TypeVar("T", bound=BaseModel)


@dataclass
class LLMUsage:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


class LLMClient:
    """Thin wrapper around AsyncAnthropic.

    Holds one shared async client (HTTP keep-alive + connection pool).
    Use ``complete_text`` for prose, ``complete_structured`` for Pydantic
    output.
    """

    def __init__(self, api_key: str | None = None) -> None:
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key or get_settings().require_anthropic()
        )
        self.last_usage: LLMUsage | None = None

    async def complete_text(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        cache_system: bool = False,
    ) -> str:
        """Plain text completion. Use for explanation / summarization."""
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system is not None:
            kwargs["system"] = _maybe_cache(system, cache_system)
        resp = await self._client.messages.create(**kwargs)
        self.last_usage = _extract_usage(resp.usage)
        return _first_text(resp.content)

    async def complete_structured(
        self,
        prompt: str,
        schema: type[T],
        *,
        system: str | None = None,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 2048,
        temperature: float = 0.0,
        cache_system: bool = False,
    ) -> T:
        """Force the model to return JSON matching ``schema``.

        Implementation: we expose a single tool whose input_schema IS the
        Pydantic schema, then ``tool_choice`` forces Claude to call it.
        The validated tool input becomes the return value.
        """
        tool_name = _camel_to_snake(schema.__name__)
        tool = {
            "name": tool_name,
            "description": (schema.__doc__ or schema.__name__).strip().splitlines()[0],
            "input_schema": _pydantic_to_input_schema(schema),
        }
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "tools": [tool],
            "tool_choice": {"type": "tool", "name": tool_name},
            "messages": [{"role": "user", "content": prompt}],
        }
        if system is not None:
            kwargs["system"] = _maybe_cache(system, cache_system)

        resp = await self._client.messages.create(**kwargs)
        self.last_usage = _extract_usage(resp.usage)

        for block in resp.content:
            if getattr(block, "type", None) == "tool_use":
                payload = block.input
                if isinstance(payload, str):
                    payload = json.loads(payload)
                try:
                    return schema.model_validate(payload)
                except ValidationError as e:
                    raise RuntimeError(
                        f"LLM returned tool_use that failed schema validation: {e}\n"
                        f"payload={payload!r}"
                    ) from e
        raise RuntimeError(
            f"expected a tool_use response calling {tool_name}, got: {resp.content!r}"
        )


# ─── helpers ──────────────────────────────────────────────────────────


def _first_text(content: list[Any]) -> str:
    for block in content:
        if getattr(block, "type", None) == "text":
            return block.text
    return ""


def _extract_usage(u: Any) -> LLMUsage:
    return LLMUsage(
        input_tokens=getattr(u, "input_tokens", 0) or 0,
        output_tokens=getattr(u, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
        cache_creation_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
    )


def _maybe_cache(system: str, cache: bool) -> Any:
    """Wrap a system prompt as cacheable if requested.

    Anthropic prompt caching needs system to be a list-of-blocks with
    ``cache_control`` set on the block to cache. Cheap when system text
    is the same across calls (which it is for an agent's static prompt).
    """
    if not cache:
        return system
    return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]


def _camel_to_snake(name: str) -> str:
    out: list[str] = []
    for i, ch in enumerate(name):
        if ch.isupper() and i and not name[i - 1].isupper():
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


def _pydantic_to_input_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """Pydantic model_json_schema → Anthropic tool input_schema.

    Anthropic expects an object schema at the top level. Pydantic v2's
    JSON schema is mostly compatible; we strip the bits Anthropic doesn't
    need (``$defs`` are inlined in references it understands; ``title``
    is informational).
    """
    js = schema.model_json_schema()
    js.pop("title", None)
    return js


__all__ = ["LLMClient", "LLMUsage", "DEFAULT_MODEL", "OPUS_MODEL"]
