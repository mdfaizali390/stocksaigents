# Multi-Agent Stock Portfolio Advisor — Technical Design

**Status:** POC / Draft v0.3
**Owner:** mmohfai

## 1. Goal

Build a multi-agent system that **advises** on stock trades. The system:

- Accepts natural-language queries about stocks, the portfolio, or specific trades.
- Routes the query to the right specialist agents (run in parallel).
- Synthesizes their outputs into a single recommendation.
- Presents both the **individual agent reports** and the **synthesized recommendation** to the user.
- **Never executes a trade automatically.** The user clicks "Execute" to place an order via Robinhood. In `dry_run` mode, execution is disabled entirely.

The system is an **advisor**, not an autotrader.

## 2. Core Design Principles

1. **Hybrid LLM + deterministic code.** LLMs handle reasoning, summarization, and judgment. Python code handles math, rules, and money. The boundary is enforced by data contracts, not prompts.
2. **Trading Constitution.** An LLM interviews the user and produces a JSON policy. Code enforces it. The user can read, edit, and re-approve the policy at any time.
3. **Structured agent outputs.** Every agent returns the same Pydantic shape. The Portfolio Manager never re-parses prose.
4. **Parallel agents, single synthesizer.** All specialist agents run concurrently. A final PM agent synthesizes after the barrier.
5. **Advisor, not executor.** Hard rule violations (Compliance, Risk) are surfaced as **blocking warnings** in the UI. The user is the final decision-maker.
6. **Dry-run first.** Default mode disables real trading. Live mode requires explicit toggle.

## 3. Architecture

```
                          ┌─────────────────┐
   User Query ─────────▶  │  Intent Router  │  (LLM, structured output)
                          └────────┬────────┘
                                   │  {intent, ticker, action, agents_to_run}
                                   ▼
              ┌──────────────────────────────────────────┐
              │            Orchestrator                  │
              │  • Loads portfolio + market data         │
              │  • Loads Trading Constitution            │
              │  • Dispatches selected agents in parallel│
              └────────────────────┬─────────────────────┘
                                   │
   ┌──────────┬──────────┬─────────┼─────────┬──────────┬──────────┐
   ▼          ▼          ▼         ▼         ▼          ▼          ▼
┌────────┐┌────────┐┌─────────┐┌────────┐┌──────────┐┌──────────┐
│Research││ Quant  ││Trending ││  Risk  ││Behavioral││Compliance│  (parallel)
└───┬────┘└───┬────┘└────┬────┘└───┬────┘└────┬─────┘└────┬─────┘
    │         │          │         │          │           │
    └─────────┴──────────┴─────────┴──────────┴───────────┘
                              │
                              ▼  (barrier — wait for all)
                   ┌──────────────────────┐
                   │ Portfolio Manager    │  (LLM synthesis)
                   │ Reads all reports    │
                   │ Produces recommendation
                   └──────────┬───────────┘
                              │
                              ▼
                   ┌──────────────────────┐
                   │  UI Presentation     │
                   │  • All agent reports │
                   │  • PM recommendation │
                   │  • Block warnings    │
                   │  • [Execute] button  │  ← user decides
                   └──────────────────────┘
```

## 4. Components

### 4.1 Intent Router

**Purpose:** Decide which agents to run for a given query.

**Implementation:** Single LLM call with structured output (Pydantic schema).

**Output:**

```python
class Intent(BaseModel):
    intent_type: Literal["trade_decision", "market_info", "portfolio_analysis", "policy_question"]
    ticker: str | None
    action: Literal["buy", "sell"] | None
    agents_to_run: list[Literal["research", "quant", "trending", "risk", "behavioral", "compliance"]]
    rationale: str  # why this dispatch
```

**Dispatch table (defaults — router can override):**

| Intent | Example | Agents |
|---|---|---|
| `market_info` | "Hot trending stocks?" | Trending, Research, Quant |
| `trade_decision` (buy) | "Should I buy NVDA?" | Research, Quant, Risk, Behavioral, Compliance |
| `trade_decision` (sell) | "Should I sell AMZN?" | Research, Quant, Risk, Behavioral, Compliance |
| `portfolio_analysis` | "Am I too concentrated?" | Risk, Behavioral |
| `policy_question` | "What's my max trade size?" | (none — answered from Constitution directly) |

**Rule:** Any `trade_decision` — buy *or* sell — runs Risk and Compliance. No exceptions.

### 4.2 Specialist Agents

All agents inherit from `BaseAgent` and return an `AgentReport` (see §5.1).

#### Research Agent — LLM-heavy
- **Input:** ticker, recent timeframe.
- **Data sources (all FREE Finnhub):**
  - Finnhub Company News — primary news feed.
  - Finnhub Earnings Calendar — upcoming earnings, surprises, estimates.
  - Finnhub SEC Filings — filing metadata (form type, date, link). Note: metadata only; LLM does not read the raw filing text in the POC. The user can follow the link for details. Direct EDGAR integration is out of scope (see §11).
- **Process:** Fetch raw data → summarize via LLM → produce signal + reasoning.
- **Signal:** `BUY` | `HOLD` | `SELL` | `INFO`.

#### Quant Agent — code-heavy, LLM explains
- **Input:** ticker, lookback window.
- **Data sources:** `yfinance` (price history, volume).
- **Process:** Compute RSI, SMA-50/200, volatility, momentum, volume trend → pass numerical results to LLM for plain-English explanation.
- **Signal:** `BUY` | `HOLD` | `SELL` | `INFO`.

#### Trending Agent — code-heavy scoring + LLM explains
- **Purpose:** Rank "hot" stocks for `market_info` queries.
- **Input:** Optional candidate universe (defaults to yfinance daily volume movers, augmented by Stocktwits trending).
- **Data sources:**
  - **Stocktwits** (`api.stocktwits.com/api/2/`) — trending symbols + per-ticker `trending_score` and `watchlist_count`. No auth, no key.
  - **Finnhub Company News** — news volume vs. trailing 30-day baseline.
  - **Finnhub Analyst Recommendations** — net upgrades/downgrades over the last 30 days.
  - **yfinance** — volume spike (today vs. 20-day average) and 5-day price return.
- **Process:** Compute a `Trending Score` (see §4.7) for each candidate, rank, and pass top-N + score breakdown to LLM for per-ticker rationale.
- **Signal:** `INFO` (returns a ranked list, not a buy/sell).
- **Notes:**
  - Stocktwits is used for *attention*, not *direction*. The platform's own `trending_score` already aggregates post volume, watcher growth, and engagement — we use it directly rather than re-deriving.
  - Filter out crypto and non-US tickers using `instrument_class` / `region` fields.
  - Stocktwits returns ticker symbols already (no Reddit-style regex extraction needed). Cross-check against Robinhood `search` for tradability before scoring.
  - `/api/2/` endpoints are undocumented — Stocktwits could rate-limit or break this path. See §10 row 8.

