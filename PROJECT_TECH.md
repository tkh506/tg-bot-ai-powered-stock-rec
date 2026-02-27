# PROJECT_TECH.md — AI Investment Advisor: Full Technical Reference

This file contains all technical detail for the project. See `CLAUDE.md` for the project overview,
principles, and directory structure. See `DEPLOY_GUIDE.md` for deployment and upgrade procedures.

---

## Tech Stack

| Component | Choice |
|---|---|
| Language | Python 3.11+ (tested on 3.13 locally; requires pydantic ≥ 2.10.6) |
| AI model | `anthropic/claude-sonnet-4-6` via OpenRouter |
| Market data | yfinance (stocks + gold OHLCV) |
| News | NewsAPI + Finnhub + Marketaux + NewsData.io + Alpha Vantage (sentiment) + RSS (Phase 1 fallback) |
| Fundamentals | Finnhub (PE, 52W range, beta, analyst consensus — US stocks; price targets: premium only) |
| Economic data | FRED (CPI, unemployment, fed funds rate, treasury yields, GDP) — Phase 1 |
| Social sentiment | Adanos (Reddit/X.com/Polymarket via /compare batch) + ApeWisdom (Reddit trending rank) |
| Scheduler | systemd timer (primary); APScheduler (`--schedule` flag, fallback) |
| Notifications | python-telegram-bot (report delivery + ad-hoc `/report` command listener) |
| Config | PyYAML + Pydantic v2 + python-dotenv |
| Storage | SQLite (`data/reports.db`) + Markdown archive (`data/archive/`) |
| Deployment | GCP e2-small VM, Debian 12, `asia-east2` |

---

## Configuration

### `config/config.yaml` — the main config file you edit regularly


Key sections:

```yaml
risk:
  appetite: "moderate"   # conservative | moderate | aggressive
  # conservative → BUY/SELL only at confidence ≥ 75
  # moderate     → BUY/SELL only at confidence ≥ 60
  # aggressive   → BUY/SELL only at confidence ≥ 45

schedule:
  systemd_oncalendar: "*-*-* 10:00:00 UTC"  # = 18:00 HKT daily (every day)

discovery:
  max_candidates: 10         # Stage 1 AI: max stocks to select from ApeWisdom top 100
  max_recommendations: 5     # Stage 2 AI: max final recommendations in report
  always_include_gold: true  # Gold (GC=F) always passed to Stage 2 regardless of Stage 1
  apewisdom_top_n: 100       # How many ApeWisdom trending entries shown to Stage 1 AI
```

### `.env` — secrets (never commit this file)

```
# Required
OPENROUTER_API_KEY=sk-or-v1-...
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=-100123456789     # negative = group chat

# Optional — all degrade gracefully if absent
NEWSAPI_KEY=...
ALPHAVANTAGE_KEY=...
FINNHUB_API_KEY=...        # https://finnhub.io/dashboard (free)
MARKETAUX_API_KEY=...      # https://www.marketaux.com (free)
NEWSDATA_API_KEY=...       # https://newsdata.io/api-key (free, 200 credits/day)
FRED_API_KEY=...           # https://fred.stlouisfed.org/docs/api/api_key.html (free)
ADANOS_API_KEY=...         # https://adanos.org (free, 250 req/month)
# ApeWisdom: no key needed

# Bot listener — ad-hoc /report command authorization
TELEGRAM_OWNER_USER_ID=YOUR_NUMERIC_USER_ID   # message @userinfobot to find yours
```

### `config/prompts.yaml` — AI prompt templates

- `discovery_system_prompt`: Stage 1 analyst persona — selects candidates from ApeWisdom list only
- `discovery_user_template`: Stage 1 user message — ApeWisdom table + FRED + macro headlines
- `system_prompt`: Stage 2 analyst persona — mandates JSON output schema
- `risk_constraints`: per-appetite behavioural rules injected into the Stage 2 system prompt
- `user_message_template`: Stage 2 user message — per-asset data blocks with prices, technicals, sentiment, news
- `retry_suffix`: appended when a previous AI response was invalid JSON

To tune AI behaviour (verbosity, format, focus areas) — edit `prompts.yaml`, not Python code.

---

## Data Flow (one pipeline run)

