# CLAUDE.md — AI Investment Advisor

This file is the canonical reference for Claude (and developers) working on this project.
Update it whenever the architecture, config schema, or key conventions change.

---

## Overarching Principles for All Sessions

These rules apply to **every Claude session** on this project, regardless of the task.

### Session Start Checklist
At the beginning of **every session**, read these three files before doing anything else:
1. `CLAUDE.md` — architecture, conventions, and these principles
2. `CHANGELOG.md` — what has changed recently
3. `LESSONS.md` — mistakes made and rules learned from past sessions

### Core Principles

- **Simplicity First** — Make every change as simple as possible. Minimise the code impacted.
- **No Laziness** — Find root causes. No temporary fixes. Senior developer standards at all times.
- **Minimal Impact** — Changes should only touch what is necessary. Avoid introducing side-effects or bugs elsewhere.

### Workflow Orchestration

#### 1. Plan-First Default
- Enter plan mode for **any non-trivial task** (3+ steps or any architectural decision)
- If something goes sideways mid-task, **STOP and re-plan immediately** — don't keep pushing
- Use plan mode for verification steps, not just building
- Write detailed specs upfront to reduce ambiguity

#### 2. Subagent Strategy
- Use subagents liberally to keep the main context window clean
- Offload research, exploration, and parallel analysis to subagents
- For complex problems, throw more compute at it via subagents
- One task per subagent for focused execution

#### 3. Self-Improvement Loop
- After **any correction** from the user: update `LESSONS.md` with the pattern
- Write rules for yourself that prevent the same mistake recurring
- Ruthlessly iterate on these lessons until mistake rate drops
- Review `LESSONS.md` at the start of each session for relevant patterns

#### 4. Verification Before Done
- **Never mark a task complete without proving it works**
- Diff behaviour between `main` and your changes when relevant
- Ask yourself: *"Would a staff engineer approve this?"*
- Run tests, check logs, demonstrate correctness

#### 5. Demand Elegance (Balanced)
- For non-trivial changes: pause and ask *"Is there a more elegant way?"*
- If a fix feels hacky: *"Knowing everything I know now, implement the elegant solution"*
- Skip this for simple, obvious fixes — don't over-engineer
- Challenge your own work before presenting it

#### 6. Autonomous Bug Fixing
- When given a bug report: **just fix it** — don't ask for hand-holding
- Point at logs, errors, and failing tests — then resolve them
- Zero context switching required from the user
- Go fix failing CI tests without being told how

### Task Management Workflow

1. **Plan First** — Write plan to `tasks/todo.md` with checkable items
2. **Verify Plan** — Check in with the user before starting implementation
3. **Track Progress** — Mark items complete as you go
4. **Explain Changes** — Provide a high-level summary at each step
5. **Document Results** — Add a review section to `tasks/todo.md` when done
6. **Capture Lessons** — Update `LESSONS.md` after any user corrections

---

## Project Overview

An automated, AI-powered investment advisor that discovers and analyses US stock opportunities daily.

**Two-stage pipeline (v0.3.0+):**
1. **Phase 1 — Broad Scan**: Fetches non-asset-specific data (ApeWisdom Reddit trending top 100, FRED economic indicators, RSS macro headlines, Gold price). A **Stage 1 AI call** selects up to 10 candidate US stocks from the ApeWisdom list.
2. **Phase 2 — Deep Dive**: Fetches full asset-specific data (news, sentiment, fundamentals, OHLCV) for all discovered candidates + Gold. A **Stage 2 AI call** produces up to 5 final **BUY / HOLD / SELL** recommendations.
3. Delivers the structured report to a **Telegram bot** and archives every run to **SQLite + Markdown files**.

**Asset scope:** US stocks (dynamically discovered from Reddit trending) + Gold (always-on commodity).

Runs as a **systemd one-shot service** on a **GCP Compute Engine VM** (Debian 12, `asia-east2`/Hong Kong).

> For full technical reference (config schema, data flow, API details, ops commands), see `PROJECT_TECH.md`.

