"""Constitution loader.

Loads ``src/constitution/policy.json`` if it exists, falls back to the
committed example, and finally to a stub. Returns both the parsed
``Constitution`` and a string describing where it came from so the UI
can surface that to the user.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.constitution.schema import (
    Approval,
    BehavioralGuards,
    Constitution,
    PositionLimits,
    UserProfile,
)


_POLICY_PATH = Path("src/constitution/policy.json")
_EXAMPLE_PATH = Path("src/constitution/policy.example.json")


def save_constitution(c: Constitution, path: Path = _POLICY_PATH) -> Path:
    """Write a Constitution to disk as pretty JSON. Returns the path
    written (so callers can show it in the UI)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(c.model_dump_json(indent=2))
    return path


def policy_path() -> Path:
    """Where ``save_constitution`` writes by default."""
    return _POLICY_PATH


def load_constitution() -> tuple[Constitution, str]:
    """Returns (constitution, source_label).

    source_label is one of:
      - "policy.json (your saved policy)"
      - "policy.example.json (committed default)"
      - "built-in stub"
    """
    if _POLICY_PATH.exists():
        return (
            Constitution.model_validate_json(_POLICY_PATH.read_text()),
            "policy.json (your saved policy)",
        )
    if _EXAMPLE_PATH.exists():
        return (
            Constitution.model_validate_json(_EXAMPLE_PATH.read_text()),
            "policy.example.json (committed default)",
        )
    return _stub(), "built-in stub"


def _stub() -> Constitution:
    return Constitution(
        version="1.0",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        user_profile=UserProfile(
            risk_profile="moderate",
            time_horizon="long_term",
            experience_level="intermediate",
        ),
        position_limits=PositionLimits(
            max_single_trade_pct=1.0,
            max_single_stock_pct=15.0,
            max_sector_pct=30.0,
            min_cash_pct=5.0,
        ),
        allowed_asset_classes=["stocks", "etfs"],
        blocked_asset_classes=["options", "margin", "crypto"],
        allowed_order_types=["limit"],
        blocked_order_types=["market", "stop_market"],
        approval=Approval(human_approval_required=True, auto_execute_threshold_pct=0.0),
        behavioral_guards=BehavioralGuards(
            cooldown_after_loss_minutes=60, max_trades_per_day=5
        ),
    )


__all__ = ["load_constitution", "save_constitution", "policy_path"]
