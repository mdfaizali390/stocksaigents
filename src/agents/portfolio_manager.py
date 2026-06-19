"""Portfolio Manager — LLM synthesizer.

Reads every specialist's ``AgentReport`` plus the original query and the
portfolio context, then produces a single ``Recommendation`` with one of
five actions: BUY / SELL / HOLD / NO_ACTION / BLOCKED.

The PM is **not allowed** to override blocking reports — the orchestrator
enforces this structurally (§6.3) by overwriting the PM's action with
BLOCKED whenever any input report has ``blocking=true``. The PM is
informed about blocks via the prompt for narrative quality, but doesn't
get the final say.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from src.data.brokerage.base import Portfolio
from src.llm.client import LLMClient, OPUS_MODEL
from src.models import AgentReport, Recommendation


_SYSTEM_PROMPT = """\
You are the Portfolio Manager.

You will receive:
  - The user's original query
  - A summary of their portfolio (total value, cash, top positions)
  - The proposed trade (if any)
  - Reports from each specialist agent (Research, Quant, Trending, Risk,
    Behavioral, Compliance) — each with signal, confidence, summary,
    reasoning

Your job: call the FinalRecommendation tool with a single coherent
recommendation that synthesizes all the inputs.

Rules:
- Action is BUY / SELL / HOLD / NO_ACTION / BLOCKED.
  - BLOCKED if any specialist returned ``blocking=true``. The system
    will enforce this regardless of your choice; pick BLOCKED so your
    narrative matches.
  - HOLD when signals are mixed or net to neutral.
  - NO_ACTION for market_info / portfolio_analysis queries (no specific
    trade in question).
- Quantity suggestion is in shares; only set when action is BUY/SELL.
  Be conservative — never exceed the smaller of "what the user asked"
  or what Risk/Compliance allow.
- Confidence is your overall conviction in the synthesis (0.0–1.0).
  Reflect agent disagreement: if Quant says BUY 0.8 but Behavioral
  flags FOMO at 0.7, your overall confidence should land around 0.4.
- Summary is one paragraph (3–5 sentences) the user actually reads.
- Cite specific agents in the citations list ("Risk: cash floor",
  "Quant: RSI 78 overbought", etc.) — pulled directly from their
  reports.
- ``block_reasons`` is required when action=BLOCKED; include each
  blocking_reason verbatim from the relevant reports. Empty otherwise.
- For pure-info queries (market_info / portfolio_analysis) use
  NO_ACTION and skip quantity_suggestion.
"""


class _FinalRecommendation(BaseModel):
    """The Portfolio Manager's structured synthesis."""

    action: Literal["BUY", "SELL", "HOLD", "NO_ACTION", "BLOCKED"]
    ticker: str | None = None
    quantity_suggestion: int | None = Field(
        default=None,
        description="Shares for BUY/SELL; null for HOLD / NO_ACTION / BLOCKED",
    )
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str = Field(description="3-5 sentence paragraph for the user")
    citations: list[str] = Field(
        description='Specific facts cited per agent, e.g. "Quant: RSI 78"'
    )
    block_reasons: list[str] = Field(
        default_factory=list,
        description="Required when action=BLOCKED; verbatim blocking_reason from reports",
    )


class PortfolioManagerAgent:
    """LLM synthesizer. Not a BaseAgent — different signature (takes
    multiple reports, not one context)."""

    name = "portfolio_manager"

    def __init__(self, llm: LLMClient | None = None, use_opus: bool = False) -> None:
        self._llm = llm or LLMClient()
        self._model_override = OPUS_MODEL if use_opus else None

    async def synthesize(
        self,
        *,
        query: str,
        reports: list[AgentReport],
        portfolio: Portfolio | None,
        ticker: str | None,
    ) -> Recommendation:
        prompt = _build_prompt(
            query=query, reports=reports, portfolio=portfolio, ticker=ticker
        )
        kwargs = {}
        if self._model_override:
            kwargs["model"] = self._model_override
        out = await self._llm.complete_structured(
            prompt=prompt,
            schema=_FinalRecommendation,
            system=_SYSTEM_PROMPT,
            cache_system=True,
            **kwargs,
        )
        return Recommendation(
            action=out.action,
            ticker=out.ticker or ticker,
            quantity_suggestion=out.quantity_suggestion,
            confidence=out.confidence,
            summary=out.summary,
            citations=out.citations,
            block_reasons=out.block_reasons,
        )


def _build_prompt(
    *,
    query: str,
    reports: list[AgentReport],
    portfolio: Portfolio | None,
    ticker: str | None,
) -> str:
    import json

    portfolio_summary = _portfolio_summary(portfolio)
    blocking_reports = [r for r in reports if r.blocking]
    report_blobs = []
    for r in reports:
        report_blobs.append(
            {
                "agent": r.agent_name,
                "signal": r.signal,
                "blocking": r.blocking,
                "blocking_reason": r.blocking_reason,
                "confidence": r.confidence,
                "summary": r.summary,
                "reasoning": r.reasoning,
            }
        )

    sections = [
        f"User query: {query}",
        "",
        f"Ticker in scope: {ticker or '(none — broad query)'}",
        "",
        "Portfolio summary:",
        portfolio_summary,
        "",
    ]
    if blocking_reports:
        sections.append(
            f"⚠ {len(blocking_reports)} report(s) returned blocking=true. "
            "Your action MUST be BLOCKED. List each blocking_reason verbatim "
            "in block_reasons."
        )
        sections.append("")
    sections.append("Specialist reports:")
    sections.append(json.dumps(report_blobs, indent=2))
    sections.append("")
    sections.append("Call FinalRecommendation.")
    return "\n".join(sections)


def _portfolio_summary(portfolio: Portfolio | None) -> str:
    if portfolio is None:
        return "  (not loaded)"
    return (
        f"  total: {portfolio.total_value} {portfolio.currency}\n"
        f"  equity: {portfolio.equity_value}\n"
        f"  cash: {portfolio.cash}\n"
        f"  buying_power: {portfolio.buying_power.buying_power}"
    )


__all__ = ["PortfolioManagerAgent"]
