from decimal import Decimal

import pytest

from src.agents.indicators import (
    momentum,
    rsi,
    sma,
    volatility,
    volume_trend,
)


def D(*nums: float) -> list[Decimal]:
    return [Decimal(str(n)) for n in nums]


# ─── sma ──────────────────────────────────────────────────────────────


def test_sma_basic():
    assert sma(D(1, 2, 3, 4, 5), 3) == 4.0  # (3+4+5)/3


def test_sma_returns_none_when_insufficient_data():
    assert sma(D(1, 2), 5) is None


def test_sma_uses_only_window_tail():
    # 100-element list, window 5 — should be mean of last 5.
    prices = D(*([0.0] * 95 + [10, 10, 10, 10, 10]))
    assert sma(prices, 5) == 10.0


# ─── rsi ──────────────────────────────────────────────────────────────


def test_rsi_returns_none_when_insufficient_data():
    assert rsi(D(*range(10)), period=14) is None


def test_rsi_all_gains_clamps_to_100():
    # 30 strictly increasing prices → no losses → RSI = 100.
    prices = D(*[float(i) for i in range(1, 31)])
    assert rsi(prices, 14) == 100.0


def test_rsi_oscillates_in_realistic_range():
    # Sawtooth: alternates +1/-1 around 100 → RSI ≈ 50 (neutral).
    prices: list[Decimal] = []
    p = 100.0
    for i in range(40):
        p = p + (1 if i % 2 == 0 else -1)
        prices.append(Decimal(str(p)))
    val = rsi(prices, 14)
    assert val is not None
    assert 30 < val < 70


# ─── momentum ─────────────────────────────────────────────────────────


def test_momentum_simple():
    # Last is 10% above the bar 20 ago.
    prices = D(*([100.0] * 20 + [110.0]))
    val = momentum(prices, 20)
    assert val == pytest.approx(0.10, rel=1e-6)


def test_momentum_returns_none_when_insufficient():
    assert momentum(D(1, 2, 3), 20) is None


# ─── volatility ────────────────────────────────────────────────────────


def test_volatility_constant_prices_is_zero():
    assert volatility(D(*([100.0] * 30)), window=20) == 0.0


def test_volatility_positive_for_noisy_prices():
    import math, random

    random.seed(42)
    prices = D(*[100.0 * math.exp(random.gauss(0, 0.01)) for _ in range(40)])
    val = volatility(prices, 20)
    assert val is not None and val > 0


# ─── volume_trend ──────────────────────────────────────────────────────


def test_volume_trend_doubled_volume():
    # Earlier 5 days at vol 100, recent 5 at vol 200 → 2.0.
    vols = [100] * 5 + [200] * 5
    assert volume_trend(vols, window=5) == pytest.approx(2.0)


def test_volume_trend_returns_none_when_insufficient():
    assert volume_trend([1, 2, 3], window=5) is None
