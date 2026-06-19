# Robinhood MCP Integration — Reference

**Status:** Working as of 2026-06-14. Read-only path verified end-to-end.
**Sanitized:** Personal account identifiers, portfolio values, holdings, token/client IDs, and exact smoke-test values are redacted.
**Last verified:** 2026-06-14
**Server URL:** `https://agent.robinhood.com/mcp/trading`
**Transport:** Streamable HTTP (the modern HTTP-based MCP transport, not stdio).
**Auth:** OAuth 2.0 / PKCE / Dynamic Client Registration.

This is the reference for *how* the integration works, what the wire-level
shapes look like, and what gotchas to watch for. The runtime code lives in
[`src/data/brokerage/robinhood_mcp.py`](../src/data/brokerage/robinhood_mcp.py)
and the smoke-test driver is [`scripts/connect_robinhood.py`](../scripts/connect_robinhood.py).

---

## 0. Challenges & decisions

### Why the integration is non-trivial

Robinhood's MCP server is not a typical "give me an API key" service:

- **No API keys exist** for `agent.robinhood.com/mcp/trading`. Every client
  must authenticate as the user via OAuth.
- **No static client ID** is published. The server requires Dynamic Client
  Registration — each app POSTs its metadata to `/oauth/trading/register`
  and gets a `client_id` back. This is unusual; most OAuth services give
  you a client ID up front from a developer portal.
- **OAuth 2.0 with PKCE is mandatory.** Discovery says
  `token_endpoint_auth_method: "none"` — no client secret. PKCE
  (Proof Key for Code Exchange) replaces the secret as the protection
  against intercepted authorization codes.
- **Browser-driven flow.** The user must log in to robinhood.com once and
  click "Authorize". There is no headless / username-password path. This
  is the reason the design notes Lambda incompatibility (§4.5).
- **Two-account safety wall is brokerage-enforced.** Writes are locked to
  accounts where `agentic_allowed=true` *at Robinhood's edge*, not by
  client-side checks. Even buggy code can't trade out of the main account.

### Why we used a localhost redirect (`http://localhost:33418/callback`)

OAuth's authorization-code flow needs a `redirect_uri` — somewhere
Robinhood can send the auth code after the user authorizes. We had three
realistic options:

| Option | Verdict |
|---|---|
| **Out-of-band copy/paste** (`urn:ietf:wg:oauth:2.0:oob`) — Robinhood shows the code on a page, user pastes into terminal | Robinhood's discovery doesn't advertise OOB support; deprecated in modern OAuth (Google removed it in 2022). Untested, ugly UX. |
| **Hosted public redirect** (e.g. `https://ourapp.com/callback`) | Requires a deployed server. Overkill for a local POC; introduces a network dependency for what should be a local-only tool. |
| **Localhost redirect** (`http://localhost:<port>/callback`) | ✅ The OAuth spec's [RFC 8252](https://datatracker.ietf.org/doc/html/rfc8252) recommendation for native apps. Standard pattern (used by GitHub CLI, Google `gcloud`, etc.). No public infrastructure needed; the redirect never leaves the machine. |

So we run a one-shot HTTP server on `localhost:33418` for the duration of
the auth flow. When the browser redirects to
`http://localhost:33418/callback?code=...&state=...`, the server captures
the code and shuts down. Total lifetime: usually under 30 seconds.

**Port choice (33418).** Arbitrary but high (above 32768) to avoid collision
with other dev tools (3000 = Node, 5000 = Flask, 8000 = Django/Streamlit,
8080 = generic HTTP). The exact port is baked into our registration's
`redirect_uris`, so changing it later requires re-registering (delete
`.cache/robinhood_oauth/client_info.json` first).

**Security note.** `http://localhost` is special-cased as secure by
browsers and OAuth specs precisely because the loopback interface is not
network-reachable. Robinhood accepts the `http://` scheme on `localhost`
during registration despite generally requiring HTTPS, again per RFC 8252.

### Bugs we hit and how we fixed them

1. **Browser callback "didn't come back" on first run.**
   Symptom: user clicked Authorize, terminal sat silent, eventually timed
   out with "OAuth callback did not return an authorization code".

   Root cause: our callback handler had `log_message` silenced for
   cleanliness, so we couldn't tell whether the GET ever arrived.

   Fix: re-enabled stdout logging in the handler. Re-running showed the
   redirect *was* arriving correctly — the auth itself succeeded; the
   silence just made the next bug invisible.

