"""Tests for the RSS client."""

import pytest
from unittest.mock import patch, MagicMock

from src.data.rss_client import fetch_rss_feed, fetch_asset_news_from_rss, RSSArticle


def _make_mock_entry(title: str, link: str = "http://example.com", published: str = "2026-02-23") -> MagicMock:
    entry = MagicMock()
    entry.title = title
    entry.link = link
    entry.published = published
    entry.summary = "A summary."
    return entry


def _make_mock_feed(entries: list) -> MagicMock:
    feed = MagicMock()
    feed.entries = entries
    return feed


def _mock_requests_response(content: bytes = b"<rss/>") -> MagicMock:
    """Return a mock requests.Response with the given bytes content."""
    resp = MagicMock()
    resp.content = content
    return resp


def test_fetch_rss_feed_returns_articles():
    mock_entries = [_make_mock_entry(f"Headline {i}") for i in range(5)]
    mock_feed = _make_mock_feed(mock_entries)
    with patch("src.data.rss_client.requests.get", return_value=_mock_requests_response()):
        with patch("src.data.rss_client.feedparser.parse", return_value=mock_feed):
            articles = fetch_rss_feed("http://fake.url/rss", "Reuters", max_items=5)
    assert len(articles) == 5
    assert all(isinstance(a, RSSArticle) for a in articles)


def test_fetch_rss_feed_respects_max_items():
    mock_entries = [_make_mock_entry(f"H{i}") for i in range(20)]
    mock_feed = _make_mock_feed(mock_entries)
    with patch("src.data.rss_client.requests.get", return_value=_mock_requests_response()):
        with patch("src.data.rss_client.feedparser.parse", return_value=mock_feed):
            articles = fetch_rss_feed("http://fake.url/rss", "Reuters", max_items=3)
    assert len(articles) == 3


def test_fetch_rss_feed_handles_error_gracefully():
    with patch("src.data.rss_client.requests.get", side_effect=Exception("connection error")):
        articles = fetch_rss_feed("http://bad.url/rss", "Bad Feed")
    assert articles == []


def test_fetch_asset_news_filters_by_keyword():
    entries = [
        _make_mock_entry("Apple quarterly earnings beat expectations"),
        _make_mock_entry("Fed holds interest rates steady"),
        _make_mock_entry("Apple iPhone 16 sales surge"),
    ]
    mock_feed = _make_mock_feed(entries)
    with patch("src.data.rss_client.requests.get", return_value=_mock_requests_response()):
        with patch("src.data.rss_client.feedparser.parse", return_value=mock_feed):
            articles = fetch_asset_news_from_rss(
                feeds=[{"name": "Reuters", "url": "http://fake.url"}],
                keywords=["Apple"],
                max_items_per_feed=10,
                max_results=5,
            )
    assert len(articles) == 2
    assert all("Apple" in a.title for a in articles)
