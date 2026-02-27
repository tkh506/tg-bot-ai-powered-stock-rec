# Local Test Run — Step by Step

## Prerequisites

You need API keys for:
- **OpenRouter** — required (AI analysis)
- **Telegram Bot Token + Chat ID** — required (to receive reports)
- **NewsAPI** — optional but recommended (free, 100 req/day)
- **Alpha Vantage** — optional (free, US stocks only)
- **Finnhub** — optional (free, stock news + fundamentals for US stocks)
- **Marketaux** — optional (free, ~100 req/month)
- **NewsData.io** — optional (free, 200 credits/day)
- **FRED** — optional (free, US economic indicators)
- **Adanos** — optional (free tier, 250 req/month, Reddit/X.com/Polymarket sentiment)
- **ApeWisdom** — no key needed (public API, activates automatically)

---

## Step 1 — Create your `.env` file

```bash
cp .env.example .env
```

Then edit `.env` and fill in your actual keys:

```
# Required
OPENROUTER_API_KEY=sk-or-v1-...
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=-100123456789

# Optional — existing sources
NEWSAPI_KEY=...
ALPHAVANTAGE_KEY=...

# Optional — new sources (leave blank to skip gracefully)
FINNHUB_API_KEY=...
MARKETAUX_API_KEY=...
NEWSDATA_API_KEY=...
FRED_API_KEY=...
ADANOS_API_KEY=...
```

How to get these:
- **OpenRouter key**: https://openrouter.ai/keys
- **Telegram bot**: message `@BotFather` on Telegram → `/newbot` → get token; then message `@userinfobot` to get your chat ID (negative ID for group chats)
- **NewsAPI**: https://newsapi.org/account (free tier, 100 req/day)
- **Alpha Vantage**: https://www.alphavantage.co — or disable it (see Step 2)
- **Finnhub**: https://finnhub.io/dashboard (free tier)
- **Marketaux**: https://www.marketaux.com/profile/dashboard (free tier)
- **NewsData.io**: https://newsdata.io/api-key (free tier, 200 credits/day)
- **FRED**: https://fred.stlouisfed.org/docs/api/api_key.html (free, instant registration)
- **Adanos**: https://adanos.org (free tier, 250 req/month)

---

## Step 2 — (Optional) Disable Alpha Vantage if you don't have a key

Edit `config/config.yaml`:

```yaml
alphavantage:
  enabled: false
```

---

## Step 3 — Activate the virtual environment and install dependencies

```bash
source venv/bin/activate
pip install -r requirements.txt
```

---

## Step 4 — Dry run (safest first test — no Telegram message sent)

```bash
python -m src.main --dry-run
```

Runs the full pipeline (fetch data → AI analysis → format report) but prints to stdout instead of sending to Telegram. Check for errors here first.

---

## Step 5 — Live run (sends report to your Telegram)

```bash
python -m src.main
```

---

## Step 6 — Check logs and database

```bash
# Tail the log file
tail -f logs/advisor.log

# Query recent reports from SQLite
sqlite3 data/reports.db "SELECT run_at, portfolio_bias, error_flag FROM reports ORDER BY run_at DESC LIMIT 5;"

# Query signals for a specific ticker
sqlite3 data/reports.db "SELECT run_at, signal, confidence FROM signals WHERE ticker='AAPL' ORDER BY run_at DESC LIMIT 10;"
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `ModuleNotFoundError` | Make sure venv is activated: `source venv/bin/activate` |
| `Config validation error` | Check `.env` has all required keys; check `config.yaml` for typos |
| Telegram not receiving | Verify `TELEGRAM_CHAT_ID` — group chats need negative IDs (e.g. `-100...`) |
| Alpha Vantage errors | Set `alphavantage.enabled: false` in `config.yaml` |
| AI returns invalid JSON | Check OpenRouter key is valid; it will auto-retry up to 3 times |