```
systemd timer fires (daily 10:00 UTC = 18:00 HKT)
  └→ src/main.py run()
       │
       ├── PHASE 1: fetcher.fetch_broad_market_data()    [ThreadPoolExecutor, 8 workers]
       │    ├── apewisdom_client    → top 100 trending stocks (no auth)
       │    ├── rss_client          → macro headlines (BBC Business + MarketWatch)
       │    ├── fred_client         → economic indicators (CPI, unemployment, yields, GDP)
       │    └── yfinance_client     → Gold (GC=F) OHLCV only
       │
       ├── STAGE 1 AI: prompt_builder.build_discovery_prompt() → ai_client.call_with_retry()
       │    └── response_parser.parse_candidates() → DiscoveryResult (up to 10 Candidates)
       │         Anti-hallucination: each ticker validated against ApeWisdom snapshot dict
       │         Gold added programmatically after validation (bypasses ApeWisdom check)
       │         Retries up to max_retries if ParseError
       │
       ├── PHASE 2: fetcher.fetch_targeted_data(candidates)    [ThreadPoolExecutor, 20 workers]
       │    ├── yfinance_client      → OHLCV batch for all candidates + Gold [single batch call]
       │    ├── newsapi_client       → headlines per stock (falls back to RSS on quota)
       │    ├── alphavantage_client  → news sentiment (US stocks only; 25 req/day limit)
       │    ├── finnhub_client       → stock news + fundamentals per ticker
       │    ├── marketaux_client     → financial news per ticker
       │    ├── newsdata_client      → latest news per keyword (rate limited: 60 req/min)
       │    ├── adanos_client        → Reddit/X/Polymarket sentiment batch (3 calls total)
       │    └── apewisdom_client     → O(1) lookup from Phase 1 snapshot (no re-fetch)
       │
       ├── STAGE 2 AI: prompt_builder.build() → ai_client.call_with_retry()
       │    └── response_parser.parse() → AnalysisResult (max 5 recommendations)
       │         Retries up to max_retries if ParseError; bad response fed back into next prompt
       │
       ├── PHASE 3: formatter.render()
       │    └── Telegram Markdown string(s), split at 4096 chars
       │         Includes discovery summary + candidate list header
       │
       ├── PHASE 4: telegram_bot.send_report()
       │    └── bot.send_message() per part
       │
       └── PHASE 5: archiver.save()
            ├── SQLite: reports + signals tables (token counts: Stage 1 + Stage 2 summed)
            └── data/archive/YYYY-MM-DD_HHMMSS.md
```

---

## API Calls by Phase

### Phase 1 — Broad Market Scan (non-asset-specific)

| Source | Client | Auth | Call count | Notes |
|---|---|---|---|---|
| ApeWisdom | `apewisdom_client.py` | None | 1 | Top 100 trending US stocks |
| RSS | `rss_client.py` | None | 2 | BBC Business + MarketWatch macro headlines |
| FRED | `fred_client.py` | `FRED_API_KEY` | 6 | CPI, unemployment, fed funds rate, 10Y yield, 2Y yield, GDP |
| yfinance | `yfinance_client.py` | None | 1 batch | Gold (GC=F) OHLCV only |

**Total Phase 1 API calls: ~10** (all parallel, 8 workers)

### Phase 2 — Deep Dive (per discovered candidate, ~10 stocks + Gold)

| Source | Client | Auth | Call count | Notes |
|---|---|---|---|---|
| yfinance | `yfinance_client.py` | None | 1 batch | All candidates + Gold in single download |
| NewsAPI | `newsapi_client.py` | `NEWSAPI_KEY` | ~10 | Per-stock; falls back to RSS if quota hit |
| Alpha Vantage | `alphavantage_client.py` | `ALPHAVANTAGE_KEY` | ~10 | US stocks only; 25 req/day free limit |
| Finnhub | `finnhub_client.py` | `FINNHUB_API_KEY` | ~20 | News + metrics per ticker (2 calls each) |
| Marketaux | `marketaux_client.py` | `MARKETAUX_API_KEY` | ~10 | Per-ticker financial news |
| NewsData.io | `newsdata_client.py` | `NEWSDATA_API_KEY` | ~11 | Per keyword; rate-limited to 60 req/min |
| Adanos | `adanos_client.py` | `ADANOS_API_KEY` | 3 | Batch: reddit_stocks, x_stocks, polymarket_stocks |
| ApeWisdom | — | — | 0 | O(1) dict lookup from Phase 1 snapshot; no re-fetch |

**Total Phase 2 API calls: ~65** (all parallel, 20 workers; many are optional/degrade gracefully)

> **Tip**: Sources are visible in logs at INFO level. Phase 2 data sources appear in the final
> `Sources: [...]` line of each run's log summary. Missing API keys skip the source silently.

