# Project Brief — Multi-Agent Stock Portfolio Advisor

**Audience:** Claude Code (the assistant building this).
**Source of truth:** the constraints, agents, and safety rules below
are intentional choices. Treat them as requirements, not suggestions.

---

## 1. What we're building

A **personal stock portfolio advisor** that:

- Accepts natural-language queries about stocks, the user's portfolio,
  or specific trades (e.g. *"Should I buy NVDA?"*, *"What's trending?"*,
  *"Am I too concentrated in tech?"*).
- Routes the query to the right specialist agents, runs them in
  parallel, and synthesizes their outputs into a single recommendation.
- Presents the individual agent reports **and** the synthesized
  recommendation to the user.
- **Never executes a trade automatically.** All output is advisory.
  When live execution is enabled (out of scope for this build), the
  user clicks Execute to place an order.

The system is an **advisor, not an autotrader.**

## 2. Non-negotiable design principles

These are constraints. They affect every architectural decision below.

1. **Hybrid LLM + deterministic code.** LLMs handle reasoning,
   summarization, and judgment. Python code handles math, rules, and
   money. The boundary is enforced by typed data contracts (Pydantic),
   not by prompts.
2. **The Trading Constitution.** A JSON policy file defines the user's
   hard trading rules (max trade size, max single-stock %, allowed
   asset classes, etc.). An LLM-driven interview helps the user
   *author* it once. After that, **only Python code reads and enforces
   it.** The LLM does not get a vote at decision time.
3. **Structured agent outputs.** Every specialist agent returns the
   same Pydantic shape (`AgentReport`). The synthesizer never re-parses
   prose.
4. **Parallel agents, single synthesizer.** All specialist agents run
   concurrently via `asyncio.gather`. A final Portfolio Manager agent
   synthesizes after the barrier.
5. **Structural block enforcement.** When any specialist returns
   `blocking=true` (a hard rule violation), the orchestrator
   **overwrites** the Portfolio Manager's action to `BLOCKED` —
   regardless of what the LLM proposed. The LLM is told about the
   block (so its narrative is coherent), but cannot override it. This
   rule is enforced in **Python**, not via prompt.
6. **Dry-run first.** Default mode disables real trading entirely.
   Live execution is out of scope for this build.

## 3. Specialist agents (build all of these)

Each agent inherits a common `BaseAgent` and returns an `AgentReport`.

| Agent | Implementation pattern | Signal range |
|---|---|---|
| **Research Agent** | LLM-heavy. Fetches recent news, earnings calendar, and SEC filing metadata. LLM produces an opinion citing specific items. | BUY / HOLD / SELL / INFO |
| **Quant Agent** | Code-heavy + LLM explains. Computes RSI, SMA-50, SMA-200, momentum, volatility, volume trend in Python. LLM writes the explanation, but never sees raw bars. | BUY / HOLD / SELL / INFO |
| **Trending Agent** | Code-heavy scoring + LLM rationale. Pulls Stocktwits trending list + Finnhub news volume + analyst signals + price/volume movement, ranks the day's universe with a deterministic Trending Score (percentile-ranked components, fixed weights). LLM narrates per-ticker. | INFO |
| **Risk Agent** | Math + LLM narration. Computes post-trade portfolio impact (concentration / sector / cash) **or** snapshot mode if no proposed trade. Hard limits in the Constitution → `blocking=true`. LLM only narrates; the block decision is Python's. | BLOCK / WARNING / INFO |
| **Behavioral Agent** | LLM-heavy. Reads the user's recent order history (filtered to user-placed orders) and the proposed trade. Detects FOMO, revenge trading, overtrading, panic selling. | WARNING / INFO |
| **Compliance Agent** | **Pure rule engine, no LLM.** Hard checks the proposed trade against Constitution rules (asset class, order type, trade size, tradability). | BLOCK / INFO |
| **Portfolio Manager** | LLM synthesizer. Reads all specialist reports + portfolio context, produces a single `Recommendation` with action / confidence / summary / citations. **Cannot override blocks** (orchestrator enforces this). | BUY / SELL / HOLD / NO_ACTION / BLOCKED |

## 4. Intent Router

A single LLM call (structured output) classifies the user's query into
one of:

