"""Central orchestrator.

Pipeline (matching design §4.4 / §6):

    query
      │
      ▼
    Intent Router  ──────────► Intent {intent_type, ticker, action, agents_to_run}
      │
      ▼
    Fetch context (parallel)
      • Portfolio + positions + recent orders (Robinhood MCP)
      • Quote → ProposedTrade for the named ticker
      • Constitution from disk
      │
      ▼
    Agent fan-out (asyncio.gather over agents_to_run)
      ├─ Compliance (pure rules, no LLM)
      ├─ Quant
      ├─ Research
      ├─ Trending
      ├─ Risk
      └─ Behavioral
      │
      ▼
    Block check (§6.3): if any report.blocking → force BLOCKED
      │
      ▼
    PM synthesis → Recommendation
      │
      ▼
    Decision = {intent, reports, recommendation, blocking_reports, mode}

Two safety patterns enforced **structurally** (Python, not prompts):
  1. ``trade_decision`` always runs Risk + Compliance — handled by the
     Intent Router (``_resolve_agents``).
  2. Any blocking report forces ``Recommendation.action = "BLOCKED"`` —
     handled here in ``_apply_block_check``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Awaitable, Callable

from src.agents.base import AgentContext, BaseAgent
from src.agents.behavioral import BehavioralAgent
from src.agents.compliance import ComplianceAgent
from src.agents.intent_router import IntentRouter
from src.agents.portfolio_manager import PortfolioManagerAgent
from src.agents.quant import QuantAgent
from src.agents.research import ResearchAgent
from src.agents.risk import RiskAgent
from src.agents.trending import TrendingAgent
from src.constitution.schema import Constitution
from src.data.brokerage.base import BrokerageClient, Portfolio
from src.llm.client import LLMClient
from src.models import (
    AgentReport,
    AgentName,
    Decision,
    Intent,
    ProposedTrade,
    Recommendation,
    RunMode,
)

logger = logging.getLogger(__name__)


# Default quantity for trade_decision queries when the user didn't specify
# size. We pick 1 share so size-checks are meaningful but not absurd. The
# UI / a future refinement can let the user dial this in.
_DEFAULT_TRADE_QUANTITY = Decimal("1")


class Orchestrator:
    def __init__(
        self,
        *,
        brokerage: BrokerageClient | None = None,
        llm: LLMClient | None = None,
    ) -> None:
        self._brokerage = brokerage
        self._llm = llm or LLMClient()
        self._router = IntentRouter(llm=self._llm)
        self._pm = PortfolioManagerAgent(llm=self._llm)

        # All agents share one LLM client so usage accumulates.
        self._agents: dict[AgentName, BaseAgent] = {
            "research": ResearchAgent(llm=self._llm),
            "quant": QuantAgent(llm=self._llm),
            "trending": TrendingAgent(llm=self._llm),
            "risk": RiskAgent(llm=self._llm),
            "behavioral": BehavioralAgent(llm=self._llm),
            "compliance": ComplianceAgent(),
        }

    async def handle_query(
        self,
        query: str,
        *,
        constitution: Constitution,
        mode: RunMode = "dry_run",
    ) -> Decision:
        intent = await self._router.classify(query)
        logger.info(
            "intent: %s ticker=%s action=%s agents=%s",
            intent.intent_type,
            intent.ticker,
            intent.action,
            intent.agents_to_run,
        )

        # Policy questions skip the agent dispatch entirely.
        if intent.intent_type == "policy_question":
            return _policy_decision(intent, mode)

        portfolio, positions, orders, proposed_trade = await self._gather_context(intent)

        # Portfolio-fact questions ("how many NVDA do I have?") are direct
        # lookups — answered from account data, no agents, no LLM lecture.
        if intent.intent_type == "portfolio_fact":
            return await self._portfolio_fact_decision(
                query, intent, portfolio, positions, mode
            )

        # Pre-fetch live quotes for held positions when Risk runs in
        # snapshot mode (portfolio_analysis). Cost-basis sizing is
        # misleading for positions that have moved significantly.
        position_quotes = []
        needs_position_quotes = (
            intent.intent_type == "portfolio_analysis"
            and "risk" in intent.agents_to_run
            and self._brokerage is not None
            and positions
        )
        if needs_position_quotes:
            symbols = [p.symbol for p in positions if p.average_buy_price is not None]
            if symbols:
                try:
                    position_quotes = await self._brokerage.get_quotes(symbols)
                except Exception as e:  # noqa: BLE001 — fail soft to cost basis
                    logger.warning("position quote fetch failed: %s", e)

        ctx = AgentContext(
            intent=intent,
            constitution=constitution,
            portfolio=portfolio,
            proposed_trade=proposed_trade,
            market={
                "positions": positions,
                "orders": orders,
                "quotes": position_quotes,
            },
        )

        reports = await self._dispatch_agents(intent.agents_to_run, ctx)

        blocking_reports = [r for r in reports if r.blocking]

        recommendation = await self._pm.synthesize(
            query=query,
            reports=reports,
            portfolio=portfolio,
            ticker=intent.ticker,
        )
        recommendation = _apply_block_check(recommendation, blocking_reports)

        return Decision(
            intent=intent,
            reports=reports,
            recommendation=recommendation,
            blocking_reports=blocking_reports,
            mode=mode,
            timestamp=datetime.now(timezone.utc),
        )

    # ─── context gathering ────────────────────────────────────────────

    async def _gather_context(
        self, intent: Intent
    ) -> tuple[Portfolio | None, list, list, ProposedTrade | None]:
        """Pull portfolio + orders + a proposed trade (when applicable).

        Each piece is fetched only if it'll be used. ``self._brokerage``
        being None means we're in offline mode (tests) — return empties.
        """
        if self._brokerage is None:
            return None, [], [], _maybe_trade_offline(intent)

        accounts = await self._brokerage.get_accounts()
        if not accounts:
            return None, [], [], None
        target = next((a for a in accounts if a.is_default), accounts[0])

        # Fetch portfolio + positions + recent orders concurrently.
        portfolio_task = asyncio.create_task(
            self._brokerage.get_portfolio(target.account_number)
        )
        positions_task = asyncio.create_task(
            self._brokerage.get_positions(target.account_number)
        )
        orders_task = asyncio.create_task(
            self._brokerage.get_orders(
                target.account_number,
                created_at_gte=datetime.now(timezone.utc) - timedelta(days=60),
            )
        )

        portfolio = await portfolio_task
        positions = await positions_task
        orders = await orders_task

        proposed_trade = await self._build_proposed_trade(intent)
        return portfolio, positions, orders, proposed_trade

    async def _build_proposed_trade(self, intent: Intent) -> ProposedTrade | None:
        """For trade_decision intents, pull a quote and build a ProposedTrade."""
        if intent.intent_type != "trade_decision":
            return None
        if not intent.ticker or not intent.action:
            return None
        if self._brokerage is None:
            return _maybe_trade_offline(intent)

        try:
            quotes = await self._brokerage.get_quotes([intent.ticker])
        except Exception as e:  # noqa: BLE001 — fail soft for quote outages
            logger.warning("quote fetch failed for %s: %s", intent.ticker, e)
            return None
        if not quotes:
            return None
        q = quotes[0]
        if not q.has_traded or q.state != "active":
            logger.warning(
                "skip ProposedTrade for %s — has_traded=%s state=%s",
                intent.ticker,
                q.has_traded,
                q.state,
            )
        return ProposedTrade(
            ticker=intent.ticker,
            side=intent.action,
            order_type="limit",  # design defaults to limit-only
            quantity=_DEFAULT_TRADE_QUANTITY,
            asset_class="stocks",
            estimated_price=q.last_trade_price,
        )

    # ─── agent dispatch ───────────────────────────────────────────────

    async def _dispatch_agents(
        self, names: list[AgentName], ctx: AgentContext
    ) -> list[AgentReport]:
        """Run named agents in parallel. Failed agents return a degraded
        INFO report rather than crashing the whole pipeline."""
        if not names:
            return []
        tasks: list[Awaitable[AgentReport]] = [
            _safe_run(self._agents[n], ctx) for n in names if n in self._agents
        ]
        return await asyncio.gather(*tasks)

    # ─── portfolio-fact fast path ─────────────────────────────────────

    async def _portfolio_fact_decision(
        self,
        query: str,
        intent: Intent,
        portfolio: Portfolio | None,
        positions: list,
        mode: RunMode,
    ) -> Decision:
        """Answer a direct factual question about holdings — no agents.

        We hand the LLM only the relevant account facts and ask it to answer
        the specific question plainly. The numbers come from real data, so
        there's nothing to hallucinate; the LLM just phrases it.
        """
        facts = _portfolio_facts(portfolio, positions, intent.ticker)
        summary = await self._answer_fact(query, facts)

        rec = Recommendation(
            action="NO_ACTION",
            ticker=intent.ticker,
            quantity_suggestion=None,
            confidence=1.0,
            summary=summary,
            citations=["portfolio"],
        )
        return Decision(
            intent=intent,
            reports=[],
            recommendation=rec,
            blocking_reports=[],
            mode=mode,
            timestamp=datetime.now(timezone.utc),
        )

    async def _answer_fact(self, query: str, facts: dict) -> str:
        import json

        if not facts.get("portfolio_loaded"):
            return (
                "Your brokerage isn't connected here, so I can't look up your "
                "holdings. Questions about the market, news, or trending "
                "stocks still work. (Portfolio access is available when "
                "running locally with Robinhood connected.)"
            )
        prompt = (
            f"User question: {query}\n\n"
            f"Account facts (the ONLY source of truth — do not invent numbers):\n"
            f"{json.dumps(facts, indent=2, default=str)}\n\n"
            "Answer the user's question directly in 1-2 sentences. Lead with "
            "the specific number they asked for. If they asked about a ticker "
            "they don't hold, say they don't currently hold it."
        )
        try:
            return await self._llm.complete_text(
                prompt=prompt,
                system=(
                    "You answer factual questions about a user's brokerage "
                    "holdings using only the data provided. Be direct and "
                    "concise. Never invent figures."
                ),
                max_tokens=200,
                temperature=0.0,
            )
        except Exception as e:  # noqa: BLE001 — fall back to a plain readout
            logger.warning("fact LLM failed: %s", e)
            return _plain_fact_readout(facts)


# ─── helpers ──────────────────────────────────────────────────────────


async def _safe_run(agent: BaseAgent, ctx: AgentContext) -> AgentReport:
    try:
        return await agent.run(ctx)
    except Exception as e:  # noqa: BLE001 — protect the pipeline
        logger.exception("agent %s crashed", getattr(agent, "name", "?"))
        return AgentReport(
            agent_name=getattr(agent, "name", "unknown"),
            signal="INFO",
            confidence=0.0,
            summary=f"Agent {getattr(agent, 'name', '?')} failed: {type(e).__name__}",
            reasoning=str(e)[:500],
            metadata={"error": True},
        )


def _maybe_trade_offline(intent: Intent) -> ProposedTrade | None:
    """Build a synthetic ProposedTrade when there's no brokerage to ask
    for a quote. Used by tests so the agents that depend on a trade
    (Compliance / Risk) still get one."""
    if intent.intent_type != "trade_decision":
        return None
    if not intent.ticker or not intent.action:
        return None
    return ProposedTrade(
        ticker=intent.ticker,
        side=intent.action,
        order_type="limit",
        quantity=_DEFAULT_TRADE_QUANTITY,
        asset_class="stocks",
        estimated_price=Decimal("100"),  # placeholder; tests override as needed
    )


def _portfolio_facts(
    portfolio: Portfolio | None,
    positions: list,
    ticker: str | None,
) -> dict:
    """Flatten portfolio + positions into a small JSON-able fact sheet for
    the LLM. If a ticker was named, highlight that holding."""
    if portfolio is None:
        return {"portfolio_loaded": False}

    holdings = [
        {
            "ticker": p.symbol,
            "shares": str(p.quantity),
            "shares_available_to_sell": str(p.shares_available_for_sells),
            "average_buy_price": str(p.average_buy_price) if p.average_buy_price else None,
        }
        for p in positions
    ]

    facts: dict = {
        "portfolio_loaded": True,
        "total_value": str(portfolio.total_value),
        "cash": str(portfolio.cash),
        "buying_power": str(portfolio.buying_power.buying_power),
        "currency": portfolio.currency,
        "holdings_count": len(holdings),
        "holdings": holdings,
    }

    if ticker:
        match = next((h for h in holdings if h["ticker"] == ticker.upper()), None)
        facts["asked_about_ticker"] = ticker.upper()
        facts["holds_asked_ticker"] = match is not None
        if match:
            facts["asked_ticker_position"] = match
    return facts


def _plain_fact_readout(facts: dict) -> str:
    """Deterministic fallback if the LLM call fails."""
    if not facts.get("portfolio_loaded"):
        return "Portfolio data is unavailable right now."
    t = facts.get("asked_about_ticker")
    if t:
        if facts.get("holds_asked_ticker"):
            pos = facts["asked_ticker_position"]
            return f"You hold {pos['shares']} shares of {t}."
        return f"You don't currently hold any {t}."
    return (
        f"Total value {facts['total_value']} {facts['currency']}, "
        f"cash {facts['cash']}, across {facts['holdings_count']} positions."
    )


def _policy_decision(intent: Intent, mode: RunMode) -> Decision:
    """Fast-path: policy_question runs no agents and is answered from the
    Constitution directly. Surface a helpful note as the recommendation."""
    rec = Recommendation(
        action="NO_ACTION",
        ticker=None,
        quantity_suggestion=None,
        confidence=1.0,
        summary=(
            "Policy question — answered directly from the Constitution. "
            "No agents dispatched."
        ),
        citations=["constitution"],
    )
    return Decision(
        intent=intent,
        reports=[],
        recommendation=rec,
        blocking_reports=[],
        mode=mode,
        timestamp=datetime.now(timezone.utc),
    )


def _apply_block_check(
    recommendation: Recommendation, blocking_reports: list[AgentReport]
) -> Recommendation:
    """§6.3: any blocking report forces action=BLOCKED.

    The PM is informed about blocks via the prompt (so its narrative is
    coherent), but the orchestrator overwrites action and block_reasons
    here to remove any LLM-decided slack.
    """
    if not blocking_reports:
        return recommendation
    reasons = [
        r.blocking_reason
        for r in blocking_reports
        if r.blocking_reason
    ]
    return recommendation.model_copy(
        update={
            "action": "BLOCKED",
            "block_reasons": reasons or recommendation.block_reasons,
            "quantity_suggestion": None,
        }
    )


__all__ = ["Orchestrator"]
