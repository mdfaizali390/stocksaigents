"""Streamlit page: Trading Constitution interview + editor.

Two modes on the page:

  - **Interview** — LLM-driven conversational flow. Asks the user 6-12
    questions, drafts a Constitution, lets them review and approve. On
    approval, writes ``src/constitution/policy.json``.

  - **Editor** — view the currently-active Constitution and tweak
    individual numeric limits / asset classes without rerunning the
    full interview.

Streamlit re-runs the script on every interaction; we hold the
transcript and pending draft in ``st.session_state``.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Repo root onto sys.path so `from src.…` resolves on Streamlit Cloud.
# This file is at <repo>/src/ui/pages/1_Constitution.py → root is parents[3].
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import streamlit as st

from src.constitution.interview import ConstitutionInterviewer, Turn
from src.constitution.loader import load_constitution, policy_path, save_constitution
from src.constitution.schema import (
    Approval,
    BehavioralGuards,
    Constitution,
    PositionLimits,
    UserProfile,
)


st.set_page_config(
    page_title="StockAIgents — Constitution",
    page_icon="📜",
    layout="wide",
)


# ─── async helper ─────────────────────────────────────────────────────


def run_async(coro):
    return asyncio.run(coro)


# ─── session state init ───────────────────────────────────────────────


def _init_state() -> None:
    st.session_state.setdefault("interview_history", [])
    st.session_state.setdefault("interview_done", False)
    st.session_state.setdefault("pending_question", None)
    st.session_state.setdefault("pending_draft", None)
    st.session_state.setdefault("pending_rationale", None)
    # Counter that becomes part of the answer-input widget key. Bumping
    # it after each Send creates a fresh widget on the next render —
    # which is the only safe way to "clear" a Streamlit text_input
    # (st.session_state[<widget_key>] can't be assigned to after the
    # widget mounts).
    st.session_state.setdefault("turn_counter", 0)


# ─── interview helpers ────────────────────────────────────────────────


async def _ask_next() -> None:
    interviewer = ConstitutionInterviewer()
    history = st.session_state["interview_history"]
    decision = await interviewer.next_question(history)
    if decision.done:
        st.session_state["interview_done"] = True
        st.session_state["pending_question"] = decision.next_question
    else:
        st.session_state["interview_done"] = False
        st.session_state["pending_question"] = decision.next_question


async def _draft() -> None:
    interviewer = ConstitutionInterviewer()
    history = st.session_state["interview_history"]
    constitution, rationale = await interviewer.draft(history)
    st.session_state["pending_draft"] = constitution
    st.session_state["pending_rationale"] = rationale


def _reset_interview() -> None:
    st.session_state["interview_history"] = []
    st.session_state["interview_done"] = False
    st.session_state["pending_question"] = None
    st.session_state["pending_draft"] = None
    st.session_state["pending_rationale"] = None
    st.session_state["turn_counter"] = st.session_state.get("turn_counter", 0) + 1


# ─── UI: interview tab ────────────────────────────────────────────────


def render_interview_tab() -> None:
    st.subheader("Conversational interview")
    st.caption(
        "I'll ask 6-12 short questions about your risk tolerance, time horizon, "
        "and trading preferences. You can adjust the result before saving."
    )

    history: list[Turn] = st.session_state["interview_history"]

    # Show transcript so far
    if history:
        with st.container(border=True):
            for i, t in enumerate(history, 1):
                st.markdown(f"**Q{i}.** {t.question}")
                st.markdown(f"_{t.answer}_")

    # Draft + approval flow if interview is done
    if st.session_state["interview_done"] and st.session_state["pending_draft"] is None:
        st.success(st.session_state.get("pending_question") or "Interview complete.")
        if st.button("Draft my Constitution", type="primary"):
            with st.spinner("Drafting…"):
                run_async(_draft())
            st.rerun()
        st.stop()

    if st.session_state["pending_draft"] is not None:
        _render_draft_review()
        st.stop()

    # Otherwise: ask next question
    if st.session_state["pending_question"] is None:
        with st.spinner("Thinking of the first question…"):
            run_async(_ask_next())
        st.rerun()

    st.markdown(f"### {st.session_state['pending_question']}")
    # Per-turn widget key — bumping turn_counter after Send produces a
    # fresh, empty text_input on the next render.
    answer_key = f"answer_input_{st.session_state['turn_counter']}"
    answer = st.text_input(
        "Your answer",
        key=answer_key,
        placeholder="Type your answer and press Enter or click Send.",
    )
    cols = st.columns([1, 1, 6])
    with cols[0]:
        send = st.button("Send", type="primary", disabled=not answer.strip())
    with cols[1]:
        if st.button("Reset interview"):
            _reset_interview()
            st.rerun()

    if send and answer.strip():
        history.append(
            Turn(
                question=st.session_state["pending_question"],
                answer=answer.strip(),
            )
        )
        # Bump the turn counter so the next render mounts a new (empty)
        # text_input widget under a different key.
        st.session_state["turn_counter"] += 1
        st.session_state["pending_question"] = None
        with st.spinner("Next question…"):
            run_async(_ask_next())
        st.rerun()


def _render_draft_review() -> None:
    draft: Constitution = st.session_state["pending_draft"]
    rationale: str = st.session_state["pending_rationale"] or ""

    st.success("Draft Constitution ready for your review.")
    st.markdown(f"**Why these values:** {rationale}")

    st.divider()
    edited = _render_editable_constitution(draft, key_prefix="draft")
    st.divider()

    cols = st.columns([1, 1, 4])
    with cols[0]:
        if st.button("✅ Approve & save", type="primary"):
            path = save_constitution(edited)
            st.session_state["pending_draft"] = None
            st.session_state["pending_rationale"] = None
            st.session_state["interview_history"] = []
            st.session_state["interview_done"] = False
            st.session_state["pending_question"] = None
            st.success(f"Saved to {path}.")
            st.rerun()
    with cols[1]:
        if st.button("Restart interview"):
            _reset_interview()
            st.rerun()


# ─── UI: current Constitution / editor ────────────────────────────────


def render_active_tab() -> None:
    constitution, source = load_constitution()
    st.caption(f"Source: **{source}**")

    if "policy.json" not in source:
        st.info(
            "You haven't saved a personal Constitution yet — the system is "
            "running off the committed default. Run the interview (left tab) "
            "or edit values below and save."
        )

    edited = _render_editable_constitution(constitution, key_prefix="active")

    cols = st.columns([1, 1, 4])
    with cols[0]:
        if st.button("Save changes", type="primary", key="save_active"):
            path = save_constitution(edited)
            st.success(f"Saved to {path}.")
            st.rerun()
    with cols[1]:
        if st.button("View raw JSON", key="raw_active"):
            st.code(constitution.model_dump_json(indent=2), language="json")


def _render_editable_constitution(
    c: Constitution, *, key_prefix: str
) -> Constitution:
    """Render every Constitution field as an editable widget; return a
    new Constitution reflecting the edits. Pure UI, no I/O."""
    k = key_prefix  # shorter

    st.markdown("#### User profile")
    cols = st.columns(3)
    risk = cols[0].selectbox(
        "Risk profile",
        ["conservative", "moderate", "aggressive"],
        index=["conservative", "moderate", "aggressive"].index(c.user_profile.risk_profile),
        key=f"{k}_risk",
    )
    horizon = cols[1].selectbox(
        "Time horizon",
        ["short_term", "medium_term", "long_term"],
        index=["short_term", "medium_term", "long_term"].index(c.user_profile.time_horizon),
        key=f"{k}_horizon",
    )
    exp = cols[2].selectbox(
        "Experience",
        ["beginner", "intermediate", "advanced"],
        index=["beginner", "intermediate", "advanced"].index(c.user_profile.experience_level),
        key=f"{k}_exp",
    )

    st.markdown("#### Position limits (% of portfolio)")
    cols = st.columns(4)
    max_trade = cols[0].number_input(
        "Max single trade %",
        min_value=0.0,
        max_value=100.0,
        value=float(c.position_limits.max_single_trade_pct),
        step=0.1,
        key=f"{k}_max_trade",
    )
    max_stock = cols[1].number_input(
        "Max single stock %",
        min_value=0.0,
        max_value=100.0,
        value=float(c.position_limits.max_single_stock_pct),
        step=0.5,
        key=f"{k}_max_stock",
    )
    max_sector = cols[2].number_input(
        "Max sector %",
        min_value=0.0,
        max_value=100.0,
        value=float(c.position_limits.max_sector_pct),
        step=0.5,
        key=f"{k}_max_sector",
    )
    min_cash = cols[3].number_input(
        "Min cash %",
        min_value=0.0,
        max_value=100.0,
        value=float(c.position_limits.min_cash_pct),
        step=0.5,
        key=f"{k}_min_cash",
    )

    st.markdown("#### Asset classes & order types")
    asset_options = ["stocks", "etfs", "options", "margin", "crypto", "futures"]
    cols = st.columns(2)
    allowed_assets = cols[0].multiselect(
        "Allowed asset classes",
        asset_options,
        default=list(c.allowed_asset_classes),
        key=f"{k}_allowed_assets",
    )
    blocked_assets = cols[1].multiselect(
        "Blocked asset classes",
        asset_options,
        default=list(c.blocked_asset_classes),
        key=f"{k}_blocked_assets",
    )
    order_options = ["market", "limit", "stop_market", "stop_limit"]
    cols = st.columns(2)
    allowed_orders = cols[0].multiselect(
        "Allowed order types",
        order_options,
        default=list(c.allowed_order_types),
        key=f"{k}_allowed_orders",
    )
    blocked_orders = cols[1].multiselect(
        "Blocked order types",
        order_options,
        default=list(c.blocked_order_types),
        key=f"{k}_blocked_orders",
    )

    st.markdown("#### Approval & behavioral guards")
    cols = st.columns(4)
    human_approval = cols[0].checkbox(
        "Require human approval",
        value=c.approval.human_approval_required,
        key=f"{k}_approval",
    )
    auto_threshold = cols[1].number_input(
        "Auto-execute threshold %",
        min_value=0.0,
        max_value=100.0,
        value=float(c.approval.auto_execute_threshold_pct),
        step=0.1,
        key=f"{k}_auto_thr",
        help="Trade size below which approval can be auto-granted (0 disables auto-execute).",
    )
    cooldown = cols[2].number_input(
        "Cooldown after loss (minutes)",
        min_value=0,
        max_value=10_000,
        value=int(c.behavioral_guards.cooldown_after_loss_minutes),
        step=15,
        key=f"{k}_cooldown",
    )
    max_per_day = cols[3].number_input(
        "Max trades per day",
        min_value=0,
        max_value=1_000,
        value=int(c.behavioral_guards.max_trades_per_day),
        step=1,
        key=f"{k}_max_per_day",
    )

    overlap_assets = set(allowed_assets) & set(blocked_assets)
    overlap_orders = set(allowed_orders) & set(blocked_orders)
    if overlap_assets:
        st.error(
            f"Asset classes can't be both allowed and blocked: {', '.join(overlap_assets)}"
        )
    if overlap_orders:
        st.error(
            f"Order types can't be both allowed and blocked: {', '.join(overlap_orders)}"
        )

    return Constitution(
        version=c.version,
        created_at=c.created_at if c.created_at else datetime.now(timezone.utc),
        user_profile=UserProfile(
            risk_profile=risk,
            time_horizon=horizon,
            experience_level=exp,
        ),
        position_limits=PositionLimits(
            max_single_trade_pct=max_trade,
            max_single_stock_pct=max_stock,
            max_sector_pct=max_sector,
            min_cash_pct=min_cash,
        ),
        allowed_asset_classes=allowed_assets,
        blocked_asset_classes=blocked_assets,
        allowed_order_types=allowed_orders,
        blocked_order_types=blocked_orders,
        approval=Approval(
            human_approval_required=human_approval,
            auto_execute_threshold_pct=auto_threshold,
        ),
        behavioral_guards=BehavioralGuards(
            cooldown_after_loss_minutes=cooldown,
            max_trades_per_day=max_per_day,
        ),
    )


# ─── page entry ───────────────────────────────────────────────────────


def main() -> None:
    _init_state()
    st.markdown(
        """
        <div style="display:flex;align-items:baseline;gap:0.6em;margin-bottom:0.2em">
          <span style="font-size:2.2em;font-weight:800;letter-spacing:-0.02em">
            📜 StockAIgents
          </span>
          <span style="font-size:1.0em;color:#64748b;font-weight:500">
            — Trading Constitution
          </span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.caption(
        "The Constitution defines the hard rules every trade is checked against. "
        "Code enforces it; the LLM only helps you draft it."
    )

    interview_tab, active_tab = st.tabs(["Run interview", "Active Constitution"])
    with interview_tab:
        render_interview_tab()
    with active_tab:
        render_active_tab()


main()
