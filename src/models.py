from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from src.constitution.schema import AssetClass, OrderType


Signal = Literal["BUY", "HOLD", "SELL", "INFO", "WARNING", "BLOCK"]
RecommendedAction = Literal["BUY", "SELL", "HOLD", "NO_ACTION", "BLOCKED"]
IntentType = Literal[
    "trade_decision",
    "market_info",
    "portfolio_analysis",
    "portfolio_fact",
    "policy_question",
]
AgentName = Literal[
    "research",
    "quant",
    "trending",
    "risk",
    "behavioral",
    "compliance",
]
TradeAction = Literal["buy", "sell"]
RunMode = Literal["dry_run", "live"]


class Evidence(BaseModel):
    source: str
    description: str
    data: dict = {}


class AgentReport(BaseModel):
    agent_name: str
    signal: Signal
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str
    reasoning: str
    evidence: list[Evidence] = []
    blocking: bool = False
    blocking_reason: str | None = None
    metadata: dict = {}


class Intent(BaseModel):
    intent_type: IntentType
    ticker: str | None = None
    action: TradeAction | None = None
    agents_to_run: list[AgentName]
    rationale: str


class Recommendation(BaseModel):
    action: RecommendedAction
    ticker: str | None = None
    quantity_suggestion: int | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str
    citations: list[str] = []
    block_reasons: list[str] = []


class Decision(BaseModel):
    intent: Intent
    reports: list[AgentReport]
    recommendation: Recommendation
    blocking_reports: list[AgentReport] = []
    mode: RunMode
    timestamp: datetime


class TrendingTicker(BaseModel):
    ticker: str
    score: float = Field(ge=0.0, le=100.0)
    components: dict[str, float]
    headline_evidence: list[str] = []


class ProposedTrade(BaseModel):
    """A concrete trade the user is asking us to evaluate.

    Built by the orchestrator from the parsed Intent + a quote. Required input
    for Compliance and Risk so they don't have to re-derive notional value
    from market data.
    """

    ticker: str
    side: TradeAction
    order_type: OrderType
    quantity: Decimal
    asset_class: AssetClass
    estimated_price: Decimal
    limit_price: Decimal | None = None
    tradability: dict | None = None

    @property
    def estimated_notional(self) -> Decimal:
        return self.quantity * self.estimated_price
