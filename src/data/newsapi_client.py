"""
NewsAPI client: fetches recent news headlines relevant to each tracked asset.
Free tier: 100 req/day. Falls back to RSS if quota is exhausted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from src.utils.logger import get_logger
from src.utils.rate_limiter import make_retry

logger = get_logger("data.newsapi")

NEWSAPI_BASE = "https://newsapi.org/v2/everything"


@dataclass
class NewsArticle:
    title: str
    source: str
    published_at: str
    url: str
    description: str = ""


_retry = make_retry(max_attempts=2, min_wait=2, max_wait=10)


class NewsAPIQuotaExhausted(Exception):
    pass


@_retry
def fetch_news_for_asset(
    query: str,
    api_key: str,
    max_articles: int = 5,
    hours_lookback: int = 24,
    language: str = "en",
    sort_by: str = "relevancy",
) -> list[NewsArticle]:
    """
    Fetch news headlines for a search query (asset name or ticker).
    Returns up to max_articles NewsArticle objects.
    Raises NewsAPIQuotaExhausted if the API quota is exhausted.
    """
    from_time = (datetime.now(timezone.utc) - timedelta(hours=hours_lookback)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    params = {
        "q": query,
        "from": from_time,
        "language": language,
        "sortBy": sort_by,
        "pageSize": max_articles,
        "apiKey": api_key,
    }

    response = requests.get(NEWSAPI_BASE, params=params, timeout=10)

    if response.status_code == 426:
        raise NewsAPIQuotaExhausted("NewsAPI quota exhausted (HTTP 426)")
    if response.status_code == 429:
        raise NewsAPIQuotaExhausted("NewsAPI rate limited (HTTP 429)")

    response.raise_for_status()
    data = response.json()

    if data.get("status") == "error":
        code = data.get("code", "")
        if code in ("maximumResultsReached", "rateLimited"):
            raise NewsAPIQuotaExhausted(f"NewsAPI error: {data.get('message', code)}")
        logger.warning(f"NewsAPI error for query '{query}': {data.get('message')}")
        return []

    articles = []
    for item in data.get("articles", []):
        title = (item.get("title") or "").strip()
        if not title or title == "[Removed]":
            continue
        articles.append(
            NewsArticle(
                title=title[:150],  # truncate for token efficiency
                source=item.get("source", {}).get("name", "Unknown"),
                published_at=item.get("publishedAt", "")[:10],
                url=item.get("url", ""),
                description=(item.get("description") or "")[:200],
            )
        )

    logger.debug(f"NewsAPI returned {len(articles)} articles for '{query}'")
    return articles
