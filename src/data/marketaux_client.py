"""
Marketaux client: fetches financial news filtered by stock symbol.

Endpoint: GET https://api.marketaux.com/v1/news/all
Authentication: api_token query parameter
Free tier: ~100 requests/month
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import requests

from src.utils.logger import get_logger

logger = get_logger("data.marketaux")

_BASE_URL = "https://api.marketaux.com/v1"
_TIMEOUT = 10


@dataclass
class MarketauxArticle:
    title: str
    description: str
    url: str
    published_at: str
    source: str
    sentiment_score: Optional[float] = None


def fetch_marketaux_news(
    ticker: str,
    api_token: str,
    max_articles: int = 5,
) -> list[MarketauxArticle]:
    """
    Fetch recent news articles mentioning a specific stock ticker.
    Returns [] on quota exhaustion or any failure.
    """
    try:
        resp = requests.get(
            f"{_BASE_URL}/news/all",
            params={
                "symbols": ticker,
                "api_token": api_token,
                "filter_entities": "true",
                "limit": max_articles,
                "language": "en",
            },
            timeout=_TIMEOUT,
        )
        if resp.status_code == 429:
            logger.warning(f"Marketaux rate limit hit for {ticker}")
            return []
        if resp.status_code in (401, 402, 422):
            logger.warning(f"Marketaux quota/auth error ({resp.status_code}) for {ticker}")
            return []
        resp.raise_for_status()
        articles = resp.json().get("data", [])
        results = []
        for a in articles[:max_articles]:
            # Try to extract per-entity sentiment score for this specific ticker
            sentiment = None
            for entity in a.get("entities", []):
                if entity.get("symbol", "").upper() == ticker.upper():
                    sentiment = entity.get("sentiment_score")
                    break
            results.append(MarketauxArticle(
                title=str(a.get("title", ""))[:150],
                description=str(a.get("description") or "")[:300],
                url=str(a.get("url", "")),
                published_at=str(a.get("published_at", ""))[:10],
                source=str(a.get("source", "")),
                sentiment_score=sentiment,
            ))
        logger.info(f"Marketaux {ticker}: {len(results)} articles fetched")
        return results
    except Exception as e:
        logger.warning(f"Marketaux news fetch failed for {ticker}: {e}")
        return []
