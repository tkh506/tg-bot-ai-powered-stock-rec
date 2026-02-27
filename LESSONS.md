# LESSONS.md — Lessons Learned Across Sessions

This file captures mistakes made and rules derived from them.
Read it at the start of every session. Update it after any user correction.

---

## Session: 2026-02-23

### LESSON 1 — Pin pydantic to a version compatible with the local Python version

**What happened:**
`requirements.txt` pinned `pydantic==2.6.4` which requires `pydantic-core==2.16.3`.
That version of `pydantic-core` has no pre-built wheel for Python 3.13, so pip tried
to compile it from source. The build failed because Python 3.13 changed the
`ForwardRef._evaluate()` internal API.

**Rule:**
- For Python 3.13, `pydantic` must be `≥ 2.10.6` (first version with Python 3.13 wheels).
- When updating dependencies for a new Python version, always check that pre-built wheels
  exist for the target Python version before pinning. Use PyPI to verify.
- The correct pinned versions for Python 3.13: `pydantic==2.10.6`, `pydantic-settings==2.7.1`.

---

### LESSON 2 — Verify all imported names actually exist in the source module

**What happened:**
`src/main.py` imported `ConfigError` from `src/utils/config_loader`:
```python
from src.utils.config_loader import load_config, ConfigError
```
But `ConfigError` was never defined in `config_loader.py`, causing an `ImportError` at startup.

**Rule:**
- When writing a module that other modules import from, define every name that will be imported.
- Before shipping new code, grep for all symbols imported from a module and confirm each one exists.
- `ConfigError` should live in `config_loader.py` since it represents a config-loading failure.

---

### LESSON 3 — Keep fields consistent across parallel model classes

**What happened:**
`StockAsset` and `CommodityAsset` both had a `currency` field.
`ForexAsset` did not — but `fetcher.py` accessed `asset.currency` on all yfinance-based
assets (stocks, forex, commodities) in the same list comprehension.
This caused an `AttributeError: 'ForexAsset' object has no attribute 'currency'` at runtime.

**Rule:**
- When a fetcher or orchestrator treats multiple asset types uniformly (e.g. iterates them
  together), all those model classes must expose the same interface.
- When adding a field to one asset model, check whether sibling models need the same field.
- For `ForexAsset`, `currency` is derived from `quote` — use a `model_validator(mode="after")`
  to set it automatically so `config.yaml` doesn't need updating.

---

### LESSON 4 — Never fire parallel yfinance requests; always use a single batch call

**What happened:**
`fetcher.py` submitted one `ThreadPoolExecutor` task per yfinance ticker (8 simultaneous
downloads). This caused two failures:
1. Yahoo Finance rate-limited the parallel requests → `JSONDecodeError('Expecting value:
   line 1 column 1 (char 0)')` on almost every ticker
2. yfinance's internal SQLite cache (peewee) was written to concurrently by multiple
   threads → `OperationalError('database is locked')` on some tickers

**Rule:**
- Always use `yf.download([list_of_tickers], group_by='ticker')` to fetch all yfinance
  assets in a **single API call**. Yahoo Finance does not tolerate N parallel requests.
- Never submit per-ticker yfinance calls to a thread pool.
- The batch function `fetch_ohlcv_batch` in `yfinance_client.py` handles this correctly.
  `fetcher.py` submits one future for the whole batch and expands the dict result before
  the per-ticker assembly loops run.

---

### LESSON 5 — Pin yfinance to a recent version; 0.2.38 is incompatible with current Yahoo Finance API

**What happened:**
`yfinance==0.2.38` used deprecated Yahoo Finance API endpoints. Every `yf.download()` call
returned an empty body, causing `JSONDecodeError('Expecting value: line 1 column 1 (char 0)')`.
This affected all tickers regardless of whether requests were parallel or serial.

**Rule:**
- Always use a recent yfinance version (≥ 0.2.60). Yahoo Finance changes their API frequently;
  old yfinance versions silently fail with empty/HTML responses.
- The canonical symptom is `JSONDecodeError` on every ticker with message `'Expecting value:
  line 1 column 1 (char 0)'` — this means Yahoo returned an empty or non-JSON response.
- Current pinned version: `yfinance==0.2.66`.

---

### LESSON 6 — TokenBucket must start with 1 token, not a full bucket

**What happened:**
`TokenBucket.__init__` set `self.tokens = float(calls_per_minute)`. This means the bucket
starts full (e.g. 5 tokens for `calls_per_minute=5`). When 3 Alpha Vantage threads start
simultaneously, they all consume tokens instantly without any waiting, bypassing the rate
limiter entirely. Alpha Vantage returned rate-limit warnings on 2 of 3 calls.

