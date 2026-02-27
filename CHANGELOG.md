# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [0.5.0] — 2026-02-27

### Added
- **After-hours / pre-market price enrichment** — the pipeline now fetches the latest extended-hours price for every ticker immediately after the historical OHLCV batch. A second `yf.download(period="1d", interval="1m", prepost=True)` call is made as a single batch (never parallel, preserving LESSON 4). For each ticker, the last bar's timestamp is checked against US/Eastern market hours:
  - Before 09:30 ET → labelled `"pre-market"`
  - At/after 16:00 ET → labelled `"after-hours"`
  - Within regular hours → no extended price (fallback to regular close)
- **`OHLCVData`** — three new optional fields added to the dataclass: `extended_price: Optional[float]`, `extended_pct: Optional[float]` (% change vs regular close), `extended_label: Optional[str]` (`"after-hours"` or `"pre-market"`). All default to `None` — no breaking change.
- **`fetch_extended_prices(tickers)`** in `yfinance_client.py` — single-batch function returning `{ticker: (price, label) | None}`. Uses `zoneinfo.ZoneInfo` (stdlib, no new dependencies) for US/Eastern timezone classification.
- **`_merge_extended_prices(ohlcv_map, extended)`** in `fetcher.py` — merges extended-hours data into `OHLCVData` objects in place; also computes `extended_pct`.