---

## AI Output Schema

The Stage 2 AI is instructed to return this exact JSON structure:

```json
{
  "run_date": "YYYY-MM-DD",
  "risk_profile": "conservative|moderate|aggressive",
  "macro_summary": "2-3 sentence macro overview",
  "portfolio_bias": "bullish|neutral|bearish",
  "assets": [
    {
      "ticker": "string",
      "name": "string",
      "asset_type": "stock|crypto|forex|commodity",
      "signal": "BUY|HOLD|SELL",
      "confidence": 0-100,
      "current_price": "string with currency",
      "target_price": "string or null",
      "stop_loss": "string or null",
      "justification": "max 3 sentences",
      "key_risks": ["risk1", "risk2"],
      "time_horizon": "short|medium|long",
      "sentiment_score": "positive|neutral|negative"
    }
  ],
  "disclaimer": "string"
}
```

Validation is strict in `response_parser.py` — invalid signals, out-of-range confidence, or missing
required fields all raise `ParseError` and trigger a retry.

The Stage 1 AI returns a simpler schema validated by `parse_candidates()`:

```json
{
  "discovery_summary": "2-3 sentence overview of market trends driving selection",
  "candidates": [
    { "ticker": "AAPL", "name": "Apple Inc.", "rationale": "..." }
  ]
}
```

---

## Running the Tool

### Local development

```bash
# Install dependencies
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Copy and fill in secrets
cp .env.example .env
# ... edit .env ...

# Dry run: full pipeline but prints to stdout instead of sending to Telegram
python -m src.main --dry-run

# Normal run (sends to Telegram)
python -m src.main

# APScheduler mode (non-systemd environments)
python -m src.main --schedule

# Run the bot listener locally (for testing /report command)
python -m src.notifications.bot_listener
```

### Ad-Hoc Reports via Telegram

Send `/report` as a **private message to the bot** to trigger an immediate pipeline run.
The report is delivered to the group chat (`TELEGRAM_CHAT_ID`), same as scheduled runs.

**Authorization**: only the user whose numeric ID matches `TELEGRAM_OWNER_USER_ID` in `.env`
is allowed. All other users are silently ignored (no response, no error — bot appears unresponsive).

**How to find your Telegram user ID**: message `@userinfobot` on Telegram — it replies with your numeric ID.

**Architecture**: the listener runs as a separate persistent systemd service
(`ai-investment-advisor-listener.service`) alongside the one-shot pipeline service. It uses a
sudoers rule to call `systemctl start ai-investment-advisor.service` without a password.

### On the GCP VM

```bash
# Manual trigger (for testing)
sudo systemctl start ai-investment-advisor.service

# Watch live logs
journalctl -u ai-investment-advisor -f

# Check timer status
systemctl list-timers ai-investment-advisor.timer

# Tail the rotating log file
tail -f /opt/ai-investment-advisor/logs/advisor.log

# Query recent reports from SQLite
sqlite3 /opt/ai-investment-advisor/data/reports.db \
  "SELECT run_at, portfolio_bias, error_flag FROM reports ORDER BY run_at DESC LIMIT 5;"

# Query recent signals for a ticker
sqlite3 /opt/ai-investment-advisor/data/reports.db \
  "SELECT run_at, signal, confidence FROM signals WHERE ticker='AAPL' ORDER BY run_at DESC LIMIT 10;"

# ── Bot listener management ────────────────────────────────────────────────────
# Check listener status
systemctl status ai-investment-advisor-listener.service

# Watch listener logs live
journalctl -u ai-investment-advisor-listener -f

# Restart listener (e.g. after .env change)
sudo systemctl restart ai-investment-advisor-listener.service
```

### Reloading after config changes

```bash
# On GCP VM — after editing config.yaml or .env
sudo systemctl restart ai-investment-advisor.timer
sudo systemctl restart ai-investment-advisor-listener.service
# No daemon-reload needed unless you changed a .service or .timer file

# If you changed a .service or .timer file
sudo systemctl daemon-reload
sudo systemctl restart ai-investment-advisor.timer
sudo systemctl restart ai-investment-advisor-listener.service
```

---

## SQLite Schema

```sql
-- One row per pipeline run
reports (id, run_at, risk_profile, macro_summary, portfolio_bias,
         report_md, token_input, token_output, data_sources_used, error_flag)

-- One row per asset per run
signals (id, report_id, run_at, ticker, asset_type, signal, confidence,
         current_price, target_price, stop_loss, justification,
         time_horizon, sentiment_score)

-- Indexes
idx_signals_ticker ON signals(ticker, run_at)
idx_reports_run_at ON reports(run_at)
```