2. **`pydantic_core.ValidationError: Input should be a valid dictionary
   ... input_value='data'`** when calling `get_accounts`.

   Root cause: response shape assumption was wrong. We assumed
   `{"accounts": [...]}` (top-level pluralized key); Robinhood actually
   returns `{"data": {"accounts": [...]}, "guide": "..."}` — nested two
   deep, with an extra `guide` string the server intends the agent to
   read for behavioral hints.

   Fix: rewrote `_unwrap_list` / `_unwrap_dict` to peel both `data` and
   the inner key. Switched the spike to dump raw payloads first
   (`call_raw`), then re-typed methods after seeing the actual shapes.

3. **Pydantic rejected unmodeled fields** — `affiliate`, `deactivated`,
   `permanently_deactivated`, `previous_close_date`, `venue_bid_time`,
   etc., were on the wire but not in our models.

   Root cause: design doc captured the documented schema; the live
   server returned more fields.

   Fix: added `model_config = {"extra": "ignore"}` to all brokerage
   models. The server is allowed to add fields without breaking us;
   we add explicit fields only for ones we actually use.

4. **`get_equity_quotes` returns nested `quote` and `close` objects.**

   Root cause: each result item is `{"quote": {...}, "close": {...}}`,
   not a flat quote.

   Fix: `get_quotes` now extracts `r["quote"]` from each item before
   `Quote.model_validate`. The `close` object (official prior-session
   settled close) is dropped for now — we can surface it if needed.

5. **"Session termination failed: 400"** at script exit.

   Root cause: MCP SDK sends an optional session-termination message;
   Robinhood's server doesn't implement it and returns 400.

   Fix: nothing — message is harmless. All real work completes before
   it. Documented in §4 so future-you doesn't chase it.

---

## 1. OAuth discovery

`GET https://agent.robinhood.com/.well-known/oauth-authorization-server`
returns:

```json
{
  "authorization_endpoint": "https://robinhood.com/oauth",
  "code_challenge_methods_supported": ["S256"],
  "grant_types_supported": ["authorization_code", "refresh_token"],
  "issuer": "https://agent.robinhood.com/mcp/trading",
  "registration_endpoint": "https://agent.robinhood.com/oauth/trading/register",
  "response_types_supported": ["code"],
  "scopes_supported": ["internal"],
  "token_endpoint": "https://api.robinhood.com/oauth2/token/",
  "token_endpoint_auth_methods_supported": ["none"]
}
```

Key implications:
- **`token_endpoint_auth_methods_supported: ["none"]`** — public client, no
  client secret. PKCE protects the auth code instead.
- **Dynamic Client Registration** is required (no static client ID). First
  run POSTs metadata to `/oauth/trading/register`, gets back a `client_id`,
  and we persist it.
- **Scope is fixed at `"internal"`** — there's only one scope.

`GET https://agent.robinhood.com/.well-known/oauth-protected-resource`:

```json
{
  "authorization_servers": ["https://agent.robinhood.com/mcp/trading"],
  "bearer_methods_supported": ["header"],
  "resource": "https://agent.robinhood.com/mcp/trading",
  "scopes_supported": ["internal"]
}
```

So tokens go in `Authorization: Bearer <token>` headers — standard.

---

## 2. Client registration metadata

Sent to `/oauth/trading/register` on first run. Required fields the server
actually cares about:

```python
OAuthClientMetadata(
    redirect_uris=["http://localhost:33418/callback"],
    token_endpoint_auth_method="none",      # must match discovery
    grant_types=["authorization_code", "refresh_token"],
    response_types=["code"],
    scope="internal",
    client_name="stock-ai-agents-poc",
)
```

Response includes the assigned `client_id` (e.g.
`CLIENT_ID_REDACTED`) plus the metadata echo. The MCP
SDK's `OAuthClientProvider` calls `TokenStorage.set_client_info` with the
result; we persist to `.cache/robinhood_oauth/client_info.json`.

**Re-using a registration is critical** — register twice and you'll have
two client IDs, only one of which the user has authorized. Always read
`client_info.json` from disk before registering.

---

## 3. Browser flow

1. SDK constructs the auth URL:
   ```
   https://robinhood.com/oauth
     ?response_type=code
     &client_id=<from registration>
     &redirect_uri=http%3A%2F%2Flocalhost%3A33418%2Fcallback
     &state=<random>
     &code_challenge=<S256(verifier)>
     &code_challenge_method=S256
     &resource=https%3A%2F%2Fagent.robinhood.com%2Fmcp%2Ftrading
     &scope=internal
   ```