#### Risk Agent — rule-heavy, LLM explains
- **Input:** ticker, proposed action, portfolio snapshot, Constitution.
- **Process:** Check concentration, sector exposure, cash %, position size delta → flag violations.
- **Signal:** `WARNING` | `BLOCK` | `INFO`.
- **`blocking=true`** when a Constitution hard limit is violated.

#### Behavioral Agent — LLM-heavy, reads trade history
- **Input:** Robinhood trade history (last N trades), proposed action.
- **Process:** Send trade log + current proposal to LLM. Detect patterns: FOMO (chasing recent winners), revenge trading (buying back after loss), overtrading (frequency spike), panic selling (selling on dips).
- **Signal:** `WARNING` | `INFO`.
- **State:** No persistent memory needed — the trade log *is* the state.

#### Compliance Agent — pure rule engine
- **Input:** proposed action, Constitution.
- **Process:** Hard checks against the Constitution. No LLM in the decision path.
  - Asset class allowed? (options/margin/crypto/etc.)
  - Order type allowed? (market vs. limit)
  - Trade size within `max_single_trade_pct`?
  - Human approval required?
- **Signal:** `BLOCK` | `INFO`.
- **Note:** LLM may be used *post-hoc* to phrase the block reason in plain English, but never to *decide* it.

#### Portfolio Manager Agent — LLM synthesizer
- **Input:** All specialist reports + original query + portfolio context.
- **Process:** Read all reports, weigh signals, produce a final recommendation.
- **Output:** `Recommendation` with:
  - Action: `BUY` / `SELL` / `HOLD` / `NO_ACTION` / `BLOCKED`
  - Confidence
  - One-paragraph summary
  - Citations to specific agent reports
- **Rule:** If any input report has `blocking=true`, the PM's recommendation **must** be `BLOCKED` and surface the blocking reason. This is enforced **structurally** in the orchestrator (see §6.3) — the PM doesn't get to decide whether to honor a block.

### 4.3 Trading Constitution

A JSON document produced by an **LLM-driven interview** of the user.

**Interview flow:**
1. User starts the interview (one-time, or anytime to re-run).
2. LLM asks 8–12 questions about risk tolerance, time horizon, allowed asset classes, etc.
3. LLM proposes a Constitution.
4. User reviews and approves (or edits values).
5. Saved to `constitution/policy.json`.

**Schema (v1):**

```json
{
  "version": "1.0",
  "created_at": "2026-06-13T10:00:00Z",
  "user_profile": {
    "risk_profile": "moderate",
    "time_horizon": "long_term",
    "experience_level": "intermediate"
  },
  "position_limits": {
    "max_single_trade_pct": 1.0,
    "max_single_stock_pct": 15.0,
    "max_sector_pct": 30.0,
    "min_cash_pct": 5.0
  },
  "allowed_asset_classes": ["stocks", "etfs"],
  "blocked_asset_classes": ["options", "margin", "crypto"],
  "allowed_order_types": ["limit"],
  "blocked_order_types": ["market", "stop_market"],
  "approval": {
    "human_approval_required": true,
    "auto_execute_threshold_pct": 0.0
  },
  "behavioral_guards": {
    "cooldown_after_loss_minutes": 60,
    "max_trades_per_day": 5
  }
}
```

**Critical:** Code reads this file at every decision. The LLM only *helped author it once*. It does not get a vote at decision time.

### 4.4 Orchestrator

Central async coordinator. Pseudocode:

```python
async def handle_query(query: str, mode: Literal["dry_run", "live"]) -> Decision:
    intent = await intent_router.classify(query)
    portfolio = await robinhood.get_portfolio()
    market_ctx = await market.get_context(intent.ticker)
    constitution = load_constitution()

    agent_inputs = AgentContext(intent, portfolio, market_ctx, constitution)

    # Parallel fan-out
    reports = await asyncio.gather(*[
        AGENT_REGISTRY[name].run(agent_inputs)
        for name in intent.agents_to_run
    ])

    # Structural block check (§6.3)
    blocking_reports = [r for r in reports if r.blocking]

    # PM always runs, but sees blocking reports flagged
    recommendation = await pm_agent.synthesize(reports, query, portfolio)

    return Decision(
        intent=intent,
        reports=reports,
        recommendation=recommendation,
        blocking_reports=blocking_reports,
        mode=mode,
    )
```

### 4.5 Brokerage Integration

**Decision:** Use **Robinhood Agentic Trading MCP** (`https://agent.robinhood.com/mcp/trading`) for the POC. Migrate to **E*TRADE OAuth API** when deploying to AWS Lambda (Robinhood MCP is interactive-auth, Lambda-incompatible). Wrapped behind a `BrokerageClient` Protocol so the swap is one file (`src/data/brokerage/`).

**Two-account read/write split (brokerage-enforced safety):**

| Operation | Account used | Reason |
|---|---|---|
| **Read** (portfolio, positions, orders, quotes) | Main account (default) | Real data — agents reason about your actual situation |
| **Write** (place_equity_order) | Agentic account (`agentic_allowed=true`) | Robinhood **brokerage-locks** writes here — main account is structurally untouchable by code |

The Agentic account is funded with a small POC budget. Even with bugs, blast radius is bounded by Robinhood's brokerage-level wall, not just our software checks.

**MCP tool surface (23 tools, equities-only, no options/crypto/margin):**

| Tool | Used by |
|---|---|
| `get_accounts` | Orchestrator init — discover accounts, identify agentic account |
| `get_portfolio` | Risk Agent — total/equity/cash, buying power |
| `get_equity_positions` | Risk Agent, Behavioral Agent — holdings per ticker |
| `get_equity_orders` | Behavioral Agent — trade history (filter `created_at_gte` to last ~60 days) |
| `get_equity_quotes` | Quant Agent — real-time price + previous close (replaces yfinance for prices) |
| `get_equity_historicals` | Quant Agent — historical price series for indicators |
| `get_equity_tradability` | Compliance Agent — pre-trade check (halted/restricted) |
| `review_equity_order` | Live mode pre-flight — preview before placing |
| `place_equity_order` | Execute (live mode only) — submits the order |
| `cancel_equity_order` | UI affordance — cancel pending |
| `search` | Intent Router — resolve "Amazon" → "AMZN" |

**Out of POC scope:** the 12 watchlist tools (`get_watchlists`, `create_watchlist`, etc.).

