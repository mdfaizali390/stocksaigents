"""Finnhub REST API wrapper.

Used by the Research Agent (company news, earnings, SEC filing metadata) and
the Trending Agent (analyst recommendations, news volume).

Free tier is **60 calls/min**, enforced per-IP. Two defenses:

  1. **Sliding-window throttle** — a process-wide async limiter caps
     outgoing requests at ~55/min (small headroom under the limit).
     This *prevents* 429s in the first place when many agents fan out
     concurrently (e.g. the Trending Agent's 30-ticker scan).

  2. **Retry-on-429 with backoff** — Finnhub returns no Retry-After
     header (verified empirically), so we use exponential backoff
     starting at 2s. Caps at 3 attempts.

Combined with disk caching, a typical session stays well under the
limit even when running multiple agents back-to-back.

Endpoint reference: https://finnhub.io/docs/api
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from src.config import get_settings
from src.data.cache import cached
from src.data.ratelimit import SlidingWindowLimiter

_BASE = "https://finnhub.io/api/v1"

# Throttle config. The free tier is 60/min; we leave 5 calls of headroom.
_RATE_LIMIT = 55
_RATE_WINDOW_SECONDS = 60.0
_MAX_RETRIES = 3
_RETRY_BASE_SECONDS = 2.0


_LIMITER = SlidingWindowLimiter(_RATE_LIMIT, _RATE_WINDOW_SECONDS)


@dataclass
class NewsItem:
    headline: str
    source: str
    summary: str
    url: str
    image: str | None
    category: str | None
    published_at: datetime
    related: str | None  # comma-separated tickers per Finnhub


@dataclass
class EarningsEvent:
    symbol: str
    date: str  # ISO date
    hour: str | None  # "bmo" | "amc" | None
    eps_estimate: float | None
    eps_actual: float | None
    revenue_estimate: float | None
    revenue_actual: float | None
    quarter: int | None
    year: int | None


@dataclass
class Filing:
    symbol: str
    cik: str | None
    form: str
    filed_at: str
    accepted_at: str | None
    report_url: str
    filing_url: str


@dataclass
class CompanyProfile:
    """Subset of /stock/profile2 we actually use. ``industry`` is the
    Finnhub-classified sector (e.g. "Technology", "Healthcare")."""

    ticker: str
    name: str | None
    industry: str | None
    exchange: str | None
    market_cap: float | None
    country: str | None


@dataclass
class AnalystRec:
    symbol: str
    period: str  # YYYY-MM-DD (start of month)
    strong_buy: int
    buy: int
    hold: int
    sell: int
    strong_sell: int


async def _get(path: str, params: dict[str, Any]) -> Any:
    """Throttled + retried GET against Finnhub.

    Throttle prevents 429s under normal load; retry handles the edge
    where our window clock drifts from Finnhub's.
    """
    token = get_settings().require_finnhub()
    full_params = {**params, "token": token}
    last_error: Exception | None = None

    for attempt in range(_MAX_RETRIES):
        await _LIMITER.acquire()
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(f"{_BASE}{path}", params=full_params)
                if resp.status_code == 429:
                    # Honour Retry-After if present; otherwise exponential.
                    retry_after = resp.headers.get("Retry-After")
                    delay = (
                        float(retry_after)
                        if retry_after and retry_after.isdigit()
                        else _RETRY_BASE_SECONDS * (2**attempt)
                    )
                    await asyncio.sleep(delay)
                    continue
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as e:
            last_error = e
            # Non-429 status — don't retry.
            raise
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_error = e
            await asyncio.sleep(_RETRY_BASE_SECONDS * (2**attempt))
            continue

    raise RuntimeError(
        f"Finnhub {path} failed after {_MAX_RETRIES} attempts: {last_error}"
    )


@cached(namespace="finnhub:news", ttl_seconds=300)
async def get_company_news(
    ticker: str,
    days_back: int = 14,
) -> list[NewsItem]:
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days_back)
    raw = await _get(
        "/company-news",
        {"symbol": ticker, "from": start.isoformat(), "to": today.isoformat()},
    )
    items: list[NewsItem] = []
    for n in raw or []:
        items.append(
            NewsItem(
                headline=n.get("headline", ""),
                source=n.get("source", ""),
                summary=n.get("summary", ""),
                url=n.get("url", ""),
                image=n.get("image") or None,
                category=n.get("category") or None,
                published_at=datetime.fromtimestamp(n["datetime"], tz=timezone.utc),
                related=n.get("related") or None,
            )
        )
    return items


@cached(namespace="finnhub:earnings", ttl_seconds=3600)
async def get_earnings_calendar(
    ticker: str | None = None,
    days_ahead: int = 14,
) -> list[EarningsEvent]:
    today = datetime.now(timezone.utc).date()
    end = today + timedelta(days=days_ahead)
    params: dict[str, Any] = {"from": today.isoformat(), "to": end.isoformat()}
    if ticker:
        params["symbol"] = ticker
    raw = await _get("/calendar/earnings", params)
    events = (raw or {}).get("earningsCalendar", []) or []
    items: list[EarningsEvent] = []
    for e in events:
        items.append(
            EarningsEvent(
                symbol=e.get("symbol", ""),
                date=e.get("date", ""),
                hour=e.get("hour") or None,
                eps_estimate=e.get("epsEstimate"),
                eps_actual=e.get("epsActual"),
                revenue_estimate=e.get("revenueEstimate"),
                revenue_actual=e.get("revenueActual"),
                quarter=e.get("quarter"),
                year=e.get("year"),
            )
        )
    return items


@cached(namespace="finnhub:filings", ttl_seconds=3600)
async def get_sec_filings(
    ticker: str,
    days_back: int = 90,
) -> list[Filing]:
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days_back)
    raw = await _get(
        "/stock/filings",
        {"symbol": ticker, "from": start.isoformat(), "to": today.isoformat()},
    )
    items: list[Filing] = []
    for f in raw or []:
        items.append(
            Filing(
                symbol=f.get("symbol", ticker),
                cik=f.get("cik") or None,
                form=f.get("form", ""),
                filed_at=f.get("filedDate", ""),
                accepted_at=f.get("acceptedDate") or None,
                report_url=f.get("reportUrl", ""),
                filing_url=f.get("filingUrl", ""),
            )
        )
    return items


@cached(namespace="finnhub:profile", ttl_seconds=86400)
async def get_company_profile(ticker: str) -> CompanyProfile | None:
    raw = await _get("/stock/profile2", {"symbol": ticker})
    if not raw:
        return None
    return CompanyProfile(
        ticker=raw.get("ticker", ticker),
        name=raw.get("name"),
        industry=raw.get("finnhubIndustry"),
        exchange=raw.get("exchange"),
        market_cap=raw.get("marketCapitalization"),
        country=raw.get("country"),
    )


@cached(namespace="finnhub:analyst", ttl_seconds=3600)
async def get_analyst_recommendations(ticker: str) -> list[AnalystRec]:
    raw = await _get("/stock/recommendation", {"symbol": ticker})
    items: list[AnalystRec] = []
    for r in raw or []:
        items.append(
            AnalystRec(
                symbol=r.get("symbol", ticker),
                period=r.get("period", ""),
                strong_buy=int(r.get("strongBuy") or 0),
                buy=int(r.get("buy") or 0),
                hold=int(r.get("hold") or 0),
                sell=int(r.get("sell") or 0),
                strong_sell=int(r.get("strongSell") or 0),
            )
        )
    return items


def net_recommendation_change(recs: list[AnalystRec], months: int = 1) -> int:
    """Net (upgrades - downgrades) over the last ``months`` periods. Used by
    the Trending Score's analyst component.

    Finnhub returns one entry per month (most recent first). We approximate
    "net upgrades" as the change in (strong_buy + buy) minus the change in
    (sell + strong_sell) between the most recent and the prior month.
    """
    if len(recs) < 2:
        return 0
    recent = recs[0]
    prior = recs[months] if months < len(recs) else recs[-1]
    bullish_delta = (recent.strong_buy + recent.buy) - (prior.strong_buy + prior.buy)
    bearish_delta = (recent.sell + recent.strong_sell) - (prior.sell + prior.strong_sell)
    return bullish_delta - bearish_delta


__all__ = [
    "NewsItem",
    "EarningsEvent",
    "Filing",
    "AnalystRec",
    "CompanyProfile",
    "get_company_news",
    "get_earnings_calendar",
    "get_sec_filings",
    "get_analyst_recommendations",
    "get_company_profile",
    "net_recommendation_change",
]