2. Our `redirect_handler` opens this URL in the user's browser.
3. User logs in to Robinhood and clicks "Authorize".
4. Robinhood redirects to `http://localhost:33418/callback?code=…&state=…`.
5. Our local one-shot HTTPServer captures the `code` (and `state`).
6. SDK exchanges code+verifier at `https://api.robinhood.com/oauth2/token/`
   for an access token + refresh token.
7. SDK calls `TokenStorage.set_tokens` — we persist to
   `.cache/robinhood_oauth/tokens.json`.

**Lifetimes** (empirical, not documented):
- Access token: ~hours; refreshed silently by SDK on 401.
- Refresh token: long-lived (weeks–months). Re-prompts only when revoked,
  expired, or password reset.

---

## 4. JSON-RPC over Streamable HTTP

After auth, every MCP method (`initialize`, `tools/list`, `tools/call`) is
a single POST to the server URL with:

```
POST https://agent.robinhood.com/mcp/trading
Authorization: Bearer <access_token>
Accept: application/json, text/event-stream
Content-Type: application/json

{"jsonrpc":"2.0","id":1,"method":"…","params":{…}}
```

The SDK does this for us — we never touch wire-level JSON-RPC.

### Side note: "Session termination failed: 400"

Harmless. At connection close the SDK tries to terminate the MCP session;
Robinhood's server returns 400 because it doesn't implement the optional
session-termination handshake. All real work has already completed by then.

---

## 5. Tool surface (23 tools)

Confirmed live:

```
add_option_to_watchlist     get_equity_quotes        place_equity_order
add_to_watchlist            get_equity_tradability   remove_from_watchlist
cancel_equity_order         get_option_watchlist     remove_option_from_watchlist
create_watchlist            get_popular_watchlists   review_equity_order
follow_watchlist            get_portfolio            search
get_accounts                get_watchlist_items      unfollow_watchlist
get_equity_historicals      get_watchlists           update_watchlist
get_equity_orders
get_equity_positions
```

Watchlist tools (12 of 23) are out of POC scope per `TECHNICAL_DESIGN.md §4.5`.

---

## 6. Response shapes (observed)

Robinhood's MCP wraps every response with `data` and a top-level `guide`
string the server intends agents to read. Our client strips both.

### `get_accounts`

```json
{
  "data": {
    "accounts": [
      {
        "account_number": "ACCOUNT_NUMBER_REDACTED",
        "rhs_account_number": "RHS_ACCOUNT_NUMBER_REDACTED",
        "type": "margin",
        "brokerage_account_type": "individual",
        "is_default": true,
        "agentic_allowed": false,
        "option_level": "option_level_3",
        "management_type": "self_directed",
        "affiliate": "rhf",
        "state": "active",
        "deactivated": false,
        "permanently_deactivated": false
      },
      {
        "account_number": "ACCOUNT_NUMBER_REDACTED",
        "rhs_account_number": "RHS_ACCOUNT_NUMBER_REDACTED",
        "type": "cash",
        "brokerage_account_type": "individual",
        "nickname": "Agentic",
        "is_default": false,
        "agentic_allowed": true,
        "option_level": "",
        "management_type": "self_directed",
        "affiliate": "rhf",
        "state": "active",
        "deactivated": false,
        "permanently_deactivated": false
      }
    ]
  },
  "guide": "Sort the list deterministically when presenting…"
}
```

**`guide` says** (paraphrased): mask all but last 4 of `account_number` in
UI, but pass full unmasked value to other tools. Default account first,
agentic next, IRAs last. For crypto-backed flows pass `rhs_account_number`;
for everything else pass `account_number`.

### `get_equity_quotes`

```json
{
  "data": {
    "results": [
      {
        "quote": {
          "symbol": "AAPL",
          "last_trade_price": "REDACTED",
          "venue_last_trade_time": "2026-06-12T19:59:59.999287473Z",
          "last_non_reg_trade_price": "REDACTED",
          "venue_last_non_reg_trade_time": "2026-06-12T23:59:50.916525511Z",
          "adjusted_previous_close": "REDACTED",
          "previous_close": "REDACTED",
          "previous_close_date": "2026-06-11",
          "bid_price": "REDACTED",
          "venue_bid_time": "2026-06-13T00:00:00.327549252Z",
          "ask_price": "REDACTED",
          "venue_ask_time": "2026-06-13T00:00:00.327549252Z",
          "has_traded": true,
          "state": "active"
        },
        "close": {
          "symbol": "AAPL",
          "date": "2026-06-11",
          "price": "REDACTED",
          "interpolated": false,
          "source": "sip-list-exchange-close"
        }
      }
    ]
  },
  "guide": "Each entry in results pairs the live quote…"
}
```

