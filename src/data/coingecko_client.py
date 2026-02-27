"""
CoinGecko client: fetches current price, volume, and market cap for crypto assets.
Uses the free public API (no key required). Rate limit: ~30 req/min.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import requests

from src.utils.logger import get_logger
from src.utils.rate_limiter import rate_limited, make_retry

logger = get_logger("data.coingecko")

COINGECKO_BASE = "https://api.coingecko.com/api/v3"


@dataclass
class CryptoData:
    ticker: str       # CoinGecko ID
    symbol: str
    name: str
    current_price: float
    market_cap: float
    total_volume: float
    price_change_24h: float    # percent
    price_change_7d: float     # percent
    market_cap_rank: int
    error: Optional[str] = None


_retry = make_retry(max_attempts=3, min_wait=2, max_wait=20)


@rate_limited(calls_per_minute=25, key="coingecko")
@_retry
def fetch_crypto(
    coin_ids: list[str],
    vs_currency: str = "usd",
    include_7d: bool = True,
) -> dict[str, CryptoData]:
    """
    Fetch market data for a list of CoinGecko coin IDs in a single API call.
    Returns a dict keyed by coin ID.
    """
    ids_str = ",".join(coin_ids)
    price_change_fields = "24h,7d" if include_7d else "24h"

    url = f"{COINGECKO_BASE}/coins/markets"
    params = {
        "vs_currency": vs_currency,
        "ids": ids_str,
        "order": "market_cap_desc",
        "per_page": len(coin_ids),
        "page": 1,
        "sparkline": False,
        "price_change_percentage": price_change_fields,
    }

    response = requests.get(url, params=params, timeout=15)
    response.raise_for_status()
    data = response.json()

    results: dict[str, CryptoData] = {}
    for item in data:
        coin_id = item["id"]
        pct_7d = item.get("price_change_percentage_7d_in_currency") or 0.0
        results[coin_id] = CryptoData(
            ticker=coin_id,
            symbol=item.get("symbol", "").upper(),
            name=item.get("name", coin_id),
            current_price=item.get("current_price") or 0.0,
            market_cap=item.get("market_cap") or 0.0,
            total_volume=item.get("total_volume") or 0.0,
            price_change_24h=round(item.get("price_change_percentage_24h") or 0.0, 2),
            price_change_7d=round(pct_7d, 2),
            market_cap_rank=item.get("market_cap_rank") or 0,
        )
        logger.debug(
            f"CoinGecko {coin_id}: ${results[coin_id].current_price:.2f} "
            f"24h={results[coin_id].price_change_24h}%"
        )

    # Handle any requested IDs that came back with no data
    for coin_id in coin_ids:
        if coin_id not in results:
            logger.warning(f"CoinGecko returned no data for {coin_id}")
            results[coin_id] = CryptoData(
                ticker=coin_id, symbol=coin_id.upper(), name=coin_id,
                current_price=0.0, market_cap=0.0, total_volume=0.0,
                price_change_24h=0.0, price_change_7d=0.0, market_cap_rank=0,
                error="No data returned",
            )

    return results
