# Session Handoff — StockAIgents

> **Purpose:** port the full context of this project to a fresh Claude Code
> session. Read this top-to-bottom and you'll have everything needed to
> continue without re-discovering decisions. Last updated: 2026-06-18.

---

## 1. What this project is

**StockAIgents** — a personal multi-agent stock advisor. User asks a
natural-language question (*"Should I buy NVDA?"*, *"What's trending?"*,
*"Am I too concentrated in tech?"*); a team of specialist AI agents answers
together, grounded in the user's real Robinhood portfolio.

**Core principle:** LLMs reason, **Python code enforces money rules**. The
boundary is enforced by typed Pydantic contracts, not prompts. It is an
**advisor, not an autotrader** — dry-run by default, never auto-executes.

**Owner:** Faiz Ali (System Development Engineer, Amazon). The work has two
tracks now:
1. The **app** itself (Python, mostly complete through Phase 3).
2. A **conference talk** about building it live with Claude — includes a
   web presentation, a build runbook, and demo materials.

---

## 2. Current status

- **65 tests passing** (`.venv/bin/python -m pytest -q`).
- Phases 0–3 substantially complete (foundations, agents, orchestration,
  Streamlit UI, Constitution interview). Phase 4 (live order execution) and
  cloud deployment are **not** done.
- Running on **Python 3.14** locally in `.venv` (the design says 3.11+).
- Models: **Anthropic Claude Sonnet 4.5** default (`claude-sonnet-4-5`),
  pinned in `src/llm/client.py`. Opus available for PM/Router upgrade.

---

## 3. Architecture in one screen

```
User query
  → Intent Router (LLM, structured output) — classifies + picks agents
  → Orchestrator — loads portfolio + market data + Constitution
  → 6 specialist agents run in PARALLEL (asyncio.gather):
       Research   (LLM)     — Finnhub news/earnings/filings
       Quant      (code+LLM)— yfinance bars → RSI/SMA/momentum/vol
       Trending   (code+LLM)— Market Scanner (Stocktwits+Finnhub+yfinance)
       Risk       (math+LLM)— concentration/sector/cash; trade + snapshot modes
       Behavioral (LLM)     — Robinhood order history → FOMO/panic/etc.
       Compliance (pure code, NO LLM) — Constitution hard checks
  → Block check (PYTHON) — any report.blocking → force action=BLOCKED
  → Portfolio Manager (LLM) — synthesizes one Recommendation
  → Decision returned to UI
```

**Two structurally-enforced safety patterns (Python, not prompts):**
1. Any `trade_decision` always runs Risk + Compliance — enforced in
   `intent_router._resolve_agents`.
2. Any `blocking=true` report forces `Recommendation.action="BLOCKED"` —
   enforced in `orchestrator._apply_block_check`. The PM's LLM output is
   overwritten; it cannot override a block.

---

## 4. Repository map

```
src/
  config.py            Settings (pydantic-settings) + require_anthropic/finnhub
  models.py            AgentReport, Intent, Recommendation, Decision,
                       TrendingTicker, ProposedTrade, signals/enums
  orchestrator.py      Orchestrator class + _apply_block_check
  agents/
    base.py            BaseAgent, AgentContext
    intent_router.py   LLM router + structural _resolve_agents
    compliance.py      Pure rule engine, no LLM
    quant.py           Indicator panel → LLM explanation
    indicators.py      Pure RSI/SMA/volatility/momentum/volume_trend
    research.py        News+earnings+filings → LLM
    trending.py        Wraps market_scanner + LLM rationale
    risk.py            Trade-impact AND portfolio-snapshot modes
    behavioral.py      Trade-history pattern detection
    portfolio_manager.py  LLM synthesizer (block-aware)
  llm/client.py        AsyncAnthropic wrapper; tool-use → Pydantic structured output
  constitution/
    schema.py          Pydantic model of policy.json
    policy.example.json  Committed default
    policy.json        Saved interview output (gitignored)
    interview.py       ConstitutionInterviewer (LLM, 6–12 Qs)
    loader.py          load_constitution / save_constitution
  data/
    cache.py           diskcache TTL decorator
    ratelimit.py       SlidingWindowLimiter + retry_async (shared)
    market.py          yfinance OHLCV + volume movers
    finnhub.py         news/earnings/filings/analysts/profile + 55/min throttle + 429 retry
    market_scanner.py  Deterministic Trending Score
    social/stocktwits.py  Public /api/2/ wrapper + throttle + 5xx/timeout retry
    brokerage/
      base.py          BrokerageClient Protocol + Pydantic models (all Decimal)
      robinhood_mcp.py RobinhoodMCPClient (reads done; writes = Phase 4 NotImplementedError)
  ui/
    streamlit_app.py   Home page (advisor query)
    pages/1_Constitution.py  Interview + editor

scripts/                CLI drivers — connect_robinhood, run_query, run_quant,
                        run_agents, run_router, scan_trending, probe_market_data, run_ui.sh
tests/                  65 tests (compliance, indicators, intent_router,
                        market_scanner, orchestrator, cache)

docs/
  TECHNICAL_DESIGN.md          The full design doc (source of truth, §-numbered)
  ROBINHOOD_MCP_INTEGRATION.md How the Robinhood MCP auth + shapes work
  BUILD_RUNBOOK.md             Step-by-step guide for audience to build it
  SESSION_HANDOFF.md           This file

presentation/index.html        12-slide React+Tailwind talk deck (CDN, no build)
project-idea.md                The 1-page brief (audience-facing "what to build")
PROJECT_BRIEF.md               Tighter internal brief for the live-build demo
DEMO_KICKOFF_PROMPT.md         Prompt to paste at demo start
DEMO_RUNBOOK.md                Private demo cheat-sheet
```

