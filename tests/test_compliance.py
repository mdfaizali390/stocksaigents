from __future__ import annotations

from decimal import Decimal

import pytest

from src.agents.base import AgentContext
from src.agents.compliance import ComplianceAgent
from tests.conftest import make_trade


@pytest.fixture
def agent() -> ComplianceAgent:
    return ComplianceAgent()


async def test_passing_trade_returns_info(agent, make_context):
    # 5 shares * $100 = $500 = 0.5% of $100k portfolio (limit is 1%).
    report = await agent.run(make_context())

    assert report.signal == "INFO"
    assert report.blocking is False
    assert report.blocking_reason is None
    assert report.confidence == 1.0
    assert "passes" in report.summary.lower()


async def test_blocked_asset_class_blocks_trade(agent, make_context):
    trade = make_trade(asset_class="options")
    report = await agent.run(make_context(trade=trade))

    assert report.signal == "BLOCK"
    assert report.blocking is True
    assert "options" in report.blocking_reason
    assert "asset_class_blocked" in report.metadata["violation_codes"]


async def test_asset_class_not_in_allowed_list_blocks(agent, make_context):
    # 'futures' is neither allowed nor explicitly blocked — should still block.
    trade = make_trade(asset_class="futures")
    report = await agent.run(make_context(trade=trade))

    assert report.signal == "BLOCK"
    assert "asset_class_not_allowed" in report.metadata["violation_codes"]


async def test_blocked_order_type_blocks_trade(agent, make_context):
    trade = make_trade(order_type="market")
    report = await agent.run(make_context(trade=trade))

    assert report.signal == "BLOCK"
    assert "order_type_blocked" in report.metadata["violation_codes"]
    assert "market" in report.blocking_reason


async def test_order_type_not_in_allowed_list_blocks(agent, make_context):
    # 'stop_limit' isn't on the allowed list and isn't on the blocked list —
    # default-deny means it still blocks.
    trade = make_trade(order_type="stop_limit")
    report = await agent.run(make_context(trade=trade))

    assert report.signal == "BLOCK"
    assert "order_type_not_allowed" in report.metadata["violation_codes"]


async def test_oversized_trade_blocks(agent, make_context):
    # 100 shares * $500 = $50,000 = 50% of a $100k portfolio. Limit is 1%.
    trade = make_trade(quantity="100", estimated_price="500")
    report = await agent.run(make_context(trade=trade))

    assert report.signal == "BLOCK"
    assert "trade_size_exceeded" in report.metadata["violation_codes"]
    assert "50.00%" in report.blocking_reason


async def test_trade_at_exact_size_limit_passes(agent, make_context):
    # Exactly 1.0% — must pass (limit is *exceeds*, not *meets*).
    trade = make_trade(quantity="10", estimated_price="100")  # $1000 = 1% of $100k
    report = await agent.run(make_context(trade=trade))

    assert report.signal == "INFO"
    assert report.blocking is False


async def test_size_check_skipped_when_no_portfolio(agent, make_context):
    # Even an oversized trade can't be size-checked without portfolio total.
    # Other rules still apply.
    trade = make_trade(quantity="10000", estimated_price="500")
    report = await agent.run(make_context(with_portfolio=False, trade=trade))

    assert report.signal == "INFO"
    assert report.blocking is False
    # No size_evidence in payload
    codes = [e.description for e in report.evidence]
    assert not any("Trade size vs. portfolio" in c for c in codes)


async def test_halted_symbol_blocks(agent, make_context):
    trade = make_trade(tradability={"tradable": True, "halted": True, "state": "active"})
    report = await agent.run(make_context(trade=trade))

    assert report.signal == "BLOCK"
    assert "not_tradable" in report.metadata["violation_codes"]
    assert "halted" in report.blocking_reason


async def test_non_active_state_blocks(agent, make_context):
    trade = make_trade(tradability={"tradable": True, "halted": False, "state": "inactive"})
    report = await agent.run(make_context(trade=trade))

    assert report.signal == "BLOCK"
    assert "not_tradable" in report.metadata["violation_codes"]


async def test_tradable_false_blocks(agent, make_context):
    trade = make_trade(tradability={"tradable": False, "state": "active"})
    report = await agent.run(make_context(trade=trade))

    assert report.signal == "BLOCK"
    assert "not_tradable" in report.metadata["violation_codes"]


async def test_active_tradable_symbol_passes(agent, make_context):
    trade = make_trade(tradability={"tradable": True, "halted": False, "state": "active"})
    report = await agent.run(make_context(trade=trade))

    assert report.signal == "INFO"
    assert report.blocking is False


async def test_multiple_violations_all_reported(agent, make_context):
    # Blocked asset class + blocked order type + oversize.
    trade = make_trade(
        asset_class="crypto",
        order_type="market",
        quantity="100",
        estimated_price="500",
    )
    report = await agent.run(make_context(trade=trade))

    assert report.signal == "BLOCK"
    codes = report.metadata["violation_codes"]
    assert "asset_class_blocked" in codes
    assert "order_type_blocked" in codes
    assert "trade_size_exceeded" in codes


async def test_no_proposed_trade_returns_info(agent, constitution, buy_intent, portfolio):
    # Edge case: policy_question / market_info routes don't produce a trade.
    ctx = AgentContext(
        intent=buy_intent,
        constitution=constitution,
        portfolio=portfolio,
        proposed_trade=None,
    )
    report = await agent.run(ctx)

    assert report.signal == "INFO"
    assert report.blocking is False
    assert report.agent_name == "compliance"


async def test_uses_decimal_not_float_for_size(agent, make_context):
    # Force a value that floats would round badly: 0.1 + 0.2 territory.
    # 333 shares at $3.003 = $999.999, just under 1% of $100k.
    trade = make_trade(quantity="333", estimated_price="3.003")
    report = await agent.run(make_context(trade=trade))

    assert report.signal == "INFO"
    # Cross-check the math is honest.
    pct = next(
        e.data["trade_pct"]
        for e in report.evidence
        if e.description == "Trade size vs. portfolio"
    )
    assert Decimal(str(pct)) < Decimal("1.0")


async def test_estimated_notional_helper():
    trade = make_trade(quantity="7", estimated_price="123.45")
    assert trade.estimated_notional == Decimal("864.15")