**Wrapper module:** `src/data/brokerage/robinhood_mcp.py`. Single `RobinhoodMCPClient` class implementing `BrokerageClient` Protocol.

**Critical implementation rules (from MCP discovery):**

- **Use `Decimal` for all monetary values, never `float`.** MCP returns strings (`"210902.2626"`); Pydantic must convert to `Decimal`.
- **Use `shares_available_for_sells`, not `quantity`,** when validating sell orders. Pending sells, options exercises, and asset transfers reduce sellable shares.
- **Filter trade history by `placed_agent="user"`** in Behavioral Agent. `recurring` and `drip` orders aren't behavioral signals.
- **Mask account numbers** in UI (`••••6664`). Pass full unmasked value to MCP tools.
- **Verify `quote.has_traded == true` and `quote.state == "active"`** before quoting a price.
- **Use `quote.last_trade_price` vs. `quote.last_non_reg_trade_price`** — pick the more recent timestamp.
- **`average_buy_price` may be `None`** for positions still reconciling — handle gracefully.
- **`get_equity_orders` paginates via `cursor`.** Always filter by `created_at_gte` to bound output size.

### 4.6 UI — TradeDesk (Streamlit)

Streamlit multi-page app, branded **"TradeDesk — AI Agents"**.

**Pages**
- **Home (`src/ui/streamlit_app.py`)** — query advisor.
  - **Query box** + Analyze button.
  - **Mode toggle:** `Dry Run` / `Live` (defaults to `Dry Run`).
  - **Sidebar:** active Constitution summary, source label
    (`policy.json` / `policy.example.json` / built-in stub), 5 sample
    queries that one-click prefill the input.
  - **Output panel** (after submit):
    - **Intent strip:** intent type, ticker, action, dispatched agents.
    - **Block banner** (red, prominent) if any agent returned `blocking=true`.
    - **PM recommendation** card, color-coded by action.
    - **Execute button** — disabled in `Dry Run`, disabled if any block,
      disabled if action ∉ {BUY, SELL}.
    - **Agent cards** — one expander per agent. Each card surfaces:
      signal badge, summary, full reasoning, **per-agent numeric panel**
      (RSI/SMA for Quant, concentration table for Risk, component score
      table for Trending, etc.), and a nested "Evidence trail" expander
      with the raw `Evidence[]` items.
  - **Failure handling:** uncaught pipeline exceptions are captured,
    `ExceptionGroup` (asyncio TaskGroup) is unwrapped to its leaf
    errors, and the full traceback is rendered in a collapsed expander
    plus echoed to the server log.

- **Constitution (`src/ui/pages/1_Constitution.py`)** — policy interview + editor.
  - **Run interview tab** — LLM-driven conversational flow (6–12 short
    questions, hard cap at 12). Uses a turn-counter widget-key pattern
    to clear the answer input between turns.
  - **Active Constitution tab** — read the current `policy.json` (or
    fall back to `policy.example.json`) and edit any field directly.
    Saves write to `src/constitution/policy.json` (gitignored). The
    same editable widget is reused for draft review and active-policy
    editing.