`token_input` and `token_output` are the **summed** token counts from Stage 1 + Stage 2 AI calls.

---

## Error Handling Philosophy

| Error type | Behaviour |
|---|---|
| Single data source fails | Warning logged; other sources continue |
| NewsAPI quota exhausted | Automatically falls back to RSS client |
| AI returns invalid JSON | Retry up to `ai.max_retries` times; bad response fed back into next prompt |
| OpenRouter rate limit (429) | Exponential backoff retry |
| Total pipeline failure | ERROR logged + Telegram error alert sent; exits 0 (timer keeps running) |
| Config validation error | CRITICAL logged; exits 1 (stops the timer — human must fix) |
| Finnhub 403 (price targets) | Logged at DEBUG — premium endpoint, expected on free tier |
| NewsData 422 (timeframe) | Would cause 422 — `timeframe` param is paywalled, never sent |

---

## Tuning Discovery

The discovery pipeline is controlled via `config/config.yaml` under `discovery:`:

```yaml
discovery:
  max_candidates: 10         # Raise to see more stocks in Stage 2 (increases API calls + cost)
  max_recommendations: 5     # Max final recommendations in the Telegram report
  always_include_gold: true  # Set false to remove Gold if not interested
  apewisdom_top_n: 100       # Reduce to constrain Stage 1 AI to a smaller trending universe
```

To adjust what Stage 1 AI selects for (e.g. focus on momentum vs. value), edit
`discovery_system_prompt` in `config/prompts.yaml`.

**Ticker format guide (for reference — used internally):**
- US stocks: standard ticker (e.g. `NVDA`, `AAPL`) — discovered via ApeWisdom
- Gold futures: `GC=F` (added programmatically, not from ApeWisdom)

---

## Changing the Schedule

Edit `config/config.yaml` and the systemd timer:

```yaml
schedule:
  systemd_oncalendar: "*-*-* 10:00:00 UTC"  # = 18:00 HKT daily — change UTC time here
```

Then on the VM:
```bash
sudo nano /etc/systemd/system/ai-investment-advisor.timer
# Edit OnCalendar= line
sudo systemctl daemon-reload
sudo systemctl restart ai-investment-advisor.timer
```

UTC offset reference:
- HKT (UTC+8): 18:00 HKT = 10:00 UTC
- For weekdays only: `Mon-Fri 10:00:00 UTC`
- For twice daily: use two `OnCalendar=` lines

---

## Changing Risk Appetite

Edit `config/config.yaml`:
```yaml
risk:
  appetite: "aggressive"   # conservative | moderate | aggressive
```

The risk constraints (confidence thresholds, language) are defined in `config/prompts.yaml`
under `risk_constraints`. Edit there to fine-tune what each appetite level means to the AI.

---

## Tuning the AI Prompt

All prompt text lives in `config/prompts.yaml`. Key things to edit:

- **`discovery_system_prompt`**: Stage 1 persona — what the AI looks for when selecting candidates
- **`discovery_user_template`**: Stage 1 data layout — how ApeWisdom + FRED + headlines are presented
- **`system_prompt`**: Stage 2 analyst persona, output format rules
- **`risk_constraints.{appetite}`**: what each risk level means behaviourally
- **`user_message_template`**: how per-asset data is presented to Stage 2
- **`retry_suffix`**: what's appended when asking the AI to fix a bad response

After editing prompts, test with `python -m src.main --dry-run`.

---

## Testing

```bash
# Run all tests
pytest tests/ -v

# Run a specific test file
pytest tests/test_response_parser.py -v

# Run with coverage
pytest tests/ --cov=src --cov-report=term-missing
```

Test files:
- `test_response_parser.py` — validates JSON parsing, error cases, Stage 1 + Stage 2 parsers
- `test_formatter.py` — Telegram Markdown rendering, message splitting
- `test_config_loader.py` — Pydantic config validation
- `test_rss_client.py` — RSS fetch and keyword filtering (mocked)

---

## API Keys & Costs (approximate)

