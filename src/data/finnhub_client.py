"""
Finnhub client: fetches stock-specific news and company fundamentals.

Provides:
  - Company news headlines per stock ticker
  - Basic financial metrics (PE ratio, 52W high/low, beta)
  - Analyst consensus (buy/hold/sell counts)
  - Consensus price target (mean, high, low)

Authentication: X-Finnhub-Token header
Rate limit: 60 calls/minute (free tier)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from src.utils.logger import get_logger

logger = get_logger("data.finnhub")

_BASE_URL = "https://finnhub.io/api/v1"
_TIMEOUT = 10


@dataclass
class FinnhubArticle:
    title: str
    summary: str
    source: str
    url: str
    datetime: str  # YYYY-MM-DD


@dataclass
class FinnhubMetrics:
    ticker: str
    pe_ratio: Optional[float] = None
    week_52_high: Optional[float] = None
    week_52_low: Optional[float] = None
    beta: Optional[float] = None
    analyst_buy: int = 0
    analyst_hold: int = 0
    analyst_sell: int = 0
    price_target_mean: Optional[float] = None
    price_target_high: Optional[float] = None
    price_target_low: Optional[float] = None
    error: Optional[str] = None


def fetch_finnhub_news(
    ticker: str,
    api_key: str,
    hours_lookback: int = 24,
    max_articles: int = 5,
) -> list[FinnhubArticle]:
    """
    Fetch recent company news for a stock ticker.
    Returns [] on failure (logged as warning).
    """
    now = datetime.now(timezone.utc)
    from_dt = (now - timedelta(hours=hours_lookback)).strftime("%Y-%m-%d")
    to_dt = now.strftime("%Y-%m-%d")
    try:
        resp = requests.get(
            f"{_BASE_URL}/company-news",
            params={"symbol": ticker, "from": from_dt, "to": to_dt},
            headers={"X-Finnhub-Token": api_key},
            timeout=_TIMEOUT,
        )
        if resp.status_code == 429:
            logger.warning(f"Finnhub rate limit hit for news ({ticker})")
            return []
        resp.raise_for_status()
        articles = resp.json()
        if not isinstance(articles, list):
            return []
        results = []
        for a in articles[:max_articles]:
            ts = a.get("datetime", 0)
            dt_str = (
                datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
                if ts else ""
            )
            results.append(FinnhubArticle(
                title=str(a.get("headline", ""))[:150],
                summary=str(a.get("summary", ""))[:300],
                source=str(a.get("source", "")),
                url=str(a.get("url", "")),
                datetime=dt_str,
            ))
        return results
    except Exception as e:
        logger.warning(f"Finnhub news fetch failed for {ticker}: {e}")
        return []


def fetch_finnhub_metrics(ticker: str, api_key: str) -> FinnhubMetrics:
    """
    Fetch basic financial metrics, analyst recommendations, and price targets.
    Makes 3 sequential API calls. Returns FinnhubMetrics with error set on failure.
    """
    headers = {"X-Finnhub-Token": api_key}
    metrics = FinnhubMetrics(ticker=ticker)

    # 1. Basic financials (PE ratio, 52W range, beta)
    try:
        resp = requests.get(
            f"{_BASE_URL}/stock/metric",
            params={"symbol": ticker, "metric": "all"},
            headers=headers,
            timeout=_TIMEOUT,
        )
        if resp.status_code == 429:
            metrics.error = "rate_limit"
            return metrics
        resp.raise_for_status()
        data = resp.json().get("metric", {})
        # peNormalizedAnnual preferred; fall back to trailing TTM
        metrics.pe_ratio = data.get("peNormalizedAnnual") or data.get("peBasicExclExtraTTM")
        metrics.week_52_high = data.get("52WeekHigh")
        metrics.week_52_low = data.get("52WeekLow")
        metrics.beta = data.get("beta")
    except Exception as e:
        logger.warning(f"Finnhub basic financials failed for {ticker}: {e}")

    # 2. Analyst recommendations (most recent period)
    try:
        resp = requests.get(
            f"{_BASE_URL}/stock/recommendation",
            params={"symbol": ticker},
            headers=headers,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        recs = resp.json()
        if isinstance(recs, list) and recs:
            latest = recs[0]
            metrics.analyst_buy = (latest.get("buy") or 0) + (latest.get("strongBuy") or 0)
            metrics.analyst_hold = latest.get("hold") or 0
            metrics.analyst_sell = (latest.get("sell") or 0) + (latest.get("strongSell") or 0)
    except Exception as e:
        logger.warning(f"Finnhub recommendations failed for {ticker}: {e}")

    # 3. Price targets (premium endpoint — 403 on free tier; skip silently)
    try:
        resp = requests.get(
            f"{_BASE_URL}/stock/price-target",
            params={"symbol": ticker},
            headers=headers,
            timeout=_TIMEOUT,
        )
        if resp.status_code == 403:
            # /stock/price-target requires a premium Finnhub subscription.
            # Log at DEBUG so the free-tier 403 doesn't spam WARNINGs every run.
            logger.debug(f"Finnhub price targets: premium endpoint, skipping {ticker}")
        else:
            resp.raise_for_status()
            pt = resp.json()
            metrics.price_target_mean = pt.get("targetMean")
            metrics.price_target_high = pt.get("targetHigh")
            metrics.price_target_low = pt.get("targetLow")
    except Exception as e:
        logger.warning(f"Finnhub price targets failed for {ticker}: {e}")

    return metrics
