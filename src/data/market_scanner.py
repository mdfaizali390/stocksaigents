"""Market Scanner — deterministic Trending Score (§4.7).

Pulls raw signals from Stocktwits, Finnhub (news + analysts), and yfinance,
percentile-ranks each component within the day's candidate universe, then
applies fixed weights to produce a 0-100 score per ticker.

The math is intentionally kept out of the agent prompt — it's deterministic
and unit-testable. The Trending Agent calls ``scan()`` and adds LLM-generated
per-ticker rationale on top.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import zip_longest

from src.data import finnhub, market
from src.data.social import stocktwits
from src.models import TrendingTicker

logger = logging.getLogger(__name__)


# Component weights (must sum to 1.0). See design §4.7.
_WEIGHTS: dict[str, float] = {
    "stocktwits_trending": 0.20,
    "stocktwits_watchers": 0.10,
    "news_volume": 0.20,
    "volume_spike": 0.20,
    "price_momentum": 0.20,
    "analyst_signal": 0.10,
}
assert abs(sum(_WEIGHTS.values()) - 1.0) < 1e-9


@dataclass
class RawSignals:
    """Per-ticker raw inputs before normalization. Public so tests can build
    fixtures without going through the network fetch path."""

    ticker: str
    stocktwits_trending: float | None = None  # platform-computed score
    stocktwits_watchers: int | None = None
    news_count_today: int = 0
    news_baseline_per_day: float = 0.0  # mean daily news count over 30d
    volume_today: int | None = None
    avg_volume_20d: float | None = None
    return_5d: float | None = None  # decimal, e.g. 0.03 = +3%
    net_rec_change: int = 0
    headline_evidence: list[str] | None = None


async def scan(
    candidates: list[str] | None = None,
    *,
    universe_size: int = 30,
    top_n: int | None = None,
) -> list[TrendingTicker]:
    """Compute Trending Scores for a candidate universe.

    If ``candidates`` is None, the universe is the union of yfinance volume
    movers and Stocktwits trending list. Final list is sorted by score
    desc; pass ``top_n`` to truncate.
    """
    # Fetch the Stocktwits trending list exactly ONCE and thread it through
    # both universe construction and the per-ticker lookup. Previously these
    # were two separate fetches (different cache keys), doubling the chance
    # the silent-hang failure mode dropped the entire social dimension.
    trending = await _safe_get_trending(limit=max(universe_size, 50))

    if candidates is None:
        candidates = await _default_universe(universe_size, trending)
    candidates = sorted({c.upper() for c in candidates if c})
    if not candidates:
        return []

    candidate_set = set(candidates)
    trending_lookup = {t.symbol: t for t in trending if t.symbol in candidate_set}
    if trending:
        logger.info(
            "scanner: %d trending tickers, %d in universe (e.g. %s)",
            len(trending),
            len(trending_lookup),
            ", ".join(list(trending_lookup)[:5]) or "none",
        )
    else:
        logger.warning(
            "scanner: Stocktwits trending unavailable — social dimension "
            "will be zero for all tickers this run"
        )

    signals = await asyncio.gather(
        *[_signals_for(t, trending_lookup) for t in candidates],
        return_exceptions=False,
    )
    ranked = score(signals)
    return ranked[: top_n if top_n else len(ranked)]


def score(signals: list[RawSignals]) -> list[TrendingTicker]:
    """Pure function: raw signals → ranked TrendingTicker list.

    Split out so tests can drive scoring math without a network."""
    if not signals:
        return []

    # 1. Per-component raw values (None → 0.0 for ranking purposes).
    raw = {
        "stocktwits_trending": [s.stocktwits_trending or 0.0 for s in signals],
        "stocktwits_watchers": [float(s.stocktwits_watchers or 0) for s in signals],
        "news_volume": [_news_volume_signal(s) for s in signals],
        "volume_spike": [_volume_spike_signal(s) for s in signals],
        "price_momentum": [s.return_5d if s.return_5d is not None else 0.0 for s in signals],
        "analyst_signal": [float(s.net_rec_change) for s in signals],
    }

    # 2. Percentile-rank each component within the universe.
    ranks = {name: percentile_ranks(values) for name, values in raw.items()}

    # 3. Weighted sum → 0-100.
    out: list[TrendingTicker] = []
    for i, s in enumerate(signals):
        components = {name: ranks[name][i] for name in _WEIGHTS}
        total = sum(_WEIGHTS[name] * components[name] for name in _WEIGHTS)
        out.append(
            TrendingTicker(
                ticker=s.ticker,
                score=round(total, 2),
                components={k: round(v, 1) for k, v in components.items()},
                headline_evidence=s.headline_evidence or [],
            )
        )
    out.sort(key=lambda t: t.score, reverse=True)
    return out


def percentile_ranks(values: list[float]) -> list[float]:
    """Percentile rank (0-100) for each value in the list.

    Ties get the average of the ranks they would have occupied. A list
    of all-equal values yields all 50.0 (no signal differentiation, fair).
    """
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [50.0]
    # Sort indexed pairs so we can assign ranks back to the original order.
    indexed = sorted(enumerate(values), key=lambda p: p[1])
    out = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and indexed[j + 1][1] == indexed[i][1]:
            j += 1
        avg_rank = (i + j) / 2.0  # 0-indexed average position of the tied group
        # Map 0..n-1 → 0..100 (0 is bottom, 100 is top).
        pct = (avg_rank / (n - 1)) * 100.0
        for k in range(i, j + 1):
            out[indexed[k][0]] = pct
        i = j + 1
    return out


def _news_volume_signal(s: RawSignals) -> float:
    """Today's news count vs. 30-day daily baseline.

    We use a simple ratio rather than a z-score: counts are sparse and
    standard deviations are unstable for low-volume tickers. Ratio is
    interpretable (1.0 = average day, 3.0 = trending) and downstream
    pct_rank handles cross-ticker normalization.
    """
    if s.news_baseline_per_day <= 0:
        return float(s.news_count_today)
    return s.news_count_today / s.news_baseline_per_day


def _volume_spike_signal(s: RawSignals) -> float:
    if s.volume_today is None or not s.avg_volume_20d:
        return 0.0
    return s.volume_today / s.avg_volume_20d


# ─── Universe construction & per-ticker fetch ──────────────────────────


async def _default_universe(
    universe_size: int,
    trending: list[stocktwits.TrendingSymbol],
) -> list[str]:
    """Volume movers ∪ Stocktwits trending. Deduped, capped at universe_size.

    ``trending`` is passed in (already fetched once by ``scan``) so we
    don't re-fetch it and re-risk the silent-hang failure. yfinance movers
    are fetched here; if that flakes we still keep whatever trending gave us.

    Interleaves the two sources so a Stocktwits-only ticker (e.g. SPCX, a
    recent IPO with no unusual volume yet) isn't starved out of the cap by
    a long movers list.
    """
    movers = await _safe_get_movers(universe_size)
    trending_syms = [s.symbol for s in trending]

    seen: dict[str, None] = {}
    # Round-robin between movers and trending so both sources get
    # representation within the universe_size budget.
    for pair in zip_longest(movers, trending_syms):
        for t in pair:
            if t and t not in seen and len(seen) < universe_size:
                seen[t] = None
    return list(seen.keys())


async def _safe_get_movers(limit: int) -> list[str]:
    try:
        return await market.get_volume_movers(limit=limit)
    except Exception as e:  # noqa: BLE001 — fail soft per design §10 row 8
        logger.warning("volume-movers fetch failed: %s: %s", type(e).__name__, e)
        return []


async def _safe_get_trending(limit: int) -> list[stocktwits.TrendingSymbol]:
    try:
        return await stocktwits.get_trending_symbols(limit=limit)
    except Exception as e:  # noqa: BLE001
        # ReadTimeout str() is empty — log the type so the failure is visible.
        logger.warning("stocktwits trending fetch failed: %s: %s", type(e).__name__, e)
        return []


async def _signals_for(
    ticker: str,
    trending_lookup: dict[str, stocktwits.TrendingSymbol],
) -> RawSignals:
    """Fetch every raw signal for one ticker. All sub-fetches are parallel."""
    news_task = asyncio.create_task(_safe_news(ticker))
    bars_task = asyncio.create_task(_safe_bars(ticker))
    recs_task = asyncio.create_task(_safe_recs(ticker))
    stream_task = (
        asyncio.create_task(_safe_stream(ticker))
        if ticker not in trending_lookup
        else None
    )
    news = await news_task
    bars = await bars_task
    recs = await recs_task
    stream = await stream_task if stream_task is not None else None

    trend = trending_lookup.get(ticker)
    stocktwits_trending = trend.trending_score if trend else None
    stocktwits_watchers = trend.watchlist_count if trend else (
        stream.watchlist_count if stream else None
    )

    news_count_today, baseline_per_day, evidence = _summarize_news(news)
    vol_today, avg_vol_20d = _volume_components(bars)
    return_5d = _five_day_return(bars)
    net_change = finnhub.net_recommendation_change(recs, months=1)

    return RawSignals(
        ticker=ticker,
        stocktwits_trending=stocktwits_trending,
        stocktwits_watchers=stocktwits_watchers,
        news_count_today=news_count_today,
        news_baseline_per_day=baseline_per_day,
        volume_today=vol_today,
        avg_volume_20d=avg_vol_20d,
        return_5d=return_5d,
        net_rec_change=net_change,
        headline_evidence=evidence,
    )


async def _safe_news(ticker: str) -> list[finnhub.NewsItem]:
    try:
        return await finnhub.get_company_news(ticker, days_back=30)
    except Exception as e:  # noqa: BLE001
        logger.warning("news fetch failed for %s: %s: %s", ticker, type(e).__name__, e)
        return []


async def _safe_bars(ticker: str) -> list[market.Bar]:
    try:
        return await market.get_history(ticker, period="3mo", interval="1d")
    except Exception as e:  # noqa: BLE001
        logger.warning("history fetch failed for %s: %s: %s", ticker, type(e).__name__, e)
        return []


async def _safe_recs(ticker: str) -> list[finnhub.AnalystRec]:
    try:
        return await finnhub.get_analyst_recommendations(ticker)
    except Exception as e:  # noqa: BLE001
        logger.warning("analyst recs fetch failed for %s: %s: %s", ticker, type(e).__name__, e)
        return []


async def _safe_stream(ticker: str) -> stocktwits.StreamSnapshot | None:
    try:
        return await stocktwits.get_symbol_stream(ticker, limit=10)
    except Exception as e:  # noqa: BLE001
        logger.warning("stocktwits stream fetch failed for %s: %s: %s", ticker, type(e).__name__, e)
        return None


def _summarize_news(
    items: list[finnhub.NewsItem],
) -> tuple[int, float, list[str]]:
    """Returns (today_count, mean_daily_count_over_30d, top_headlines)."""
    if not items:
        return 0, 0.0, []
    now = datetime.now(timezone.utc)
    cutoff_today = now - timedelta(hours=24)
    cutoff_30d = now - timedelta(days=30)
    today = 0
    by_day: Counter[str] = Counter()
    for item in items:
        if item.published_at >= cutoff_today:
            today += 1
        if item.published_at >= cutoff_30d:
            by_day[item.published_at.date().isoformat()] += 1
    days_seen = max(len(by_day), 1)
    baseline = sum(by_day.values()) / days_seen
    sorted_recent = sorted(items, key=lambda n: n.published_at, reverse=True)
    headlines = [n.headline for n in sorted_recent[:2] if n.headline]
    return today, baseline, headlines


def _volume_components(bars: list[market.Bar]) -> tuple[int | None, float | None]:
    """Most recent bar's volume + 20-day average of preceding bars."""
    if not bars:
        return None, None
    latest = bars[-1].volume
    window = bars[-21:-1] if len(bars) >= 21 else bars[:-1]
    if not window:
        return latest, None
    avg = sum(b.volume for b in window) / len(window)
    return latest, avg


def _five_day_return(bars: list[market.Bar]) -> float | None:
    """(latest_close - close_5_days_ago) / close_5_days_ago."""
    if len(bars) < 6:
        return None
    latest = bars[-1].close
    earlier = bars[-6].close
    if earlier == 0:
        return None
    return float((latest - earlier) / earlier)


__all__ = [
    "RawSignals",
    "scan",
    "score",
    "percentile_ranks",
]
