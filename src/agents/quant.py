"""Quant Agent — code-heavy, LLM explains.

Pulls historical bars from yfinance, computes a deterministic indicator
panel (RSI / SMA-50 / SMA-200 / vol / momentum / volume trend), and asks
the LLM to produce a structured ``AgentReport``. The LLM never sees raw
bars — only the computed numerics — so the explanation is constrained
to what the math says.
"""

from __future__ import annotations

from decimal import Decimal

from src.agents.base import AgentContext, BaseAgent
from src.agents.indicators import (
    momentum,
    rsi,
    sma,
    volatility,
    volume_trend,
)
from src.data import market
from src.llm.client import LLMClient
from src.models import AgentReport


_SYSTEM_PROMPT = """\
You are a Quantitative Technical Analyst.

You will be given a JSON panel of computed indicators for a single ticker.
Your job is to call the QuantAssessment tool with a structured opinion.

Rules:
- The ONLY data you can use is the panel below. Do NOT speculate about
  fundamentals, news, or macro events you weren't given.
- Signal is BUY / HOLD / SELL / INFO. Use INFO if data is insufficient
  (e.g. < 50 bars available, or critical indicators are null).
- Confidence is 0.0–1.0. A single bullish indicator with everything else
  neutral should not exceed 0.5.
- Reasoning must cite specific values (e.g. "RSI 71 is borderline
  overbought"); no vague hand-waving.
- Keep the summary to 1 sentence. Reasoning to 2–4 sentences.
"""


class QuantAgent(BaseAgent):
    name = "quant"

    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm or LLMClient()

    async def run(self, context: AgentContext) -> AgentReport:
        ticker = (
            context.proposed_trade.ticker
            if context.proposed_trade
            else context.intent.ticker
        )
        if not ticker:
            return self._info("Quant has no ticker to analyze.")

        bars = await market.get_history(ticker, period="1y", interval="1d")
        if not bars:
            return self._info(f"No price history available for {ticker}.")

        panel = _build_panel(ticker, bars)
        prompt = _build_prompt(panel)

        report = await self._llm.complete_structured(
            prompt=prompt,
            schema=_QuantAssessment,
            system=_SYSTEM_PROMPT,
            cache_system=True,
        )

        return AgentReport(
            agent_name=self.name,
            signal=report.signal,
            confidence=report.confidence,
            summary=report.summary,
            reasoning=report.reasoning,
            evidence=_panel_evidence(panel),
            metadata={"indicator_panel": panel, "bars_used": len(bars)},
        )

    @staticmethod
    def _info(summary: str) -> AgentReport:
        return AgentReport(
            agent_name="quant",
            signal="INFO",
            confidence=0.0,
            summary=summary,
            reasoning=summary,
        )


# ─── indicator panel ───────────────────────────────────────────────────


def _build_panel(ticker: str, bars: list[market.Bar]) -> dict:
    closes: list[Decimal] = [b.close for b in bars]
    volumes: list[int] = [b.volume for b in bars]
    latest = closes[-1] if closes else None
    return {
        "ticker": ticker,
        "bars_used": len(bars),
        "latest_close": float(latest) if latest is not None else None,
        "rsi_14": _round(rsi(closes, 14)),
        "sma_50": _round(sma(closes, 50)),
        "sma_200": _round(sma(closes, 200)),
        "above_sma_50": _above(latest, sma(closes, 50)),
        "above_sma_200": _above(latest, sma(closes, 200)),
        "annualized_volatility": _round(volatility(closes, 20)),
        "momentum_20d": _round(momentum(closes, 20)),
        "momentum_60d": _round(momentum(closes, 60)),
        "volume_trend_5v5": _round(volume_trend(volumes, 5)),
    }


def _build_prompt(panel: dict) -> str:
    import json

    return (
        "Indicator panel (computed from daily bars):\n\n"
        f"{json.dumps(panel, indent=2, default=str)}\n\n"
        f"Analyze {panel['ticker']} and call the QuantAssessment tool."
    )


def _panel_evidence(panel: dict):
    from src.models import Evidence

    items: list[Evidence] = []
    for key in (
        "rsi_14",
        "sma_50",
        "sma_200",
        "annualized_volatility",
        "momentum_20d",
        "momentum_60d",
        "volume_trend_5v5",
    ):
        val = panel.get(key)
        if val is None:
            continue
        items.append(
            Evidence(
                source="yfinance",
                description=f"{key} = {val}",
                data={"key": key, "value": val},
            )
        )
    return items


def _round(v: float | None, digits: int = 4) -> float | None:
    return None if v is None else round(v, digits)


def _above(price: Decimal | None, ma: float | None) -> bool | None:
    if price is None or ma is None:
        return None
    return float(price) > ma


# ─── structured output schema ──────────────────────────────────────────


from typing import Literal as _Literal

from pydantic import BaseModel as _BaseModel
from pydantic import Field as _Field


class _QuantAssessment(_BaseModel):
    """Quant Agent's structured opinion on a ticker."""

    signal: _Literal["BUY", "HOLD", "SELL", "INFO"]
    confidence: float = _Field(ge=0.0, le=1.0)
    summary: str = _Field(description="One sentence headline")
    reasoning: str = _Field(description="2-4 sentences citing specific indicator values")


__all__ = ["QuantAgent"]
