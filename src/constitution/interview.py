"""LLM-driven Constitution interview.

Two-stage flow:

  1. ``next_question(history)`` — given the conversation so far, returns
     either the next question to ask the user or a signal that we're
     done. ~8-12 questions; the model decides when it has enough.
  2. ``propose_constitution(history)`` — once the interview is done,
     synthesize a draft Constitution. The user reviews and approves.

The interviewer never enforces anything — it just gathers preferences
and produces JSON. Code enforces the Constitution thereafter.

Why split into two prompts: the question-asking model sees the running
transcript and decides "ask more or done?"; the synthesizer sees the
full transcript and writes the JSON. Same model, different prompts —
keeps each task focused.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from src.constitution.schema import (
    Approval,
    BehavioralGuards,
    Constitution,
    PositionLimits,
    UserProfile,
)
from src.llm.client import LLMClient


# Hard cap so the interview can't run forever.
_MAX_QUESTIONS = 12
_MIN_QUESTIONS = 6


@dataclass
class Turn:
    """One exchange in the interview transcript."""

    question: str
    answer: str


_QUESTION_SYSTEM_PROMPT = f"""\
You are conducting a short interview to draft a personal Trading
Constitution for the user. The Constitution defines hard rules the system
will enforce on every trade — not advice, not goals, but limits.

Cover these dimensions in roughly this order:
  1. Risk tolerance (conservative / moderate / aggressive)
  2. Time horizon (short / medium / long term)
  3. Investing experience level
  4. Max single trade as % of portfolio
  5. Max single-stock allocation
  6. Max sector allocation
  7. Min cash %
  8. Allowed asset classes (stocks, ETFs, options, margin, crypto, etc.)
  9. Allowed order types (market / limit / stop)
 10. Whether human approval is required for every trade
 11. Behavioral guards (cooldown after a loss, max trades per day)

Rules:
- Ask ONE question per turn. Be concise — one sentence.
- Don't ask compound questions ("what's your risk profile and how long do
  you invest"). Pick one.
- After answer #1 (risk profile), the rest of your questions should
  PROPOSE a typical value first, then ask if the user wants to change
  it. Example: "For a moderate-risk profile, a 1% per-trade cap is
  typical. Keep it at 1%, or adjust?"
- You can skip dimensions you can confidently infer from earlier answers
  — don't be robotic.
- Stop early when you have enough signal. Minimum {_MIN_QUESTIONS} questions,
  maximum {_MAX_QUESTIONS}.
- When done, set ``done=true`` and put a 1-line wrap-up in ``next_question``
  ("Got it — I'll draft your Constitution now.").
"""


_DRAFT_SYSTEM_PROMPT = """\
You are drafting a JSON Trading Constitution from an interview transcript.

You will receive:
  - The full Q/A transcript

Your job: call the ConstitutionDraft tool with concrete numeric values
for every field. Make defensible choices when the user gave a vague
answer; the user will see the draft and can adjust.

Rules:
- Numeric limits should be appropriate for the stated risk profile:
    conservative: max_single_trade ≤ 0.5%, max_single_stock ≤ 10%,
                  max_sector ≤ 25%, min_cash ≥ 10%
    moderate:     max_single_trade ≤ 1.0%, max_single_stock ≤ 15%,
                  max_sector ≤ 30%, min_cash ≥ 5%
    aggressive:   max_single_trade ≤ 2.0%, max_single_stock ≤ 25%,
                  max_sector ≤ 40%, min_cash ≥ 2%
- ``allowed_asset_classes`` and ``blocked_asset_classes`` must NOT
  overlap. If the user didn't mention crypto/options/margin, BLOCK them
  by default (safer prior).
- ``allowed_order_types`` defaults to ["limit"] only — market orders
  are dangerous and require explicit user opt-in.
- ``human_approval_required`` defaults to true.
- ``cooldown_after_loss_minutes`` defaults to 60; ``max_trades_per_day``
  to 5. Adjust if the user expressed strong opinions.
"""


class _NextQuestion(BaseModel):
    """The interviewer's next move."""

    next_question: str = Field(
        description="Either the next question to ask the user, OR a 1-line wrap-up if done"
    )
    done: bool = Field(description="True when the interview is complete")
    reason: str = Field(description="One-sentence rationale for the decision")


