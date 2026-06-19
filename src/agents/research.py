"""Research Agent — LLM-heavy, fundamentals + news context.

Pulls recent news, the next earnings event, and SEC filing metadata for
a ticker, then asks the LLM to synthesize a BUY/HOLD/SELL/INFO opinion
backed by specific items it cited. Filings are surfaced as links only —
we do not read filing text (design §4.2).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from src.agents.base import AgentContext, BaseAgent
from src.data import finnhub
from src.llm.client import LLMClient
from src.models import AgentReport, Evidence


_SYSTEM_PROMPT = """\
You are a Fundamental Research Analyst.

You will receive:
  - A ticker
  - Recent news headlines + summaries (last 14 days, up to 10)
  - The next earnings event if scheduled
  - SEC filing metadata (form, date, link) for the last 30 days

Your job: call the ResearchAssessment tool with a structured opinion.

Rules:
- Signal is BUY / HOLD / SELL / INFO. Use INFO when news is sparse or
  context is too thin to support a directional view.
- Reasoning must reference SPECIFIC headlines or events from the input.
  Never speculate beyond what's there.
- Do not read between the lines on filings — you only see metadata
  (form type, date, URL). You can flag a 10-Q or 8-K as worth checking
  but do not pretend to know its contents.
- Keep summary to 1 sentence. Reasoning to 2-4 sentences.
- Confidence is 0.0–1.0; sparse-news → confidence ≤ 0.4.
"""


class _ResearchAssessment(BaseModel):
    """Research Agent's structured opinion on a ticker."""

    signal: Literal["BUY", "HOLD", "SELL", "INFO"]
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str
    reasoning: str
    cited_headlines: list[str] = Field(
        default_factory=list,
        description="Headlines you cited in the reasoning, verbatim",
    )


class ResearchAgent(BaseAgent):
    name = "research"

    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm or LLMClient()

    async def run(self, context: AgentContext) -> AgentReport:
        ticker = (
            context.proposed_trade.ticker
            if context.proposed_trade
            else context.intent.ticker
        )
        if not ticker:
            return self._info("Research has no ticker to analyze.")

        news = await finnhub.get_company_news(ticker, days_back=14)
        earnings = await finnhub.get_earnings_calendar(ticker, days_ahead=60)
        filings = await finnhub.get_sec_filings(ticker, days_back=30)

        if not news and not earnings and not filings:
            return self._info(f"No research data available for {ticker}.")

        prompt = _build_prompt(ticker, news[:10], earnings[:1], filings[:5])
        out = await self._llm.complete_structured(
            prompt=prompt,
            schema=_ResearchAssessment,
            system=_SYSTEM_PROMPT,
            cache_system=True,
        )

        return AgentReport(
            agent_name=self.name,
            signal=out.signal,
            confidence=out.confidence,
            summary=out.summary,
            reasoning=out.reasoning,
            evidence=_evidence(news[:5], earnings[:1], filings[:5]),
            metadata={
                "news_count": len(news),
                "filings_count": len(filings),
                "next_earnings": earnings[0].date if earnings else None,
                "cited_headlines": out.cited_headlines,
            },
        )

    @staticmethod
    def _info(summary: str) -> AgentReport:
        return AgentReport(
            agent_name="research",
            signal="INFO",
            confidence=0.0,
            summary=summary,
            reasoning=summary,
        )


def _build_prompt(
    ticker: str,
    news: list[finnhub.NewsItem],
    earnings: list[finnhub.EarningsEvent],
    filings: list[finnhub.Filing],
) -> str:
    lines = [f"Ticker: {ticker}", ""]

    lines.append(f"News (last 14 days, {len(news)} items shown):")
    for n in news:
        date = n.published_at.date().isoformat()
        summary = (n.summary or "")[:200]
        lines.append(f"  - [{date}] {n.headline} — {summary}")
    if not news:
        lines.append("  (none)")
    lines.append("")

    if earnings:
        e = earnings[0]
        when = e.hour or "intraday"
        lines.append(
            f"Next earnings: {e.symbol} on {e.date} ({when}); "
            f"EPS estimate: {e.eps_estimate}"
        )
    else:
        lines.append("Next earnings: none scheduled in window")
    lines.append("")

    lines.append(f"Recent SEC filings (last 30 days, {len(filings)} shown):")
    for f in filings:
        lines.append(f"  - {f.form} on {f.filed_at[:10]} — {f.filing_url}")
    if not filings:
        lines.append("  (none)")
    lines.append("")
    lines.append(f"Call ResearchAssessment with your opinion on {ticker}.")
    return "\n".join(lines)


def _evidence(
    news: list[finnhub.NewsItem],
    earnings: list[finnhub.EarningsEvent],
    filings: list[finnhub.Filing],
) -> list[Evidence]:
    items: list[Evidence] = []
    for n in news:
        items.append(
            Evidence(
                source="finnhub:news",
                description=n.headline,
                data={
                    "date": n.published_at.isoformat(),
                    "url": n.url,
                    "source": n.source,
                },
            )
        )
    for e in earnings:
        items.append(
            Evidence(
                source="finnhub:earnings",
                description=f"Next earnings {e.date}",
                data={"hour": e.hour, "eps_estimate": e.eps_estimate},
            )
        )
    for f in filings[:3]:
        items.append(
            Evidence(
                source="finnhub:filings",
                description=f"{f.form} filed {f.filed_at[:10]}",
                data={"url": f.filing_url},
            )
        )
    return items


__all__ = ["ResearchAgent"]
