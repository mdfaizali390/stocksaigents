"""Risk Agent — rule-heavy math, LLM explains.

Computes the post-trade portfolio impact on a proposed buy/sell:
  - Concentration in this stock (current and after the trade)
  - Sector exposure (current and after)
  - Cash % after the trade

Hard rule violations (limits set in the Constitution) produce
``signal="BLOCK"`` with ``blocking=true`` — these flow into the
orchestrator's structural block check (§6.3). Soft concerns (e.g. close
to a limit but not over it) produce ``WARNING``.

The LLM is allowed to narrate, but block/warning decisions are made by
Python from numeric facts, not by the model.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from src.agents.base import AgentContext, BaseAgent
from src.constitution.schema import Constitution
from src.data import finnhub
from src.data.brokerage.base import Portfolio, Position
from src.llm.client import LLMClient
from src.models import AgentReport, Evidence, ProposedTrade


_SYSTEM_PROMPT = """\
You are a Risk Analyst.

You will receive a JSON ``RiskAssessment`` payload describing the
post-trade portfolio impact: concentration in the proposed ticker,
sector exposure, and cash %, both before and after, plus the
Constitution's limits.

Your job: call the RiskNarrative tool with a structured opinion.

Rules:
- The Python layer has already computed whether any HARD limits are
  breached. You do NOT re-decide that — accept the precomputed
  ``violations`` list as authoritative.
- Your job is the prose: a 1-sentence summary and 2-4 sentence
  reasoning that walks through the most important numbers.
- Cite specific percentages (e.g. "post-trade concentration would be
  18% vs. the 15% single-stock limit").
- Confidence is high (0.8-1.0) when violations are present, since
  these are deterministic facts. Otherwise keep confidence proportional
  to how close to limits the trade lands.
"""


_SNAPSHOT_SYSTEM_PROMPT = """\
You are a Risk Analyst answering a portfolio-snapshot question.

You will receive a JSON payload describing the user's CURRENT portfolio:
top single-stock concentrations, sector exposures, cash %, plus the
Constitution's limits, and lists of ``violations`` (current state already
exceeds a limit) and ``warnings`` (within 10% of a limit).

Your job: call the RiskNarrative tool with a structured opinion.

Rules:
- The math (which sectors / stocks exceed which limits) is already
  computed. You do NOT re-decide that — accept the precomputed lists.
- Walk through the top 2-3 specific concentrations by name and number
  (e.g. "Technology sector at 38%, above the 30% limit"). Be concrete.
- If everything is within limits, say so plainly and note the closest
  exposure (e.g. "Tech at 24% is well below the 30% limit").
- Confidence is high (0.8-1.0) when violations exist (deterministic
  facts). Otherwise reflect how close limits are.
- Summary = 1 sentence answering the user's question. Reasoning = 2-4
  sentences with specific numbers.
"""


class _RiskNarrative(BaseModel):
    """Risk Agent's narrative wrapping the precomputed analysis."""

    confidence: float = Field(ge=0.0, le=1.0)
    summary: str
    reasoning: str


