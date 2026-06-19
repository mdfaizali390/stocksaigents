"""Smoke-test the four LLM agents against live data.

Usage::

    .venv/bin/python -m scripts.run_agents NVDA

Runs Research, Trending, Risk, and Behavioral against the given ticker
(default NVDA), using the live Robinhood MCP for portfolio + trade
history, real Finnhub/yfinance/Stocktwits for everything else.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.agents.base import AgentContext
from src.agents.behavioral import BehavioralAgent
from src.agents.research import ResearchAgent
from src.agents.risk import RiskAgent
from src.agents.trending import TrendingAgent
from src.constitution.schema import (
    Approval,
    BehavioralGuards,
    Constitution,
    PositionLimits,
    UserProfile,
)
from src.data.brokerage.robinhood_mcp import RobinhoodMCPClient
from src.models import Intent, ProposedTrade


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


def _print_report(report) -> None:
    print(f"\n─── {report.agent_name.upper()} ─" + ("─" * (40 - len(report.agent_name))))
    print(f"signal:     {report.signal}  blocking={report.blocking}")
    print(f"confidence: {report.confidence}")
    print(f"summary:    {report.summary}")
    print(f"reasoning:  {report.reasoning[:600]}")
    if report.blocking_reason:
        print(f"blocking_reason: {report.blocking_reason}")


async def main() -> None:
    ticker = sys.argv[1].upper() if len(sys.argv) > 1 else "NVDA"
    constitution = _stub_constitution()

    # Pull live portfolio + orders from Robinhood MCP
    print(f"Fetching live portfolio + orders for read account …")
    async with RobinhoodMCPClient() as rh:
        accounts = await rh.get_accounts()
        target = next((a for a in accounts if a.is_default), accounts[0])
        portfolio = await rh.get_portfolio(target.account_number)
        positions = await rh.get_positions(target.account_number)
        sixty_days_ago = datetime.now(timezone.utc) - timedelta(days=60)
        orders = await rh.get_orders(
            target.account_number,
            created_at_gte=sixty_days_ago,
        )
        # Spot price for the proposed trade
        try:
            (quote,) = await rh.get_quotes([ticker])
            est_price = quote.last_trade_price
        except Exception:
            est_price = Decimal("100")

    print(
        f"  portfolio.total={portfolio.total_value} "
        f"positions={len(positions)} orders={len(orders)} "
        f"{ticker} ~{est_price}"
    )

    # Build a representative proposed trade — a small buy.
    qty = Decimal("1")
    proposed = ProposedTrade(
        ticker=ticker,
        side="buy",
        order_type="limit",
        quantity=qty,
        asset_class="stocks",
        estimated_price=est_price,
    )

    intent = Intent(
        intent_type="trade_decision",
        ticker=ticker,
        action="buy",
        agents_to_run=["research", "trending", "risk", "behavioral"],
        rationale="manual smoke test",
    )

    ctx = AgentContext(
        intent=intent,
        constitution=constitution,
        portfolio=portfolio,
        proposed_trade=proposed,
        market={"positions": positions, "orders": orders},
    )

    print(f"\nRunning 4 agents in parallel for {ticker} …")
    research, trending, risk, behavioral = await asyncio.gather(
        ResearchAgent().run(ctx),
        TrendingAgent(top_n=6).run(ctx),
        RiskAgent().run(ctx),
        BehavioralAgent().run(ctx),
    )

    for r in (research, trending, risk, behavioral):
        _print_report(r)


if __name__ == "__main__":
    asyncio.run(main())
