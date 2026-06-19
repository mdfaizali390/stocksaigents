from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol

from pydantic import BaseModel


class Account(BaseModel):
    model_config = {"extra": "ignore"}

    account_number: str
    rhs_account_number: str
    type: Literal["margin", "cash"]
    brokerage_account_type: str
    nickname: str | None = None
    is_default: bool
    agentic_allowed: bool
    option_level: str
    management_type: str
    state: str
    affiliate: str | None = None
    deactivated: bool = False
    permanently_deactivated: bool = False


class BuyingPower(BaseModel):
    buying_power: Decimal
    unleveraged_buying_power: Decimal
    display_currency: str


class Portfolio(BaseModel):
    model_config = {"extra": "ignore"}

    total_value: Decimal
    equity_value: Decimal
    options_value: Decimal
    futures_value: Decimal
    event_contracts_value: Decimal
    crypto_value: Decimal
    mutual_funds_value: Decimal
    fixed_income_value: Decimal
    cash: Decimal
    pending_deposits: Decimal
    currency: str
    buying_power: BuyingPower


class Position(BaseModel):
    model_config = {"extra": "ignore"}

    symbol: str
    quantity: Decimal
    intraday_quantity: Decimal
    average_buy_price: Decimal | None = None
    shares_available_for_sells: Decimal
    shares_held_for_sells: Decimal
    type: Literal["long", "short"]


class Quote(BaseModel):
    model_config = {"extra": "ignore"}

    symbol: str
    last_trade_price: Decimal
    last_non_reg_trade_price: Decimal
    venue_last_trade_time: datetime
    venue_last_non_reg_trade_time: datetime
    previous_close: Decimal
    adjusted_previous_close: Decimal
    bid_price: Decimal
    ask_price: Decimal
    has_traded: bool
    state: str
    venue_bid_time: datetime | None = None
    venue_ask_time: datetime | None = None
    previous_close_date: str | None = None


class OrderExecution(BaseModel):
    id: str
    price: Decimal
    quantity: Decimal
    timestamp: datetime
    fees: Decimal


OrderState = Literal[
    "new",
    "queued",
    "confirmed",
    "unconfirmed",
    "partially_filled",
    "filled",
    "cancelled",
    "rejected",
    "failed",
    "voided",
]


class Order(BaseModel):
    model_config = {"extra": "ignore"}

    id: str
    instrument_id: str
    symbol: str
    side: Literal["buy", "sell"]
    type: Literal["limit", "market", "stop_loss", "stop_limit"]
    state: OrderState
    quantity: Decimal
    cumulative_quantity: Decimal
    price: Decimal
    stop_price: Decimal | None = None
    average_price: Decimal | None = None
    fees: Decimal
    dollar_based_amount: Decimal | None = None
    time_in_force: Literal["gfd", "gtc", "ioc", "fok"]
    market_hours: Literal["regular_hours", "extended_hours", "all_hours"]
    trigger: Literal["immediate", "stop"]
    placed_agent: Literal["user", "agentic", "recurring", "drip"]
    created_at: datetime
    last_transaction_at: datetime
    executions: list[OrderExecution] = []


# Order placement shapes — concrete schema captured in Phase 4 when wiring
# review_equity_order / place_equity_order. Loose for now so the Protocol
# below type-checks.
class OrderRequest(BaseModel):
    account_number: str
    symbol: str
    side: Literal["buy", "sell"]
    type: Literal["limit", "market", "stop_loss", "stop_limit"]
    quantity: Decimal
    price: Decimal | None = None
    time_in_force: Literal["gfd", "gtc", "ioc", "fok"] = "gfd"


class OrderPreview(BaseModel):
    order: OrderRequest
    estimated_cost: Decimal | None = None
    warnings: list[str] = []


class BrokerageClient(Protocol):
    """Brokerage abstraction. Swappable: RobinhoodMCPClient (POC) → ETradeClient (Lambda)."""

    async def get_accounts(self) -> list[Account]: ...
    async def get_portfolio(self, account_number: str) -> Portfolio: ...
    async def get_positions(self, account_number: str) -> list[Position]: ...
    async def get_orders(
        self,
        account_number: str,
        created_at_gte: datetime | None = None,
        symbol: str | None = None,
        placed_agent: str | None = None,
    ) -> list[Order]: ...
    async def get_quotes(self, symbols: list[str]) -> list[Quote]: ...
    async def get_tradability(self, symbol: str) -> dict: ...
    async def review_order(self, order: OrderRequest) -> OrderPreview: ...
    async def place_order(self, order: OrderRequest) -> Order: ...
    async def cancel_order(self, order_id: str) -> None: ...
    async def search(self, query: str) -> list[dict]: ...