**Rule:**
- `TokenBucket` must initialise `self.tokens = 1.0`, not `float(calls_per_minute)`.
  This prevents startup burst — only 1 call fires immediately, the rest wait for refill.
- For Alpha Vantage (free tier: 1 req/sec), use `calls_per_minute=20` (3s gap). This accounts
  for network round-trip latency: our token bucket measures *departure* time, but AV measures
  *arrival* time. A 2s departure gap (~30/min) can appear as <1s at AV's servers; 3s is safe.
- The free tier also has a **25 req/day** hard cap. This is not a code issue — it is only
  a concern during heavy local testing (multiple dry runs in one day). In production
  (1 run/day × 3 US stocks = 3 calls/day), the daily limit is never reached.
- Whenever adding `@rate_limited` to a function called from multiple threads, verify the
  bucket init starts at 1 token, not the full capacity.

---

### LESSON 7 — RSS client must use requests for HTTP, not feedparser's built-in urllib

**What happened:**
`rss_client.py` called `feedparser.parse(url)` directly. feedparser uses Python's `urllib`
for HTTP, which on macOS (Python.org installer) does not have the system CA bundle configured.
This caused `SSL: CERTIFICATE_VERIFY_FAILED` on every HTTPS RSS feed — feedparser returned
0 entries silently (the exception was swallowed inside feedparser), so the pipeline ran but
always produced "No macro headlines are available for this session".

A second issue: the configured Reuters feed domain (`feeds.reuters.com`) no longer resolves
in DNS — Reuters deprecated their legacy RSS infrastructure. This affected both macOS and the
GCP VM.

**Rule:**
- Never call `feedparser.parse(url)` with a URL. Always fetch via `requests` first, then pass
  the bytes content to `feedparser.parse(response.content)`. `requests` bundles `certifi` and
  handles SSL correctly on all platforms.
- When an RSS feed returns 0 items in the log (DEBUG: "RSS X: fetched 0 items"), the cause is
  almost always either a dead feed URL or an SSL/network failure — not an empty feed.
- Keep a list of tested working RSS feed URLs here. As of 2026-02-23, confirmed working:
  - `https://feeds.bbci.co.uk/news/business/rss.xml` (BBC Business — 50 items)
  - `https://feeds.content.dowjones.io/public/rss/mw_marketpulse` (MarketWatch Pulse — 30 items)
- Dead Reuters feeds (do not use):
  - `https://feeds.reuters.com/reuters/businessNews` — DNS dead
  - `https://feeds.reuters.com/reuters/financialNews` — DNS dead
  - `https://feeds.reuters.com/reuters/topNews` — DNS dead

---

---

## Session: 2026-02-26

### LESSON 8 — When tests mock a lower-level function, ensure the mock matches the actual call chain

**What happened:**
`test_rss_client.py` patched `feedparser.parse` to intercept RSS feed fetches. But LESSON 7 (applied earlier) changed `rss_client.py` to call `requests.get(url)` first and pass `response.content` bytes to `feedparser.parse()`. The tests were never updated to match this new call chain, so `requests.get` was called for real during the test, causing network I/O (and potential SSL failures) in the test suite.

**Rule:**
- When you change the implementation of a module (e.g. switching from `feedparser.parse(url)` to `requests.get` → `feedparser.parse(bytes)`), immediately update all tests that mock the old call path.
- Test mocks must match the *actual* call chain in the implementation, not what the code used to do.
- If a function calls A → B internally, you must mock A (the outermost call) for the mock to intercept correctly. Mocking B alone while A makes a real network call is insufficient.
- Symptom: tests pass locally when network works, but fail in CI or offline — because the "mocked" test is actually making real HTTP calls.

---

### LESSON 9 — Add new parameters to a function before referencing them at the call site

**What happened:**
`main.py` was written to call `build_discovery_prompt(config, broad_data, previous_bad_response=previous_bad_discovery)`, but `build_discovery_prompt()` in `prompt_builder.py` was initially defined without a `previous_bad_response` parameter. This caused a `TypeError` when the retry path was hit.

**Rule:**
- When designing a function that will be called from multiple places (especially with optional args), define all the parameters upfront — even if they're not used in the first implementation.
- Check all call sites before finalising a function signature. Grep for the function name to find every caller.
- For retry-pattern functions that accept `previous_bad_response`, always add `previous_bad_response: str | None = None` as a default parameter from the start.

---

### LESSON 10 — TokenBucket max_tokens must be capped at 1 to prevent cross-phase burst

