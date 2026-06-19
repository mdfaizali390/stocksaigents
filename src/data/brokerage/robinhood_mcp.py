"""Robinhood MCP brokerage client.

Connects to ``https://agent.robinhood.com/mcp/trading`` over Streamable HTTP.
Auth is OAuth 2.0 with PKCE + Dynamic Client Registration; both client info
and tokens are persisted to disk so subsequent runs skip the browser flow.

Token lifecycle (per OAuth):
    - access_token: refreshed automatically by the SDK when expired.
    - refresh_token: long-lived; if Robinhood revokes it (e.g. password reset),
      the next call surfaces an auth error and the user is re-prompted.
"""

from __future__ import annotations

import json
import threading
import webbrowser
from datetime import datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)

from src.data.brokerage.base import (
    Account,
    BrokerageClient,
    Order,
    OrderPreview,
    OrderRequest,
    Portfolio,
    Position,
    Quote,
)


DEFAULT_SERVER_URL = "https://agent.robinhood.com/mcp/trading"
DEFAULT_CALLBACK_PORT = 33418
DEFAULT_REDIRECT_URI = f"http://localhost:{DEFAULT_CALLBACK_PORT}/callback"
DEFAULT_TOKEN_DIR = Path(".cache/robinhood_oauth")


class FileTokenStorage(TokenStorage):
    """Persists OAuth tokens + client registration to disk.

    The MCP SDK calls these methods at three moments:
      1. set_client_info — after Dynamic Client Registration (once).
      2. set_tokens      — after the user completes the browser flow, and
                           again every time the access token is refreshed.
      3. get_*           — at the start of every connection.
    """

    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._dir.mkdir(parents=True, exist_ok=True)
        self._client_info_path = self._dir / "client_info.json"
        self._tokens_path = self._dir / "tokens.json"

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        if not self._client_info_path.exists():
            return None
        return OAuthClientInformationFull.model_validate_json(
            self._client_info_path.read_text()
        )

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._client_info_path.write_text(client_info.model_dump_json(indent=2))

    async def get_tokens(self) -> OAuthToken | None:
        if not self._tokens_path.exists():
            return None
        return OAuthToken.model_validate_json(self._tokens_path.read_text())

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._tokens_path.write_text(tokens.model_dump_json(indent=2))


def _build_redirect_handler():
    async def handler(url: str) -> None:
        print(
            "\n"
            "──────────────────────────────────────────────────────────────────\n"
            "Robinhood wants you to authorize this app.\n"
            "Opening your browser. If it doesn't open, paste this URL:\n\n"
            f"  {url}\n"
            "──────────────────────────────────────────────────────────────────\n"
        )
        webbrowser.open(url)

    return handler


