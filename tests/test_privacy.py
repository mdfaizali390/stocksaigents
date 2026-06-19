"""Tests for the privacy redaction at the display boundary.

Owner rule: never display personal holdings dollar amounts or share counts.
Percentages are fine. These tests lock that in.
"""

from src.privacy import redact, redact_list, redact_shares


def test_redacts_dollar_amounts():
    assert redact("Your cash balance is $4,382.76.") == "Your cash balance is $[redacted]."


def test_redacts_share_counts():
    assert redact("You have 68 shares of AAPL.") == "You have [redacted] shares of AAPL."


def test_redacts_both_in_one_string():
    out = redact("You hold 1,200 shares worth $210,902.26.")
    assert "1,200" not in out
    assert "210,902" not in out
    assert "[redacted] shares" in out
    assert "$[redacted]" in out


def test_preserves_percentages():
    s = "Technology sector at 34.54% is below the 30% limit."
    assert redact(s) == s


def test_preserves_no_number_holdings_phrase():
    s = "You don't currently hold any NVDA shares."
    assert redact(s) == s


def test_redacts_simple_dollar_no_decimals():
    assert redact("a shortfall of approximately $159") == "a shortfall of approximately $[redacted]"


def test_redact_list_scrubs_shares_keeps_dollars():
    # redact_list is shares-only (used on agent prose / citations, which
    # carry public market dollar figures we must keep).
    out = redact_list(["target is $100", "12 shares held", "all good"])
    assert out == ["target is $100", "[redacted] shares held", "all good"]


def test_redact_shares_keeps_public_dollar_figures():
    # The bug we're fixing: analyst price targets / valuations must survive.
    s = "Oppenheimer set a $25 price target; valuation near $2 trillion."
    assert redact_shares(s) == s


def test_redact_shares_still_scrubs_share_counts():
    assert redact_shares("You have 68 shares of AAPL.") == "You have [redacted] shares of AAPL."


def test_redact_handles_none_and_empty():
    assert redact(None) == ""
    assert redact("") == ""
    assert redact_list(None) == []


def test_idempotent():
    once = redact("You have 68 shares worth $1,000.")
    assert redact(once) == once