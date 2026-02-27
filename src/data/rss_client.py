"""
RSS client: fetches and keyword-filters RSS feed entries from financial news sources.
Used as a fallback when NewsAPI quota is exhausted, and as a macro news source.
"""

from __future__ import annotations

from dataclasses import dataclass

import feedparser
import requests

from src.utils.logger import get_logger

logger = get_logger("data.rss")

_RSS_HEADERS = {"User-Agent": "python-requests/advisor"}


@dataclass
class RSSArticle:
    title: str
    source: str
    published: str
    link: str
    summary: str = ""


def fetch_rss_feed(url: str, source_name: str, max_items: int = 10) -> list[RSSArticle]:
    """Fetch and parse a single RSS feed. Returns up to max_items entries.

    Uses requests for HTTP so that SSL certificates (via certifi) work correctly
    on all platforms, including macOS where urllib's CA bundle is not configured.
    """
    try:
        response = requests.get(url, timeout=15, headers=_RSS_HEADERS)
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        articles: list[RSSArticle] = []
        for entry in feed.entries[:max_items]:
            title = (getattr(entry, "title", "") or "").strip()
            if not title:
                continue
            published = ""
            if hasattr(entry, "published"):
                published = str(entry.published)[:10]
            elif hasattr(entry, "updated"):
                published = str(entry.updated)[:10]

            articles.append(
                RSSArticle(
                    title=title[:150],
                    source=source_name,
                    published=published,
                    link=getattr(entry, "link", ""),
                    summary=(getattr(entry, "summary", "") or "")[:200],
                )
            )
        logger.debug(f"RSS {source_name}: fetched {len(articles)} items")
        return articles
    except Exception as exc:
        logger.warning(f"RSS fetch failed for {source_name} ({url}): {exc}")
        return []


def fetch_asset_news_from_rss(
    feeds: list[dict],
    keywords: list[str],
    max_items_per_feed: int = 10,
    max_results: int = 5,
) -> list[RSSArticle]:
    """
    Fetch all configured RSS feeds and filter articles by keywords.
    Returns up to max_results matching articles across all feeds.
    """
    kw_lower = [k.lower() for k in keywords if k]
    all_articles: list[RSSArticle] = []

    for feed_cfg in feeds:
        articles = fetch_rss_feed(
            url=feed_cfg["url"],
            source_name=feed_cfg["name"],
            max_items=max_items_per_feed,
        )
        for article in articles:
            text = f"{article.title} {article.summary}".lower()
            if any(kw in text for kw in kw_lower):
                all_articles.append(article)

    return all_articles[:max_results]


def fetch_macro_headlines(
    feeds: list[dict],
    max_items_per_feed: int = 10,
    max_total: int = 5,
) -> list[RSSArticle]:
    """Fetch top macro headlines from all feeds without keyword filtering."""
    all_articles: list[RSSArticle] = []
    for feed_cfg in feeds:
        items = fetch_rss_feed(
            url=feed_cfg["url"],
            source_name=feed_cfg["name"],
            max_items=max_items_per_feed,
        )
        all_articles.extend(items)

    # Deduplicate by title
    seen: set[str] = set()
    unique: list[RSSArticle] = []
    for a in all_articles:
        key = a.title.lower()[:60]
        if key not in seen:
            seen.add(key)
            unique.append(a)

    return unique[:max_total]
