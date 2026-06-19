"""Stocktwits public API wrapper.

Used by the Trending Agent for the social-attention component of the
Trending Score (§4.7). No auth, no key — relies on the undocumented
``/api/2/`` endpoints the Stocktwits site itself uses.

Stability: these endpoints are not officially documented and could
change without notice. The Trending Agent treats Stocktwits as
**fail-soft** (§10 row 8): if calls fail, the scanner produces a
ranked list from price/news/analyst components alone.

Concurrency hazard (learned the hard way): Stocktwits does NOT return a
clean 429 under load — it silently stops responding and requests hang
until they ReadTimeout (observed: 10 of 30 concurrent calls timed out).
When this hit the universe-construction trending fetch, the entire
social-attention dimension silently vanished and Stocktwits-only tickers
(e.g. SPCX) never became scanner candidates. Two defenses, mirroring the
Finnhub wrapper:

  1. A sliding-window throttle caps outgoing calls (spacing them so we
     don't trigger the silent-hang behavior).
  2. Retry-on-timeout with exponential backoff.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from src.data.cache import cached
from src.data.ratelimit import SlidingWindowLimiter, retry_async

_BASE = "https://api.stocktwits.com/api/2"
_USER_AGENT = "stock-ai-poc/0.1 (+research)"

# Stocktwits publishes no rate limit. Empirically it tolerates ~moderate
# request rates but hangs under a 30-wide concurrent burst. We cap at 20
# calls / 10s — fast enough for a scan, gentle enough to avoid the hang.
_RATE_LIMIT = 20
_RATE_WINDOW_SECONDS = 10.0
_MAX_RETRIES = 3
_RETRY_BASE_SECONDS = 1.5
# Generous per-request timeout: Stocktwits is slow even when healthy.
_TIMEOUT = 12.0

_LIMITER = SlidingWindowLimiter(_RATE_LIMIT, _RATE_WINDOW_SECONDS)


@dataclass
class TrendingSymbol:
    """One entry from Stocktwits' platform-wide trending list."""

    symbol: str
    title: str | None
    exchange: str | None
    sector: str | None
    industry: str | None
    instrument_class: str | None
    region: str | None
    trending_score: float | None
    watchlist_count: int | None
    rank: int | None


@dataclass
class StreamSnapshot:
    """Per-ticker stream summary. Used when a candidate is in our universe
    but not in Stocktwits' trending list."""

    symbol: str
    watchlist_count: int | None
    message_count: int  # number of messages returned in the stream sample
    sample_messages: list[str]  # short snippets, top of stream


class _TransientServerError(Exception):
    """Raised for retryable 5xx responses (504 Gateway Timeout etc.).

    Stocktwits, under load, returns 502/503/504 as often as it hangs.
    These are transient and worth retrying — unlike 4xx, which won't
    recover (e.g. 404 for a ticker Stocktwits doesn't track).
    """


async def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    """Throttled + retried GET against Stocktwits.

    Throttle spaces calls to avoid the silent-hang-under-load behavior.
    Retry covers the transient failures Stocktwits actually exhibits:
    ReadTimeout (silent hang), transport errors, and 5xx server errors.
    4xx responses are NOT retried — they won't recover.
    """
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}

    async def _attempt() -> Any:
        await _LIMITER.acquire()
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=headers) as client:
            resp = await client.get(f"{_BASE}{path}", params=params or None)
            if resp.status_code >= 500:
                raise _TransientServerError(f"{resp.status_code} for {path}")
            resp.raise_for_status()  # 4xx → non-retryable HTTPStatusError
            return resp.json()

    return await retry_async(
        _attempt,
        retry_on=(httpx.TimeoutException, httpx.TransportError, _TransientServerError),
        max_attempts=_MAX_RETRIES,
        base_delay=_RETRY_BASE_SECONDS,
        label=f"stocktwits {path}",
    )


_TRADABLE_INSTRUMENT_CLASSES = {"Stock", "ExchangeTradedFund"}


def _is_us_equity(item: dict[str, Any]) -> bool:
    """Filter out crypto, options, futures, and non-US tickers.

    Stocktwits returns crypto with ``exchange="CRYPTO"`` and ``region="X"``.
    Real equities/ETFs use ``instrument_class`` of ``"Stock"`` or
    ``"ExchangeTradedFund"``.
    """
    if item.get("exchange") == "CRYPTO":
        return False
    if item.get("region") and item["region"] != "US":
        return False
    instrument = item.get("instrument_class")
    if instrument and instrument not in _TRADABLE_INSTRUMENT_CLASSES:
        return False
    return True


@cached(namespace="stocktwits:trending", ttl_seconds=300)
async def get_trending_symbols(limit: int = 30) -> list[TrendingSymbol]:
    """Stocktwits' platform-wide trending list — US equities only.

    Returns up to ``limit`` items, ranked by Stocktwits' own
    ``trending_score`` (which already aggregates message volume,
    watcher growth, and engagement).
    """
    raw = await _get("/trending/symbols.json")
    symbols = (raw or {}).get("symbols", []) or []
    items: list[TrendingSymbol] = []
    for s in symbols:
        if not _is_us_equity(s):
            continue
        items.append(
            TrendingSymbol(
                symbol=s.get("symbol", ""),
                title=s.get("title"),
                exchange=s.get("exchange"),
                sector=s.get("sector"),
                industry=s.get("industry"),
                instrument_class=s.get("instrument_class"),
                region=s.get("region"),
                trending_score=_to_float(s.get("trending_score")),
                watchlist_count=_to_int(s.get("watchlist_count")),
                rank=_to_int(s.get("rank")),
            )
        )
        if len(items) >= limit:
            break
    return items


@cached(namespace="stocktwits:stream", ttl_seconds=600)
async def get_symbol_stream(ticker: str, limit: int = 20) -> StreamSnapshot:
    """Per-ticker stream snapshot. Used as a fallback for candidates not
    in the trending list — gives us at least ``message_count`` and the
    ticker's ``watchlist_count`` so the scoring components have a value.
    """
    raw = await _get(f"/streams/symbol/{ticker.upper()}.json", params={"limit": limit})
    symbol_meta = (raw or {}).get("symbol", {}) or {}
    messages = (raw or {}).get("messages", []) or []
    samples: list[str] = []
    for m in messages[:5]:
        body = (m.get("body") or "").strip()
        if body:
            samples.append(body[:140])
    return StreamSnapshot(
        symbol=symbol_meta.get("symbol", ticker.upper()),
        watchlist_count=_to_int(symbol_meta.get("watchlist_count")),
        message_count=len(messages),
        sample_messages=samples,
    )


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


__all__ = [
    "TrendingSymbol",
    "StreamSnapshot",
    "get_trending_symbols",
    "get_symbol_stream",
]
