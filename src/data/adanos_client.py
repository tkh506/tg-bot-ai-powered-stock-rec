"""
Adanos Software sentiment client.

Fetches Reddit, X.com, and Polymarket sentiment for stocks and crypto.
API documentation: https://api.adanos.org/docs

Authentication: X-API-Key header
Free tier: 250 requests/month

Call efficiency strategy: Use the /compare endpoint to batch all tickers
in one API call per source, instead of one call per ticker.
  - reddit_stocks:    https://api.adanos.org/reddit/stocks/v1/compare
  - x_stocks:         https://api.adanos.org/x/stocks/v1/compare
  - polymarket_stocks: https://api.adanos.org/polymarket/stocks/v1/compare
  - reddit_crypto:    https://api.adanos.org/reddit/crypto/v1/compare

This gives 4 calls/run × 22 runs/month = 88 calls — well within the 250/month limit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import requests

from src.utils.logger import get_logger

logger = get_logger("data.adanos")

_BASE_URLS: dict[str, str] = {
    "reddit_stocks":     "https://api.adanos.org/reddit/stocks/v1/compare",
    "x_stocks":          "https://api.adanos.org/x/stocks/v1/compare",
    "polymarket_stocks": "https://api.adanos.org/polymarket/stocks/v1/compare",
    "reddit_crypto":     "https://api.adanos.org/reddit/crypto/v1/compare",
}

_TIMEOUT = 15


@dataclass
class AdanosTickerSentiment:
    ticker: str
    buzz_score: Optional[float] = None       # 0–100
    trend: Optional[str] = None              # rising | falling | stable
    sentiment_score: Optional[float] = None  # -1.0 to +1.0
    bullish_pct: Optional[float] = None
    bearish_pct: Optional[float] = None
    mentions: Optional[int] = None


@dataclass
class AdanosBatch:
    """Results from one Adanos /compare call, keyed by ticker/symbol (uppercase)."""
    data: dict[str, AdanosTickerSentiment] = field(default_factory=dict)
    source: str = ""
    error: Optional[str] = None


def fetch_adanos_batch(
    tickers: list[str],
    api_key: str,
    source: str,
    days: int = 7,
) -> AdanosBatch:
    """
    Fetch sentiment for multiple tickers in a single /compare API call.

    Args:
        tickers: Stock tickers (e.g. ["AAPL", "NVDA"]) or crypto symbols (["BTC", "ETH"])
        api_key:  Adanos API key
        source:   One of "reddit_stocks", "x_stocks", "polymarket_stocks", "reddit_crypto"
        days:     Lookback window in days (1-90)

    Returns:
        AdanosBatch with .data dict keyed by ticker (uppercase), or empty batch on failure.
    """
    if not tickers or not api_key:
        return AdanosBatch(source=source, error="no_tickers_or_key")

    url = _BASE_URLS.get(source)
    if not url:
        return AdanosBatch(source=source, error=f"unknown_source: {source}")

    # Crypto compare uses "symbols" param; stocks use "tickers"
    param_key = "symbols" if source == "reddit_crypto" else "tickers"

    try:
        resp = requests.get(
            url,
            params={param_key: ",".join(tickers), "days": days},
            headers={"X-API-Key": api_key},
            timeout=_TIMEOUT,
        )
        if resp.status_code == 429:
            logger.warning(f"Adanos rate limit hit for {source}")
            return AdanosBatch(source=source, error="rate_limit")
        if resp.status_code == 401:
            logger.warning("Adanos authentication failed — check ADANOS_API_KEY")
            return AdanosBatch(source=source, error="auth_failed")
        resp.raise_for_status()

        payload = resp.json()
        # Response format: {"stocks": [...]} (current API)
        # Fallback: bare list, or dict with "results"/"data" key (legacy)
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            items = (
                payload.get("stocks")
                or payload.get("results")
                or payload.get("data")
                or []
            )
        else:
            items = []

        batch = AdanosBatch(source=source)
        for item in items:
            ticker = str(item.get("ticker") or item.get("symbol") or "").upper()
            if not ticker:
                continue
            batch.data[ticker] = AdanosTickerSentiment(
                ticker=ticker,
                buzz_score=item.get("buzz_score"),
                trend=item.get("trend"),
                # API uses "sentiment" field; fall back to "sentiment_score" for
                # any future schema variant
                sentiment_score=item.get("sentiment") or item.get("sentiment_score"),
                bullish_pct=item.get("bullish_pct"),
                bearish_pct=item.get("bearish_pct"),
                mentions=item.get("mentions"),
            )

        logger.info(f"Adanos {source}: sentiment for {list(batch.data.keys())}")
        return batch

    except Exception as e:
        logger.warning(f"Adanos fetch failed for {source}: {e}")
        return AdanosBatch(source=source, error=str(e))