def _build_callback_handler(port: int):
    """One-shot HTTP server that captures ?code=...&state=... from Robinhood's redirect."""

    captured: dict[str, str | None] = {}
    done = threading.Event()

    class CallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            print(f"[callback-server] {self.client_address[0]} {fmt % args}", flush=True)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            print(f"[callback-server] GET {self.path}", flush=True)
            if parsed.path != "/callback":
                self.send_response(404)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"not found")
                return
            captured["code"] = params.get("code", [None])[0]
            captured["state"] = params.get("state", [None])[0]
            error = params.get("error", [None])[0]

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if error or not captured["code"]:
                msg = f"Authorization failed: {error or 'no code returned'}. You can close this tab."
            else:
                msg = "Authorization complete. You can close this tab."
            self.wfile.write(f"<html><body><h2>{msg}</h2></body></html>".encode())
            done.set()

    async def callback() -> tuple[str, str | None]:
        server = HTTPServer(("localhost", port), CallbackHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        print(
            f"[callback-server] listening on http://localhost:{port}/callback",
            flush=True,
        )
        try:
            # Block until the redirect arrives. Wait in a worker thread so the
            # async event loop stays free.
            import asyncio

            got_it = await asyncio.to_thread(done.wait, 300)
            if not got_it:
                print(
                    "[callback-server] timeout — never received the redirect.",
                    flush=True,
                )
        finally:
            server.shutdown()
            server.server_close()
            print("[callback-server] shut down.", flush=True)
        if not captured.get("code"):
            raise RuntimeError("OAuth callback did not return an authorization code")
        return captured["code"], captured.get("state")

    return callback


def _build_client_metadata() -> OAuthClientMetadata:
    """Metadata sent to /oauth/trading/register on first run."""
    return OAuthClientMetadata(
        redirect_uris=[DEFAULT_REDIRECT_URI],
        token_endpoint_auth_method="none",  # Robinhood's discovery says none
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope="internal",
        client_name="stock-ai-agents-poc",
    )


def _decode_tool_result(result: Any) -> Any:
    """Robinhood returns tool results as a single text content with JSON.

    MCP's ``CallToolResult`` carries a ``content`` list; for this server every
    item we've seen is a ``TextContent`` whose ``.text`` is JSON. We tolerate
    a few shapes so adding new tools doesn't trip on shape variance.
    """
    if hasattr(result, "structuredContent") and result.structuredContent is not None:
        return result.structuredContent
    if not getattr(result, "content", None):
        return None
    text_blobs: list[str] = []
    for item in result.content:
        text = getattr(item, "text", None)
        if text:
            text_blobs.append(text)
    if not text_blobs:
        return None
    joined = "\n".join(text_blobs)
    try:
        return json.loads(joined)
    except json.JSONDecodeError:
        return joined


def _unwrap_list(raw: Any, *keys: str) -> list:
    """Extract a list from Robinhood's wrapper shapes.

    Observed shapes so far:
        {"data": {"accounts": [...]}, "guide": "..."}
        {"data": {"results": [...]}, "guide": "..."}
        [...]                                 # already a list

    Tries the named keys at the top level *and* nested under ``data``.
    """
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, dict):
        raise ValueError(f"could not unwrap list from response: {raw!r}")

    candidates = (*keys, "data", "results", "items")
    # Direct hit at top level.
    for k in candidates:
        v = raw.get(k)
        if isinstance(v, list):
            return v
    # Nested under data.
    inner = raw.get("data")
    if isinstance(inner, dict):
        for k in candidates:
            v = inner.get(k)
            if isinstance(v, list):
                return v
    if isinstance(inner, list):
        return inner
    raise ValueError(f"could not unwrap list from response: {raw!r}")


def _unwrap_dict(raw: Any, *keys: str) -> dict:
    """Extract an inner dict, peeling ``data`` and named wrappers as needed."""
    if not isinstance(raw, dict):
        raise ValueError(f"could not unwrap dict from response: {raw!r}")

    inner = raw.get("data") if "data" in raw and isinstance(raw["data"], dict) else raw
    for k in keys:
        v = inner.get(k)
        if isinstance(v, dict):
            return v
    return inner