class RiskAgent(BaseAgent):
    name = "risk"

    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm or LLMClient()

    async def run(self, context: AgentContext) -> AgentReport:
        if context.portfolio is None:
            return self._info("Portfolio data unavailable; can't analyze risk.")

        positions = context.market.get("positions", []) or []

        # Two modes:
        #   1. Trade impact (proposed_trade present) — pre/post comparison
        #   2. Portfolio snapshot (no trade — answer "am I too concentrated?")
        if context.proposed_trade is None:
            return await self._snapshot(context, positions)

        impact = await _compute_impact(
            trade=context.proposed_trade,
            portfolio=context.portfolio,
            positions=positions,
            constitution=context.constitution,
        )

        # Decide signal from the math BEFORE asking the LLM.
        violations = impact["violations"]
        warnings = impact["warnings"]
        signal: Literal["BLOCK", "WARNING", "INFO"]
        blocking = bool(violations)
        if violations:
            signal = "BLOCK"
        elif warnings:
            signal = "WARNING"
        else:
            signal = "INFO"

        prompt = _build_prompt(impact)
        narrative = await self._llm.complete_structured(
            prompt=prompt,
            schema=_RiskNarrative,
            system=_SYSTEM_PROMPT,
            cache_system=True,
        )

        blocking_reason = "; ".join(violations) if violations else None

        return AgentReport(
            agent_name=self.name,
            signal=signal,
            confidence=narrative.confidence,
            summary=narrative.summary,
            reasoning=narrative.reasoning,
            evidence=_evidence(impact),
            blocking=blocking,
            blocking_reason=blocking_reason,
            metadata={
                "impact": impact,
                "violation_count": len(violations),
                "warning_count": len(warnings),
            },
        )

    async def _snapshot(
        self,
        context: AgentContext,
        positions: list[Position],
    ) -> AgentReport:
        """Portfolio-snapshot mode: no trade, just analyze current state vs.
        the Constitution's concentration / sector / cash limits."""
        # Live prices for accurate sizing. Cost-basis math drifts badly when
        # positions have multiplied — e.g. a 6× winner ranks lower by cost
        # basis than a position you haven't grown into.
        prices = await _live_prices(positions, context.market.get("quotes"))
        snapshot = await _compute_snapshot(
            portfolio=context.portfolio,
            positions=positions,
            constitution=context.constitution,
            prices=prices,
        )

        violations = snapshot["violations"]
        warnings = snapshot["warnings"]
        signal: Literal["BLOCK", "WARNING", "INFO"]
        if violations:
            signal = "WARNING"  # snapshot violations don't block — there's no trade
        elif warnings:
            signal = "WARNING"
        else:
            signal = "INFO"

        prompt = _build_snapshot_prompt(snapshot)
        narrative = await self._llm.complete_structured(
            prompt=prompt,
            schema=_RiskNarrative,
            system=_SNAPSHOT_SYSTEM_PROMPT,
            cache_system=True,
        )

        return AgentReport(
            agent_name=self.name,
            signal=signal,
            confidence=narrative.confidence,
            summary=narrative.summary,
            reasoning=narrative.reasoning,
            evidence=_snapshot_evidence(snapshot),
            metadata={
                "snapshot": snapshot,
                "violation_count": len(violations),
                "warning_count": len(warnings),
            },
        )

    @staticmethod
    def _info(summary: str) -> AgentReport:
        return AgentReport(
            agent_name="risk",
            signal="INFO",
            confidence=0.0,
            summary=summary,
            reasoning=summary,
        )


# ─── deterministic impact math ────────────────────────────────────────


