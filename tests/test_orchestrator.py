"""Tests for the orchestrator's structural enforcement.

LLM-driven flows are exercised by ``scripts/run_query.py``. These tests
lock down the safety patterns that must NOT depend on prompt obedience.
"""

from __future__ import annotations

from src.models import AgentReport, Recommendation
from src.orchestrator import _apply_block_check


def _rec(action: str = "BUY", **overrides) -> Recommendation:
    base = dict(
        action=action,
        ticker="NVDA",
        quantity_suggestion=10,
        confidence=0.8,
        summary="Test recommendation",
        citations=["Test"],
        block_reasons=[],
    )
    base.update(overrides)
    return Recommendation(**base)


def _report(agent: str, blocking: bool, reason: str | None = None) -> AgentReport:
    return AgentReport(
        agent_name=agent,
        signal="BLOCK" if blocking else "INFO",
        confidence=1.0,
        summary="x",
        reasoning="x",
        blocking=blocking,
        blocking_reason=reason,
    )


def test_no_blocking_reports_passes_recommendation_through():
    rec = _rec(action="BUY")
    out = _apply_block_check(rec, [])
    assert out.action == "BUY"
    assert out.quantity_suggestion == 10


def test_blocking_report_forces_blocked_action_even_when_pm_said_buy():
    # Even if the PM model returned BUY, a blocking report must override.
    rec = _rec(action="BUY")
    out = _apply_block_check(rec, [_report("compliance", True, "asset_class blocked")])
    assert out.action == "BLOCKED"


def test_blocked_action_drops_quantity_suggestion():
    rec = _rec(action="BUY", quantity_suggestion=50)
    out = _apply_block_check(rec, [_report("compliance", True, "trade size 8% > 1%")])
    assert out.quantity_suggestion is None


def test_block_reasons_pulled_from_reports_verbatim():
    rec = _rec(action="BUY", block_reasons=["pm-made-up-reason"])
    out = _apply_block_check(
        rec,
        [
            _report("compliance", True, "asset_class blocked"),
            _report("risk", True, "cash floor breached"),
        ],
    )
    assert "asset_class blocked" in out.block_reasons
    assert "cash floor breached" in out.block_reasons


def test_block_reasons_keep_pm_value_when_reports_have_none():
    # Pathological case: blocking=true but no reason text. We don't
    # silently lose any reason the PM produced.
    rec = _rec(action="BUY", block_reasons=["fallback reason"])
    out = _apply_block_check(rec, [_report("compliance", True, None)])
    assert out.action == "BLOCKED"
    assert out.block_reasons == ["fallback reason"]


def test_already_blocked_recommendation_stays_blocked():
    # PM correctly chose BLOCKED — pass-through still updates reasons.
    rec = _rec(action="BLOCKED", quantity_suggestion=None)
    out = _apply_block_check(rec, [_report("compliance", True, "x")])
    assert out.action == "BLOCKED"


def test_recommendation_object_is_not_mutated_in_place():
    # model_copy returns a new object; the original PM rec stays intact.
    rec = _rec(action="BUY")
    out = _apply_block_check(rec, [_report("compliance", True, "y")])
    assert rec.action == "BUY"  # original untouched
    assert out.action == "BLOCKED"
