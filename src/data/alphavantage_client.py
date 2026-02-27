"""
Alpha Vantage client (paid): fetches news sentiment scores per ticker.
Used to enrich the AI prompt with structured bullish/neutral/bearish sentiment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import requests

from src.utils.logger import get_logger
from src.utils.rate_limiter import rate_limited, make_retry

logger = get_logger("data.alphavantage")

AV_BASE = "https://www.alphavantage.co/query"


@dataclass
class SentimentData:
    ticker: str
    overall_sentiment: str        # "Bullish" | "Somewhat-Bullish" | "Neutral" | "Somewhat-Bearish" | "Bearish"
    overall_score: float          # -1.0 to 1.0
    bullish_count: int
    neutral_count: int
    bearish_count: int
    top_headlines: list[str]      # top 3 headlines from AV news
    error: Optional[str] = None


_retry = make_retry(max_attempts=3, min_wait=2, max_wait=20)


@rate_limited(calls_per_minute=20, key="alphavantage")  # free tier: 1 req/sec; use 20/min (3s gap) to account for network latency at AV's end
@_retry
def fetch_sentiment(
    ticker: str,
    api_key: str,
    limit: int = 50,
) -> SentimentData:
    """
    Fetch news sentiment for a stock ticker via Alpha Vantage NEWS_SENTIMENT.
    ticker should be in AV format: e.g. "AAPL", "IBM" (US stocks only).
    HK stocks and crypto are not well-supported; returns neutral on failure.
    """
    params = {
        "function": "NEWS_SENTIMENT",
        "tickers": ticker,
        "limit": limit,
        "apikey": api_key,
    }

    response = requests.get(AV_BASE, params=params, timeout=15)
    response.raise_for_status()
    data = response.json()

    if "Information" in data:
        # API quota / key issue
        logger.warning(f"Alpha Vantage quota/key issue: {data['Information']}")
        return SentimentData(
            ticker=ticker,
            overall_sentiment="Neutral",
            overall_score=0.0,
            bullish_count=0,
            neutral_count=0,
            bearish_count=0,
            top_headlines=[],
            error=data["Information"],
        )

    feed = data.get("feed", [])
    if not feed:
        return SentimentData(
            ticker=ticker,
            overall_sentiment="Neutral",
            overall_score=0.0,
            bullish_count=0,
            neutral_count=0,
            bearish_count=0,
            top_headlines=[],
        )

    bullish = neutral = bearish = 0
    scores: list[float] = []
    headlines: list[str] = []

    for article in feed:
        # Find the ticker-specific sentiment from each article
        ticker_sentiments = article.get("ticker_sentiment", [])
        for ts in ticker_sentiments:
            if ts.get("ticker", "").upper() == ticker.upper():
                label = ts.get("ticker_sentiment_label", "Neutral")
                score = float(ts.get("ticker_sentiment_score", 0.0))
                scores.append(score)
                if "Bullish" in label:
                    bullish += 1
                elif "Bearish" in label:
                    bearish += 1
                else:
                    neutral += 1
                break

        if len(headlines) < 3:
            title = (article.get("title") or "").strip()[:150]
            if title:
                headlines.append(title)

    avg_score = sum(scores) / len(scores) if scores else 0.0

    if avg_score > 0.15:
        overall = "Bullish" if avg_score > 0.35 else "Somewhat-Bullish"
    elif avg_score < -0.15:
        overall = "Bearish" if avg_score < -0.35 else "Somewhat-Bearish"
    else:
        overall = "Neutral"

    logger.debug(f"AV sentiment {ticker}: {overall} (score={avg_score:.3f}, n={len(scores)})")
    return SentimentData(
        ticker=ticker,
        overall_sentiment=overall,
        overall_score=round(avg_score, 3),
        bullish_count=bullish,
        neutral_count=neutral,
        bearish_count=bearish,
        top_headlines=headlines,
    )
