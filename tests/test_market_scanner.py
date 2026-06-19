"""Unit tests for the deterministic Trending Score math.

These intentionally avoid the network — they drive ``score()`` directly with
hand-built ``RawSignals``. Live integration is exercised by
``scripts/scan_trending.py``.
"""

from __future__ import annotations

import pytest

from src.data.market_scanner import (
    RawSignals,
    _WEIGHTS,
    percentile_ranks,
    score,
)


def _bare(ticker: str, **overrides) -> RawSignals:
    """RawSignals with safe zeros for everything not specified."""
    base = dict(
        stocktwits_trending=0.0,
        stocktwits_watchers=0,
        news_count_today=0,
        news_baseline_per_day=0.0,
        volume_today=0,
        avg_volume_20d=1.0,
        return_5d=0.0,
        net_rec_change=0,
    )
    base.update(overrides)
    return RawSignals(ticker=ticker, **base)


# ─── percentile_ranks ──────────────────────────────────────────────────


def test_pct_ranks_empty_list():
    assert percentile_ranks([]) == []


def test_pct_ranks_single_value_is_50():
    # One ticker can't be ranked relative to anything; 50 is neutral.
    assert percentile_ranks([42.0]) == [50.0]


def test_pct_ranks_strict_order():
    # 3 distinct values → 0, 50, 100.
    assert percentile_ranks([1.0, 2.0, 3.0]) == [0.0, 50.0, 100.0]


def test_pct_ranks_preserves_input_order():
    # Result is in input order, not sorted order.
    result = percentile_ranks([3.0, 1.0, 2.0])
    assert result == [100.0, 0.0, 50.0]


def test_pct_ranks_ties_get_average():
    # Two ties at the bottom → both get rank 0.5/3 (positions 0 and 1
    # averaged = 0.5; mapped to 0.5 / 2 * 100 = 25).
    result = percentile_ranks([1.0, 1.0, 2.0])
    assert result == [25.0, 25.0, 100.0]


def test_pct_ranks_all_equal_yields_50():
    # No differentiation when every value is the same.
    assert percentile_ranks([7.0, 7.0, 7.0, 7.0]) == [50.0, 50.0, 50.0, 50.0]


# ─── score() ───────────────────────────────────────────────────────────


def test_weights_sum_to_one():
    assert abs(sum(_WEIGHTS.values()) - 1.0) < 1e-9


def test_score_returns_empty_for_empty_input():
    assert score([]) == []


def test_score_with_single_signal_returns_50():
    # All components map to 50.0 (single-element percentile rank).
    result = score([_bare("X", stocktwits_trending=99.9)])
    assert len(result) == 1
    assert result[0].ticker == "X"
    assert result[0].score == 50.0


def test_score_ranks_clear_winner_first():
    # A is best on every component; B is mid; C is worst.
    a = _bare(
        "A",
        stocktwits_trending=10.0,
        stocktwits_watchers=1_000_000,
        news_count_today=20,
        news_baseline_per_day=2.0,
        volume_today=10_000_000,
        avg_volume_20d=1_000_000.0,
        return_5d=0.10,
        net_rec_change=5,
    )
    b = _bare(
        "B",
        stocktwits_trending=5.0,
        stocktwits_watchers=500_000,
        news_count_today=5,
        news_baseline_per_day=2.0,
        volume_today=2_000_000,
        avg_volume_20d=1_000_000.0,
        return_5d=0.02,
        net_rec_change=1,
    )
    c = _bare(
        "C",
        stocktwits_trending=0.5,
        stocktwits_watchers=10_000,
        news_count_today=0,
        news_baseline_per_day=2.0,
        volume_today=500_000,
        avg_volume_20d=1_000_000.0,
        return_5d=-0.05,
        net_rec_change=-2,
    )

    result = score([c, a, b])  # input order intentionally scrambled
    assert [t.ticker for t in result] == ["A", "B", "C"]
    assert result[0].score == 100.0  # A wins every component
    assert result[2].score == 0.0  # C loses every component


def test_score_scales_to_zero_to_hundred():
    a = _bare("A", stocktwits_trending=10.0, return_5d=0.10)
    b = _bare("B", stocktwits_trending=0.0, return_5d=-0.10)
    result = score([a, b])
    for t in result:
        assert 0.0 <= t.score <= 100.0


def test_score_components_in_output():
    a = _bare("A", stocktwits_trending=10.0)
    b = _bare("B", stocktwits_trending=1.0)
    result = score([a, b])
    by_t = {t.ticker: t for t in result}
    assert set(by_t["A"].components.keys()) == set(_WEIGHTS.keys())
    # A wins the stocktwits_trending component → 100; B → 0.
    assert by_t["A"].components["stocktwits_trending"] == 100.0
    assert by_t["B"].components["stocktwits_trending"] == 0.0


def test_score_handles_none_values():
    # Missing data shouldn't crash; treated as a neutral "0" raw signal.
    a = RawSignals(
        ticker="A",
        stocktwits_trending=None,
        stocktwits_watchers=None,
        volume_today=None,
        avg_volume_20d=None,
        return_5d=None,
    )
    b = _bare("B", return_5d=0.05)
    result = score([a, b])
    assert len(result) == 2
    # Just check it didn't blow up and produced numbers in range.
    assert all(0.0 <= t.score <= 100.0 for t in result)


def test_headline_evidence_passes_through():
    a = _bare("A", stocktwits_trending=10.0)
    a.headline_evidence = ["headline 1", "headline 2"]
    result = score([a])
    assert result[0].headline_evidence == ["headline 1", "headline 2"]