async def _compute_impact(
    *,
    trade: ProposedTrade,
    portfolio: Portfolio,
    positions: list[Position],
    constitution: Constitution,
) -> dict:
    """Returns a dict with before/after concentration, sector, cash, and
    lists of violations + warnings. All percentages are floats in [0,100].
    """
    total = portfolio.total_value
    if total <= 0:
        return {"error": "portfolio total is zero", "violations": [], "warnings": []}

    notional = trade.estimated_notional
    delta = notional if trade.side == "buy" else -notional

    # Current ticker holding (if any)
    pos = next((p for p in positions if p.symbol == trade.ticker), None)
    current_ticker_value = (
        (pos.quantity * trade.estimated_price) if pos else Decimal("0")
    )
    new_ticker_value = max(current_ticker_value + delta, Decimal("0"))

    current_ticker_pct = _pct(current_ticker_value, total)
    new_total = total + delta
    new_ticker_pct = _pct(new_ticker_value, new_total) if new_total > 0 else 0.0

    # Sector classification (best-effort; missing → "unknown")
    sector = await _ticker_sector(trade.ticker)
    sector_value_before = await _sector_value(positions, sector, trade.estimated_price)
    sector_value_after = sector_value_before + delta
    if sector_value_after < 0:
        sector_value_after = Decimal("0")
    current_sector_pct = _pct(sector_value_before, total)
    new_sector_pct = (
        _pct(sector_value_after, new_total) if new_total > 0 else 0.0
    )

    # Cash impact
    current_cash_pct = _pct(portfolio.cash, total)
    new_cash_value = portfolio.cash - delta  # buy reduces cash
    new_cash_pct = _pct(new_cash_value, new_total) if new_total > 0 else 0.0

    limits = constitution.position_limits
    violations: list[str] = []
    warnings: list[str] = []

    # Hard rule: max single-stock pct
    if new_ticker_pct > limits.max_single_stock_pct:
        violations.append(
            f"post-trade concentration in {trade.ticker} would be "
            f"{new_ticker_pct:.2f}% > {limits.max_single_stock_pct:.2f}% limit"
        )
    elif new_ticker_pct > 0.9 * limits.max_single_stock_pct:
        warnings.append(
            f"post-trade concentration in {trade.ticker} ({new_ticker_pct:.2f}%) "
            f"is within 10% of the {limits.max_single_stock_pct:.2f}% limit"
        )

    # Hard rule: max sector pct (only when we know the sector)
    if sector and sector != "unknown":
        if new_sector_pct > limits.max_sector_pct:
            violations.append(
                f"post-trade {sector} sector exposure would be {new_sector_pct:.2f}% "
                f"> {limits.max_sector_pct:.2f}% limit"
            )
        elif new_sector_pct > 0.9 * limits.max_sector_pct:
            warnings.append(
                f"post-trade {sector} sector ({new_sector_pct:.2f}%) is within "
                f"10% of the {limits.max_sector_pct:.2f}% limit"
            )

    # Hard rule: min cash pct (buys only — sells increase cash)
    if trade.side == "buy" and new_cash_pct < limits.min_cash_pct:
        violations.append(
            f"post-trade cash would be {new_cash_pct:.2f}% < {limits.min_cash_pct:.2f}% minimum"
        )

    return {
        "ticker": trade.ticker,
        "side": trade.side,
        "notional": float(notional),
        "concentration_before": current_ticker_pct,
        "concentration_after": new_ticker_pct,
        "concentration_limit": limits.max_single_stock_pct,
        "sector": sector,
        "sector_pct_before": current_sector_pct,
        "sector_pct_after": new_sector_pct,
        "sector_limit": limits.max_sector_pct,
        "cash_pct_before": current_cash_pct,
        "cash_pct_after": new_cash_pct,
        "cash_min": limits.min_cash_pct,
        "violations": violations,
        "warnings": warnings,
    }


def _pct(value: Decimal, total: Decimal) -> float:
    if total <= 0:
        return 0.0
    return float((value / total) * Decimal(100))


async def _ticker_sector(ticker: str) -> str:
    try:
        profile = await finnhub.get_company_profile(ticker)
        return profile.industry or "unknown" if profile else "unknown"
    except Exception:
        return "unknown"


async def _sector_value(
    positions: list[Position],
    sector: str,
    price_proxy: Decimal,
) -> Decimal:
    """Sum of values of positions matching ``sector``.

    We classify each position via Finnhub profile (cached daily). For
    pricing we use the proposed-trade's price as a proxy when the
    position itself doesn't carry a recent price — this is a small
    inaccuracy we accept for POC scope. The Risk numbers are about
    *order-of-magnitude* concentration, not penny-perfect accounting.
    """
    if sector == "unknown" or not positions:
        return Decimal("0")
    sectors = await asyncio.gather(
        *[_ticker_sector(p.symbol) for p in positions], return_exceptions=False
    )
    total = Decimal("0")
    for pos, sec in zip(positions, sectors):
        if sec == sector and pos.average_buy_price:
            total += pos.quantity * pos.average_buy_price
    return total


def _build_prompt(impact: dict) -> str:
    import json

    return (
        "Pre-computed risk impact for this trade:\n\n"
        f"{json.dumps(impact, indent=2)}\n\n"
        "Call RiskNarrative. Do not re-decide whether limits are breached — "
        "the violations list is authoritative."
    )