class _ConstitutionDraft(BaseModel):
    """A drafted Constitution ready for user review."""

    risk_profile: Literal["conservative", "moderate", "aggressive"]
    time_horizon: Literal["short_term", "medium_term", "long_term"]
    experience_level: Literal["beginner", "intermediate", "advanced"]
    max_single_trade_pct: float = Field(ge=0.0, le=100.0)
    max_single_stock_pct: float = Field(ge=0.0, le=100.0)
    max_sector_pct: float = Field(ge=0.0, le=100.0)
    min_cash_pct: float = Field(ge=0.0, le=100.0)
    allowed_asset_classes: list[Literal["stocks", "etfs", "options", "margin", "crypto", "futures"]]
    blocked_asset_classes: list[Literal["stocks", "etfs", "options", "margin", "crypto", "futures"]]
    allowed_order_types: list[Literal["market", "limit", "stop_market", "stop_limit"]]
    blocked_order_types: list[Literal["market", "limit", "stop_market", "stop_limit"]]
    human_approval_required: bool
    auto_execute_threshold_pct: float = Field(ge=0.0, le=100.0)
    cooldown_after_loss_minutes: int = Field(ge=0)
    max_trades_per_day: int = Field(ge=0)
    rationale: str = Field(
        description="One paragraph explaining why these values fit the user"
    )


class ConstitutionInterviewer:
    """Stateless. The Streamlit page (or any caller) holds the transcript
    and calls these methods turn-by-turn."""

    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm or LLMClient()

    async def next_question(self, history: list[Turn]) -> _NextQuestion:
        """Decide the next question, or signal done. Caller appends the
        user's answer to history before calling again."""
        # Hard cap — force done after _MAX_QUESTIONS.
        if len(history) >= _MAX_QUESTIONS:
            return _NextQuestion(
                next_question="Got it — I'll draft your Constitution now.",
                done=True,
                reason=f"Reached the {_MAX_QUESTIONS}-question cap.",
            )

        prompt = _format_history_prompt(history)
        return await self._llm.complete_structured(
            prompt=prompt,
            schema=_NextQuestion,
            system=_QUESTION_SYSTEM_PROMPT,
            cache_system=True,
        )

    async def draft(self, history: list[Turn]) -> tuple[Constitution, str]:
        """Synthesize a Constitution from the full interview transcript.
        Returns the parsed Constitution and the model's rationale prose."""
        if not history:
            raise ValueError("can't draft a Constitution from an empty interview")

        prompt = (
            "Interview transcript:\n\n"
            f"{_format_transcript(history)}\n\n"
            "Call ConstitutionDraft with concrete values for every field."
        )
        draft = await self._llm.complete_structured(
            prompt=prompt,
            schema=_ConstitutionDraft,
            system=_DRAFT_SYSTEM_PROMPT,
            cache_system=True,
        )

        # Defense in depth: if the model emits overlapping allow/block
        # lists, drop overlap from the *allowed* list (safer side).
        allowed_assets = [
            a for a in draft.allowed_asset_classes if a not in draft.blocked_asset_classes
        ]
        allowed_orders = [
            o for o in draft.allowed_order_types if o not in draft.blocked_order_types
        ]

        constitution = Constitution(
            version="1.0",
            created_at=datetime.now(timezone.utc),
            user_profile=UserProfile(
                risk_profile=draft.risk_profile,
                time_horizon=draft.time_horizon,
                experience_level=draft.experience_level,
            ),
            position_limits=PositionLimits(
                max_single_trade_pct=draft.max_single_trade_pct,
                max_single_stock_pct=draft.max_single_stock_pct,
                max_sector_pct=draft.max_sector_pct,
                min_cash_pct=draft.min_cash_pct,
            ),
            allowed_asset_classes=allowed_assets,  # type: ignore[arg-type]
            blocked_asset_classes=draft.blocked_asset_classes,  # type: ignore[arg-type]
            allowed_order_types=allowed_orders,  # type: ignore[arg-type]
            blocked_order_types=draft.blocked_order_types,  # type: ignore[arg-type]
            approval=Approval(
                human_approval_required=draft.human_approval_required,
                auto_execute_threshold_pct=draft.auto_execute_threshold_pct,
            ),
            behavioral_guards=BehavioralGuards(
                cooldown_after_loss_minutes=draft.cooldown_after_loss_minutes,
                max_trades_per_day=draft.max_trades_per_day,
            ),
        )
        return constitution, draft.rationale


def _format_history_prompt(history: list[Turn]) -> str:
    if not history:
        return (
            "Start the interview by asking your first question. "
            "Set done=false."
        )
    lines = [
        f"Transcript so far ({len(history)} exchanges):",
        "",
        _format_transcript(history),
        "",
        "Decide: ask another question, or are you done?",
    ]
    return "\n".join(lines)


def _format_transcript(history: list[Turn]) -> str:
    parts = []
    for i, t in enumerate(history, 1):
        parts.append(f"Q{i}: {t.question}")
        parts.append(f"A{i}: {t.answer}")
    return "\n".join(parts)


__all__ = ["Turn", "ConstitutionInterviewer"]
