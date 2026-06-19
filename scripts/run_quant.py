"""Run the Quant Agent on a ticker, end-to-end.

Usage::

    .venv/bin/python -m scripts.run_quant NVDA

Defaults to NVDA if no arg given. Prints the indicator panel + the
LLM-generated AgentReport.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal

from src.agents.base import AgentContext
from src.agents.quant import QuantAgent
from src.constitution.schema import (
    Approval,
    BehavioralGuards,
    Constitution,
    PositionLimits,
    UserProfile,
)
from src.models import Intent


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
    ticker = sys.argv[1].upper() if len(sys.argv) > 1 else "NVDA"
    print(f"Running Quant Agent on {ticker} …\n")

    agent = QuantAgent()
    ctx = AgentContext(
        intent=Intent(
            intent_type="trade_decision",
            ticker=ticker,
            action="buy",
            agents_to_run=["quant"],
            rationale=f"manual quant probe for {ticker}",
        ),
        constitution=_stub_constitution(),
    )
    report = await agent.run(ctx)

    print("─── INDICATOR PANEL ───")
    print(json.dumps(report.metadata.get("indicator_panel", {}), indent=2, default=str))

    print(f"\n─── AGENT REPORT ─────")
    print(f"signal:     {report.signal}")
    print(f"confidence: {report.confidence}")
    print(f"summary:    {report.summary}")
    print(f"reasoning:  {report.reasoning}")
    if agent._llm.last_usage:  # type: ignore[attr-defined]
        u = agent._llm.last_usage  # type: ignore[attr-defined]
        print(
            f"\ntokens:     in={u.input_tokens}  out={u.output_tokens}  "
            f"cached_read={u.cache_read_tokens}  cached_write={u.cache_creation_tokens}"
        )


if __name__ == "__main__":
    asyncio.run(main())
