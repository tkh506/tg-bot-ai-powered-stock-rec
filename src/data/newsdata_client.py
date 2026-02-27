"""
NewsData.io client: fetches latest news headlines by keyword query.

Endpoint: GET https://newsdata.io/api/1/latest
Authentication: apikey query parameter
Free tier: 200 credits/day, 10 articles per request
"""

from __future__ import annotations

from dataclasses import dataclass

import requests

from src.utils.logger import get_logger
from src.utils.rate_limiter import rate_limited

logger = get_logger("data.newsdata")

_BASE_URL = "https://newsdata.io/api/1"
_TIMEOUT = 10


@dataclass
class NewsDataArticle:
    title: str
    description: str
    url: str
    pub_date: str
    source_name: str


@rate_limited(calls_per_minute=60, key="newsdata")  # free tier: ~1 req/sec safe limit
def fetch_newsdata_news(
    query: str,
    api_key: str,
    max_articles: int = 5,
    timeframe_hours: int = 24,  # kept for API compatibility; not sent (paid feature)
) -> list[NewsDataArticle]:
    """
    Fetch latest news articles matching a keyword query.
    Returns [] on quota exhaustion or any failure.

    Note: the `timeframe` parameter is a paid-plan-only feature on NewsData.io.
    Passing it (even as an integer) causes a 422 on the free tier.  The /latest
    endpoint always returns the most-recent articles first, so omitting timeframe
    gives equivalent behaviour for our 5-article use case.
    """
    try:
        resp = requests.get(
            f"{_BASE_URL}/latest",
            params={
                "apikey": api_key,
                "q": query,
                "language": "en",
                "category": "business,technology",
                # timeframe omitted — paywalled on free tier (causes 422)
            },
            timeout=_TIMEOUT,
        )
        if resp.status_code in (401, 402, 409, 429):
            logger.warning(f"NewsData quota/rate limit hit for '{query}' (HTTP {resp.status_code})")
            return []
        resp.raise_for_status()
        payload = resp.json()
        # NewsData also returns errors in the response body
        if payload.get("status") != "success":
            logger.warning(f"NewsData API error for '{query}': {payload.get('message', 'unknown')}")
            return []
        articles = payload.get("results") or []
        results = []
        for a in articles[:max_articles]:
            results.append(NewsDataArticle(
                title=str(a.get("title", ""))[:150],
                description=str(a.get("description") or "")[:300],
                url=str(a.get("link", "")),
                pub_date=str(a.get("pubDate", ""))[:10],
                source_name=str(a.get("source_name", "")),
            ))
        logger.info(f"NewsData '{query}': {len(results)} articles fetched")
        return results
    except Exception as e:
        logger.warning(f"NewsData fetch failed for '{query}': {e}")
        return []
