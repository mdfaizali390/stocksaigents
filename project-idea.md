# TradeDesk — AI Agents · Project Brief

> This is the brief I'm handing to Claude. It describes the product I want
> and asks Claude to design how to build it.

---

## The idea

I want a personal **stock advisor made of several AI agents working
together**. I ask a question in plain English, and a team of specialists
answers it as a group.

Questions I'd ask it:

- _"Should I buy NVDA?"_
- _"What's trending today?"_
- _"Am I too concentrated in tech?"_
- _"What's my max trade size?"_

It should connect to my real **Robinhood** account so its answers are based
on my actual portfolio and holdings — not generic advice.

**Most important: it advises, it never trades on its own.** Every answer is
a recommendation. I always make the final call.

## How I picture it working

I ask a question. Something figures out what I'm really asking and hands it
to the right specialists. They each look at the problem from their own angle,
at the same time. Then one "manager" agent reads all their takes and gives me
a single clear recommendation — along with each specialist's reasoning so I
can see how it got there.

## The specialists

I want these agents, each with its own job:

- **Research** — reads the recent news, earnings, and filings on a stock and
  forms an opinion.
- **Quant** — looks at the price chart and the technical indicators and says
  what the numbers suggest.
- **Trending** — finds what's hot right now across the market, using social
  buzz and news and price action together.
- **Risk** — checks what a trade would do to my portfolio: am I getting too
  concentrated, too low on cash, over-exposed to one sector?
- **Behavioral** — looks at my recent trading history and calls out bad
  habits: chasing hype, panic selling, revenge trading, overtrading.
- **Compliance** — the strict rule-checker. Makes sure a trade doesn't break
  any of my hard limits.
- **Portfolio Manager** — the one that reads everyone's input and gives me
  the final recommendation.

A key rule: **the Risk and Compliance agents can block a trade, and nothing
can talk them out of it.** If they say a trade breaks my rules, the final
answer is "blocked" — period. The manager doesn't get to override that.

## My trading rules ("the Constitution")

I want my own personal trading rules written down in one place — things like
the biggest trade I'll allow, the most I'll hold in any one stock, how much
cash to keep, which kinds of trades are off-limits. The agents must follow
these rules every single time.

I'd like to set these up by having the app **interview me** — ask me a series
of questions about how I like to invest, then propose a set of rules I can
review and tweak. After that, the rules are locked in and enforced, and I can
edit them whenever I want.

The principle I care about here: **the AI can help me write the rules, but
once they're set, plain code enforces them.** I don't want the AI deciding,
in the moment, whether to bend a rule. Judgment is the AI's job; enforcing
the rules is code's job.

## What I want to build it with

- **Claude** (Anthropic) for all the AI reasoning. My API key is in `.env`.
- **Robinhood** — I want it connected to my real brokerage account via
  Robinhood's official MCP server (`agent.robinhood.com/mcp/trading`). This
  gives the agents read access to my actual portfolio, holdings, and order
  history. There's a `ROBINHOOD_MCP_INTEGRATION.md` in this workspace with connection
  details.
- **Finnhub** (`finnhub.io`) — free market data API for company news,
  earnings dates, SEC filings, and analyst recommendations.
- **yfinance** — free Python library for historical stock price and volume
  data.
- **Stocktwits** (`stocktwits.com`) — social platform where traders discuss
  stocks. Its public API shows which tickers are trending and how much buzz
  they're getting. Useful for the Trending agent's social signal.
- **Python**, and a simple **Streamlit** web app for the interface, branded
  **"TradeDesk — AI Agents."** The interface needs a place to ask questions
  and see the answer, plus a place to set up and edit my trading rules.

Create scripts to run individual agents independently for testing and create a shell script to run the streamlit app locally

---

## What I want from you (Claude)

Take this brief and produce a **`TECHNICAL_DESIGN.md`**: the architecture,
how the pieces fit together, the data shapes the agents pass around, how a
query flows through the system, the project structure, and a phased plan to
build it. Be specific and opinionated where I've left things open — you're
the engineer here.

**Then stop and let me review the design before you write any code.**

A few things I care about:

- **Be honest about tradeoffs.** If there are two good ways to do something,
  tell me both before you pick one.
- **Do your homework on the data sources.** Look up the current docs for
  Finnhub, yfinance, and Stocktwits and flag any limits, costs, or gotchas
  before we build on them.
- **Speak up if something I said is wrong or unclear.** I'd much rather you
  ask me a question than quietly guess.
- **Tell me what you had to invent.** Anything I didn't specify that you had
  to decide — call it out.