**`guide` says**: Use the most recent of `last_trade_price` /
`last_non_reg_trade_price`. Drop bid/ask when zero. Surface
`has_traded=false` or non-active `state` before quoting. Daily change uses
`adjusted_previous_close`. "Yesterday's close" uses `close.price` (the
official settled close), falling back to `quote.previous_close` if that's
missing.

### `get_portfolio`, `get_equity_positions`

Same wrapper pattern: `{"data": {<inner-key>: ...}, "guide": "..."}`.
`get_portfolio` returns a single dict; `get_equity_positions` returns an
array. Both have `extra="ignore"` on our Pydantic models so future-added
fields don't break parsing.

---

## 7. Field gotchas (the ones the design called out, now confirmed)

- **`Decimal` for all money** — Robinhood serializes monetary values as
  strings (`"REDACTED"`). Pydantic v2 coerces them to `Decimal`
  cleanly; never `float`.
- **`shares_available_for_sells` ≠ `quantity`** — pending sells, options
  exercises, and asset transfers reduce sellable shares. Always use
  `shares_available_for_sells` when validating sells.
- **`average_buy_price` may be `None`** during reconciliation. Modeled as
  `Decimal | None`.
- **`bid_price` / `ask_price` of 0** means no live book — drop from UI.
- **`agentic_allowed` is the write gate.** Reads work on any account;
  writes (Phase 4) must target an account with `agentic_allowed=true`.

---

## 8. Files & locations

| File | Purpose | In git? |
|---|---|---|
| `src/data/brokerage/robinhood_mcp.py` | `RobinhoodMCPClient` + `FileTokenStorage` | ✅ |
| `src/data/brokerage/base.py` | Pydantic models + `BrokerageClient` Protocol | ✅ |
| `scripts/connect_robinhood.py` | First-run OAuth + smoke test | ✅ |
| `.cache/robinhood_oauth/client_info.json` | Registration result | ❌ (gitignored) |
| `.cache/robinhood_oauth/tokens.json` | Access + refresh tokens | ❌ (gitignored) |

`.cache/` is in `.gitignore` so tokens never leave the machine. **Do not
commit the contents of `.cache/robinhood_oauth/` under any circumstance.**

---

## 9. Re-authenticating

If the refresh token is rejected (revoked, expired, password reset), the
SDK will surface an auth error and re-prompt the browser flow. To force a
fresh login:

```bash
rm -rf .cache/robinhood_oauth/
.venv/bin/python -m scripts.connect_robinhood
```

Removing `tokens.json` only forces a re-login but keeps the registered
client. Removing the whole directory forces full re-registration.

---

## 10. Local callback server

Bound to `localhost:33418`. If the port is already in use, change
`DEFAULT_CALLBACK_PORT` in `robinhood_mcp.py` and re-register
(redirect URI is fixed at registration time, so a port change requires
deleting `client_info.json` and starting over).

The server logs every incoming request to stdout — useful when debugging
"why didn't my browser redirect come back?" issues. We saw two requests
in normal flow:

```
[callback-server] GET /callback?code=…&state=…
[callback-server] GET /favicon.ico
```

The favicon hit is the browser auto-fetching a favicon for the success
page; our handler returns 404 for it.

---

## 11. Lambda deployment

Robinhood MCP's initial OAuth flow needs an interactive browser, which
is incompatible with stateless Lambda. The deployment plan (copy
local tokens to Secrets Manager) and the eventual E*TRADE migration
live in **`TECHNICAL_DESIGN.md` §11** — that's the right place for
deployment concerns; this document covers how the integration *works*,
not how it deploys.

Two facts from this integration that the deployment plan depends on:

- Access token lifetime: **`expires_in: REDACTED` (lifetime redacted)**.
  Translates to ~3 refreshes/month — important context for whether
  refresh-write races matter.
- The SDK needs **both** persisted artifacts: `tokens.json` *and*
  `client_info.json` (registered `client_id` from Dynamic Client
  Registration; there is no static client ID — see §0).

---

## 12. Run summary (smoke test, 2026-06-14)

```
Accounts (2):
  · ****0000  individual  type=margin  default=True  state=active
  · ****1111  Agentic     type=cash    default=False state=active [agentic]

Portfolio:
  total_value:  REDACTED
  equity_value: REDACTED
  cash:         REDACTED
  buying_power: REDACTED

Positions:
  REDACTED

Quote example:
  symbol: EXAMPLE_SYMBOL
  last_trade_price: REDACTED
  prev_close: REDACTED
  bid/ask: REDACTED/REDACTED
  state: active
  has_traded: True
```
