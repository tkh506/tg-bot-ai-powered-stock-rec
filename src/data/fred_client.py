"""
FRED (Federal Reserve Economic Data) client.

Fetches key US economic indicators: CPI, unemployment, fed funds rate,
treasury yields, and GDP growth.

Base URL: https://api.stlouisfed.org/fred
Authentication: api_key query parameter
Rate limit: 120 req/min (free tier, no daily cap)
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import requests

from src.utils.logger import get_logger

logger = get_logger("data.fred")

_BASE_URL = "https://api.stlouisfed.org/fred"
_TIMEOUT = 10


@dataclass
class FredObservation:
    series_id: str
    name: str
    value: Optional[float]
    date: str
    prev_value: Optional[float]
    prev_date: str
    units: str = ""


@dataclass
class EconomicIndicators:
    observations: list[FredObservation] = field(default_factory=list)
    fetched_at: str = ""
    error: Optional[str] = None

    @property
    def yield_curve_spread(self) -> Optional[float]:
        """10Y minus 2Y treasury yield spread. Positive = normal curve, negative = inverted."""
        obs_map = {o.series_id: o.value for o in self.observations if o.value is not None}
        dgs10 = obs_map.get("DGS10")
        dgs2 = obs_map.get("DGS2")
        if dgs10 is not None and dgs2 is not None:
            return round(dgs10 - dgs2, 2)
        return None


def _fetch_single_series(series_id: str, name: str, api_key: str) -> FredObservation:
    """Fetch the two most recent non-missing observations for one FRED series."""
    try:
        resp = requests.get(
            f"{_BASE_URL}/series/observations",
            params={
                "series_id": series_id,
                "api_key": api_key,
                "file_type": "json",
                "sort_order": "desc",
                "limit": 5,  # Fetch a few extra to skip any "." (missing) values
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        obs_raw = resp.json().get("observations", [])
        # Filter out observations with value "." (not yet released)
        valid = [o for o in obs_raw if o.get("value") not in (".", "", None)]
        if not valid:
            return FredObservation(
                series_id=series_id, name=name,
                value=None, date="", prev_value=None, prev_date="",
            )
        latest = valid[0]
        prev = valid[1] if len(valid) > 1 else None
        return FredObservation(
            series_id=series_id,
            name=name,
            value=float(latest["value"]),
            date=latest["date"],
            prev_value=float(prev["value"]) if prev else None,
            prev_date=prev["date"] if prev else "",
        )
    except Exception as e:
        logger.warning(f"FRED fetch failed for {series_id} ({name}): {e}")
        return FredObservation(
            series_id=series_id, name=name,
            value=None, date="", prev_value=None, prev_date="",
        )


def fetch_economic_indicators(
    api_key: str,
    series_config: list,  # list of FredSeriesConfig (has .id and .name)
) -> EconomicIndicators:
    """
    Fetch all configured FRED economic series in parallel.
    Returns EconomicIndicators with all available observations.
    """
    indicators = EconomicIndicators(
        fetched_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    )

    with ThreadPoolExecutor(max_workers=min(len(series_config), 6)) as executor:
        futures = {
            executor.submit(_fetch_single_series, s.id, s.name, api_key): s.id
            for s in series_config
        }
        for future in as_completed(futures):
            obs = future.result()
            indicators.observations.append(obs)

    # Sort in a logical order (same as config order where possible)
    config_order = {s.id: i for i, s in enumerate(series_config)}
    indicators.observations.sort(key=lambda o: config_order.get(o.series_id, 99))

    fetched = [o.series_id for o in indicators.observations if o.value is not None]
    logger.info(f"FRED: fetched {len(fetched)}/{len(series_config)} series: {fetched}")
    return indicators
