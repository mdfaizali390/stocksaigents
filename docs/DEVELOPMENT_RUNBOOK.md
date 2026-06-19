# StockAIgents — Build Runbook

A step-by-step guide to build this multi-agent stock advisor from scratch,
starting with an empty machine and ending with a deployed web app.

**Who this is for:** anyone who watched the talk and wants to build it
themselves. No prior AI-agent experience assumed. Basic comfort with a
terminal helps.

**What you'll end up with:** a Streamlit web app where you ask
*"Should I buy NVDA?"* and a team of AI agents answers — grounded in your
real Robinhood portfolio.

**Rough time:** 60–90 minutes, most of it spent letting Claude write code
while you review.

---

## Table of contents

0. [What you'll need](#0-what-youll-need)
1. [Install VS Code](#1-install-vs-code)
2. [Install Claude Code](#2-install-claude-code)
3. [Install Python](#3-install-python)
4. [Get your API keys](#4-get-your-api-keys)
5. [Set up the project folder](#5-set-up-the-project-folder)
6. [Write the brief](#6-write-the-brief)
7. [Let Claude design it](#7-let-claude-design-it)
8. [Let Claude build it](#8-let-claude-build-it)
9. [Add your secrets](#9-add-your-secrets)
10. [Connect Robinhood](#10-connect-robinhood)
11. [Run it locally](#11-run-it-locally)
12. [Deploy to the web](#12-deploy-to-the-web)
13. [Troubleshooting](#13-troubleshooting)

---

## 0. What you'll need

Before you start, make sure you have:

- A computer (Mac, Windows, or Linux)
- A **Robinhood account** (the app reads your real portfolio)
- An **Anthropic account** with a little API credit (~$5 is plenty)
- A **GitHub account** (free — for deployment)
- About an hour

**Cost:** The whole thing runs on free tiers except Anthropic, which is
pay-as-you-go. Building + demoing this costs roughly **$1–3** in API usage.

---

## 1. Install VS Code

VS Code is the code editor we'll work in.

1. Go to **https://code.visualstudio.com**
2. Click **Download** (it auto-detects your OS)
3. Open the downloaded file and follow the installer
4. Launch VS Code

> **Mac tip:** drag the app into your Applications folder.
> **Windows tip:** check "Add to PATH" during install.

---

## 2. Install Claude Code

Claude Code is the AI assistant that writes the app. It lives inside VS Code.

1. Open VS Code
2. Click the **Extensions** icon in the left sidebar (four squares)
3. Search for **Claude Code**
4. Click **Install** on the official Anthropic extension
5. After install, sign in when prompted (you'll need your Anthropic account)

> If you prefer the terminal, you can instead run:
> ```bash
> npm install -g @anthropic-ai/claude-code
> ```
> Then type `claude` in any terminal. The VS Code extension is friendlier
> for beginners.

---

## 3. Install Python

The app is written in Python.

**Check if you already have it.** Open a terminal (in VS Code:
**Terminal → New Terminal**) and run:

```bash
python3 --version
```

If you see `Python 3.11` or higher, skip to step 4.

**If not installed (or too old):**

- **Mac:** install [Homebrew](https://brew.sh) then `brew install python@3.12`
- **Windows:** download from **https://python.org/downloads** — during
  install, **check "Add Python to PATH"**
- **Linux:** `sudo apt install python3 python3-venv python3-pip`

Verify again with `python3 --version`.

### Create the virtual environment

A virtual environment (venv) keeps this project's packages isolated from
the rest of your system. Run these from your project folder:

```bash
rm -rf .venv                       # remove any old/broken venv
python -m venv .venv               # create a fresh one
source .venv/bin/activate          # activate it (Mac/Linux)
```

> **Windows:** activate with `.venv\Scripts\activate` instead.

Confirm the venv is active and on the right Python:

```bash
which python                       # should point inside .venv
python --version
```

**Expected:** `Python 3.12.13` (any 3.11+ works, but 3.12 is what this
guide is built and tested against).

### Install the dependencies

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

> The `requirements.txt` file is created by Claude during the build
> (step 8). If you're following along live, run this install step **after**
> Claude has generated that file. Re-run it any time dependencies change.

---

## 4. Get your API keys

You need two keys. Grab them now so they're ready.

### Anthropic (powers the AI)

1. Go to **https://console.anthropic.com**
2. Sign in / sign up
3. Add credit: **https://console.anthropic.com/settings/billing** (~$5 is plenty)
4. Create your key: **https://console.anthropic.com/settings/keys** → **Create Key**
5. Copy the key (starts with `sk-ant-...`) — **save it somewhere safe, you
   can't see it again**

### Finnhub (market data — free)

1. Go to **https://finnhub.io**
2. Click **Get free API key** and sign up
3. Copy the API key from your dashboard

> **Stocktwits and yfinance need no keys.** Robinhood connects through a
> browser login later (step 10).

---

## 5. Set up the project folder

1. In VS Code: **File → Open Folder**
2. Create a new empty folder somewhere, e.g. `stockaigents`, and open it
3. Open a terminal (**Terminal → New Terminal**)

You now have an empty project open in VS Code with a terminal ready.

---

## 6. Write the brief

This is the magic step. Instead of coding, you describe **what you want**
in plain English, and Claude figures out **how** to build it.

**The brief is already written for you** — it's the `project-idea.md` file
from the talk. Grab it and drop it into your project:

1. Get `project-idea.md` (from the shared repo / the link given in the
   talk).
2. Copy it into your project folder so VS Code can see it.
3. Open it and read through — this is your brief. It describes the product,
   names the seven agents, lists the data sources, and states the safety
   principles. Crucially, it stays at the level of *what* to build and
   leaves the *how* to Claude.

> **Want to make it your own?** Edit `project-idea.md` freely — swap in a
> different idea, change the agents, point it at different data sources.
> Just keep it about the *what*, not the *how*: describe the product, name
> the pieces, state the principles, and stop there. Let Claude make the
> technical calls. A good brief is about 1–1.5 pages.

---

## 7. Let Claude design it

1. Open the Claude Code panel in VS Code (click the Claude icon, or
   **Cmd/Ctrl+Esc**)
2. Type this prompt:

   > Read `project-idea.md`. Produce a `TECHNICAL_DESIGN.md` that turns it
   > into a concrete plan — architecture, components, data shapes, and a
   > phased build plan. Be specific and opinionated, but flag anything
   > you'd want me to decide. **Then stop and wait for me to review — don't
   > write code yet.**

3. Claude writes a design document. **Read it.** This is your chance to
   catch wrong assumptions before any code exists.
4. If something looks off, just tell Claude in plain English
   ("use Stocktwits for trending, not Reddit") and it'll revise.

---

## 8. Let Claude build it

Once you're happy with the design:

> Looks good. Build it phase by phase: foundations first (data models,
> config), then one agent end-to-end, then the rest of the agents and the
> orchestrator. Run the tests after each phase. Show me each piece working
> before moving on.

Claude will now write the code in stages. Your job:

- **Review each phase** — skim what it wrote, ask questions if unclear
- **Approve tool actions** — Claude asks permission to run commands; read
  them, then allow
- **Answer its questions** — it'll ask you to decide things the brief
  didn't cover. Pick what makes sense.

This is the longest part (30–45 min). Let it cook, stay in the loop.

> **Tip:** if Claude proposes something you don't want, say so. You're the
> reviewer — nothing ships without your okay.

---

## 9. Add your secrets

Claude will have created a `.env.example` file showing which keys are
needed. Now make your real one:

1. In the terminal, copy the template:
   ```bash
   cp .env.example .env
   ```
2. Open `.env` in VS Code and fill in your keys:
   ```
   ANTHROPIC_API_KEY=sk-ant-...your key...
   FINNHUB_API_KEY=...your finnhub key...
   ```
3. Save the file.

> `.env` is **gitignored** — it never gets committed or shared. Your keys
> stay on your machine.

---

## 10. Connect Robinhood

Robinhood connects through its **Agentic Trading** feature — a one-time
browser authorization, no API key. Official guide:
**https://robinhood.com/us/en/support/articles/agentic-trading-overview/#ConnectyourAIagent**

### What's happening

Robinhood lets AI agents connect through an **MCP server**
(`https://agent.robinhood.com/mcp/trading`). When you authorize:

- The agent gets **read access** to all your accounts, positions, balances,
  and order history.
- The agent can only ever **place trades in a separate "Agentic" account** —
  never your main account. (This app stays in Dry Run anyway, so it places
  nothing.)

### Prerequisites

- A primary individual Robinhood account in good standing.
- **Use a desktop browser** for setup. (Authentication can't be completed on
  mobile — if a mobile link appears, copy it to a desktop browser.)

### Option A — Let the app's script do it (what we use)

The project includes a connection script that opens the OAuth flow directly:

1. Ask Claude:
   > Run the Robinhood connection script so I can authorize my account.
2. The script opens your **browser** to a Robinhood login.
3. Log in. Robinhood prompts you to **open an Agentic account** — follow the
   on-screen onboarding steps to create it (one-time).
4. Click **Authorize**.
5. The browser shows "authorization complete" — close the tab.
6. Back in the terminal you'll see your accounts and portfolio print out.
   Tokens are saved locally (in `.cache/`, gitignored) so you only do this
   once.

> If the browser doesn't open, the script prints a URL — paste it into your
> browser manually (on desktop).

### Option B — Connect via Claude Code's own MCP client

You can also register the Robinhood MCP with Claude Code itself (useful if
you want to chat with your portfolio directly inside Claude, separate from
the app):

1. In the terminal, run:
   ```bash
   claude mcp add robinhood-trading --transport http https://agent.robinhood.com/mcp/trading
   ```
2. In Claude Code, type `/mcp`
3. Select **robinhood-trading** and authenticate — the same browser
   authorization + Agentic-account onboarding as above.

> The app uses its own connection (Option A) so it works when deployed.
> Option B is just for poking at your portfolio interactively.

---

## 11. Run it locally

Time to see it work.

1. Ask Claude:
   > Start the Streamlit app.

   Or run it yourself from the terminal:
   ```bash
   ./scripts/run_ui.sh
   ```
2. Your browser opens to **http://localhost:8501**
3. Try a query: *"Should I buy NVDA?"* or *"Am I too concentrated in tech?"*
4. Watch the agents run in parallel and the Portfolio Manager give you a
   recommendation.

> **First query is slow** (~30–60s) because data caches are cold. Repeat
> queries are fast. Keep it in **Dry Run** mode — no real trades happen.

---

## 12. Deploy to the web

So you can use it from anywhere (or show friends). We'll use **Streamlit
Community Cloud** — free.

> ### ⚠️ Read this first: we deploy with Robinhood **disabled** on purpose
>
> Robinhood's login is a one-time **browser** flow — it physically cannot
> run on a headless cloud server. The only way to connect Robinhood on the
> cloud is to **copy your live brokerage tokens onto Streamlit's servers**,
> behind a public URL. Those tokens give **read access to your real
> portfolio**.
>
> **So the default, recommended deployment turns Robinhood off.** The app
> detects there are no Robinhood tokens and runs in a graceful
> "portfolio not connected" mode:
>
> - ✅ **Works fully:** market questions, trending stocks, company news,
>   technical analysis (Finnhub / Stocktwits / yfinance — no login needed).
> - 🔌 **Degrades cleanly:** "How many AAPL do I have?" / trade-specific
>   risk → a friendly "brokerage isn't connected here" message, never a
>   crash or a wrong answer.
>
> For the full portfolio experience, **run the app locally** (steps 10–11),
> where the browser login works and your tokens never leave your machine.
>
> If you still want Robinhood live on the cloud, see §12.4 — **at your own
> risk.**

### 12.1 Put your code on GitHub

1. Create a **private** repo at **https://github.com/new** (private matters —
   you'll point it at your real accounts).
2. Push your project to that repo (VS Code Source Control, GitHub Desktop,
   or the GitHub web upload — whatever you used).

> Double-check `.env` and `.cache/` are **not** in the repo — they're
> gitignored. **Never push secrets.** Confirm by browsing the repo on
> github.com: you should NOT see `.env` or a `.cache` folder.

### 12.2 Deploy (Robinhood disabled — recommended)

1. Go to **https://share.streamlit.io**
2. Sign in with GitHub (authorize access to your repo, including private).
3. Click **Create app** → deploy from your repo.
4. Set:
   - **Repository:** `your-username/your-repo`
   - **Branch:** `main`
   - **Main file path:** `src/ui/streamlit_app.py`
5. Open **Advanced settings → Secrets** and paste **only these two keys**
   (TOML format) — no Robinhood section:
   ```toml
   ANTHROPIC_API_KEY = "sk-ant-...your key..."
   FINNHUB_API_KEY = "...your finnhub key..."
   ```
6. Click **Deploy**.

After ~2 minutes you'll get a public URL like `your-app.streamlit.app`.
The sidebar will show a **"🔌 Portfolio not connected"** banner — that
confirms it's running in the safe, Robinhood-disabled mode.

### 12.3 Lock the app down

Even without Robinhood, the app spends **your** Anthropic credit on every
query — so don't leave it open to the world:

1. In the app → **⋮ menu → Settings → Sharing**
2. Set it to **"Only specific people"** and add your own email (plus anyone
   you trust).

> Streamlit's free tier limits private apps to a small number of viewers —
> fine for you + a couple of people. That's the baseline protection;
> there's no login wall otherwise.

### 12.4 (Advanced — at your own risk) Deploy WITH Robinhood live

> **This puts live brokerage tokens with read access to your real portfolio
> onto Streamlit's servers, reachable at a public URL. Only do this if you
> understand and accept that.** Mitigations: keep the app **private**
> (§12.3), keep your Agentic account's balance small, and **rotate the
> tokens after** (revoke the app in Robinhood → Account → connected/agentic
> apps, then re-auth locally).

The app already supports this — it reads tokens from Streamlit secrets and
runs Robinhood in **headless** mode (no browser attempt). You just supply
the tokens.

**Step 1 — Authenticate locally first.** You need valid tokens to copy.
Run the app locally and connect Robinhood once (steps 10–11). This creates:
- `.cache/robinhood_oauth/client_info.json`
- `.cache/robinhood_oauth/tokens.json`

**Step 2 — Print the two files** so you can copy their contents:
```bash
cat .cache/robinhood_oauth/client_info.json
cat .cache/robinhood_oauth/tokens.json
```

**Step 3 — Add them to Streamlit secrets.** In the app →
**⋮ → Settings → Secrets**, use this exact structure. The `'''`
triple-quotes let each JSON blob span multiple lines:
```toml
ANTHROPIC_API_KEY = "sk-ant-...your key..."
FINNHUB_API_KEY = "...your finnhub key..."

[robinhood]
client_info = '''
{ ...paste the entire contents of client_info.json here... }
'''
tokens = '''
{ ...paste the entire contents of tokens.json here... }
'''
```

**Step 4 — Save.** Streamlit reboots. With a `[robinhood]` section present,
the app switches to **headless** mode and uses those tokens — the sidebar
banner disappears and portfolio questions work on the cloud.

**Token expiry.** Robinhood access tokens last ~9 days, and refreshes are
**not** persisted on the cloud (the disk is wiped on every reboot). When
the token expires the app shows a "re-authenticate locally and update the
tokens" message — at that point, re-run the local connect, re-copy
`tokens.json`, and paste it into secrets again.

**Force-disable switch.** If you've pasted tokens but want to temporarily
turn Robinhood off (e.g. for a public demo), add this to secrets:
```toml
DISABLE_ROBINHOOD = "1"
```

### 12.5 Redeploying after changes

Streamlit Cloud **auto-redeploys** — there's no deploy button to re-click.
Push a new commit to the watched branch (`main`) and it rebuilds within a
minute. To force it (e.g. after only changing secrets): app →
**⋮ → Reboot app**.

---

## 13. Troubleshooting

| Problem | Fix |
|---|---|
| `python3: command not found` | Python isn't on your PATH. Reinstall and check "Add to PATH" (Windows) or use `brew install python` (Mac). |
| `ANTHROPIC_API_KEY is required` | Your `.env` is missing the key or has a typo. Check for stray spaces. |
| Finnhub `429 Too Many Requests` | You hit the 60-calls/min free limit. Wait a minute; the app caches and throttles, so it recovers on its own. |
| Robinhood browser login didn't return | The script prints a URL — paste it manually. If tokens expired, delete `.cache/robinhood_oauth/` and reconnect. |
| Streamlit `ModuleNotFoundError: src` | Run via `./scripts/run_ui.sh` (it sets the path correctly), not `streamlit run` directly. On the cloud, the app adds the repo root to `sys.path` itself. |
| First query takes ~60s | Normal — cold cache. Subsequent queries are fast. |
| `401 invalid x-api-key` | Your Anthropic key is wrong, revoked, or has a **space after the `=`** in `.env`. Also check you don't have a stale `ANTHROPIC_API_KEY` exported in your shell (`echo $ANTHROPIC_API_KEY`) — a shell export overrides `.env`. Fully restart the app after fixing. |
| Cloud: keys ignored / "required" error | Streamlit Cloud puts secrets in `st.secrets`, not env vars. The app bridges them automatically — make sure the secret names match exactly (`ANTHROPIC_API_KEY`, `FINNHUB_API_KEY`). |
| Cloud: `OSError: Address already in use` / OAuth crash | Robinhood tried its browser flow on the cloud (no tokens supplied). Either deploy disabled (§12.2) or supply tokens (§12.4). |
| Claude wants to run a command I don't understand | Ask it: *"what does this command do and is it safe?"* before approving. |

---

## A note on philosophy

You didn't write much code in this runbook — you described what you
wanted, reviewed what Claude proposed, and steered it. That's the point.

**The human stays in the loop at every stage.** You define the goal, you
review the design, you approve the code, you decide what ships. The AI
does the heavy lifting; the judgment stays with you.

That pattern — *describe, review, steer, approve* — works for far more
than a stock advisor. Pick your own problem and try it.
