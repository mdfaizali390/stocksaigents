"""Live smoke test for the market-data wrappers.

Usage::

    .venv/bin/python -m scripts.probe_market_data

Hits yfinance and Finnhub for AAPL and prints concise summaries. Useful
to confirm API keys are wired and response shapes parse cleanly.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from src.data import finnhub, market

TICKER = "AAPL"


async def probe_yfinance() -> None:
    print(f"\n=== yfinance: {TICKER} (6mo daily) ===")
    bars = await market.get_history(TICKER, period="6mo", interval="1d")
    if not bars:
        print("  (no bars returned)")
        return
    first, last = bars[0], bars[-1]
    print(f"  bars: {len(bars)}")
    print(f"  first: {first.timestamp.date()}  close={first.close}")
    print(f"  last:  {last.timestamp.date()}  close={last.close}  vol={last.volume}")

    print(f"\n=== yfinance: top volume movers ===")
    movers = await market.get_volume_movers(limit=10)
    print(f"  {movers}")


async def probe_finnhub() -> None:
    print(f"\n=== Finnhub: {TICKER} company news (last 7d) ===")
    news = await finnhub.get_company_news(TICKER, days_back=7)
    print(f"  items: {len(news)}")
    for n in news[:3]:
        print(f"    · {n.published_at.date()}  [{n.source}]  {n.headline[:80]}")

    print(f"\n=== Finnhub: {TICKER} earnings (next 14d) ===")
    earnings = await finnhub.get_earnings_calendar(TICKER, days_ahead=90)
    print(f"  events: {len(earnings)}")
    for e in earnings[:3]:
        print(f"    · {e.date} {e.hour or '-'}  EPS est={e.eps_estimate}")

    print(f"\n=== Finnhub: {TICKER} SEC filings (last 90d) ===")
    filings = await finnhub.get_sec_filings(TICKER, days_back=90)
    print(f"  filings: {len(filings)}")
    for f in filings[:3]:
        print(f"    · {f.filed_at}  {f.form}  -> {f.filing_url}")

    print(f"\n=== Finnhub: {TICKER} analyst recommendations ===")
    recs = await finnhub.get_analyst_recommendations(TICKER)
    print(f"  rows: {len(recs)}")
    for r in recs[:3]:
        print(
            f"    · {r.period}  SB={r.strong_buy} B={r.buy} H={r.hold} "
            f"S={r.sell} SS={r.strong_sell}"
        )
    print(f"  net change (last vs prior month): {finnhub.net_recommendation_change(recs)}")


async def main() -> None:
    print(f"Probing market-data wrappers at {datetime.now(timezone.utc).isoformat()}")
    await probe_yfinance()
    await probe_finnhub()
    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