---

## 5. Key decisions & gotchas (don't re-litigate these)

- **Data sources are fixed & all free:** Anthropic (key), Finnhub (key,
  60/min), yfinance (no key), Stocktwits (no key, undocumented `/api/2/`),
  Robinhood MCP (browser OAuth, no key). **Reddit was rejected**
  (hostile to programmatic access); **Finnhub Social Sentiment is paywalled**
  (confirmed 403).
- **Robinhood MCP** = OAuth 2.0 + PKCE + Dynamic Client Registration. No
  static key. First run opens a browser; tokens persist in
  `.cache/robinhood_oauth/` (gitignored). Reads work on any account; writes
  brokerage-locked to the `agentic_allowed=true` account. Full details +
  observed response shapes in `docs/ROBINHOOD_MCP_INTEGRATION.md`. The
  harmless `Session termination failed: 400` at shutdown is expected.
- **Finnhub throttling:** 55/min sliding-window limiter + 429 retry, in
  `data/ratelimit.py` (shared with Stocktwits). Added after hitting real
  429s under the 30-ticker Trending fan-out.
- **Stocktwits hangs under load** (silent ReadTimeout + 504, not clean 429).
  Same limiter + retry-on-5xx/timeout. A past bug: SPCX (a real IPO ticker)
  was silently dropped from the scanner because the trending fetch flaked —
  fixed by fetching the trending list once and threading it through.
- **Risk has two modes:** trade-impact (proposed_trade present) and
  portfolio-snapshot ("Am I too concentrated?"). Snapshot uses **live
  quotes** for sizing — cost-basis was misleading for grown positions (e.g.
  a position up 6x ranks wrong by cost basis).
- **Money is always `Decimal`**, never float. Robinhood returns strings.
- **npm is wired to internal Amazon CodeArtifact** (`.npmrc`) — public
  packages need `--registry https://registry.npmjs.org` or fail E401. The
  presentation uses **CDN React/Tailwind, no npm build** for this reason.
- **MockBrokerageClient was dropped** — tests use conftest fixtures instead.
- A `slides/` Vite+React+Tailwind+framer-motion project was built then
  **deleted** — the user preferred the single-file HTML presentation.

---

## 6. How to run things

```bash
# Setup (already done; venv exists)
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt

# Tests
.venv/bin/python -m pytest -q

# Full pipeline against real APIs (needs .env keys + Robinhood tokens)
.venv/bin/python -m scripts.run_query "Should I buy NVDA?"

# Other drivers
.venv/bin/python -m scripts.connect_robinhood     # one-time Robinhood OAuth
.venv/bin/python -m scripts.scan_trending          # Market Scanner output
.venv/bin/python -m scripts.run_agents NVDA        # 4 LLM agents in parallel

# Streamlit UI
./scripts/run_ui.sh                                # → http://localhost:8501

# Presentation
open presentation/index.html                       # 12-slide talk deck
```

**Secrets:** `.env` holds `ANTHROPIC_API_KEY` + `FINNHUB_API_KEY` (real key
present locally, gitignored). Robinhood auth lives in `.cache/`.

---

## 7. The presentation (talk deck)

`presentation/index.html` — single self-contained file, React + Tailwind via
CDN, dark **GitHub-Next-style** theme. Accent purple `#a371f7`, bg `#0d1117`.
Fonts: **Inter** for headings, **Outfit** for body. 12 slides:

1. Title (StockAIgents hero) · 2. Disclaimer ("you might expect a trading
app") · 3. Audience interaction (click-to-reveal reason cards → agents) ·
4. What is an AI Agent · 5. What is MCP (hub-and-spoke diagram) · 6. Our
Agents (6 + PM, with tools) · 7. Architecture (animated top-down flow) ·
8. What you need (setup/keys) · 9. Dev journey (6 stages) · 10. Demo (LIVE
badge) · 11. Deployment (bare section divider) · 12. Thank you.

Nav: arrow keys / floating glass control bar / dots. Slide 3 reveals one
reason per click then all agent labels on the final click. Slides 7 & 9
animate on entry (keyed on `isActive`).

**Design taste learned the hard way:** the user wants big, confident,
modern, lots of whitespace — NOT busy/boxy. Dark theme only (light was
rejected). When unsure, ask for a screenshot reference rather than guessing.

---

## 8. Likely next tasks

Things that may come up — no action needed now, just context:

- **Phase 4 — live execution:** wire `review_equity_order` /
  `place_equity_order` against the Agentic account. High blast radius;
  needs a funded Agentic account; gated behind typed confirmation. See
  TECHNICAL_DESIGN §9 Phase 4 and §11.
- **Cloud deploy:** Streamlit Community Cloud is the target. Needs a
  `SecretsTokenStorage` so Robinhood tokens survive (sketch in
  ROBINHOOD_MCP_INTEGRATION §11 / TECHNICAL_DESIGN §11.2). Robinhood's
  browser OAuth can't run on Lambda — that's why E*TRADE is the long-term
  Lambda plan.
- **Trade log (SQLite):** the one Phase 3 item deliberately skipped.
- More presentation polish / runbook edits.

---

## 9. Working style the user expects

- **Be honest about tradeoffs** — present options, recommend one, don't
  hedge endlessly.
- **Verify before claiming** — run the probe/test, don't assume.
- **Real working code over scaffolding.**
- **Surface problems** — if something looks wrong or contradicts the design,
  say so.
- For design/visual work: **get a concrete reference** (screenshot/URL)
  before iterating; guessing wasted many rounds.
- Confirm scope on big/ambiguous asks before building; just act on clear
  ones.