- `trade_decision` (buy / sell named ticker)
- `market_info` (general market / trending)
- `portfolio_analysis` (state of my holdings)
- `policy_question` (what's my max trade size?)

It also picks which agents to dispatch. **Structural rule:** any
`trade_decision` always runs Risk + Compliance regardless of LLM
choice. `policy_question` runs no agents (answered from Constitution
directly).

## 5. External services (use exactly these, all free tier)

- **Anthropic Claude** — every LLM call. Sonnet 4.5 default. API key
  required.
- **Robinhood Agentic Trading MCP** (`agent.robinhood.com/mcp/trading`)
  — read brokerage data: accounts, portfolio, positions, orders,
  quotes, tradability. **OAuth 2.0 with PKCE + Dynamic Client
  Registration** — there's no static API key. Browser flow on first
  run, tokens persist locally.
- **Finnhub** — company news, earnings calendar, SEC filing metadata,
  analyst recommendations, company profile (for sector). Free tier is
  60 calls/min. Paywalled endpoints to avoid: Social Sentiment,
  Premium News.
- **yfinance** — historical OHLCV bars and volume movers (the candidate
  universe for the Trending scanner).
- **Stocktwits** — public `/api/2/` endpoints (`/trending/symbols.json`,
  `/streams/symbol/<TICKER>.json`). No auth, no key. Used as the
  social-attention signal.

## 6. Tech stack (use exactly these)

- Python 3.11+ with `asyncio`
- `pydantic` v2 for all data contracts
- `pydantic-settings` for config loading from `.env`
- `anthropic` SDK (async client)
- `mcp` SDK for the Robinhood MCP transport (Streamable HTTP)
- `httpx` for Finnhub / Stocktwits
- `yfinance`
- `diskcache` for shared TTL caching
- **Streamlit** for the UI (multi-page)
- `pytest` + `pytest-asyncio` for tests

## 7. UI — Streamlit, multi-page

**Home page** — query advisor:
- Query box + Analyze button
- Mode toggle: Dry Run / Live (default Dry Run)
- Sidebar: active Constitution summary
- Output: intent strip, **block banner** (red, prominent if any
  agent blocked), color-coded PM recommendation, Execute button
  (disabled in Dry Run / when blocked / when no trade), expandable
  agent cards, per-agent **numeric panels** (RSI/SMA for Quant,
  concentration tables for Risk, etc.), evidence trail.

**Constitution page** — interview + editor:
- Run interview tab: 6–12 LLM-driven questions, then a draft for
  user review
- Active Constitution tab: read current `policy.json`, edit any
  field, save

**Brand:** "TradeDesk — AI Agents."

## 8. Repository structure

Build the project under `src/`:

```
src/
  agents/         {one file per agent} + base.py + indicators.py
  llm/client.py   AsyncAnthropic wrapper, tool-use → Pydantic structured output
  data/
    brokerage/    BrokerageClient Protocol + RobinhoodMCPClient
    market.py     yfinance
    finnhub.py    Throttled (sliding window 55/min) + retry on 429
    social/stocktwits.py
    market_scanner.py    Deterministic Trending Score
    cache.py      diskcache TTL decorator
  constitution/   schema.py + interview.py + loader.py + policy.example.json
  orchestrator.py
  ui/streamlit_app.py + pages/1_Constitution.py
```

## 9. Build phases (target for this session)

- **Phase 0 — Foundations.** Repo bones, all Pydantic data contracts,
  Constitution schema, BrokerageClient Protocol + Robinhood MCP read
  path, all data wrappers, Compliance Agent + tests, Market Scanner +
  tests.
- **Phase 1 — Quant vertical slice.** LLM client wrapper with
  structured output, indicators module + tests, Quant agent
  end-to-end, CLI driver.
- **Phase 2 — Full orchestration.** Intent Router (with structural
  agent dispatch), Research / Trending / Risk / Behavioral agents,
  Orchestrator with parallel fan-out, Portfolio Manager with
  structural block check, full-pipeline CLI driver.

**Out of scope for this session:** Streamlit UI (Phase 3), Constitution
interview UI, live execution (Phase 4), AWS Lambda deployment.

## 10. Things to verify with me before assuming

The author of this brief expects you to ask before:

- **Choosing a starting slice that doesn't match Phase 0** — confirm
  you're starting with foundations (data contracts + Compliance), not
  jumping to Quant.
- **Wiring real OAuth.** Robinhood MCP requires a one-time browser
  login. Don't run that mid-demo without warning. Mock or stub
  brokerage if needed; we can wire real OAuth at the end.
- **Test strategy.** This codebase values fast deterministic tests;
  ask before adding network-dependent tests.
- **Skipping any of the 7 agents.** Each one earns its keep in the
  design (especially the BLOCK pattern from Risk + Compliance). Don't
  collapse them into fewer.

## 11. Done criteria for this session

- All Phase 0 + 1 + 2 items in §9 are built and tested.
- `pytest` is green.
- A CLI driver runs the full pipeline against fixture or real data:
  query → router → context fetch → 5 specialists in parallel → PM →
  formatted output.
- The structural block pattern is demonstrably working: when Compliance
  or Risk emits `blocking=true`, the final recommendation is `BLOCKED`
  with the reasons surfaced verbatim — even though the PM's LLM call
  may have proposed BUY.
