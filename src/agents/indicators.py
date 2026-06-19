"""Pure-function technical indicators.

Lifted out of the Quant Agent so they're testable without an LLM. All
inputs are ``list[Decimal]`` of close prices (or volumes); outputs are
plain floats so they round-trip cleanly through JSON for the prompt.
"""

from __future__ import annotations

from decimal import Decimal


def sma(prices: list[Decimal], window: int) -> float | None:
    """Simple moving average of the last ``window`` prices."""
    if len(prices) < window or window <= 0:
        return None
    return float(sum(prices[-window:]) / Decimal(window))


def rsi(prices: list[Decimal], period: int = 14) -> float | None:
    """Wilder's RSI on close-to-close changes.

    Uses Wilder's smoothing (the classic): seed with simple averages of
    the first ``period`` gains/losses, then exponentially smooth with
    ``alpha = 1 / period``. Returns None when there aren't enough bars.
    """
    if len(prices) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        change = float(prices[i] - prices[i - 1])
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for i in range(period + 1, len(prices)):
        change = float(prices[i] - prices[i - 1])
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def volatility(prices: list[Decimal], window: int = 20) -> float | None:
    """Annualized stdev of daily log returns (last ``window`` bars).

    Result is decimal (e.g. 0.32 = 32% annual vol). Multiply by 100 for
    display.
    """
    if len(prices) < window + 1:
        return None
    import math

    returns: list[float] = []
    for i in range(len(prices) - window, len(prices)):
        prev = float(prices[i - 1])
        cur = float(prices[i])
        if prev <= 0 or cur <= 0:
            continue
        returns.append(math.log(cur / prev))
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    daily_stdev = math.sqrt(var)
    return daily_stdev * math.sqrt(252)  # ≈252 trading days per year


def momentum(prices: list[Decimal], lookback: int = 20) -> float | None:
    """Total return over the last ``lookback`` bars: (last - first) / first."""
    if len(prices) <= lookback:
        return None
    earlier = prices[-lookback - 1]
    latest = prices[-1]
    if earlier == 0:
        return None
    return float((latest - earlier) / earlier)


def volume_trend(volumes: list[int], window: int = 5) -> float | None:
    """Recent average volume vs. preceding window average.

    > 1.0 → recent volume is higher than typical. Used as an indication
    of accumulation/distribution conviction behind a price move.
    """
    if len(volumes) < window * 2:
        return None
    recent = volumes[-window:]
    earlier = volumes[-window * 2 : -window]
    earlier_avg = sum(earlier) / len(earlier)
    if earlier_avg == 0:
        return None
    return (sum(recent) / len(recent)) / earlier_avg


__all__ = ["sma", "rsi", "volatility", "momentum", "volume_trend"]