def _evidence(impact: dict) -> list[Evidence]:
    items = [
        Evidence(
            source="risk_calc",
            description=(
                f"Concentration in {impact['ticker']}: "
                f"{impact['concentration_before']:.2f}% → "
                f"{impact['concentration_after']:.2f}% "
                f"(limit {impact['concentration_limit']:.2f}%)"
            ),
            data={
                "before": impact["concentration_before"],
                "after": impact["concentration_after"],
                "limit": impact["concentration_limit"],
            },
        ),
        Evidence(
            source="risk_calc",
            description=(
                f"{impact['sector']} sector: "
                f"{impact['sector_pct_before']:.2f}% → "
                f"{impact['sector_pct_after']:.2f}% "
                f"(limit {impact['sector_limit']:.2f}%)"
            ),
            data={
                "sector": impact["sector"],
                "before": impact["sector_pct_before"],
                "after": impact["sector_pct_after"],
                "limit": impact["sector_limit"],
            },
        ),
        Evidence(
            source="risk_calc",
            description=(
                f"Cash: {impact['cash_pct_before']:.2f}% → "
                f"{impact['cash_pct_after']:.2f}% "
                f"(min {impact['cash_min']:.2f}%)"
            ),
            data={
                "before": impact["cash_pct_before"],
                "after": impact["cash_pct_after"],
                "min": impact["cash_min"],
            },
        ),
    ]
    for v in impact.get("violations", []):
        items.append(
            Evidence(source="risk_rule", description=f"VIOLATION: {v}", data={"hard": True})
        )
    for w in impact.get("warnings", []):
        items.append(
            Evidence(source="risk_rule", description=f"WARNING: {w}", data={"hard": False})
        )
    return items


# ─── snapshot mode ────────────────────────────────────────────────────


async def _compute_snapshot(
    *,
    portfolio: Portfolio,
    positions: list[Position],
    constitution: Constitution,
    prices: dict[str, Decimal] | None = None,
) -> dict:
    """Current-state portfolio analysis (no trade involved).

    Computes per-stock and per-sector concentration as a percentage of
    portfolio total, plus cash %, and flags anything over (violation) or
    near (warning, within 10%) the Constitution's limits.

    ``prices`` is a ticker→live-price map. Falls back to
    ``average_buy_price`` per position when a live price isn't available
    — but the caller should always supply prices in production paths;
    cost-basis sizing is misleading for positions that have moved
    significantly.
    """
    total = portfolio.total_value
    if total <= 0:
        return {
            "error": "portfolio total is zero",
            "stock_concentrations": [],
            "sector_concentrations": [],
            "cash_pct": 0.0,
            "violations": [],
            "warnings": [],
        }

    limits = constitution.position_limits
    cash_pct = _pct(portfolio.cash, total)
    prices = prices or {}

    # Per-position value at LIVE prices when available; cost basis fallback.
    position_values: list[tuple[Position, Decimal, str]] = []
    for p in positions:
        live = prices.get(p.symbol)
        if live is not None:
            position_values.append((p, p.quantity * live, "live"))
        elif p.average_buy_price is not None:
            position_values.append((p, p.quantity * p.average_buy_price, "cost_basis"))

    # Single-stock concentrations
    stock_rows = []
    for p, value, source in position_values:
        pct = _pct(value, total)
        stock_rows.append(
            {"ticker": p.symbol, "pct": pct, "value": float(value), "price_source": source}
        )
    stock_rows.sort(key=lambda r: r["pct"], reverse=True)

    # Sector classification (parallel)
    if position_values:
        sectors = await asyncio.gather(
            *[_ticker_sector(p.symbol) for p, _, _ in position_values],
            return_exceptions=False,
        )
    else:
        sectors = []

    sector_totals: dict[str, Decimal] = {}
    for (_, value, _src), sector in zip(position_values, sectors):
        if sector and sector != "unknown":
            sector_totals[sector] = sector_totals.get(sector, Decimal("0")) + value

    sector_rows = [
        {"sector": s, "pct": _pct(v, total), "value": float(v)}
        for s, v in sector_totals.items()
    ]
    sector_rows.sort(key=lambda r: r["pct"], reverse=True)

    violations: list[str] = []
    warnings: list[str] = []

    # Single-stock limit
    for row in stock_rows:
        if row["pct"] > limits.max_single_stock_pct:
            violations.append(
                f"{row['ticker']} is {row['pct']:.2f}% of the portfolio, "
                f"above the {limits.max_single_stock_pct:.2f}% single-stock limit"
            )
        elif row["pct"] > 0.9 * limits.max_single_stock_pct:
            warnings.append(
                f"{row['ticker']} is {row['pct']:.2f}% of the portfolio, "
                f"within 10% of the {limits.max_single_stock_pct:.2f}% limit"
            )

    # Sector limit
    for row in sector_rows:
        if row["pct"] > limits.max_sector_pct:
            violations.append(
                f"{row['sector']} sector is {row['pct']:.2f}%, "
                f"above the {limits.max_sector_pct:.2f}% sector limit"
            )
        elif row["pct"] > 0.9 * limits.max_sector_pct:
            warnings.append(
                f"{row['sector']} sector is {row['pct']:.2f}%, "
                f"within 10% of the {limits.max_sector_pct:.2f}% limit"
            )

    # Cash floor
    if cash_pct < limits.min_cash_pct:
        violations.append(
            f"Cash is {cash_pct:.2f}%, below the {limits.min_cash_pct:.2f}% minimum"
        )

    return {
        "total_value": float(total),
        "cash_pct": cash_pct,
        "cash_min": limits.min_cash_pct,
        "stock_concentrations": stock_rows[:8],
        "sector_concentrations": sector_rows,
        "single_stock_limit": limits.max_single_stock_pct,
        "sector_limit": limits.max_sector_pct,
        "violations": violations,
        "warnings": warnings,
    }


