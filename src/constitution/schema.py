from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


RiskProfile = Literal["conservative", "moderate", "aggressive"]
TimeHorizon = Literal["short_term", "medium_term", "long_term"]
ExperienceLevel = Literal["beginner", "intermediate", "advanced"]
AssetClass = Literal["stocks", "etfs", "options", "margin", "crypto", "futures"]
OrderType = Literal["market", "limit", "stop_market", "stop_limit"]


class UserProfile(BaseModel):
    risk_profile: RiskProfile
    time_horizon: TimeHorizon
    experience_level: ExperienceLevel


class PositionLimits(BaseModel):
    max_single_trade_pct: float = Field(ge=0.0, le=100.0)
    max_single_stock_pct: float = Field(ge=0.0, le=100.0)
    max_sector_pct: float = Field(ge=0.0, le=100.0)
    min_cash_pct: float = Field(ge=0.0, le=100.0)


class Approval(BaseModel):
    human_approval_required: bool
    auto_execute_threshold_pct: float = Field(ge=0.0, le=100.0)


class BehavioralGuards(BaseModel):
    cooldown_after_loss_minutes: int = Field(ge=0)
    max_trades_per_day: int = Field(ge=0)


class Constitution(BaseModel):
    version: str
    created_at: datetime
    user_profile: UserProfile
    position_limits: PositionLimits
    allowed_asset_classes: list[AssetClass]
    blocked_asset_classes: list[AssetClass]
    allowed_order_types: list[OrderType]
    blocked_order_types: list[OrderType]
    approval: Approval
    behavioral_guards: BehavioralGuards
