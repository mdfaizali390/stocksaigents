"""Streamlit UI for the multi-agent advisor.

Run from repo root::

    streamlit run src/ui/streamlit_app.py

Single-page app:
  - Query box + send button
  - Mode toggle: Dry Run / Live (defaults to Dry Run)
  - Constitution status (active source, last updated)
  - Output panel:
      • PM recommendation, color-coded
      • Block banner (red, prominent) if any agent returned blocking
      • Expandable cards per agent report
      • Execute button — disabled in Dry Run, disabled if any block
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from datetime import datetime, timezone

import streamlit as st

# Make uncaught backend errors loud in the terminal running Streamlit.
# The default config swallows our application-level INFO/WARNING.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)

from src.constitution.loader import load_constitution
from src.data.brokerage.robinhood_mcp import RobinhoodMCPClient
from src.models import AgentReport, Decision
from src.privacy import redact_shares
from src.orchestrator import Orchestrator


# ─── page config ──────────────────────────────────────────────────────


APP_NAME = "StockAIgents"
APP_TAGLINE = "Your AI-Powered Stock Advisor"

st.set_page_config(
    page_title=f"{APP_NAME} — {APP_TAGLINE}",
    page_icon="📈",
    layout="wide",
    menu_items={
        "About": (
            f"{APP_NAME} — {APP_TAGLINE}\n\n"
            "Multi-agent stock advisor. Advisor only — never auto-trades."
        ),
    },
)


# ─── async helper ─────────────────────────────────────────────────────


def run_async(coro):
    """Run an async coroutine in Streamlit's sync context.

    Streamlit re-runs the script on every interaction, so we open a
    fresh asyncio event loop per call. The Robinhood MCP session is
    inside the coroutine — fast (~1-2s) since OAuth tokens persist.
    """
    return asyncio.run(coro)


def _flatten_exception_group(exc: BaseException) -> list[BaseException]:
    """Walk an ExceptionGroup tree and return the leaf exceptions.

    asyncio.TaskGroup wraps every concurrent failure in BaseExceptionGroup,
    which prints as "unhandled errors in a TaskGroup (N sub-exceptions)" —
    useless without unwrapping. This recursively collects the actual
    exceptions so we can surface their messages directly.
    """
    leaves: list[BaseException] = []
    if isinstance(exc, BaseExceptionGroup):
        for child in exc.exceptions:
            leaves.extend(_flatten_exception_group(child))
    else:
        leaves.append(exc)
    return leaves or [exc]


async def submit_query(query: str, mode: str) -> Decision:
    """Open Robinhood MCP, run the orchestrator end-to-end, close."""
    constitution, _ = load_constitution()
    async with RobinhoodMCPClient() as rh:
        orch = Orchestrator(brokerage=rh)
        return await orch.handle_query(query, constitution=constitution, mode=mode)


# ─── rendering helpers ────────────────────────────────────────────────


_SIGNAL_COLOR = {
    "BUY": "#16a34a",      # green
    "SELL": "#dc2626",     # red
    "HOLD": "#ca8a04",     # amber
    "INFO": "#475569",     # slate
    "WARNING": "#ea580c",  # orange
    "BLOCK": "#dc2626",    # red
}

_ACTION_COLOR = {
    "BUY": "#16a34a",
    "SELL": "#dc2626",
    "HOLD": "#ca8a04",
    "NO_ACTION": "#475569",
    "BLOCKED": "#dc2626",
}


def _badge(text: str, color: str) -> str:
    return (
        f'<span style="background:{color};color:white;padding:2px 8px;'
        f'border-radius:4px;font-size:0.85em;font-weight:600">{text}</span>'
    )


def render_recommendation(d: Decision) -> None:
    rec = d.recommendation
    color = _ACTION_COLOR.get(rec.action, "#475569")
    icon = {
        "BUY": "📈",
        "SELL": "📉",
        "HOLD": "⏸️",
        "NO_ACTION": "ℹ️",
        "BLOCKED": "🛑",
    }.get(rec.action, "ℹ️")

    st.markdown(
        f"""
        <div style="border:2px solid {color};border-radius:8px;
                    padding:1rem 1.25rem;margin-bottom:1rem;background:#fafafa">
          <div style="font-size:1.5em;font-weight:700;color:{color}">
            {icon} {rec.action}
            {f'<span style="font-weight:400;font-size:0.7em;color:#666">'
             f' &middot; confidence {rec.confidence:.2f}</span>' if rec.confidence else ''}
          </div>
          {f'<div style="margin-top:0.4em;color:#444"><strong>Ticker:</strong> {rec.ticker}'
           f'{f" &middot; <strong>Suggested:</strong> {rec.quantity_suggestion} shares" if rec.quantity_suggestion else ""}'
           f'</div>' if rec.ticker else ''}
          <div style="margin-top:0.6em;color:#222;line-height:1.5">{redact_shares(rec.summary)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if rec.block_reasons:
        st.error("**🛑 Blocked.** " + " · ".join(redact_shares(b) for b in rec.block_reasons))

    if rec.citations:
        with st.expander("Citations", expanded=False):
            for c in rec.citations:
                st.markdown(f"- {redact_shares(c)}")


def render_intent(d: Decision) -> None:
    i = d.intent
    cols = st.columns([1, 1, 1, 2])
    cols[0].caption("Intent")
    cols[0].markdown(f"`{i.intent_type}`")
    cols[1].caption("Ticker")
    cols[1].markdown(f"`{i.ticker or '—'}`")
    cols[2].caption("Action")
    cols[2].markdown(f"`{i.action or '—'}`")
    cols[3].caption("Agents dispatched")
    cols[3].markdown(", ".join(f"`{a}`" for a in i.agents_to_run) or "_none_")


def render_agent_card(r: AgentReport) -> None:
    color = _SIGNAL_COLOR.get(r.signal, "#475569")
    block_marker = " 🚫" if r.blocking else ""
    title = (
        f"{r.agent_name.upper()}{block_marker} — "
        f"{r.signal} (confidence {r.confidence:.2f})"
    )
    with st.expander(title, expanded=r.blocking):
        st.markdown(_badge(r.signal, color), unsafe_allow_html=True)
        st.markdown(f"**{redact_shares(r.summary)}**")
        st.markdown(redact_shares(r.reasoning))
        if r.blocking_reason:
            st.error(f"**Blocking reason:** {redact_shares(r.blocking_reason)}")

        # Per-agent numeric panels — show the actual computations behind
        # the prose so "this is real math, not LLM hand-waving" is obvious.
        renderer = _AGENT_RENDERERS.get(r.agent_name)
        if renderer:
            renderer(r)

        if r.evidence:
            with st.expander("Evidence trail", expanded=False):
                for e in r.evidence[:12]:
                    st.markdown(f"- _{e.source}_: {redact_shares(e.description)}")
                if len(r.evidence) > 12:
                    st.caption(f"… and {len(r.evidence) - 12} more evidence items")


# ─── per-agent numeric panels ─────────────────────────────────────────


def _fmt_pct(v: float | None, digits: int = 2) -> str:
    return f"{v:.{digits}f}%" if v is not None else "—"


def _fmt_num(v, digits: int = 2) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "yes" if v else "no"
    try:
        return f"{float(v):,.{digits}f}"
    except (TypeError, ValueError):
        return str(v)


def _render_quant_panel(r: AgentReport) -> None:
    panel = r.metadata.get("indicator_panel")
    if not panel:
        return
    st.caption("Indicator panel")
    cols = st.columns(4)
    cols[0].metric("RSI(14)", _fmt_num(panel.get("rsi_14")))
    cols[1].metric("SMA-50", _fmt_num(panel.get("sma_50")))
    cols[2].metric("SMA-200", _fmt_num(panel.get("sma_200")))
    cols[3].metric("Latest close", _fmt_num(panel.get("latest_close")))
    cols = st.columns(4)
    mom_20 = panel.get("momentum_20d")
    mom_60 = panel.get("momentum_60d")
    cols[0].metric(
        "Momentum 20d",
        f"{mom_20*100:.2f}%" if mom_20 is not None else "—",
    )
    cols[1].metric(
        "Momentum 60d",
        f"{mom_60*100:.2f}%" if mom_60 is not None else "—",
    )
    vol = panel.get("annualized_volatility")
    cols[2].metric(
        "Volatility (annual)",
        f"{vol*100:.1f}%" if vol is not None else "—",
    )
    cols[3].metric("Volume trend (5v5)", _fmt_num(panel.get("volume_trend_5v5"), 2))
    flags = []
    if panel.get("above_sma_50") is True:
        flags.append("✅ above SMA-50")
    elif panel.get("above_sma_50") is False:
        flags.append("🔻 below SMA-50")
    if panel.get("above_sma_200") is True:
        flags.append("✅ above SMA-200")
    elif panel.get("above_sma_200") is False:
        flags.append("🔻 below SMA-200")
    if flags:
        st.caption(" · ".join(flags) + f"  ·  {panel.get('bars_used', '?')} bars")


def _render_research_panel(r: AgentReport) -> None:
    cols = st.columns(3)
    cols[0].metric("News items (14d)", r.metadata.get("news_count", 0))
    cols[1].metric("Filings (30d)", r.metadata.get("filings_count", 0))
    next_e = r.metadata.get("next_earnings")
    cols[2].metric("Next earnings", next_e or "—")
    cited = r.metadata.get("cited_headlines") or []
    if cited:
        st.caption("Headlines cited by the agent")
        for h in cited[:5]:
            st.markdown(f"- {h}")


def _render_trending_panel(r: AgentReport) -> None:
    ranked = r.metadata.get("ranked") or []
    if not ranked:
        return
    notes_lookup = {
        n["ticker"]: n.get("rationale", "") for n in (r.metadata.get("notes") or [])
    }
    st.caption(f"Top {len(ranked)} ranked tickers (deterministic Trending Score)")

    # Build a tabular view: ticker, score, each component
    import pandas as pd  # local import — pandas is already a yfinance dep

    rows = []
    for t in ranked:
        comps = t.get("components", {})
        rows.append(
            {
                "Ticker": t["ticker"],
                "Score": round(t["score"], 1),
                "ST trending": round(comps.get("stocktwits_trending", 0), 0),
                "ST watchers": round(comps.get("stocktwits_watchers", 0), 0),
                "News vol": round(comps.get("news_volume", 0), 0),
                "Vol spike": round(comps.get("volume_spike", 0), 0),
                "Momentum": round(comps.get("price_momentum", 0), 0),
                "Analyst": round(comps.get("analyst_signal", 0), 0),
            }
        )
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    if notes_lookup:
        with st.expander("Per-ticker rationale", expanded=False):
            for t in ranked:
                note = notes_lookup.get(t["ticker"])
                if note:
                    st.markdown(f"**{t['ticker']}** ({t['score']:.0f}): {note}")


def _render_risk_panel(r: AgentReport) -> None:
    impact = r.metadata.get("impact")
    snapshot = r.metadata.get("snapshot")
    if impact:
        _render_risk_impact(impact)
    elif snapshot:
        _render_risk_snapshot(snapshot)


def _render_risk_impact(impact: dict) -> None:
    st.caption("Trade impact (before → after)")
    cols = st.columns(3)
    cols[0].metric(
        f"{impact['ticker']} concentration",
        _fmt_pct(impact["concentration_after"]),
        delta=f"{impact['concentration_after'] - impact['concentration_before']:+.2f}% pts",
        help=f"Limit: {impact['concentration_limit']:.2f}%",
    )
    cols[1].metric(
        f"{impact.get('sector') or 'Sector'} exposure",
        _fmt_pct(impact["sector_pct_after"]),
        delta=f"{impact['sector_pct_after'] - impact['sector_pct_before']:+.2f}% pts",
        help=f"Limit: {impact['sector_limit']:.2f}%",
    )
    cols[2].metric(
        "Cash %",
        _fmt_pct(impact["cash_pct_after"]),
        delta=f"{impact['cash_pct_after'] - impact['cash_pct_before']:+.2f}% pts",
        help=f"Min: {impact['cash_min']:.2f}%",
        delta_color="inverse",
    )
    violations = impact.get("violations") or []
    warnings = impact.get("warnings") or []
    for v in violations:
        st.error(f"🛑 {v}")
    for w in warnings:
        st.warning(f"⚠️ {w}")


def _render_risk_snapshot(snapshot: dict) -> None:
    import pandas as pd

    st.caption("Top single-stock concentrations (live prices)")
    stocks = snapshot.get("stock_concentrations") or []
    if stocks:
        limit = snapshot.get("single_stock_limit", 15)
        df = pd.DataFrame(
            [
                {
                    "Ticker": s["ticker"],
                    "% of portfolio": round(s["pct"], 2),
                    "Status": (
                        "🛑 over"
                        if s["pct"] > limit
                        else "⚠️ near" if s["pct"] > 0.9 * limit else "✅"
                    ),
                }
                for s in stocks
            ]
        )
        st.dataframe(df, hide_index=True, width="stretch")

    sectors = snapshot.get("sector_concentrations") or []
    if sectors:
        st.caption("Sector exposure")
        limit = snapshot.get("sector_limit", 30)
        df = pd.DataFrame(
            [
                {
                    "Sector": s["sector"],
                    "% of portfolio": round(s["pct"], 2),
                    "Status": (
                        "🛑 over"
                        if s["pct"] > limit
                        else "⚠️ near" if s["pct"] > 0.9 * limit else "✅"
                    ),
                }
                for s in sectors
            ]
        )
        st.dataframe(df, hide_index=True, width="stretch")

    cols = st.columns(3)
    cols[0].metric(
        "Cash %",
        _fmt_pct(snapshot.get("cash_pct")),
        help=f"Min: {snapshot.get('cash_min', 0):.2f}%",
    )
    cols[1].metric("Stock limit", _fmt_pct(snapshot.get("single_stock_limit")))
    cols[2].metric("Sector limit", _fmt_pct(snapshot.get("sector_limit")))

    for v in snapshot.get("violations") or []:
        st.error(f"🛑 {v}")
    for w in snapshot.get("warnings") or []:
        st.warning(f"⚠️ {w}")


def _render_behavioral_panel(r: AgentReport) -> None:
    cols = st.columns(2)
    cols[0].metric("Orders analyzed", r.metadata.get("orders_analyzed", 0))
    patterns = r.metadata.get("patterns") or []
    pretty = [p for p in patterns if p != "none"]
    cols[1].metric(
        "Patterns detected",
        ", ".join(pretty) if pretty else "none",
    )


def _render_compliance_panel(r: AgentReport) -> None:
    codes = r.metadata.get("violation_codes") or []
    if codes:
        st.caption("Rules violated")
        for c in codes:
            st.markdown(f"- `{c}`")
    else:
        approval = r.metadata.get("human_approval_required")
        if approval is not None:
            st.caption(
                "Human approval required: " + ("yes" if approval else "no")
            )


_AGENT_RENDERERS = {
    "quant": _render_quant_panel,
    "research": _render_research_panel,
    "trending": _render_trending_panel,
    "risk": _render_risk_panel,
    "behavioral": _render_behavioral_panel,
    "compliance": _render_compliance_panel,
}


# ─── sidebar ──────────────────────────────────────────────────────────


def render_sidebar() -> str:
    st.sidebar.title(APP_NAME)
    st.sidebar.caption(f"{APP_TAGLINE} · advisor only · never auto-trades")

    mode = st.sidebar.radio(
        "Mode",
        options=["dry_run", "live"],
        format_func=lambda m: "Dry Run" if m == "dry_run" else "Live",
        index=0,
        help="Dry Run keeps the Execute button disabled. Live enables it (still requires confirmation).",
    )
    if mode == "live":
        st.sidebar.warning("⚠️ Live mode is on. Execute button will require confirmation per trade.")

    st.sidebar.divider()
    st.sidebar.subheader("Trading Constitution")
    constitution, source = load_constitution()
    st.sidebar.caption(f"Source: {source}")
    st.sidebar.markdown(
        f"- Risk profile: **{constitution.user_profile.risk_profile}**\n"
        f"- Max single trade: **{constitution.position_limits.max_single_trade_pct}%**\n"
        f"- Max single stock: **{constitution.position_limits.max_single_stock_pct}%**\n"
        f"- Max sector: **{constitution.position_limits.max_sector_pct}%**\n"
        f"- Min cash: **{constitution.position_limits.min_cash_pct}%**\n"
        f"- Allowed: {', '.join(constitution.allowed_asset_classes)}\n"
        f"- Blocked: {', '.join(constitution.blocked_asset_classes)}"
    )

    st.sidebar.divider()
    st.sidebar.caption("Try a query:")
    suggestions = [
        "Should I buy NVDA?",
        "Should I sell my AMZN?",
        "What stocks are trending now?",
        "Am I too concentrated in tech?",
        "What's my max trade size?",
    ]
    for s in suggestions:
        if st.sidebar.button(s, width="stretch", key=f"suggest_{s}"):
            st.session_state["query_input"] = s

    return mode


# ─── main page ────────────────────────────────────────────────────────


def main() -> None:
    mode = render_sidebar()

    st.markdown(
        f"""
        <div style="display:flex;align-items:baseline;gap:0.6em;margin-bottom:0.2em">
          <span style="font-size:2.2em;font-weight:800;letter-spacing:-0.02em">
            📈 {APP_NAME}
          </span>
          <span style="font-size:1.0em;color:#64748b;font-weight:500">
            — {APP_TAGLINE}
          </span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.caption(
        "Ask about a trade, a ticker, your portfolio, or your trading policy. "
        "All decisions are advisory — Execute is always your call."
    )

    query = st.text_input(
        "",
        key="query_input",
        placeholder="e.g. Should I buy NVDA?",
        label_visibility="collapsed",
    )
    submitted = st.button("Analyze", type="primary", width="content")

    if submitted and query.strip():
        with st.spinner("Routing intent, fetching context, running agents…"):
            try:
                decision = run_async(submit_query(query.strip(), mode))
                st.session_state["last_decision"] = decision
                st.session_state["last_query"] = query.strip()
            except BaseException as e:  # noqa: BLE001 — capture *Group too
                # Unwrap ExceptionGroup (asyncio TaskGroup wraps inner errors).
                inner = _flatten_exception_group(e)
                tb_text = "".join(
                    traceback.format_exception(type(e), e, e.__traceback__)
                )
                # Echo to the server log so we have it in the terminal too.
                logging.exception("query pipeline failed: %s", query.strip())
                st.error(
                    "**Pipeline failed.** "
                    + " · ".join(f"{type(x).__name__}: {x}" for x in inner)
                )
                with st.expander("Full traceback (for debugging)", expanded=False):
                    st.code(tb_text, language="python")
                return

    decision: Decision | None = st.session_state.get("last_decision")
    if decision is None:
        st.info("Enter a query above and click **Analyze** to begin.")
        return

    st.markdown(f"#### Query: _{st.session_state.get('last_query','')}_")
    render_intent(decision)
    st.divider()

    # Block banner first — most important info
    if decision.blocking_reports:
        reasons = " · ".join(
            redact_shares(r.blocking_reason) for r in decision.blocking_reports if r.blocking_reason
        )
        st.error(f"🛑 **Trade blocked.** {reasons}")

    # PM recommendation, color-coded
    render_recommendation(decision)

    # Execute button — disabled in dry_run, disabled if any block
    can_execute = (
        decision.mode == "live"
        and not decision.blocking_reports
        and decision.recommendation.action in ("BUY", "SELL")
    )
    exec_col, status_col = st.columns([1, 3])
    with exec_col:
        clicked = st.button("Execute trade", disabled=not can_execute, type="primary")
    with status_col:
        if decision.mode == "dry_run":
            st.caption("Execute is disabled in Dry Run mode.")
        elif decision.blocking_reports:
            st.caption("Execute is disabled — at least one agent blocked the trade.")
        elif decision.recommendation.action not in ("BUY", "SELL"):
            st.caption(f"No trade to execute (recommendation: {decision.recommendation.action}).")
        else:
            st.caption("Execute is enabled. Live order placement is not yet wired (Phase 4).")
    if clicked:
        st.warning("Live execution path not yet implemented. See TECHNICAL_DESIGN §9 Phase 4.")

    st.divider()
    st.subheader("Agent reports")
    for r in decision.reports:
        render_agent_card(r)

    ts = decision.timestamp.astimezone(timezone.utc).isoformat(timespec="seconds")
    st.caption(f"Decided at {ts} · mode={decision.mode}")


if __name__ == "__main__":
    main()