---

## Directory Structure

```
ai-investment-advisor/
├── config/
│   ├── config.yaml              # EDIT THIS — discovery, schedule, risk, API settings
│   ├── config.example.yaml      # Documented template (safe to commit)
│   └── prompts.yaml             # AI system + user prompt templates (Stage 1 + Stage 2)
│
├── src/
│   ├── main.py                  # Entry point — orchestrates the full two-stage pipeline
│   ├── data/
│   │   ├── fetcher.py           # Phase 1 (8 workers) + Phase 2 (20 workers) orchestration
│   │   ├── yfinance_client.py   # Stocks + Gold OHLCV (single batch call)
│   │   ├── coingecko_client.py  # Crypto prices — DISABLED (crypto removed in v0.3.0)
│   │   ├── newsapi_client.py    # News headlines (with quota error detection)
│   │   ├── alphavantage_client.py # News sentiment scores (US stocks only)
│   │   ├── rss_client.py        # RSS feeds (BBC Business, MarketWatch) — Phase 1 always-on
│   │   ├── finnhub_client.py    # Stock news + fundamentals (PE, analysts; price targets: premium only)
│   │   ├── marketaux_client.py  # Financial news filtered by ticker symbol
│   │   ├── newsdata_client.py   # Latest news by keyword (rate limited: 60 req/min)
│   │   ├── fred_client.py       # US economic indicators — Phase 1 (CPI, unemployment, yields, GDP)
│   │   ├── adanos_client.py     # Reddit/X.com/Polymarket sentiment — /compare batch (3 calls/run)
│   │   └── apewisdom_client.py  # Reddit trending rank — Phase 1 fetch, Phase 2 O(1) lookup
│   ├── analysis/
│   │   ├── prompt_builder.py    # Stage 1: build_discovery_prompt() | Stage 2: build()
│   │   ├── ai_client.py         # OpenRouter API calls + retry on 429
│   │   └── response_parser.py   # Stage 1: parse_candidates() | Stage 2: parse()
│   ├── reporting/
│   │   ├── formatter.py         # Renders Telegram Markdown (splits at 4096 chars)
│   │   └── archiver.py          # SQLite insert + .md file write + old-file purge
│   ├── notifications/
│   │   ├── telegram_bot.py      # Send report parts + send error alerts
│   │   └── bot_listener.py      # Persistent listener: /report command → ad-hoc pipeline trigger
│   └── utils/
│       ├── config_loader.py     # Pydantic v2 models + .env merge → AppConfig
│       ├── logger.py            # Rotating file handler + stream handler
│       └── rate_limiter.py      # Token bucket (max_burst=1) + tenacity retry factory
│
├── data/
│   ├── reports.db               # SQLite database (auto-created on first run)
│   └── archive/                 # Markdown files: YYYY-MM-DD_HHMMSS.md
│
├── logs/
│   └── advisor.log              # Rotating log (10 MB × 5 = 50 MB max)
│
├── deploy/
│   ├── ai-investment-advisor.service           # systemd one-shot pipeline service
│   ├── ai-investment-advisor.timer             # systemd timer (daily 18:00 HKT)
│   ├── ai-investment-advisor-listener.service  # systemd persistent bot listener service
│   ├── install.sh               # Create user, venv, sudoers, register all systemd units
│   └── setup_gcp.sh             # Fresh VM bootstrap (run once as root)
│
├── tests/                       # pytest test suite
├── .env                         # Secrets — NEVER commit
├── .env.example                 # Secrets template — safe to commit
├── requirements.txt
├── LOCAL_SETUP.md               # Step-by-step local dev / test-run guide
├── LESSONS.md                   # Lessons learned across sessions — read at session start
├── CLAUDE.md                    # This file (slim — see PROJECT_TECH.md for full tech docs)
├── PROJECT_TECH.md              # Full technical reference: config, data flow, API details, ops
├── DEPLOY_GUIDE.md              # Step-by-step deployment and upgrade guide
└── CHANGELOG.md
```