### Changed
- **`fetch_broad_market_data()`** (Phase 1) — after Gold's historical OHLCV batch, calls `fetch_extended_prices([GC=F])` sequentially and merges into the Gold `OHLCVData`.
- **`fetch_targeted_data()`** (Phase 2) — after the main `ThreadPoolExecutor` block, calls `fetch_extended_prices(all_tickers)` sequentially. Since `yf_batch` holds the same `OHLCVData` object references as `raw_results`, the in-place update propagates to all downstream assembly automatically.
- **`prompt_builder.py`** — price block restructured in both `_build_stock_section` and `_build_commodity_section`: when extended price is available, shows `Current price: $X.XX (after-hours/pre-market, +Y% vs $Z.ZZ prev close)` first, then regular session data on the next line. Without extended data, falls back to a single `Current price:` line. Gold context in `build_discovery_prompt` updated the same way.
- **`formatter.py`** — `_signal_line()` now accepts an optional `ohlcv_map: dict[str, OHLCVData]`; when extended-hours data is present for a ticker, the `Price:` line in the Telegram report shows `$X.XX (+Y%) *after-hours* | Prev close: $Z.ZZ` sourced directly from the fetched data (not from the AI's JSON response). `render()` updated to accept and pass through `ohlcv_map`.
- **`main.py`** — builds `{ticker: OHLCVData}` from `MarketSnapshot` before calling `formatter.render()` and passes it as `ohlcv_map`.

---

## [0.4.0] — 2026-02-27

### Added
- **Ad-hoc `/report` command via Telegram** — send `/report` in a private chat with the bot to trigger an immediate pipeline run on demand. The report is delivered to the configured group chat, identical to a scheduled run. Only the owner (`TELEGRAM_OWNER_USER_ID`) can trigger runs; all other senders are silently ignored.
- **`src/notifications/bot_listener.py`** — persistent Telegram bot listener (polling mode). Validates sender identity against `TELEGRAM_OWNER_USER_ID`, then calls `sudo systemctl start ai-investment-advisor.service` via subprocess. Handles the "already running" case gracefully. Uses `drop_pending_updates=True` so queued commands from when the bot was offline are discarded.
- **`deploy/ai-investment-advisor-listener.service`** — new systemd `Type=simple` service for the persistent bot listener. Runs as `advisor` user, restarts automatically on failure (`RestartSec=30`), capped at 128 MB memory.
- **Sudoers rule** (`/etc/sudoers.d/advisor-trigger`) — allows the `advisor` system user to start `ai-investment-advisor.service` without a password, scoped to that single command only.
- **`TELEGRAM_OWNER_USER_ID`** — new required secret for the bot listener. Added to `Secrets` Pydantic model in `config_loader.py` and documented in `.env.example`. Find your ID by messaging `@userinfobot` on Telegram.
- **`deploy/install.sh`** updated — now also creates the sudoers rule, registers, enables, and starts the listener service. Prints listener status in the completion summary.

### Changed
- **Schedule changed to daily 18:00 HKT** (10:00 UTC) — previously Mon–Fri 08:00 HKT. Updated `deploy/ai-investment-advisor.timer`, `config/config.yaml` (`systemd_oncalendar`, `cron_hour`, `cron_days_of_week`).

---

## [0.3.1] — 2026-02-27

### Fixed
- **Adanos API response format changed** — Adanos updated their schema from `{"results": [...]}` to `{"stocks": [...]}` and renamed the sentiment field from `sentiment_score` to `sentiment`. Updated `adanos_client.py` to check for the `stocks` key first (with fallback to `results`/`data` for legacy compatibility) and map `item.get("sentiment") or item.get("sentiment_score")`.
- **NewsData.io `timeframe` parameter is paywalled** — Passing the `timeframe` parameter (in any format or value) causes HTTP 422 on the free tier ("Access Denied"). Removed the parameter entirely from the request in `newsdata_client.py`; the `/latest` endpoint returns newest articles first by default, giving equivalent behaviour. Added `@rate_limited(calls_per_minute=60, key="newsdata")` decorator to prevent 429s from the ~11 parallel Phase 2 calls firing simultaneously.
- **Finnhub `/stock/price-target` is a premium-only endpoint** — This endpoint always returns HTTP 403 on the free tier, producing 10 WARNING log lines per run (one per stock). Added an explicit 403 check before `raise_for_status()`; the endpoint is now logged at `DEBUG` instead of `WARNING`.
- **TokenBucket cross-phase accumulation burst** — `max_tokens` was previously set to `float(calls_per_minute)` (e.g. 20.0 for Alpha Vantage), allowing the bucket to accumulate up to 9 tokens during the ~24-second idle window between Phase 1 completion and Phase 2 start. This caused all 9 pending Alpha Vantage calls to fire simultaneously at Phase 2 start. Fixed by introducing a `max_burst: int = 1` parameter on both `TokenBucket.__init__()` and the `@rate_limited()` decorator; the default `max_burst=1` caps accumulation to 1 token, preventing inter-phase burst.

### Added
- **Success-level INFO logging for Marketaux and NewsData clients** — both clients previously only logged on failure (WARNING level), making them invisible in logs even when working correctly. Added `logger.info(f"Marketaux {ticker}: {len(results)} articles fetched")` and `logger.info(f"NewsData '{query}': {len(results)} articles fetched")` on successful fetch.

---

## [0.3.0] — 2026-02-26

### Changed (breaking — pipeline redesign)
- **Two-stage discovery pipeline** — the bot no longer analyses a fixed pre-configured list of assets. Each run now dynamically discovers investment targets:
  - **Stage 1 (Discovery)**: Bot fetches broad non-asset-specific data (ApeWisdom Reddit trending top 100, FRED economic indicators, RSS macro headlines, Gold OHLCV), sends it to AI, which selects up to 10 candidate stocks from the ApeWisdom list.
  - **Stage 2 (Deep Dive)**: Bot fetches full asset-specific data (OHLCV, news, sentiment, fundamentals) for all discovered candidates, sends everything to AI for final recommendations (max 5).
- **Asset scope narrowed to US stocks + Gold only** — HK stocks, crypto, and forex removed. New target universe: ApeWisdom top 100 Reddit trending US stocks + Gold (always-on commodity).
- **`config/config.yaml`** — removed entire `assets:` section (stocks/crypto/forex/commodities); added `discovery:` section with `max_candidates`, `max_recommendations`, `always_include_gold`, `apewisdom_top_n` settings; disabled CoinGecko and Reddit crypto.
- **`src/utils/config_loader.py`** — removed `AssetsConfig`, `CryptoAsset`, `ForexAsset` model classes; added `DiscoveryConfig` Pydantic model; `AppConfig.assets` replaced by `AppConfig.discovery`.
- **`src/data/fetcher.py`** — full rewrite: `fetch_all()` removed; replaced by `fetch_broad_market_data()` (Phase 1, 8 workers) and `fetch_targeted_data()` (Phase 2, 20 workers); added `BroadMarketData` dataclass; `MarketSnapshot` now contains only `stocks` + `commodities`; ApeWisdom snapshot from Phase 1 reused in Phase 2 (O(1) lookup, no re-fetch).
- **`src/analysis/prompt_builder.py`** — removed crypto/forex section builders; added `build_discovery_prompt()` for Stage 1 AI call; updated `build()` to use `max_recommendations` from config; added `_build_trending_table()` helper.
- **`src/analysis/response_parser.py`** — added `Candidate` and `DiscoveryResult` dataclasses; added `parse_candidates()` function for Stage 1 JSON validation.
- **`config/prompts.yaml`** — added `discovery_system_prompt` and `discovery_user_template` for Stage 1 AI; updated Stage 2 system prompt (selects best N from candidates, not every asset); removed crypto/forex placeholders from `user_message_template`.
- **`src/main.py`** — complete pipeline redesign: Phase 1 broad scan → Stage 1 AI (discovery with retry) → anti-hallucination validation → Phase 2 deep dive → Stage 2 AI (analysis with retry) → Phase 3/4/5 unchanged; token counts from both AI stages summed.
- **`src/reporting/formatter.py`** — added `_discovery_section()` showing which candidates were discovered and the AI's discovery summary; `render()` accepts optional `discovery_result` parameter; removed crypto/forex from type ordering.

### Added
- **Anti-hallucination safeguard** — after Stage 1, each AI-suggested ticker is validated against the ApeWisdom snapshot dict. Any ticker not present in the list is silently dropped before Phase 2. Gold is added programmatically, bypassing this filter.
- **`BroadMarketData` dataclass** — holds Phase 1 non-asset-specific data (ApeWisdom trending snapshot, macro headlines, FRED indicators, Gold OHLCV). Passed into `fetch_targeted_data()` to reuse already-fetched data.
- **`Candidate` / `DiscoveryResult` dataclasses** — represent Stage 1 AI output (selected tickers with rationale, discovery summary).
- **Gold always-on** — `Candidate("GC=F", "Gold", exchange="COMMODITY")` appended programmatically after Stage 1 when `discovery.always_include_gold: true` (default).

### Removed
- Fixed assets config (`assets:` section in `config.yaml`) — assets are now discovered dynamically each run.
- Crypto and forex data paths — `CoinGecko`, `ForexAsset`, forex prompt section, crypto prompt section all removed.
- `fetch_all()` function in `fetcher.py` — replaced by the two-phase fetch architecture.

---

## [0.2.0] — 2026-02-26

### Added
- **Finnhub client** (`src/data/finnhub_client.py`) — company news per stock ticker + financial metrics (PE ratio, 52W high/low, beta, analyst buy/hold/sell consensus, mean/high/low price targets). Metrics fetched for non-HKEX stocks only (consistent with Alpha Vantage scope).
- **Marketaux client** (`src/data/marketaux_client.py`) — financial news filtered by stock symbol/ticker, with per-entity sentiment scores where available.
- **NewsData.io client** (`src/data/newsdata_client.py`) — latest news by keyword query covering stocks, crypto, and commodities (business + technology categories, 24h lookback).
- **FRED client** (`src/data/fred_client.py`) — US economic indicators fetched in parallel: CPI (inflation), unemployment rate, fed funds rate, 10Y and 2Y treasury yields, GDP. Includes computed yield curve spread (10Y–2Y) with inversion flag.
- **Adanos client** (`src/data/adanos_client.py`) — Reddit, X.com, and Polymarket sentiment for stocks and crypto via the `/compare` batch endpoint (4 calls/run, well within the 250/month free tier). HK stocks skipped (not covered by Adanos).
- **ApeWisdom client** (`src/data/apewisdom_client.py`) — Reddit retail discussion rankings (rank, mentions, 24h rank change) for stocks and crypto. No API key required.
- **`EconomicIndicators` section in AI prompt** — FRED data rendered as a compact table showing current value, date, previous value, and delta for all configured series. Yield curve spread appended with normal/inverted label.
- **Fundamentals block per stock** — Finnhub metrics (PE, 52W range, beta, analyst consensus, price target) injected into each stock's prompt section.
- **Social Sentiment block per stock and crypto** — Adanos Reddit/X.com/Polymarket scores (buzz, sentiment, trend, mentions, bull/bear%) and ApeWisdom rank rendered per asset.
- **Consolidated multi-source news** — News from all sources (NewsAPI, RSS, Finnhub, Marketaux, NewsData.io) merged and deduplicated by title prefix (60-char key) before passing to the AI; up to 5 unique headlines per asset.
- **New Pydantic config models** in `config_loader.py`: `FinnhubConfig`, `MarketauxConfig`, `NewsDataConfig`, `FredSeriesConfig`, `FredConfig`, `AdanosConfig`, `ApeWisdomConfig`.
- **New optional secrets** in `Secrets`: `FINNHUB_API_KEY`, `MARKETAUX_API_KEY`, `NEWSDATA_API_KEY`, `FRED_API_KEY`, `ADANOS_API_KEY`. All `None`-defaulted; missing keys skip the corresponding source gracefully.
- **`config/config.yaml`** — new `finnhub`, `marketaux`, `newsdata`, `fred`, `adanos`, `apewisdom` sections with all config options.
- **`.env.example`** — documented entries for all 5 new API keys (ApeWisdom noted as keyless).
- **`ThreadPoolExecutor` workers increased** from 10 → 20 in `fetcher.py` to accommodate the larger parallel task set.
- **`{economic_indicators_section}` placeholder** added to `config/prompts.yaml` `user_message_template`.

---

## [0.1.6] — 2026-02-23

### Fixed
- **Alpha Vantage per-second burst warnings** — `calls_per_minute=30` (2s departure gap) was insufficient because AV measures *arrival* time, not departure. Network latency caused back-to-back calls to arrive within <1s at AV's servers, triggering the burst warning. Reduced to `calls_per_minute=20` (3s gap), which comfortably absorbs round-trip latency variance and keeps arrivals ≥1s apart at AV's end.

---

## [0.1.5] — 2026-02-23

### Fixed
- **RSS client SSL failure on macOS** — `rss_client.py` called `feedparser.parse(url)` directly, which uses Python's `urllib` for HTTP. On macOS (Python.org installer), `urllib` does not have the system CA bundle configured, causing `SSL: CERTIFICATE_VERIFY_FAILED` on every HTTPS RSS feed. feedparser swallowed the error and returned 0 entries silently, causing "No macro headlines are available for this session" on every run. Fixed by fetching via `requests` (which bundles `certifi` and handles SSL on all platforms) and passing `response.content` bytes to `feedparser.parse()`.
- **Dead Reuters RSS feed URLs** — `feeds.reuters.com` DNS no longer resolves; Reuters deprecated their legacy RSS infrastructure. Replaced both feeds in `config/config.yaml` with confirmed-working alternatives: BBC Business (`feeds.bbci.co.uk/news/business/rss.xml`, 50 items) and MarketWatch Pulse (`feeds.content.dowjones.io/public/rss/mw_marketpulse`, 30 items).

---

## [0.1.4] — 2026-02-23

### Fixed
- **yfinance outdated** — `yfinance 0.2.38` was using stale Yahoo Finance API endpoints, causing `JSONDecodeError('Expecting value: line 1 column 1 (char 0)')` for every ticker. Upgraded to `0.2.66` (latest 0.2.x stable) which tracks the current Yahoo Finance API.
- **Alpha Vantage rate limiter ineffective** — `TokenBucket` initialised with `tokens = calls_per_minute` (e.g. 5), so all simultaneous thread calls consumed tokens instantly and fired without any delay. Fixed by initialising `tokens = 1.0` so only one call fires immediately and subsequent calls wait for the bucket to refill. Also corrected the AV rate limit from `calls_per_minute=5` to `calls_per_minute=50` (1 token per 1.2 s), which matches the actual free-tier 1 req/sec allowance.

---

## [0.1.3] — 2026-02-23

### Fixed
- **yfinance rate limiting & cache lock** — `fetcher.py` was submitting one `ThreadPoolExecutor` task per ticker for yfinance (8 parallel downloads), causing Yahoo Finance to rate-limit requests (`JSONDecodeError`) and yfinance's internal SQLite cache to deadlock (`OperationalError: database is locked`). Fixed by replacing the per-ticker loop with a single `fetch_ohlcv_batch()` call that downloads all tickers in one `yf.download([...], group_by='ticker')` request. The batch result dict is expanded back into per-ticker keys before assembly, so all downstream logic is unchanged.

---

## [0.1.2] — 2026-02-23

### Added
- `LOCAL_SETUP.md` — step-by-step local dev setup guide: venv activation, `.env` creation, dependency install, dry-run and live-run commands, log/DB inspection, and troubleshooting table

### Fixed
- **Python 3.13 compatibility** — bumped `pydantic 2.6.4 → 2.10.6` and `pydantic-settings 2.2.1 → 2.7.1` in `requirements.txt`; `pydantic-core 2.16.3` had no pre-built wheel for Python 3.13 and failed to compile from source due to a `ForwardRef._evaluate()` API change in 3.13
- **Missing `ConfigError`** — `src/main.py` imported `ConfigError` from `src/utils/config_loader` but the class was never defined there; added `class ConfigError(Exception)` to `config_loader.py`
- **Missing `currency` field on `ForexAsset`** — `src/data/fetcher.py` accessed `asset.currency` on all forex assets but `ForexAsset` in `config_loader.py` had no such field; added `currency` field with a `model_validator` that auto-derives it from `quote` (e.g. `USDHKD=X` → `currency="HKD"`); no changes to `config.yaml` required

---

## [0.1.1] — 2026-02-23

### Added
- `CLAUDE.md` — added **Overarching Principles for All Sessions** section at the top of the file, covering: Core Principles (Simplicity First, No Laziness, Minimal Impact), Workflow Orchestration (Plan-First Default, Subagent Strategy, Self-Improvement Loop, Verification Before Done, Demand Elegance, Autonomous Bug Fixing), and Task Management Workflow. These apply to every future Claude session on this project.

---

## [0.1.0] — 2026-02-23

### Initial build — complete project scaffold

#### Added

**Project structure**
- Full directory layout: `src/`, `config/`, `data/`, `logs/`, `deploy/`, `tests/`
- `.gitignore` (excludes `.env`, `data/`, `logs/`, `venv/`)
- `.env.example` — template for all required API keys
- `requirements.txt` — pinned Python dependencies

**Configuration layer** (`config/`)
- `config/config.yaml` — main user-editable config: assets (stocks, crypto, forex, commodities), schedule, risk appetite, data source settings, AI settings, reporting settings
- `config/config.example.yaml` — fully documented template safe to commit
- `config/prompts.yaml` — all AI prompt text: system prompt, risk constraint rules per appetite level, user message template, asset section templates, retry suffix

**Utility modules** (`src/utils/`)
- `config_loader.py` — Pydantic v2 models for full config schema; merges `config.yaml` + `.env` into a single validated `AppConfig` object; raises `FileNotFoundError` / `ValidationError` on bad config
- `logger.py` — rotating file handler (10 MB × 5 backups) + stream handler captured by systemd journal
- `rate_limiter.py` — thread-safe token bucket decorator (`@rate_limited`) + tenacity retry factory (`make_retry`)

**Data clients** (`src/data/`)
- `yfinance_client.py` — fetches OHLCV for stocks, forex pairs, and commodities; computes RSI(14), MA(20), 5d/20d percent change, volume ratio; handles MultiIndex DataFrame from yfinance
- `coingecko_client.py` — batch fetch for crypto: price, 24h/7d change, market cap, volume, rank; respects free-tier 30 req/min via `@rate_limited`
- `newsapi_client.py` — fetches recent news headlines per asset query; detects quota exhaustion (HTTP 426/429 + error codes) and raises `NewsAPIQuotaExhausted` for fallback logic
- `alphavantage_client.py` — fetches `NEWS_SENTIMENT` per US stock ticker; computes overall sentiment label, score, and bull/neutral/bear counts; handles AV quota responses gracefully
- `rss_client.py` — parses RSS feeds via `feedparser`; keyword-filters by asset name/ticker; fetches macro headlines without filtering; used as always-on fallback and supplemental source

**Data orchestrator** (`src/data/fetcher.py`)
- Runs all data clients in parallel via `ThreadPoolExecutor` (up to 10 workers)
- Graceful degradation: a failed source logs a warning and contributes empty data; pipeline continues
- Auto-falls back to RSS news if NewsAPI quota is exhausted
- Returns a `MarketSnapshot` dataclass with all asset data grouped by type
- Alpha Vantage sentiment only requested for US stocks (not HKEX; AV does not support HK tickers)

**Analysis layer** (`src/analysis/`)
- `prompt_builder.py` — builds the system + user prompt from `MarketSnapshot` and `AppConfig`; formats per-asset blocks with prices, technicals, sentiment, headlines; injects risk constraints; appends retry suffix on parse-error retries
- `ai_client.py` — calls `openrouter.ai/api/v1/chat/completions` with `response_format: json_object`; handles HTTP 429 with exponential backoff; returns (response_text, input_tokens, output_tokens)
- `response_parser.py` — strictly validates AI JSON against schema; validates signal values, confidence range, required fields; raises `ParseError` with descriptive messages for retry logic

**Reporting layer** (`src/reporting/`)
- `formatter.py` — renders `AnalysisResult` as Telegram Markdown; groups assets by type (stocks → crypto → forex → commodities); uses signal emoji (🟢/🟡/🔴); auto-splits into multiple messages when > 4096 chars; falls back gracefully if fields are absent
- `archiver.py` — writes Markdown file to `data/archive/YYYY-MM-DD_HHMMSS.md`; inserts report row into SQLite `reports` table and per-asset rows into `signals` table; purges archive files older than `retention_days`; creates SQLite schema on first run

**Notifications** (`src/notifications/telegram_bot.py`)
- Sends multi-part reports via `python-telegram-bot` async API wrapped in `asyncio.run()`
- Falls back to plain text if Markdown parse mode fails
- `send_error_alert()` for pipeline failure notifications

**Main orchestrator** (`src/main.py`)
- `--dry-run` flag: runs the full pipeline but prints report to stdout instead of sending to Telegram
- `--schedule` flag: runs on APScheduler (fallback for non-systemd environments)
- Top-level error handling: `PipelineError` → Telegram error alert + exit 0 (timer continues); `ConfigError` → exit 1 (stops timer)
- Retry loop for AI parse errors: up to `ai.max_retries` attempts; previous bad response fed back into next prompt

**Deployment** (`deploy/`)
- `ai-investment-advisor.service` — systemd `Type=oneshot` service; runs as `advisor` system user; `EnvironmentFile=.env`; 5-minute timeout; 512 MB memory cap
- `ai-investment-advisor.timer` — fires Mon-Fri 00:00 UTC (= 08:00 HKT); `Persistent=true` so missed runs fire on next VM start
- `setup_gcp.sh` — one-time VM bootstrap: installs Python 3.11, git, sqlite3; clones repo
- `install.sh` — creates `advisor` system user; creates virtualenv; installs dependencies; registers and starts systemd units

**Tests** (`tests/`)
- `test_response_parser.py` — 10 tests: valid parse, markdown fence stripping, invalid JSON, missing fields, invalid signal, confidence out of range, unknown portfolio bias, missing optional fields
- `test_formatter.py` — 5 tests: return type, message length splitting, ticker presence, signal presence, disclaimer presence
- `test_config_loader.py` — 5 tests: default values, file loading, missing file, invalid risk appetite, empty asset lists
- `test_rss_client.py` — 4 tests with mocked feedparser: article fetch, max_items, error handling, keyword filtering

**Documentation**
- `CLAUDE.md` — comprehensive project reference: architecture, data flow, config guide, run commands, SQLite schema, error handling, how to add assets, how to tune prompts, API cost estimates, GCP VM reference, key conventions
- `CHANGELOG.md` — this file

---

<!-- Template for future entries:

## [X.Y.Z] — YYYY-MM-DD

### Added
- ...

### Changed
- ...

### Fixed
- ...

### Removed
- ...

-->
