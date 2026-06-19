"""Live smoke test for the Market Scanner.

Run from repo root::

    .venv/bin/python -m scripts.scan_trending

Pulls Stocktwits + Finnhub + yfinance, computes Trending Scores, and
prints the top 10 ranked tickers with component breakdowns. Hits real
APIs — uses the disk cache, so re-runs within TTL are instant.
"""

from __future__ import annotations

import asyncio
import logging

from src.data.market_scanner import scan

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")


async def main() -> None:
    print("Scanning trending tickers (this will take ~30s on a cold cache)…")
    result = await scan(top_n=10)

    if not result:
        print("\n(no candidates returned)")
        return

    print(f"\nTop {len(result)} ranked by Trending Score:")
    header = (
        f"{'#':>2}  {'Ticker':<7} {'Score':>6}  {'ST_T':>5} {'ST_W':>5} "
        f"{'News':>5} {'Vol':>5} {'Mom':>5} {'Anlst':>5}"
    )
    print(header)
    print("-" * len(header))
    for i, t in enumerate(result, 1):
        c = t.components
        print(
            f"{i:>2}  {t.ticker:<7} {t.score:>6.1f}  "
            f"{c['stocktwits_trending']:>5.0f} "
            f"{c['stocktwits_watchers']:>5.0f} "
            f"{c['news_volume']:>5.0f} "
            f"{c['volume_spike']:>5.0f} "
            f"{c['price_momentum']:>5.0f} "
            f"{c['analyst_signal']:>5.0f}"
        )
        if t.headline_evidence:
            print(f"      └─ {t.headline_evidence[0][:90]}")


if __name__ == "__main__":
    asyncio.run(main())
