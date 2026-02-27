"""
ApeWisdom client: fetches Reddit retail discussion rankings for stocks and crypto.

Public API — no authentication required.
Endpoint: https://apewisdom.io/api/v1.0/filter/{filter_type}

Returns a ranked list of the most-mentioned tickers on Reddit.
We fetch the full trending list once per type and look up our tickers by name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import requests

from src.utils.logger import get_logger

logger = get_logger("data.apewisdom")

_BASE_URL = "https://apewisdom.io/api/v1.0/filter"
_TIMEOUT = 10


@dataclass
class ApeWisdomEntry:
    rank: int
    ticker: str
    name: str
    mentions: int
    upvotes: int
    rank_24h_ago: Optional[int] = None
    mentions_24h_ago: Optional[int] = None


@dataclass
class ApeWisdomSnapshot:
    """Trending results keyed by ticker (uppercase) for O(1) lookup."""
    data: dict[str, ApeWisdomEntry] = field(default_factory=dict)
    filter_type: str = ""
    error: Optional[str] = None


def fetch_apewisdom(filter_type: str = "all-stocks") -> ApeWisdomSnapshot:
    """
    Fetch the current Reddit trending list for stocks or crypto.

    Args:
        filter_type: "all-stocks" | "all-crypto" | "wallstreetbets" | etc.

    Returns:
        ApeWisdomSnapshot with .data dict keyed by ticker (uppercase).
        Empty snapshot on failure.
    """
    try:
        resp = requests.get(
            f"{_BASE_URL}/{filter_type}",
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        items = resp.json().get("results") or []
        snapshot = ApeWisdomSnapshot(filter_type=filter_type)
        for item in items:
            ticker = str(item.get("ticker", "")).upper()
            if not ticker:
                continue
            snapshot.data[ticker] = ApeWisdomEntry(
                rank=int(item.get("rank", 0)),
                ticker=ticker,
                name=str(item.get("name", "")),
                mentions=int(item.get("mentions", 0)),
                upvotes=int(item.get("upvotes", 0)),
                rank_24h_ago=item.get("rank_24h_ago"),
                mentions_24h_ago=item.get("mentions_24h_ago"),
            )
        logger.info(f"ApeWisdom {filter_type}: {len(snapshot.data)} tickers fetched")
        return snapshot
    except Exception as e:
        logger.warning(f"ApeWisdom fetch failed for {filter_type}: {e}")
        return ApeWisdomSnapshot(filter_type=filter_type, error=str(e))
