"""Unit tests for the deterministic parts of the Intent Router.

The LLM-driven part is exercised by the live driver; here we lock down
the structural rules so the router can never be coaxed into skipping
Risk or Compliance on a trade.
"""

from __future__ import annotations

from src.agents.intent_router import _resolve_agents


def test_trade_decision_always_includes_risk_and_compliance():
    # LLM only suggested research+quant — but it's a trade.
    agents = _resolve_agents(intent_type="trade_decision", suggested=["research", "quant"])
    assert "risk" in agents
    assert "compliance" in agents


def test_trade_decision_with_no_suggestions_uses_full_default():
    agents = _resolve_agents(intent_type="trade_decision", suggested=[])
    assert set(agents) == {"research", "quant", "risk", "behavioral", "compliance"}


def test_trade_decision_cannot_drop_compliance():
    # Even if the LLM tries to suggest something weird, compliance is forced.
    agents = _resolve_agents(intent_type="trade_decision", suggested=["research"])
    assert "compliance" in agents


def test_market_info_runs_default_agents():
    agents = _resolve_agents(intent_type="market_info", suggested=[])
    assert set(agents) == {"trending", "research", "quant"}


def test_market_info_can_be_narrowed():
    # Market info doesn't have safety-critical agents — narrowing is allowed.
    agents = _resolve_agents(intent_type="market_info", suggested=["trending"])
    assert agents == ["trending"]


def test_market_info_doesnt_get_compliance_appended():
    agents = _resolve_agents(intent_type="market_info", suggested=["trending"])
    assert "compliance" not in agents


def test_policy_question_returns_no_agents():
    agents = _resolve_agents(intent_type="policy_question", suggested=["research"])
    assert agents == []


def test_portfolio_analysis_uses_risk_and_behavioral():
    agents = _resolve_agents(intent_type="portfolio_analysis", suggested=[])
    assert set(agents) == {"risk", "behavioral"}


def test_portfolio_fact_runs_no_agents():
    # "How many NVDA do I have?" is a direct lookup — answered from data,
    # never runs Risk/Behavioral (which would lecture instead of answering).
    agents = _resolve_agents(intent_type="portfolio_fact", suggested=["risk"])
    assert agents == []


def test_suggestions_outside_default_set_are_ignored():
    # LLM suggested "trending" for portfolio_analysis — not in the default
    # set, so we ignore the suggestion and keep the default.
    agents = _resolve_agents(intent_type="portfolio_analysis", suggested=["trending"])
    assert set(agents) == {"risk", "behavioral"}


def test_agents_returned_in_stable_order():
    # Order should be deterministic regardless of suggestion order.
    a = _resolve_agents(intent_type="trade_decision", suggested=["compliance", "research"])
    b = _resolve_agents(intent_type="trade_decision", suggested=["research", "compliance"])
    assert a == b
