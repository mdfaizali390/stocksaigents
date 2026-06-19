"""Privacy redaction for user-facing output.

Rule (owner directive): never display the user's **personal holdings** —
how many shares they own, or the dollar value of their holdings/cash.
Percentages are fine. Public market data (analyst price targets, company
valuations, stock prices) is NOT personal and must stay visible.

Two scrubbers, applied in different places:

  - ``redact_shares`` — removes share counts ("68 shares" → "[redacted]
    shares"). A share count is essentially always the user's holding, so
    this is safe to apply to EVERY rendered string.

  - ``redact`` — removes share counts AND dollar amounts. Dollar amounts
    are ambiguous (a "$2T valuation" is public; "$4,382 cash" is personal),
    so we only apply this where the value is *known to be personal* — the
    portfolio-fact answer, which deliberately reports the user's own cash /
    value. We apply it there at the source, not at the display boundary.

Enforced in CODE, not via LLM prompts — same philosophy as the block check.
"""

from __future__ import annotations

import re

REDACTED = "[redacted]"

# $1,234.56  ·  $159  ·  $4,382.76  ·  $0.99   (with or without decimals/commas)
_DOLLAR = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?")

# "68 shares", "1,200 shares", "12.5 shares of AAPL"
_SHARES = re.compile(r"\b\d[\d,]*(?:\.\d+)?\s+shares\b", re.IGNORECASE)


def redact_shares(text: str | None) -> str:
    """Scrub share counts only. Safe for any user-facing string —
    percentages, prices, and all other content are preserved."""
    if not text:
        return text or ""
    return _SHARES.sub(f"{REDACTED} shares", text)


def redact(text: str | None) -> str:
    """Scrub share counts AND dollar amounts. Use only where the value is
    known to be the user's personal holdings (e.g. the portfolio-fact
    answer) — NOT on general agent prose, which carries public market data."""
    if not text:
        return text or ""
    out = _DOLLAR.sub(f"${REDACTED}", text)
    out = _SHARES.sub(f"{REDACTED} shares", out)
    return out


def redact_list(items: list[str] | None) -> list[str]:
    """Redact share counts in each string of a list (citations, etc.)."""
    if not items:
        return []
    return [redact_shares(s) for s in items]


__all__ = ["redact", "redact_shares", "redact_list", "REDACTED"]
