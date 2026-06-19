"""Robinhood MCP read-path smoke test.

Run from the repo root::

    .venv/bin/python -m scripts.connect_robinhood

On first run the MCP SDK will:
  1. Register this app with Robinhood (POSTs to /oauth/trading/register).
  2. Open your browser to robinhood.com/oauth — log in and authorize.
  3. Capture the redirect on http://localhost:33418/callback.
  4. Persist client_info.json + tokens.json under .cache/robinhood_oauth/.

Subsequent runs reuse the saved tokens and refresh silently.
"""

from __future__ import annotations

import asyncio

from src.data.brokerage.robinhood_mcp import RobinhoodMCPClient


def _mask(s: str) -> str:
    return f"••••{s[-4:]}" if s and len(s) >= 4 else "••••"


async def main() -> None:
    print("Connecting to Robinhood MCP …")
    async with RobinhoodMCPClient() as rh:
        tools = await rh.list_tools()
        print(f"\nMCP tools available ({len(tools)}):")
        for t in tools:
            print(f"  · {t}")

        accounts = await rh.get_accounts()
        print(f"\nAccounts ({len(accounts)}):")
        for a in accounts:
            tag = " [agentic]" if a.agentic_allowed else ""
            label = a.nickname or a.brokerage_account_type
            print(
                f"  · {_mask(a.account_number)}  {label}  "
                f"type={a.type}  default={a.is_default}  state={a.state}{tag}"
            )

        target = next((a for a in accounts if a.is_default), accounts[0])
        print(f"\nPortfolio for {_mask(target.account_number)}:")
        try:
            p = await rh.get_portfolio(target.account_number)
            print(f"  total_value:  {p.total_value} {p.currency}")
            print(f"  equity_value: {p.equity_value}")
            print(f"  cash:         {p.cash}")
            print(f"  buying_power: {p.buying_power.buying_power}")
        except Exception as e:
            print(f"  (failed — {type(e).__name__}: {e})")

        print(f"\nPositions for {_mask(target.account_number)}:")
        try:
            positions = await rh.get_positions(target.account_number)
            if not positions:
                print("  (none)")
            for pos in positions[:10]:
                print(
                    f"  · {pos.symbol}  qty={pos.quantity}  "
                    f"sellable={pos.shares_available_for_sells}  "
                    f"avg_buy={pos.average_buy_price}"
                )
            if len(positions) > 10:
                print(f"  … and {len(positions) - 10} more")
        except Exception as e:
            print(f"  (failed — {type(e).__name__}: {e})")

        print("\nQuote for AAPL:")
        try:
            (q,) = await rh.get_quotes(["AAPL"])
            print(
                f"  last_trade_price: {q.last_trade_price}  "
                f"prev_close: {q.previous_close}  "
                f"bid/ask: {q.bid_price}/{q.ask_price}  "
                f"state: {q.state}  has_traded: {q.has_traded}"
            )
        except Exception as e:
            print(f"  (failed — {type(e).__name__}: {e})")

    print("\nDone. Tokens cached under .cache/robinhood_oauth/.")


if __name__ == "__main__":
    asyncio.run(main())
