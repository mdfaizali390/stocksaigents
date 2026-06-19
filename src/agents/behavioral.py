"""Behavioral Agent — LLM-heavy, reads the trade log.

Examines the user's recent order history and the proposed trade,
looking for behavioral patterns:
  - FOMO: chasing recent winners
  - Revenge trading: re-buying after a loss
  - Overtrading: frequency spike
  - Panic selling: selling on dips

Per design §4.2 we filter ``placed_agent="user"`` to exclude recurring
investments and DRIP, which aren't behavioral signals.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from src.agents.base import AgentContext, BaseAgent
from src.data.brokerage.base import Order
from src.llm.client import LLMClient
from src.models import AgentReport, Evidence


_SYSTEM_PROMPT = """\
You are a Behavioral Trading Analyst.

You will receive:
  - The proposed trade (ticker, side, quantity, price)
  - The user's recent order history (filtered to user-placed orders only,
    most recent first)

Your job: call the BehavioralAssessment tool with a structured opinion.

Look for these patterns:
- FOMO: chasing recent winners; e.g. buying a ticker the day after a
  large up-move, especially if the user hasn't held it before.
- Revenge trading: re-buying a stock recently sold at a loss.
- Overtrading: a marked spike in trade frequency in the last week vs.
  the prior weeks.
- Panic selling: selling into a sharp drop, especially soon after a
  buy at a higher price.

Rules:
- Signal is WARNING when you detect a pattern; INFO otherwise.
- Confidence reflects pattern strength: a single matching trade pair
  is weak (≤0.4); 3+ matching events in 30 days is strong (≥0.7).
- Reasoning must reference SPECIFIC orders by date+ticker. Don't
  hand-wave.
- If the order list is empty or very short (< 5 orders), default to
  INFO with confidence 0.0 and say so plainly.
"""


class _BehavioralAssessment(BaseModel):
    """Behavioral Agent's structured opinion."""

    signal: Literal["WARNING", "INFO"]
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str
    reasoning: str
    detected_patterns: list[
        Literal["fomo", "revenge_trading", "overtrading", "panic_selling", "none"]
    ] = Field(default_factory=list)


class BehavioralAgent(BaseAgent):
    name = "behavioral"

    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm or LLMClient()

    async def run(self, context: AgentContext) -> AgentReport:
        orders: list[Order] = context.market.get("orders", []) or []
        # Filter to user-placed orders only (design §4.2)
        user_orders = [o for o in orders if o.placed_agent == "user"]

        if len(user_orders) < 3:
            return self._info(
                f"Trade history too thin to analyze ({len(user_orders)} user-placed orders)."
            )

        # Two modes: with a proposed trade (assess pattern relevance to the
        # planned trade) or portfolio-wide history scan (no specific trade).
        prompt = _build_prompt(context.proposed_trade, user_orders[:50])
        out = await self._llm.complete_structured(
            prompt=prompt,
            schema=_BehavioralAssessment,
            system=_SYSTEM_PROMPT,
            cache_system=True,
        )

        return AgentReport(
            agent_name=self.name,
            signal=out.signal,
            confidence=out.confidence,
            summary=out.summary,
            reasoning=out.reasoning,
            evidence=_evidence(user_orders, out.detected_patterns),
            metadata={
                "orders_analyzed": len(user_orders[:50]),
                "patterns": out.detected_patterns,
            },
        )

    @staticmethod
    def _info(summary: str) -> AgentReport:
        return AgentReport(
            agent_name="behavioral",
            signal="INFO",
            confidence=0.0,
            summary=summary,
            reasoning=summary,
        )


def _build_prompt(trade, orders: list[Order]) -> str:
    import json

    orders_payload = [_order_summary(o) for o in orders]
    if trade is None:
        return (
            "No specific proposed trade — scan the user's recent trade history "
            "for behavioral patterns (FOMO, revenge trading, overtrading, "
            "panic selling) and report what you see.\n\n"
            f"Recent user-placed orders ({len(orders_payload)} shown, most recent first):\n"
            f"{json.dumps(orders_payload, indent=2, default=str)}\n\n"
            "Call BehavioralAssessment."
        )
    proposed = {
        "ticker": trade.ticker,
        "side": trade.side,
        "quantity": str(trade.quantity),
        "estimated_price": str(trade.estimated_price),
        "estimated_notional": str(trade.estimated_notional),
    }
    return (
        f"Proposed trade:\n{json.dumps(proposed, indent=2)}\n\n"
        f"Recent user-placed orders ({len(orders_payload)} shown, most recent first):\n"
        f"{json.dumps(orders_payload, indent=2, default=str)}\n\n"
        "Call BehavioralAssessment."
    )


def _order_summary(o: Order) -> dict:
    return {
        "date": o.created_at.date().isoformat(),
        "symbol": o.symbol,
        "side": o.side,
        "state": o.state,
        "quantity": str(o.quantity),
        "average_price": str(o.average_price) if o.average_price else None,
    }


def _evidence(orders: list[Order], patterns: list[str]) -> list[Evidence]:
    items: list[Evidence] = []
    items.append(
        Evidence(
            source="trade_history",
            description=f"{len(orders)} user-placed orders in window",
            data={"count": len(orders)},
        )
    )
    if patterns and patterns != ["none"]:
        items.append(
            Evidence(
                source="behavioral_pattern",
                description=f"Patterns detected: {', '.join(patterns)}",
                data={"patterns": patterns},
            )
        )
    return items


__all__ = ["BehavioralAgent"]