**What happened:**
`TokenBucket` was initialised with `max_tokens = float(calls_per_minute)` (e.g. 20.0). Between
Phase 1 and Phase 2 the pipeline is idle for ~24 seconds (Stage 1 AI call). During that idle time
the bucket refills from 1.0 to `1 + 24 × 0.333 = 9.0 tokens`. When Phase 2 starts, the first 9
Alpha Vantage tasks all acquire tokens immediately and fire simultaneously — a 9-request burst.
AV returned the "please spread out" information message for 8 of the 9 burst calls, losing all
their sentiment data.

**Rule:**
- `TokenBucket.max_tokens` must default to `1.0` (one call at a time). The "start at 1 token"
  fix (LESSON 6) only prevents startup burst; it does NOT prevent cross-phase accumulation.
- Both `tokens = 1.0` (initial) AND `max_tokens = 1.0` (ceiling) are required.
- The `rate_limited()` decorator now accepts `max_burst: int = 1` to override the ceiling only
  when the API explicitly allows burst calls.
- Symptom of accumulated burst: multiple rate-limited calls log warnings/errors all within the
  same 1-2 second window at the start of a new phase.

---

### LESSON 11 — External API response formats change; always handle multiple field names

**What happened:**
The Adanos `/compare` endpoint changed its response schema between versions:
- Old format: `{"results": [...]}` with fields `sentiment_score`, `trend`, `bullish_pct`, `bearish_pct`
- New format: `{"stocks": [...]}` with field `sentiment` (not `sentiment_score`); `trend`,
  `bullish_pct`, `bearish_pct` removed

The client code only looked for `"results"` and `"data"` keys, so `payload.get("results") or
payload.get("data")` returned `None`, giving an empty item list. All three Adanos sources logged
`sentiment for []` — a silent total failure.

**Rule:**
- When parsing third-party API responses, always handle multiple plausible key names (use `or`
  chaining): `payload.get("stocks") or payload.get("results") or payload.get("data") or []`.
- When mapping response fields, fall back to alternative field names:
  `item.get("sentiment") or item.get("sentiment_score")`.
- If an INFO-level log says `sentiment for []` (or similar empty data) for ALL tickers across
  ALL sources simultaneously, the cause is almost always a response format mismatch — not a
  quota issue (quota issues give 4xx or a specific error message).
- After any external API integration, periodically verify the response format hasn't changed by
  comparing the actual response payload against the parsing code.

---

### LESSON 12 — NewsData `timeframe` parameter is a paid-plan-only feature; omit it on the free tier

**What happened:**
`newsdata_client.py` first passed `"timeframe": f"{timeframe_hours}h"` (422), then after
"fixing" the format to `"timeframe": timeframe_hours` (integer), STILL got 422 because the
`timeframe` parameter itself is locked behind a paid subscription — the format was never the
issue. The 422 response body says: *"Access Denied! To use the timeframe parameter, please
upgrade your plan or contact support."*

**Rule:**
- Any `422 UNPROCESSABLE ENTITY` from a news API is most likely a paywalled parameter, not a
  format error. Always check the **response body** for the exact reason (not just the HTTP code).
- On the NewsData free tier: omit `timeframe` entirely. The `/latest` endpoint returns
  most-recent articles first by default — omitting `timeframe` gives equivalent behaviour for
  our 5-article use case.
- The `category` parameter (comma-separated) IS supported on the free tier and should be kept.
- A second failure mode: firing 10+ parallel NewsData calls causes 429. Add
  `@rate_limited(calls_per_minute=60, key="newsdata")` to `fetch_newsdata_news` to serialize
  calls at ~1/sec and prevent burst-induced rate limits.

---

### LESSON 13 — Finnhub /stock/price-target is a premium endpoint; handle 403 at DEBUG level

**What happened:**
`fetch_finnhub_metrics` called `/stock/price-target` and logged `logger.warning(...)` on every
403 Forbidden response. With 10 stocks per run, this produced 10 WARNING lines per run.
The 403 is expected — the free Finnhub tier does not include price targets.

**Rule:**
- Check the Finnhub free-tier endpoint list before adding a new `/stock/` API call.
  Free tier includes: `/company-news`, `/stock/metric`, `/stock/recommendation`.
  Premium-only includes: `/stock/price-target`, `/stock/earnings`, `/stock/financials-reported`.
- Handle expected free-tier 403s at DEBUG level, not WARNING. WARNING should be reserved for
  unexpected failures. If a 403 is known and structural, add an explicit `if resp.status_code == 403`
  check before `raise_for_status()`.

---

<!-- Template for new lessons:

### LESSON N — Short title

**What happened:**
[Describe the mistake and its symptom]

**Rule:**
- [Actionable rule to prevent recurrence]

-->