| Service | Tier | Cost | Limit | Key env var |
|---|---|---|---|---|
| OpenRouter (Claude Sonnet 4.6) | Pay-per-use | ~$0.003–0.006/run | — | `OPENROUTER_API_KEY` |
| NewsAPI | Free | $0 | 100 req/day | `NEWSAPI_KEY` |
| yfinance | Free | $0 | No hard limit | — |
| Alpha Vantage | Free | $0 | 25 req/day, 1 req/sec | `ALPHAVANTAGE_KEY` |
| Telegram Bot API | Free | $0 | — | `TELEGRAM_BOT_TOKEN` |
| Finnhub | Free | $0 | 60 req/min | `FINNHUB_API_KEY` |
| Marketaux | Free | $0 | ~100 req/month | `MARKETAUX_API_KEY` |
| NewsData.io | Free | $0 | 200 credits/day | `NEWSDATA_API_KEY` |
| FRED | Free | $0 | 120 req/min | `FRED_API_KEY` |
| Adanos | Free | $0 | 250 req/month (uses 3/run) | `ADANOS_API_KEY` |
| ApeWisdom | Free | $0 | No key required | — |
| GCP e2-small VM | — | ~$13–17/month | — | — |

**Monthly estimate (weekdays only, ~22 runs/month):** < $15 total (mostly VM cost).

OpenRouter cost is slightly higher than v0.2.0 (~+$0.001/run) due to the additional Stage 1 AI call.

---

## GCP VM Reference

- **Machine type**: `e2-small` (2 vCPU, 2 GB RAM)
- **OS**: Debian 12 Bookworm
- **Region**: `asia-east2` (Hong Kong)
- **App path**: `/opt/ai-investment-advisor/`
- **App user**: `advisor` (system user, no shell)
- **Python env**: `/opt/ai-investment-advisor/venv/`
- **Secrets**: `/opt/ai-investment-advisor/.env` (chmod 600)
- **Logs**: `/opt/ai-investment-advisor/logs/advisor.log` + `journalctl -u ai-investment-advisor`

---

## Key Conventions

- All prompt text is in `config/prompts.yaml` — no hardcoded prompt strings in Python
- All config is in `config/config.yaml` — no magic numbers in Python
- All secrets are in `.env` — no secrets in YAML or Python
- Data failures are non-fatal — the pipeline runs even with partial data
- AI JSON is always validated before use — no blind trust of model output
- Telegram messages fall back to plain text if Markdown parsing fails
- Token usage logged per run to `reports.token_input / token_output` for cost tracking
- **yfinance**: always use single batch call — never parallel per-ticker (Yahoo rate limit + cache lock)
- **Adanos**: always use `/compare` endpoint to batch all tickers in one call per source — stays within 250 req/month free tier. US stocks only (HK tickers not covered by Adanos).
- **ApeWisdom**: fetch full trending list once in Phase 1, look up tickers by dict key in Phase 2 — no per-ticker calls, no re-fetch
- **Alpha Vantage + Finnhub metrics**: US/non-HKEX stocks only — HK tickers not supported
- **NewsData.io `timeframe` param**: paywalled — never send it; `/latest` returns newest-first by default
- **Finnhub `/stock/price-target`**: premium endpoint — always 403 on free tier; logged at DEBUG
- **TokenBucket `max_burst=1`**: prevents token accumulation during Phase 1 + Stage 1 AI idle period; eliminates burst at Phase 2 start
- New data source keys are all optional in `.env` (`None` default) — source is skipped silently if key is absent

---

## Planned Enhancements (Next Sessions)

Items scoped but not yet implemented — pick up from here in future sessions.

### Data source refinements
- **Fear & Greed Index** — CNN Money API (free, no key) to enrich macro context for equity bias; complements existing FRED data
- **Economic calendar** — lightweight source for upcoming macro events (Fed decisions, CPI release dates) to give the AI forward-looking context; FRED does not include scheduled event dates
- **Additional RSS feeds** — FT Markets or WSJ Markets for broader macro coverage; validate with `requests`+feedparser before adding to `config.yaml`

### Signal / asset tuning
- **Add more commodities** — Silver (`SI=F`), Oil WTI (`CL=F`) are already supported by yfinance; add programmatically in `main.py` alongside Gold or add a `commodities:` list to `discovery:` config
- **Confidence floor per asset type** — consider separate confidence thresholds for commodities vs stocks in `prompts.yaml` risk constraints

### Quality / observability
- **Token usage trending** — query `reports.token_input / token_output` over time to monitor prompt growth as new data sources are added; trim if approaching model context limits
- **Stage 1 candidate hit rate** — track how often Stage 1 candidates make it into Stage 2 final recommendations; low hit rate may indicate Stage 1 prompt needs tuning
