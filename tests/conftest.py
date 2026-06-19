from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.agents.base import AgentContext
from src.constitution.schema import (
    Approval,
    BehavioralGuards,
    Constitution,
    PositionLimits,
    UserProfile,
)
from src.data.brokerage.base import BuyingPower, Portfolio
from src.models import Intent, ProposedTrade


@pytest.fixture
def constitution() -> Constitution:
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


@pytest.fixture
def portfolio() -> Portfolio:
    """100k portfolio — max_single_trade_pct=1.0 means $1,000 cap."""
    return Portfolio(
        total_value=Decimal("100000"),
        equity_value=Decimal("90000"),
        options_value=Decimal("0"),
        futures_value=Decimal("0"),
        event_contracts_value=Decimal("0"),
        crypto_value=Decimal("0"),
        mutual_funds_value=Decimal("0"),
        fixed_income_value=Decimal("0"),
        cash=Decimal("10000"),
        pending_deposits=Decimal("0"),
        currency="USD",
        buying_power=BuyingPower(
            buying_power=Decimal("10000"),
            unleveraged_buying_power=Decimal("10000"),
            display_currency="USD",
        ),
    )


@pytest.fixture
def buy_intent() -> Intent:
    return Intent(
        intent_type="trade_decision",
        ticker="NVDA",
        action="buy",
        agents_to_run=["research", "quant", "risk", "behavioral", "compliance"],
        rationale="user asked to buy NVDA",
    )


def make_trade(
    *,
    ticker: str = "NVDA",
    side: str = "buy",
    order_type: str = "limit",
    quantity: str = "5",
    asset_class: str = "stocks",
    estimated_price: str = "100",
    tradability: dict | None = None,
) -> ProposedTrade:
    return ProposedTrade(
        ticker=ticker,
        side=side,
        order_type=order_type,
        quantity=Decimal(quantity),
        asset_class=asset_class,
        estimated_price=Decimal(estimated_price),
        tradability=tradability,
    )


@pytest.fixture
def make_context(constitution, portfolio, buy_intent):
    """Factory: returns an AgentContext with the given trade overrides."""

    def _factory(
        *,
        with_portfolio: bool = True,
        trade: ProposedTrade | None = None,
    ) -> AgentContext:
        return AgentContext(
            intent=buy_intent,
            constitution=constitution,
            portfolio=portfolio if with_portfolio else None,
            proposed_trade=trade if trade is not None else make_trade(),
        )

    return _factory