async def _live_prices(
    positions: list[Position],
    cached_quotes,
) -> dict[str, Decimal]:
    """Resolve live prices for each position.

    Uses ``cached_quotes`` from ``AgentContext.market`` if the caller
    pre-fetched them; otherwise pulls from Robinhood directly. Failures
    return an empty dict — the caller falls back to cost basis.
    """
    if cached_quotes:
        return {q.symbol: q.last_trade_price for q in cached_quotes}
    if not positions:
        return {}
    try:
        # Local import to avoid a hard dependency on Robinhood when the
        # agent is exercised offline (tests).
        from src.data.brokerage.robinhood_mcp import RobinhoodMCPClient

        symbols = [p.symbol for p in positions if p.average_buy_price is not None]
        if not symbols:
            return {}
        async with RobinhoodMCPClient() as rh:
            quotes = await rh.get_quotes(symbols)
        return {q.symbol: q.last_trade_price for q in quotes}
    except Exception:  # noqa: BLE001 — fail soft to cost basis
        return {}


def _build_snapshot_prompt(snapshot: dict) -> str:
    import json

    return (
        "Current portfolio snapshot vs. the Constitution's limits:\n\n"
        f"{json.dumps(snapshot, indent=2)}\n\n"
        "Call RiskNarrative. Treat the violations and warnings lists as "
        "authoritative — do not re-decide them."
    )


def _snapshot_evidence(snapshot: dict) -> list[Evidence]:
    items: list[Evidence] = []
    for s in snapshot.get("sector_concentrations", [])[:5]:
        items.append(
            Evidence(
                source="risk_calc",
                description=(
                    f"{s['sector']}: {s['pct']:.2f}% "
                    f"(limit {snapshot['sector_limit']:.2f}%)"
                ),
                data={"sector": s["sector"], "pct": s["pct"]},
            )
        )
    for s in snapshot.get("stock_concentrations", [])[:5]:
        items.append(
            Evidence(
                source="risk_calc",
                description=(
                    f"{s['ticker']}: {s['pct']:.2f}% "
                    f"(limit {snapshot['single_stock_limit']:.2f}%)"
                ),
                data={"ticker": s["ticker"], "pct": s["pct"]},
            )
        )
    items.append(
        Evidence(
            source="risk_calc",
            description=(
                f"Cash: {snapshot.get('cash_pct', 0):.2f}% "
                f"(min {snapshot.get('cash_min', 0):.2f}%)"
            ),
            data={"cash_pct": snapshot.get("cash_pct"), "min": snapshot.get("cash_min")},
        )
    )
    for v in snapshot.get("violations", []):
        items.append(
            Evidence(source="risk_rule", description=f"VIOLATION: {v}", data={"hard": True})
        )
    for w in snapshot.get("warnings", []):
        items.append(
            Evidence(source="risk_rule", description=f"WARNING: {w}", data={"hard": False})
        )
    return items


__all__ = ["RiskAgent"]
