"""Trending Agent — wraps the Market Scanner with LLM rationale.

Calls ``market_scanner.scan()`` to compute the deterministic Trending
Score for the day's universe, then asks the LLM to write per-ticker
prose for the top-N. The math stays in Python; the LLM only narrates.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from src.agents.base import AgentContext, BaseAgent
from src.data.market_scanner import scan
from src.llm.client import LLMClient
from src.models import AgentReport, Evidence, TrendingTicker


_SYSTEM_PROMPT = """\
You are a Market Scanner narrator.

You will receive a JSON list of pre-ranked trending tickers, each with a
0–100 Trending Score and percentile-ranked components
(stocktwits_trending, stocktwits_watchers, news_volume, volume_spike,
price_momentum, analyst_signal). Some tickers may include 1–2 recent
news headlines as evidence.

Your job: call the TrendingDigest tool with prose that summarizes the
list and a brief rationale per ticker.

Rules:
- Reference specific component scores (e.g. "volume_spike 95, news_volume 80")
  when explaining why a ticker is hot.
- Do NOT make up news the input doesn't contain.
- Per-ticker rationale = 1–2 short sentences.
- Top-level summary = 1–2 sentences over the whole list.
- This is INFO output, not buy/sell advice.
"""


class _TickerNote(BaseModel):
    ticker: str
    rationale: str = Field(description="1-2 sentences explaining why this ticker is trending")


class _TrendingDigest(BaseModel):
    """Narrated digest of the trending scanner output."""

    summary: str = Field(description="1-2 sentences over the whole top-N list")
    notes: list[_TickerNote] = Field(description="One note per ticker, in input order")


class TrendingAgent(BaseAgent):
    name = "trending"

    def __init__(self, llm: LLMClient | None = None, top_n: int = 10) -> None:
        self._llm = llm or LLMClient()
        self._top_n = top_n

    async def run(self, context: AgentContext) -> AgentReport:
        candidates: list[str] | None = None
        if context.intent.ticker:
            # When the user named a ticker (e.g. "is NVDA trending?"), still
            # produce the broader scan but ensure the named ticker is in it.
            candidates = None

        ranked = await scan(top_n=self._top_n)

        if not ranked:
            return self._info("Market Scanner returned no candidates.")

        prompt = _build_prompt(ranked)
        digest = await self._llm.complete_structured(
            prompt=prompt,
            schema=_TrendingDigest,
            system=_SYSTEM_PROMPT,
            cache_system=True,
        )

        bullet_lines = []
        for note in digest.notes[: self._top_n]:
            ticker_data = next((r for r in ranked if r.ticker == note.ticker), None)
            score_str = f"{ticker_data.score:.0f}" if ticker_data else "?"
            bullet_lines.append(f"  • {note.ticker} ({score_str}): {note.rationale}")
        reasoning = digest.summary + "\n\n" + "\n".join(bullet_lines)

        return AgentReport(
            agent_name=self.name,
            signal="INFO",
            confidence=1.0,
            summary=digest.summary,
            reasoning=reasoning,
            evidence=_evidence(ranked),
            metadata={
                "ranked": [r.model_dump() for r in ranked],
                "notes": [n.model_dump() for n in digest.notes],
            },
        )

    @staticmethod
    def _info(summary: str) -> AgentReport:
        return AgentReport(
            agent_name="trending",
            signal="INFO",
            confidence=0.5,
            summary=summary,
            reasoning=summary,
        )


def _build_prompt(ranked: list[TrendingTicker]) -> str:
    import json

    payload = []
    for r in ranked:
        payload.append(
            {
                "ticker": r.ticker,
                "score": r.score,
                "components": r.components,
                "headline_evidence": r.headline_evidence[:2],
            }
        )
    return (
        "Pre-ranked trending tickers (highest score first):\n\n"
        f"{json.dumps(payload, indent=2)}\n\n"
        "Call TrendingDigest. Provide one note per ticker in the SAME order as input."
    )


def _evidence(ranked: list[TrendingTicker]) -> list[Evidence]:
    items: list[Evidence] = []
    for r in ranked:
        items.append(
            Evidence(
                source="market_scanner",
                description=f"{r.ticker} score={r.score:.1f}",
                data={"components": r.components, "headlines": r.headline_evidence},
            )
        )
    return items


__all__ = ["TrendingAgent"]