**Local launch.** `./scripts/run_ui.sh` (sets `PYTHONPATH` to repo root
so `from src.…` imports resolve under Streamlit's launcher) or
`PYTHONPATH=. .venv/bin/streamlit run src/ui/streamlit_app.py`.

**Cloud deploy — Streamlit Community Cloud.** The intended demo host.

| Aspect | Detail |
|---|---|
| Pricing | Free tier covers a single private app, 1 GB RAM, viewable by up to 3 whitelisted Google accounts. |
| Source | github.com only — no GitLab/Bitbucket. Repo can be private. |
| Auth on app | Built-in private-app allow-list (3 emails). No password gate, no API tokens. Sufficient for personal-use POC. |
| Cold start | ~15–30s wake-up after ~7 days idle, then a further ~30–60s on a cold cache (Trending fans out 30 tickers through Finnhub at 55/min). |
| Disk | Ephemeral per-container. `.cache/` (diskcache, Robinhood OAuth artifacts) does NOT persist across restarts. |

**Secrets to paste into Streamlit Cloud's secrets panel:**

```
ANTHROPIC_API_KEY    # Claude API
FINNHUB_API_KEY      # market data
[robinhood_mcp]      # nested table for Robinhood OAuth artifacts
client_info = """{...contents of .cache/robinhood_oauth/client_info.json...}"""
tokens      = """{...contents of .cache/robinhood_oauth/tokens.json...}"""
```

**Code change required for cloud deploy.** `FileTokenStorage` currently
reads/writes `.cache/robinhood_oauth/`. For cloud, add a sibling
`SecretsTokenStorage` that loads from `st.secrets["robinhood_mcp"]` and
writes refreshed tokens back via the Streamlit Cloud API (or accepts
that refreshes are lost on container restart). Sketch lives in
[docs/ROBINHOOD_MCP_INTEGRATION.md](docs/ROBINHOOD_MCP_INTEGRATION.md);
not yet implemented. **Without this, every Robinhood call on cloud
fails** — Risk and Behavioral go dark, trade-decision queries can't
fetch quotes.

**Token rotation operations.** Robinhood's access token TTL is
`expires_in: 816744` (~9 days 11 hours). Refresh-token TTL is
unpublished. When the refresh token eventually fails on cloud, the
manual recovery is: re-run `scripts/connect_robinhood.py` locally,
then `aws secretsmanager put-secret-value` (AWS) or paste the new
`tokens.json` into Streamlit's secrets UI (Streamlit Cloud).

**Security posture.** Robinhood tokens stored in Streamlit Cloud's
backend. Reads access the user's full main portfolio; writes are
brokerage-locked to the Agentic account. For a personal POC this is
acceptable. For multi-user / production use, neither Streamlit Cloud
nor copy-tokens-to-secrets scales — see §11.

**What does NOT work on Streamlit Cloud as-is:**
- Robinhood reads (until `SecretsTokenStorage` lands).
- Phase 4 live execution — same auth blocker.
- Persistent diskcache across restarts (mitigation: aggressive TTLs
  already tuned; first query slow, subsequent cached).

### 4.7 Trending Score (Market Scanner)

The Trending Agent uses a deterministic scoring function. Each component is normalized to a percentile rank (0–100) within the day's candidate universe before weighting — so component scales (counts, %, sentiment) don't distort the result.

**Per-ticker components:**

```python
p_stocktwits_trending   = pct_rank(stocktwits.trending_score)         # short-term attention
p_stocktwits_watchers   = pct_rank(stocktwits.watchlist_count)        # long-term attention
p_news_volume           = pct_rank(news_count_24h_zscore_vs_30d_baseline)
p_volume_spike          = pct_rank(today_volume / avg_20d_volume)
p_price_momentum        = pct_rank(5d_return)
p_analyst_signal        = pct_rank(net_upgrades_last_30d)
```

**Weighted score (0–100):**

```python
Trending Score =
    0.20 * p_stocktwits_trending
  + 0.10 * p_stocktwits_watchers
  + 0.20 * p_news_volume
  + 0.20 * p_volume_spike
  + 0.20 * p_price_momentum
  + 0.10 * p_analyst_signal
```

Roughly: 50% objective price/volume/news, 30% Stocktwits, 20% other. Social attention gets weight without dominating.

**Note on `trending_score`:** Stocktwits returns this already pre-computed
on the trending endpoint. For tickers found in our universe but not in
Stocktwits' trending list, we fall back to per-ticker stream calls and
compute message-volume z-score ourselves; details in `market_scanner.py`.

**Output object:**

```python
class TrendingTicker(BaseModel):
    ticker: str
    score: float                    # 0-100
    components: dict[str, float]    # individual percentile ranks
    headline_evidence: list[str]    # top 1-2 news titles, top 1-2 Stocktwits posts
```

**Candidate universe:**
- Default: top 50 by yfinance daily volume movers (free, fast).
- The scanner does not score "all stocks" — that would burn rate limit on tickers nobody cares about.

**Implementation placement:** The scoring logic lives in `src/data/market_scanner.py` as a tool. The Trending Agent calls it, then asks the LLM to add per-ticker rationale citing specific evidence. Keeping the math out of the agent prompt is intentional — it's deterministic and testable.

### 4.8 External Data Sources — Summary

| Agent | Sources | Cost |
|---|---|---|
| Research | Finnhub Company News, Finnhub Earnings Calendar, Finnhub SEC Filings | Free |
| Quant | yfinance | Free |
| Trending | Stocktwits trending API, Finnhub Company News, Finnhub Analyst Recommendations, yfinance | Free |
| Risk, Behavioral | Robinhood MCP (portfolio + trade history) | Free |
| Compliance | Constitution JSON only | — |

**Five external dependencies total:** Finnhub, Stocktwits, yfinance, Robinhood MCP, Anthropic API.

**Stocktwits access:** The undocumented public `/api/2/` endpoints
(`/trending/symbols.json`, `/streams/symbol/<TICKER>.json`). No auth, no
key, no account. Empirical rate limit ~200 req/hr per IP — well within
our scanner budget. Reddit (AsyncPRAW) was the original choice but
rejected: required a one-time OAuth-style app registration with `client_id`
+ `client_secret`, and Reddit has grown increasingly hostile to
programmatic access in 2024–2025. Stocktwits gives us cleaner symbol
attribution (tickers come pre-tagged on every post — no regex extraction
or denylist gymnastics) at the cost of using an undocumented endpoint.
Finnhub Social Sentiment was also evaluated but is paywalled on the
free tier (confirmed: HTTP 403).

**Caching:** Aggressive caching is mandatory given Finnhub's 60 calls/min
free tier and Stocktwits' unpublished limit. Use `diskcache` for the POC.
Suggested TTLs:
- Company news: 5 minutes
- Earnings calendar: 1 hour
- SEC filings: 1 hour
- Analyst recommendations: 1 hour
- Stocktwits trending list: 5 minutes
- Stocktwits per-ticker stream: 10 minutes
- yfinance price/volume: 5 minutes

### 4.9 API Keys & Configuration

See [API_KEYS.md](API_KEYS.md) for the full list of required credentials, sign-up instructions, the `Settings` loader pattern, and security rules.

**Summary:**
- All secrets live in `.env` (gitignored). `.env.example` is the committed template.
- Loaded via `pydantic-settings` so missing keys fail fast at startup.
- Robinhood MCP holds its own auth — no Robinhood credentials in `.env`.
- Lambda deployment swaps `.env` for AWS Secrets Manager (Phase 5+).

## 5. Data Contracts

### 5.1 AgentReport (universal)

```python
from pydantic import BaseModel
from typing import Literal

Signal = Literal["BUY", "HOLD", "SELL", "INFO", "WARNING", "BLOCK"]

class Evidence(BaseModel):
    source: str           # "yfinance", "finnhub", "constitution", "trade_log"
    description: str      # "RSI = 78 (overbought)"
    data: dict            # raw data point

class AgentReport(BaseModel):
    agent_name: str
    signal: Signal
    confidence: float                  # 0.0 - 1.0
    summary: str                       # 1-2 sentence headline
    reasoning: str                     # full explanation (LLM-generated)
    evidence: list[Evidence]
    blocking: bool = False             # hard rule violation
    blocking_reason: str | None = None
    metadata: dict = {}                # agent-specific fields
```

### 5.2 Recommendation (PM output)

```python
RecommendedAction = Literal["BUY", "SELL", "HOLD", "NO_ACTION", "BLOCKED"]

class Recommendation(BaseModel):
    action: RecommendedAction
    ticker: str | None
    quantity_suggestion: int | None    # shares, if action is BUY/SELL
    confidence: float
    summary: str                       # one paragraph
    citations: list[str]               # references to agent reports
    block_reasons: list[str] = []      # if action == BLOCKED
```

### 5.3 Decision (top-level response)

```python
class Decision(BaseModel):
    intent: Intent
    reports: list[AgentReport]
    recommendation: Recommendation
    blocking_reports: list[AgentReport]
    mode: Literal["dry_run", "live"]
    timestamp: datetime
```

### 5.4 Brokerage Models (from Robinhood MCP discovery)

All monetary values are `Decimal` (never `float`). MCP returns them as strings; Pydantic coerces.

```python
from decimal import Decimal
from datetime import datetime
from typing import Literal
from pydantic import BaseModel

class Account(BaseModel):
    account_number: str
    rhs_account_number: str
    type: Literal["margin", "cash"]
    brokerage_account_type: str
    nickname: str | None = None
    is_default: bool
    agentic_allowed: bool                   # ⭐ writes restricted to accounts where True
    option_level: str
    management_type: str
    state: str

class BuyingPower(BaseModel):
    buying_power: Decimal
    unleveraged_buying_power: Decimal
    display_currency: str

class Portfolio(BaseModel):
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
    symbol: str
    quantity: Decimal
    intraday_quantity: Decimal
    average_buy_price: Decimal | None       # may be None during reconciliation
    shares_available_for_sells: Decimal     # ⭐ use this, not quantity, for sell capacity
    shares_held_for_sells: Decimal
    type: Literal["long", "short"]

class Quote(BaseModel):
    symbol: str
    last_trade_price: Decimal
    last_non_reg_trade_price: Decimal
    venue_last_trade_time: datetime
    venue_last_non_reg_trade_time: datetime
    previous_close: Decimal
    adjusted_previous_close: Decimal
    bid_price: Decimal                      # drop from display if 0
    ask_price: Decimal                      # drop from display if 0
    has_traded: bool                        # ⭐ verify before quoting
    state: str                              # ⭐ verify == "active"

class OrderExecution(BaseModel):
    id: str
    price: Decimal
    quantity: Decimal
    timestamp: datetime
    fees: Decimal

OrderState = Literal[
    "new", "queued", "confirmed", "unconfirmed",
    "partially_filled", "filled", "cancelled",
    "rejected", "failed", "voided",
]

class Order(BaseModel):
    id: str
    instrument_id: str
    symbol: str
    side: Literal["buy", "sell"]
    type: Literal["limit", "market", "stop_loss", "stop_limit"]
    state: OrderState
    quantity: Decimal
    cumulative_quantity: Decimal
    price: Decimal
    stop_price: Decimal | None
    average_price: Decimal | None
    fees: Decimal
    dollar_based_amount: Decimal | None
    time_in_force: Literal["gfd", "gtc", "ioc", "fok"]
    market_hours: Literal["regular_hours", "extended_hours", "all_hours"]
    trigger: Literal["immediate", "stop"]
    placed_agent: Literal["user", "agentic", "recurring", "drip"]  # ⭐ Behavioral Agent filters on this
    created_at: datetime
    last_transaction_at: datetime
    executions: list[OrderExecution]
```

### 5.5 BrokerageClient Protocol

```python
from typing import Protocol

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
```

`OrderRequest` (input shape for `review_equity_order` / `place_equity_order`) will be discovered when we wire those tools — they aren't on the read-only path so we defer schema capture to Phase 4.

## 6. Workflows

### 6.1 "Give me some hot trending stocks?"

```
Router → intent: market_info, agents: [trending, research, quant]

  ├─ Trending: Market Scanner produces ranked list of 50 candidates.
  │            LLM picks top 5-10, generates per-ticker rationale citing
  │            Stocktwits posts, news headlines, volume/price moves.
  ├─ Research: For each top-N ticker, summarizes recent news / earnings /
  │            filings (if any).
  └─ Quant:    For each top-N ticker, computes technicals.

PM: merges, deduplicates, presents as a briefing — NOT a trade decision.
UI: list view; per-ticker cards expandable; no Execute button (no specific trade).
```

### 6.2 "Should I sell AMZN?"

```
Router → intent: trade_decision (sell), ticker: AMZN
         agents: [research, quant, risk, behavioral, compliance]

  ├─ Research:   recent AMZN news, earnings posture          → HOLD, conf 0.6
  ├─ Quant:      AMZN technicals (RSI, trend, vol)           → SELL, conf 0.7
  ├─ Risk:       impact of selling on portfolio mix          → INFO (no concentration issue)
  ├─ Behavioral: pattern check vs. last 20 trades            → WARNING (recent panic-sell pattern)
  └─ Compliance: order type, asset class, size               → INFO (allowed)

PM: synthesizes
    "Quant favors selling on technicals, but Behavioral flags this matches a
     panic-sell pattern from your recent history. Suggest: wait 24h or sell
     half. Confidence 0.55."

UI: shows all 5 reports + PM recommendation + Execute button (disabled in dry_run).
```

### 6.3 Blocking case: "Buy $50K of NVDA" (Constitution: max 1% per trade)

```
Router → intent: trade_decision (buy), ticker: NVDA
         agents: [research, quant, risk, behavioral, compliance]

  ├─ Research:   bullish ............................... BUY  conf 0.8
  ├─ Quant:      bullish ............................... BUY  conf 0.7
  ├─ Risk:       trade is 8% of portfolio, limit is 1%   BLOCK conf 1.0  blocking=true
  ├─ Behavioral: FOMO pattern detected ................. WARNING conf 0.6
  └─ Compliance: trade size violates max_single_trade_pct BLOCK conf 1.0  blocking=true

Orchestrator detects blocking_reports.length > 0
PM: produces Recommendation(action="BLOCKED", block_reasons=[...])
    PM is *not allowed* to override (enforced in code, not prompt — see §4.4)

UI: red banner: "TRADE BLOCKED — Compliance: trade size 8% exceeds 1% limit"
    All reports still shown for transparency.
    Execute button disabled.
    User may re-prompt with smaller size or update Constitution.
```

## 7. Tech Stack

| Layer | Choice | Reason |
|---|---|---|
| Language | Python 3.11+ | Async, ecosystem |
| Async runtime | `asyncio` | Native parallel agent dispatch |
| LLM | Anthropic Claude (Sonnet 4.5 default; Opus 4.x available for PM/Router upgrade) | Reasoning quality, tool use, structured output. Model IDs pinned in `src/llm/client.py`. |
| Data contracts | Pydantic v2 | Validation, structured LLM output |
| Market data | `yfinance` (prices/volume) + Finnhub (news, earnings, filings, analysts) | All free tier |
| Social data | Stocktwits (`api.stocktwits.com/api/2/`) | Free, no auth, undocumented public endpoints |
| Brokerage (POC) | Robinhood MCP (`agent.robinhood.com/mcp/trading`) | Discovered: 23 tools, equities-only, agentic-account-locked writes |
| Brokerage (Lambda) | E*TRADE OAuth API | Headless-friendly; deferred to Phase 5+ |
| Caching | `diskcache` | Necessary given Finnhub 60/min limit |
| Trade log | SQLite | Lightweight, queryable, persistent |
| UI | Streamlit (multi-page; "TradeDesk — AI Agents") | Fastest to build, good for demos. Streamlit Community Cloud is the target host. |
| Config | JSON files (Constitution) + `.env` (secrets) | Simple |
| Testing | `pytest` + recorded fixtures | Replayable |

## 8. Repository Structure

Tree below mirrors the repo as of Phase 3 completion. Items marked
`(Phase 4)` or `(Phase 5+)` are not yet implemented; everything else is
in-tree and tested.

```
s-ai-agents-poc/
├── README.md
├── TECHNICAL_DESIGN.md          (this file)
├── API_KEYS.md                  Required keys + Settings loader pattern
├── project-idea                 Original notes
├── pyproject.toml               pytest config (pythonpath, asyncio mode)
├── requirements.txt             Runtime deps
├── requirements-dev.txt         Dev deps (pytest)
├── .env                         Real secrets (gitignored)
├── .env.example                 Committed template, no values
├── docs/
│   └── ROBINHOOD_MCP_INTEGRATION.md   Auth flow, response shapes, gotchas
├── scripts/
│   ├── connect_robinhood.py     One-time OAuth + read smoke test
│   ├── run_router.py            Live Intent Router on sample queries
│   ├── run_quant.py             Quant agent end-to-end
│   ├── run_agents.py            Research + Trending + Risk + Behavioral
│   ├── run_query.py             Full pipeline (router → 5 agents → PM)
│   ├── scan_trending.py         Market Scanner top-N output
│   └── run_ui.sh                Launch Streamlit with PYTHONPATH set
├── src/
│   ├── config.py                pydantic-settings Settings + require_* helpers
│   ├── models.py                Evidence, AgentReport, Intent, Recommendation,
│   │                              Decision, TrendingTicker, ProposedTrade
│   ├── orchestrator.py          Async coordinator, structural block check
│   ├── agents/
│   │   ├── base.py              BaseAgent, AgentContext
│   │   ├── intent_router.py     LLM router + structural agent dispatch
│   │   ├── compliance.py        Pure rule engine, no LLM
│   │   ├── quant.py             Indicator panel → LLM explanation
│   │   ├── indicators.py        Pure RSI / SMA / volatility / momentum
│   │   ├── research.py          News + earnings + filings → LLM opinion
│   │   ├── trending.py          Market Scanner → LLM rationale
│   │   ├── risk.py              Trade-impact + portfolio-snapshot modes
│   │   ├── behavioral.py        Trade history → pattern detection
│   │   └── portfolio_manager.py LLM synthesizer (block-aware)
│   ├── llm/
│   │   └── client.py            AsyncAnthropic wrapper; tool-use → Pydantic
│   ├── constitution/
│   │   ├── schema.py            Pydantic model of policy.json
│   │   ├── policy.example.json  Committed default
│   │   ├── policy.json          Saved interview output (gitignored)
│   │   ├── interview.py         ConstitutionInterviewer (LLM)
│   │   └── loader.py            load_constitution / save_constitution
│   ├── data/
│   │   ├── cache.py             diskcache TTL decorator
│   │   ├── market.py            yfinance OHLCV + volume movers
│   │   ├── finnhub.py           News/earnings/filings/analysts/profile
│   │   │                          + sliding-window throttle (55/min) + retry
│   │   ├── market_scanner.py    Trending Score (deterministic)
│   │   ├── social/
│   │   │   └── stocktwits.py    Public /api/2/ wrapper
│   │   ├── brokerage/
│   │   │   ├── base.py          BrokerageClient Protocol + Pydantic models
│   │   │   └── robinhood_mcp.py POC implementation; reads done, writes (Phase 4)
│   │   └── trade_log.py         SQLite (Phase 3 follow-up — not yet built)
│   ├── execution/               (Phase 4 — not yet built)
│   │   ├── dry_run.py
│   │   └── live.py
│   └── ui/
│       ├── streamlit_app.py     Home page (advisor query)
│       └── pages/
│           └── 1_Constitution.py  Interview + editor
├── tests/
│   ├── conftest.py              Shared fixtures
│   ├── test_compliance.py       16 tests, every Constitution rule
│   ├── test_indicators.py       12 tests, RSI/SMA/vol/momentum edge cases
│   ├── test_intent_router.py    10 tests, structural enforcement
│   ├── test_market_scanner.py   14 tests, ranking + tie behavior
│   ├── test_orchestrator.py     7 tests, _apply_block_check
│   └── (LLM-driven flows exercised by scripts/, not unit-tested)
└── .cache/                      diskcache + Robinhood OAuth (gitignored)
    └── robinhood_oauth/
        ├── client_info.json     Dynamic Client Registration result
        └── tokens.json          Access + refresh tokens
```

**Test count: 65 (all passing as of Phase 3).**

## 9. Build Phases

### Phase 0 — Foundations (no LLM yet) ✅ Complete
- [x] Bootstrap repo: `pyproject.toml`, `.env.example`, `.gitignore`. (Used `python -m venv .venv` + pip — `uv` not adopted.)
- [x] Pydantic models: `AgentReport`, `Intent`, `Recommendation`, `Decision`, `TrendingTicker`, `ProposedTrade`, `Account`, `Portfolio`, `BuyingPower`, `Position`, `Quote`, `Order`, `OrderExecution`.
- [x] Constitution schema + sample `policy.example.json`.
- [x] `BrokerageClient` Protocol + `RobinhoodMCPClient` read methods (`get_accounts`, `get_portfolio`, `get_positions`, `get_orders`, `get_quotes`, `get_tradability`, `search`). Verified live against the real server.
- [~] `MockBrokerageClient` — **dropped**. Replaced by lightweight conftest fixtures; no separate mock class.
- [x] yfinance wrapper (`src/data/market.py`).
- [x] Finnhub wrapper (`src/data/finnhub.py`) with `diskcache`. Added a sliding-window throttle (55/min) + retry-on-429 with exponential backoff after observing the free tier hit 429 under fan-out load.
- [x] Stocktwits public API wrapper (`src/data/social/stocktwits.py`). Replaced original Reddit/AsyncPRAW plan after Reddit's hostility to programmatic access; Finnhub's Social Sentiment endpoint is paywalled (verified 403).
- [x] Market Scanner (`src/data/market_scanner.py`) — deterministic Trending Score, fail-soft per source.
- [x] Compliance Agent (pure rule engine, no LLM).
- [x] Unit tests: Compliance (16), Market Scanner (14).

**Spike (complete):** Robinhood MCP discovery + OAuth + read-path verification. See §4.5 and `docs/ROBINHOOD_MCP_INTEGRATION.md`.

### Phase 1 — Single-agent vertical slice ✅ Complete
- [x] LLM client wrapper with structured output (`src/llm/client.py`). Tool-use → Pydantic-validated return; supports prompt caching.
- [x] Quant Agent end-to-end: indicator panel → LLM explanation → `AgentReport`. Indicator math split into `src/agents/indicators.py` for unit testability.
- [x] CLI driver `scripts/run_quant.py`.
- [x] Unit tests: Indicators (12).

### Phase 2 — Full orchestration ✅ Complete
- [x] Intent Router (`src/agents/intent_router.py`) — LLM-driven, with **structural** rule that any `trade_decision` always runs Risk + Compliance regardless of LLM suggestion.
- [x] Research, Trending, Risk, Behavioral agents.
- [x] Risk Agent has two modes: trade-impact (with `proposed_trade`) and portfolio-snapshot (without). Snapshot uses live quotes for sizing; cost-basis is misleading for positions that have moved.
- [x] Orchestrator (`src/orchestrator.py`) with `asyncio.gather` fan-out and a `_safe_run` wrapper that converts agent crashes into degraded INFO reports.
- [x] Portfolio Manager (`src/agents/portfolio_manager.py`) with **structural** block enforcement: `_apply_block_check` overwrites the PM's action with `BLOCKED` whenever any report has `blocking=true`, regardless of what the LLM proposed.
- [x] CLI driver `scripts/run_query.py`.
- [x] Unit tests: Intent Router structural rules (10), Orchestrator block-check (7).

### Phase 3 — UI + Constitution interview ✅ (mostly)
- [x] Streamlit app — branded "TradeDesk — AI Agents". Multi-page (Home + Constitution).
- [x] Per-agent numeric panels: indicator panel for Quant, concentration tables for Risk, Trending component table, etc.
- [x] Constitution interview flow (LLM-driven, 6–12 questions, draft review, save to `policy.json`).
- [x] Editable Constitution view (no need to redo interview to tweak limits).
- [ ] Trade log persistence (SQLite). **Skipped — not required for demo per current scope.**

### Phase 3.5 — Cloud deployment (in progress)
- [ ] `SecretsTokenStorage` for Robinhood OAuth artifacts (loads from `st.secrets`, writes refreshes back). Required for Streamlit Cloud — see §4.6.
- [ ] Push to private GitHub repo + connect to Streamlit Community Cloud.
- [ ] Smoke test on cloud + manual rotation runbook.

### Phase 4 — Live execution (not started)
- [ ] `review_equity_order`, `place_equity_order`, `cancel_order` against the Agentic account.
- [ ] Pre-flight (re-quote + tradability) → review modal → typed-confirmation → submit → status polling.
- [ ] Live-mode toggle hardening (typed confirmation, not just a click).
- [ ] Manual end-to-end testing on a small Agentic-account budget.

## 10. Open Questions / Risks

| # | Topic | Question | Plan |
|---|---|---|---|
| 1 | Market data freshness | yfinance is delayed 15min. Acceptable for POC? | Yes for now; revisit if PM signals depend on real-time. |
| 2 | Finnhub free-tier limits | 60 calls/min; some endpoints (Social Sentiment, Market Movers) may be paid-only | Cache aggressively (`diskcache`); confirm endpoint availability before wiring. |
| 3 | LLM determinism | Same query, different answer | Set `temperature=0` for Compliance/Risk-explanation, allow higher for Research. |
| 4 | Constitution drift | User wants to "just this once" violate a rule | UI offers "Update Constitution and retry" — never silent override. |
| 5 | Trade history privacy | Behavioral agent sees all trades | OK for single-user POC; revisit before any multi-tenant version. |
| 6 | Test strategy | How do we validate PM quality? | Replay historical scenarios from trade log; spot-check vs. what the user actually did. |
| 7 | LLM model selection | Same model for all agents? | Start uniform (Sonnet 4.5 — pinned in `src/llm/client.py`); upgrade Router + PM to Opus 4.x if synthesis quality is weak. |
| 8 | Stocktwits API stability | Endpoints are undocumented; no SLA | Cache aggressively; fail soft (skip social signal entirely if Stocktwits returns errors so the scanner still produces a ranked list from price/news/analyst components). |
| 9 | Stocktwits signal quality | Same risk Reddit had — retail platform, sentiment is noisy | Treat as *attention*, not *direction*. Use platform-computed `trending_score` rather than DIY sentiment classification. |
| 10 | Trending Score weight tuning | Easy to overfit weights to recent days | Pick reasonable weights (§4.7), leave them, change only with strong evidence. |
| 11 | Filing content vs. metadata | Finnhub returns filing metadata only, not full text | Acceptable for POC — agent flags filings, user clicks link to read. Revisit if "summarize the 8-K" becomes important. |
| 12 | Robinhood MCP auth & Lambda | MCP uses interactive OAuth; not headless | POC only. Migrate to E*TRADE OAuth API for Lambda deployment (Phase 5+). `BrokerageClient` Protocol makes the swap a one-file change. |
| 13 | Robinhood MCP rate limits | Not published | Cache reads (`diskcache`), throttle ad-hoc; monitor empirically. |
| 14 | Agentic account funding | Trade execution requires funded Agentic account | User responsibility. Read agents work fine against the unfunded account; only Execute needs funding. |
| 15 | E*TRADE API approval lag | Consumer-key approval can take days/weeks | Apply early in Phase 5 timeline; build against sandbox first. |
| 16 | Order state polling | Robinhood orders move through queued → confirmed → filled asynchronously | Post-execution, poll `get_orders(order_id=...)` until terminal state, with timeout. |

## 11. Future-proofing — AWS Lambda deployment

> **Note.** The immediate deploy target is **Streamlit Community Cloud**
> (see §4.6 "Cloud deploy" block), not Lambda. This section is preserved
> as a forward-looking record for the day the demo outgrows Streamlit
> Cloud or moves to a multi-user setup.

The POC runs on a laptop. This section captures the migration plan for
when we deploy to AWS Lambda — *not* a Phase 4 deliverable, but the
shape of the change is fixed now so we don't paint ourselves into a
corner.

### 11.1 The constraint, in one sentence

Robinhood MCP's *initial* OAuth flow needs an interactive browser
(see §4.5 and `docs/ROBINHOOD_MCP_INTEGRATION.md` §0). Lambda is
stateless and headless. Those two facts are the entire reason this
section exists.

### 11.2 Demo deployment plan: copy tokens to Secrets Manager

For a single-user demo on Lambda, we **don't** redo OAuth on Lambda.
Instead:

1. Auth once on the laptop (already working — see ROBINHOOD_MCP_INTEGRATION.md).
2. Copy the resulting `tokens.json` and `client_info.json` into AWS
   Secrets Manager.
3. Lambda reads both at cold start, calls Robinhood MCP, refreshes when
   needed.

This is viable because the access token's measured lifetime is **~9 days
11 hours** (`expires_in: 816744`), so refreshes happen ~3×/month —
infrequent enough that single-Lambda concurrency races on token rewrite
are a non-issue at demo scale.

**Setup commands** (run from laptop after local OAuth completes):

```bash
aws secretsmanager create-secret \
  --name robinhood-mcp-tokens \
  --secret-string file://.cache/robinhood_oauth/tokens.json

aws secretsmanager create-secret \
  --name robinhood-mcp-client-info \
  --secret-string file://.cache/robinhood_oauth/client_info.json
```

**Both secrets are required.** `client_info.json` carries the registered
`client_id` from Dynamic Client Registration; without it the SDK doesn't
know who's making the request.

**Code change required.** Add a `SecretsManagerTokenStorage` class
implementing the same `TokenStorage` interface as `FileTokenStorage`,
then swap by environment variable (`STORAGE_BACKEND=file|secretsmanager`).
Roughly 60 lines of code; sketch lives in
`docs/ROBINHOOD_MCP_INTEGRATION.md` §11. Not yet implemented.

**IAM permissions** (least privilege):

```
secretsmanager:GetSecretValue   → robinhood-mcp-tokens, robinhood-mcp-client-info
secretsmanager:PutSecretValue   → robinhood-mcp-tokens     (refresh writes only)
```

`client_info` is read-only after registration — never needs `PutSecretValue`.

**Re-auth recovery (when refresh token eventually dies):**

```bash
.venv/bin/python -m scripts.connect_robinhood        # local re-login
aws secretsmanager put-secret-value \
  --secret-id robinhood-mcp-tokens \
  --secret-string file://.cache/robinhood_oauth/tokens.json
```

Lambda picks up the new value on next invocation; no redeploy needed.
For a demo this is an acceptable manual process.

### 11.3 Why not "browser redirects directly to Lambda URL"?

The alternative pattern is: register a public Lambda Function URL as
the OAuth `redirect_uri` so the browser flow can complete entirely on
AWS, no laptop step.

| | Pros | Cons |
|---|---|---|
| Token-copy (chosen) | Zero AWS infra to set up auth; matches `aws-cli sso login` / `gh auth` ergonomics | Manual re-auth when refresh token expires |
| Lambda-URL redirect | No laptop step | Requires stable public Function URL; locks redirect URI at registration; exposes a public callback endpoint that does nothing 99.999% of the time |

For a single-user demo, copy-tokens is strictly less infrastructure.
For multi-user production, neither pattern scales without rethinking
the trust model.

### 11.4 What stops being acceptable at production scale

The demo plan deliberately ignores three problems. List them here so
nobody trips on them later:

| Problem | Demo mitigation | Production fix |
|---|---|---|
| Concurrent Lambdas both refresh, last write wins | Won't happen at demo scale (3 refreshes/month) | Single-writer pattern: dedicated cron Lambda owns refresh; agent Lambdas read-only |
| Refresh token rotation (does Robinhood return a new one each refresh?) | Unknown until first refresh — log and observe | If yes, conditional writes (DynamoDB optimistic lock) |
| Refresh-token expiry pages a human | Manual `put-secret-value` from laptop | E*TRADE OAuth 1.0a (long-lived tokens, headless) — already the §4.5 long-term plan |

The third row is why the design's long-term Lambda plan (§4.5) is
**E*TRADE, not Robinhood**. Robinhood-on-Lambda is a demo workaround,
not a production architecture.

### 11.5 Other Lambda-shaped migrations (deferred — captured for completeness)

These are storage and config migrations that come along for the ride
when we move off the laptop. Not blocking the brokerage swap.

| Component | POC | Lambda |
|---|---|---|
| Secrets (`.env`) | Local file | Secrets Manager (loaded into env via Lambda config) |
| Constitution (`policy.json`) | Local JSON | S3 object (versioned) or DynamoDB row |
| Trade log (SQLite) | Local file | DynamoDB table |
| diskcache (Finnhub/Stocktwits/yfinance) | Local directory | ElastiCache, Lambda `/tmp` (per-container), or just disable caching for the demo |

The `Settings` loader (§4.9) already reads from environment variables —
Lambda populates env from Secrets Manager natively, so there's no app
code change needed for that path.

### 11.6 Lambda surface (front door)

**Decision deferred** — when we deploy, pick between:

- **API Gateway → Lambda.** User hits HTTPS endpoint, Lambda runs
  orchestrator, returns JSON. Streamlit moves to a separate frontend
  (Amplify, S3+CloudFront, or Lambda Web Adapter).
- **Lambda Web Adapter for Streamlit.** Streamlit runs *inside* Lambda;
  one function hosts everything. Cold starts hurt for Streamlit but
  works for a demo.

This decision doesn't gate any current work — both paths reuse the
same orchestrator + agents.

## 12. Out of Scope (POC)

- Multi-user / auth / hosting.
- Tax-aware lot selection (wash sales, FIFO/LIFO).
- Options, futures, crypto.
- Continuous monitoring / alerting (system is query-driven).
- Backtesting framework (deferred — real-time + dry_run is the eval path for now).
- Direct SEC EDGAR integration — relying on Finnhub's filings metadata only.
- Reading raw filing text — links surfaced to the user; LLM doesn't read documents.
- Paid news sources (Bloomberg, Reuters, NewsAPI).
- Twitter/X social signal (paid, low ROI).
- Reddit / AsyncPRAW — Stocktwits covers retail attention adequately for the POC and avoids the OAuth setup overhead.
- Robinhood watchlist features (12 of the 23 MCP tools).
- E*TRADE integration — deferred to Phase 5+ (Lambda deployment).
- AWS Lambda deployment — Phase 5+. Brokerage swap and storage migration (DynamoDB/S3/Secrets Manager) deferred.

## 13. Glossary

- **Constitution** — the JSON policy file that defines hard trading rules.
- **Blocking report** — an `AgentReport` with `blocking=true`, indicating a hard rule violation that cannot be overridden by the PM.
- **Dry run** — system produces full output but Execute is disabled; no orders placed.
- **Structural enforcement** — a rule enforced by Python code in the orchestrator, not by an LLM prompt.
- **Market Scanner** — the deterministic tool (`src/data/market_scanner.py`) that computes Trending Scores. Called by the Trending Agent.
- **Trending Score** — a 0–100 score combining Stocktwits attention (`trending_score` + `watchlist_count`), news volume, volume spike, price momentum, and analyst signals (§4.7).
- **Stocktwits** — retail-investor social platform. We use its undocumented public `/api/2/` endpoints (`/trending/symbols.json`, `/streams/symbol/<TICKER>.json`) as the social-attention signal for the Trending Agent. No auth, empirical ~200 req/hr rate limit.
- **BrokerageClient** — the Pydantic Protocol abstracting brokerage operations. Two implementations: `RobinhoodMCPClient` (POC) and `ETradeClient` (Phase 5+, Lambda).
- **Agentic account** — a Robinhood account flagged `agentic_allowed=true`. The Robinhood MCP brokerage-locks all writes to such accounts. Reads work against any account.
- **Read/write account split** — main account for reads (real portfolio data) + Agentic account for writes (sandboxed budget). Brokerage-enforced safety wall.
