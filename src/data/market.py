"""yfinance market-data wrapper.

The Quant Agent needs historical OHLCV bars to compute RSI / SMAs / volatility
and the Trending Agent needs recent volume + return for the score. Real-time
quotes come from the brokerage (Robinhood MCP), not yfinance — yfinance is
delayed ~15 minutes.

yfinance is synchronous and blocks; we run calls in a thread pool so the
async orchestrator stays responsive.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import yfinance as yf

from src.data.cache import cached


@dataclass
class Bar:
    """One OHLCV bar. Decimals for price; ints for volume."""

    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


@cached(namespace="yfinance:history", ttl_seconds=300)
async def get_history(
    ticker: str,
    period: str = "6mo",
    interval: str = "1d",
) -> list[Bar]:
    """Historical OHLCV bars.

    ``period`` accepts yfinance shorthand: ``"1d"``, ``"5d"``, ``"1mo"``,
    ``"6mo"``, ``"1y"``, ``"5y"``, ``"max"``. ``interval`` is ``"1d"``,
    ``"1h"``, ``"1m"``, etc.
    """
    df = await asyncio.to_thread(_fetch_history, ticker, period, interval)
    if df is None or df.empty:
        return []
    bars: list[Bar] = []
    for ts, row in df.iterrows():
        bars.append(
            Bar(
                timestamp=ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts,
                open=Decimal(str(row["Open"])),
                high=Decimal(str(row["High"])),
                low=Decimal(str(row["Low"])),
                close=Decimal(str(row["Close"])),
                volume=int(row["Volume"]),
            )
        )
    return bars


def _fetch_history(ticker: str, period: str, interval: str):
    return yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=False)


@cached(namespace="yfinance:movers", ttl_seconds=300)
async def get_volume_movers(limit: int = 50) -> list[str]:
    """Tickers with the largest daily volume — candidate universe for the
    Trending Agent. yfinance exposes this via ``yf.screener``; we wrap so
    the agent doesn't depend on the screener API directly.
    """
    return await asyncio.to_thread(_fetch_movers, limit)


def _fetch_movers(limit: int) -> list[str]:
    try:
        # yf.screener returns a DataFrame keyed on screener id.
        results = yf.screen("most_actives", count=limit)
    except Exception:
        return []
    quotes = results.get("quotes", []) if isinstance(results, dict) else []
    return [q["symbol"] for q in quotes if "symbol" in q][:limit]


__all__ = ["Bar", "get_history", "get_volume_movers"]
