"""Run a full end-to-end query through the orchestrator.

Usage::

    .venv/bin/python -m scripts.run_query "Should I buy NVDA?"

Wires:
  - Robinhood MCP for live portfolio/positions/orders/quotes
  - All LLM agents
  - Compliance (no LLM)
  - PM synthesizer
  - Structural block enforcement
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone

from src.constitution.schema import (
    Approval,
    BehavioralGuards,
    Constitution,
    PositionLimits,
    UserProfile,
)
from src.data.brokerage.robinhood_mcp import RobinhoodMCPClient
from src.orchestrator import Orchestrator
from src.privacy import redact_list, redact_shares


def _stub_constitution() -> Constitution:
    return Constitution(
        version="1.0",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        user_profile=UserProfile(
            risk_profile="moderate",
            time_horizon="long_term",
            experience_level="intermediate",
        ),
        position_limits=PositionLimits(
            max_single_trade_pct=1.0,
            max_single_stock_pct=15.0,
            max_sector_pct=30.0,
            min_cash_pct=5.0,
        ),
        allowed_asset_classes=["stocks", "etfs"],
        blocked_asset_classes=["options", "margin", "crypto"],
        allowed_order_types=["limit"],
        blocked_order_types=["market", "stop_market"],
        approval=Approval(human_approval_required=True, auto_execute_threshold_pct=0.0),
        behavioral_guards=BehavioralGuards(
            cooldown_after_loss_minutes=60, max_trades_per_day=5
        ),
    )


async def main() -> None:
    query = " ".join(sys.argv[1:]) or "Should I buy NVDA?"
    constitution = _stub_constitution()

    async with RobinhoodMCPClient() as rh:
        orch = Orchestrator(brokerage=rh)
        decision = await orch.handle_query(query, constitution=constitution)

    print(f"\n══ QUERY ══════════════════════════════════════")
    print(f"  {query}")
    print(f"\n══ INTENT ═════════════════════════════════════")
    print(f"  type:    {decision.intent.intent_type}")
    print(f"  ticker:  {decision.intent.ticker}")
    print(f"  action:  {decision.intent.action}")
    print(f"  agents:  {decision.intent.agents_to_run}")
    print(f"  why:     {decision.intent.rationale}")

    print(f"\n══ AGENT REPORTS ══════════════════════════════")
    for r in decision.reports:
        marker = "🚫" if r.blocking else (
            "📈" if r.signal == "BUY" else
            "📉" if r.signal == "SELL" else
            "⚠️ " if r.signal == "WARNING" else "  "
        )
        print(f"\n{marker} {r.agent_name.upper()}  [{r.signal}, conf={r.confidence:.2f}]")
        print(f"    {redact_shares(r.summary)}")
        if r.blocking_reason:
            print(f"    ↳ blocking: {redact_shares(r.blocking_reason)}")

    print(f"\n══ PM RECOMMENDATION ══════════════════════════")
    rec = decision.recommendation
    if rec.action == "BLOCKED":
        print(f"  🛑 BLOCKED  (confidence {rec.confidence:.2f})")
    else:
        print(f"  → {rec.action}  (confidence {rec.confidence:.2f})")
    if rec.quantity_suggestion is not None:
        print(f"  quantity: {rec.quantity_suggestion} shares of {rec.ticker}")
    print(f"\n  {redact_shares(rec.summary)}")
    if rec.block_reasons:
        print(f"\n  Block reasons:")
        for br in redact_list(rec.block_reasons):
            print(f"    - {br}")
    if rec.citations:
        print(f"\n  Citations:")
        for c in redact_list(rec.citations):
            print(f"    - {c}")


if __name__ == "__main__":
    asyncio.run(main())