class RobinhoodMCPClient:
    """Async context manager that wraps an MCP session for the trading server.

    Usage::

        async with RobinhoodMCPClient() as rh:
            accounts = await rh.get_accounts()
    """

    def __init__(
        self,
        server_url: str = DEFAULT_SERVER_URL,
        token_dir: Path | str = DEFAULT_TOKEN_DIR,
        callback_port: int = DEFAULT_CALLBACK_PORT,
    ) -> None:
        self._server_url = server_url
        self._storage = FileTokenStorage(Path(token_dir))
        self._auth = OAuthClientProvider(
            server_url=server_url,
            client_metadata=_build_client_metadata(),
            storage=self._storage,
            redirect_handler=_build_redirect_handler(),
            callback_handler=_build_callback_handler(callback_port),
        )
        self._transport_cm = None
        self._session_cm = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> "RobinhoodMCPClient":
        self._transport_cm = streamablehttp_client(self._server_url, auth=self._auth)
        read, write, _ = await self._transport_cm.__aenter__()
        self._session_cm = ClientSession(read, write)
        self._session = await self._session_cm.__aenter__()
        await self._session.initialize()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            if self._session_cm is not None:
                await self._session_cm.__aexit__(exc_type, exc, tb)
        finally:
            if self._transport_cm is not None:
                await self._transport_cm.__aexit__(exc_type, exc, tb)
            self._session = None
            self._session_cm = None
            self._transport_cm = None

    async def list_tools(self) -> list[str]:
        assert self._session is not None, "client not connected"
        result = await self._session.list_tools()
        return [t.name for t in result.tools]

    async def _call(self, tool: str, **arguments: Any) -> Any:
        assert self._session is not None, "client not connected"
        result = await self._session.call_tool(tool, arguments=arguments or None)
        return _decode_tool_result(result)

    # ── BrokerageClient implementation (read-only first) ────────────────

    async def call_raw(self, tool: str, **arguments: Any) -> Any:
        """Escape hatch: invoke a tool and return the decoded payload as-is.

        Useful while reverse-engineering response shapes for tools we haven't
        modeled yet (write methods, watchlists, historicals).
        """
        return await self._call(tool, **arguments)

    async def get_accounts(self) -> list[Account]:
        raw = await self._call("get_accounts")
        return [Account.model_validate(a) for a in _unwrap_list(raw, "accounts")]

    async def get_portfolio(self, account_number: str) -> Portfolio:
        raw = await self._call("get_portfolio", account_number=account_number)
        return Portfolio.model_validate(_unwrap_dict(raw, "portfolio"))

    async def get_positions(self, account_number: str) -> list[Position]:
        raw = await self._call("get_equity_positions", account_number=account_number)
        return [Position.model_validate(p) for p in _unwrap_list(raw, "positions")]

    async def get_orders(
        self,
        account_number: str,
        created_at_gte: datetime | None = None,
        symbol: str | None = None,
        placed_agent: str | None = None,
    ) -> list[Order]:
        args: dict[str, Any] = {"account_number": account_number}
        if created_at_gte is not None:
            args["created_at_gte"] = created_at_gte.isoformat()
        if symbol is not None:
            args["symbol"] = symbol
        if placed_agent is not None:
            args["placed_agent"] = placed_agent
        raw = await self._call("get_equity_orders", **args)
        return [Order.model_validate(o) for o in _unwrap_list(raw, "orders")]

    async def get_quotes(self, symbols: list[str]) -> list[Quote]:
        raw = await self._call("get_equity_quotes", symbols=symbols)
        # Each result item is {"quote": {...}, "close": {...}} — pull quote out.
        results = _unwrap_list(raw, "quotes", "results")
        quotes_raw = [r.get("quote", r) if isinstance(r, dict) else r for r in results]
        return [Quote.model_validate(q) for q in quotes_raw]

    async def get_tradability(self, symbol: str) -> dict:
        raw = await self._call("get_equity_tradability", symbol=symbol)
        return raw if isinstance(raw, dict) else {"raw": raw}

    async def search(self, query: str) -> list[dict]:
        raw = await self._call("search", query=query)
        return _unwrap_list(raw, "results")

    # ── Write methods (Phase 4 — schemas captured when wiring) ──────────

    async def review_order(self, order: OrderRequest) -> OrderPreview:
        raise NotImplementedError("review_order — implement in Phase 4")

    async def place_order(self, order: OrderRequest) -> Order:
        raise NotImplementedError("place_order — implement in Phase 4")

    async def cancel_order(self, order_id: str) -> None:
        raise NotImplementedError("cancel_order — implement in Phase 4")


__all__ = ["RobinhoodMCPClient", "FileTokenStorage", "DEFAULT_SERVER_URL"]


# Static check that the class satisfies the Protocol.
_check: BrokerageClient = RobinhoodMCPClient()  # type: ignore[assignment]
del _check
