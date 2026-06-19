"""Live driver for the Intent Router.

Usage::

    .venv/bin/python -m scripts.run_router

Runs a handful of representative queries through the LLM and prints how
each was classified. Useful for sanity-checking router behavior before
wiring the full orchestrator.
"""

from __future__ import annotations

import asyncio

from src.agents.intent_router import IntentRouter


_QUERIES = [
    "Should I buy NVDA?",
    "Should I sell some of my AMZN position?",
    "What's trending today?",
    "Am I too concentrated in tech?",
    "What's my max trade size?",
    "Tell me about Apple's recent news",
    "Is now a good time to dump META?",
]


async def main() -> None:
    router = IntentRouter()
    for q in _QUERIES:
        intent = await router.classify(q)
        print(f"\n› {q}")
        print(f"  intent: {intent.intent_type}")
        if intent.ticker:
            print(f"  ticker: {intent.ticker}")
        if intent.action:
            print(f"  action: {intent.action}")
        print(f"  agents: {intent.agents_to_run}")
        print(f"  why:    {intent.rationale}")


if __name__ == "__main__":
    asyncio.run(main())
