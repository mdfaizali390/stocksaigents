"""Intent Router — single LLM call, structured output.

The orchestrator's first step. Reads the user's natural-language query and
emits an ``Intent`` object: what they're asking, which ticker (if any), and
which specialist agents should run.

Two invariants are enforced **structurally** (not via prompt):
  1. Any ``trade_decision`` always runs Risk + Compliance (design §4.1).
  2. ``policy_question`` runs no agents — it's answered from the
     Constitution directly.

The LLM is allowed to *propose* an agent list, and we add Risk/Compliance
on top for trades. This way the prompt stays simple and we don't trust the
model to honor a critical safety rule.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from src.llm.client import LLMClient
from src.models import AgentName, Intent, IntentType, TradeAction


# Default agent dispatch by intent. Router can override via the LLM call,
# but Risk + Compliance on trade_decision are added structurally and
# can't be removed.
_DEFAULT_AGENTS: dict[str, list[AgentName]] = {
    "market_info": ["trending", "research", "quant"],
    "trade_decision": ["research", "quant", "risk", "behavioral", "compliance"],
    "portfolio_analysis": ["risk", "behavioral"],
    "portfolio_fact": [],  # answered directly from portfolio data, no agents
    "policy_question": [],
}

_TRADE_REQUIRED_AGENTS: set[AgentName] = {"risk", "compliance"}


_SYSTEM_PROMPT = """\
You are the Intent Router for a stock-advisor system.

Your only job: classify the user's query and call the IntentClassification tool.

Intent types:
- trade_decision: user wants advice on buying or selling a specific ticker
  ("Should I buy NVDA?", "Is now a good time to sell my AMZN?")
- market_info: user wants market info, no specific trade
  ("What's trending?", "How is META doing?", "Tell me about Apple's news")
- portfolio_fact: user wants a SPECIFIC FACT about their holdings — a direct
  lookup, not an opinion. ("How many NVDA do I have?", "What's my cash
  balance?", "What's my buying power?", "Do I own any TSLA?", "What's my
  total portfolio value?"). These are answered straight from account data.
- portfolio_analysis: user wants a JUDGEMENT about their portfolio overall
  ("Am I too concentrated in tech?", "How am I doing?", "Is my portfolio
  too risky?"). These need the Risk/Behavioral agents to reason.
- policy_question: user is asking about their own trading policy/rules
  ("What's my max trade size?", "What asset classes am I allowed?")

Distinguishing fact vs analysis: if a database row answers it, it's
portfolio_fact. If it needs reasoning or an opinion, it's portfolio_analysis.

Rules:
- Extract the primary ticker if one is mentioned (uppercased symbol like NVDA).
  If they say "Apple" use AAPL, "Amazon" → AMZN, "Microsoft" → MSFT, etc.
  Only set ``ticker`` if you're confident.
- For trade_decision, also set ``action`` to "buy" or "sell".
- Leave ticker null when the query is broad ("what's trending?").
- ``rationale`` is one sentence explaining your classification.

Pick agents that match the question. The system will add Risk + Compliance
automatically for any trade_decision — you don't need to include them.
"""


class _IntentClassification(BaseModel):
    """Routing decision for a user query."""

    intent_type: Literal[
        "trade_decision", "market_info", "portfolio_analysis",
        "portfolio_fact", "policy_question",
    ]
    ticker: str | None = Field(
        default=None,
        description="Uppercase stock symbol if one is mentioned, else null",
    )
    action: Literal["buy", "sell"] | None = Field(
        default=None,
        description="Required for trade_decision; null otherwise",
    )
    suggested_agents: list[
        Literal[
            "research", "quant", "trending", "risk", "behavioral", "compliance"
        ]
    ] = Field(
        default_factory=list,
        description="Agents you think should run (Risk+Compliance auto-added for trades)",
    )
    rationale: str = Field(description="One sentence explaining the classification")


class IntentRouter:
    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm or LLMClient()

    async def classify(self, query: str) -> Intent:
        if not query.strip():
            return Intent(
                intent_type="policy_question",
                rationale="empty query",
                agents_to_run=[],
            )

        decision = await self._llm.complete_structured(
            prompt=f"User query:\n\n{query.strip()}",
            schema=_IntentClassification,
            system=_SYSTEM_PROMPT,
            cache_system=True,
        )

        agents = _resolve_agents(
            intent_type=decision.intent_type,
            suggested=decision.suggested_agents,
        )
        ticker = (decision.ticker or "").strip().upper() or None
        action = decision.action

        # Sanity: trade_decision must have an action; if missing, default
        # to buy (rare — model usually fills it). Better to default than
        # to crash.
        if decision.intent_type == "trade_decision" and not action:
            action = "buy"

        return Intent(
            intent_type=decision.intent_type,
            ticker=ticker,
            action=action,
            agents_to_run=agents,
            rationale=decision.rationale,
        )


def _resolve_agents(
    *, intent_type: IntentType, suggested: list[AgentName]
) -> list[AgentName]:
    """Combine LLM suggestions with structurally required agents.

    Priority: defaults from the table act as a baseline; LLM suggestions
    *narrow* it but can't drop required agents on a trade_decision.
    """
    base: list[AgentName] = list(_DEFAULT_AGENTS.get(intent_type, []))
    if suggested:
        # Use suggested as the chosen set, but only those that appear in
        # the default list for the intent (LLM can't summon agents that
        # don't fit). For empty default (policy_question), suggestions
        # are ignored.
        allowed = set(base)
        chosen = [a for a in suggested if a in allowed]
        if chosen:
            base = chosen

    if intent_type == "trade_decision":
        for required in _TRADE_REQUIRED_AGENTS:
            if required not in base:
                base.append(required)

    # Stable order matching enum/dispatch table for readability.
    order: list[AgentName] = [
        "research",
        "quant",
        "trending",
        "risk",
        "behavioral",
        "compliance",
    ]
    return [a for a in order if a in set(base)]


__all__ = ["IntentRouter"]
