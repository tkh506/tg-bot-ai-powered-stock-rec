"""
yfinance client: fetches OHLCV price data for stocks, forex pairs, and commodities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time as dt_time
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf

from src.utils.logger import get_logger
from src.utils.rate_limiter import make_retry

_ET = ZoneInfo("America/New_York")
_MARKET_OPEN = dt_time(9, 30)
_MARKET_CLOSE = dt_time(16, 0)

logger = get_logger("data.yfinance")


@dataclass
class OHLCVData:
    ticker: str
    name: str
    current_price: float
    open_price: float
    day_high: float
    day_low: float
    pct_5d: float          # 5-trading-day percent change
    pct_20d: float         # 20-trading-day percent change
    ma20: float            # 20-day simple moving average
    rsi: float             # RSI(14)
    vol_ratio: float       # Today's volume / 20-day average volume
    currency: str = "USD"
    error: Optional[str] = None
    # Extended-hours price (populated by a separate fetch_extended_prices call)
    extended_price: Optional[float] = None   # latest after-hours or pre-market price
    extended_pct: Optional[float] = None     # % change vs current_price (prev close)
    extended_label: Optional[str] = None     # "after-hours" | "pre-market"


_retry = make_retry(max_attempts=3, min_wait=2, max_wait=15)


def _compute_rsi(closes: pd.Series, period: int = 14) -> float:
    """Compute RSI(period) from a closing price series."""
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return round(float(rsi.iloc[-1]), 1) if not rsi.empty else 50.0


def _make_error(ticker: str, name: str, currency: str, exc: Exception) -> OHLCVData:
    """Build an error OHLCVData placeholder."""
    return OHLCVData(
        ticker=ticker, name=name, current_price=0.0, open_price=0.0,
        day_high=0.0, day_low=0.0, pct_5d=0.0, pct_20d=0.0,
        ma20=0.0, rsi=50.0, vol_ratio=1.0, currency=currency,
        error=str(exc),
    )


def _extract_ohlcv(df: pd.DataFrame, ticker: str, name: str, currency: str) -> OHLCVData:
    """Compute OHLCVData metrics from a clean single-ticker DataFrame."""
    closes = df["Close"].dropna()
    volumes = df["Volume"].dropna() if "Volume" in df.columns else pd.Series(dtype=float)

    current = float(closes.iloc[-1])
    open_p = float(df["Open"].iloc[-1]) if "Open" in df.columns else current
    high = float(df["High"].iloc[-1]) if "High" in df.columns else current
    low = float(df["Low"].iloc[-1]) if "Low" in df.columns else current

    pct_5d = round((current / float(closes.iloc[-5]) - 1) * 100, 2) if len(closes) >= 5 else 0.0
    pct_20d = round((current / float(closes.iloc[-20]) - 1) * 100, 2) if len(closes) >= 20 else 0.0
    ma20 = round(float(closes.tail(20).mean()), 4)
    rsi = _compute_rsi(closes)

    if not volumes.empty and len(volumes) >= 20:
        avg_vol = float(volumes.tail(20).mean())
        vol_ratio = round(float(volumes.iloc[-1]) / avg_vol, 2) if avg_vol > 0 else 1.0
    else:
        vol_ratio = 1.0

    logger.debug(f"Fetched {ticker}: price={current:.4f} pct5d={pct_5d}% rsi={rsi}")
    return OHLCVData(
        ticker=ticker, name=name,
        current_price=round(current, 4),
        open_price=round(open_p, 4),
        day_high=round(high, 4),
        day_low=round(low, 4),
        pct_5d=pct_5d,
        pct_20d=pct_20d,
        ma20=ma20,
        rsi=rsi,
        vol_ratio=vol_ratio,
        currency=currency,
    )


def fetch_ohlcv(
    ticker: str,
    name: str,
    lookback_days: int = 20,
    interval: str = "1d",
    currency: str = "USD",
) -> OHLCVData:
    """
    Fetch OHLCV data for a single ticker from yfinance.
    Returns OHLCVData; sets .error if the fetch fails.
    """
    try:
        return _fetch_with_retry(ticker, name, lookback_days, interval, currency)
    except Exception as exc:
        logger.warning(f"yfinance failed for {ticker}: {exc}")
        return _make_error(ticker, name, currency, exc)


@_retry
def _fetch_with_retry(
    ticker: str,
    name: str,
    lookback_days: int,
    interval: str,
    currency: str,
) -> OHLCVData:
    # Fetch enough bars: lookback_days + 14 extra for RSI warmup
    period_map = {20: "2mo", 30: "3mo", 60: "3mo"}
    period = period_map.get(lookback_days, "2mo")

    df = yf.download(ticker, period=period, interval=interval, progress=False, auto_adjust=True)

    if df.empty or len(df) < 5:
        raise ValueError(f"Insufficient data returned for {ticker}")

    # Flatten MultiIndex columns if present (single-ticker download)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    return _extract_ohlcv(df, ticker, name, currency)


def fetch_ohlcv_batch(
    assets: list[tuple[str, str, str]],   # [(ticker, name, currency), ...]
    lookback_days: int = 20,
    interval: str = "1d",
) -> dict[str, OHLCVData]:
    """
    Fetch OHLCV data for all tickers in a single yfinance call.

    Using one batch request instead of parallel per-ticker calls avoids Yahoo
    Finance rate limiting and the yfinance SQLite cache lock contention that
    occurs when multiple threads write simultaneously.

    Returns {ticker: OHLCVData}.
    """
    if not assets:
        return {}

    tickers = [t for t, _, _ in assets]
    info = {t: (n, c) for t, n, c in assets}
    period_map = {20: "2mo", 30: "3mo", 60: "3mo"}
    period = period_map.get(lookback_days, "2mo")
    results: dict[str, OHLCVData] = {}

    try:
        raw = yf.download(
            tickers,
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=True,
            group_by="ticker",
        )
    except Exception as exc:
        logger.warning(f"yfinance batch download failed: {exc}")
        for ticker, name, currency in assets:
            results[ticker] = _make_error(ticker, name, currency, exc)
        return results

    for ticker in tickers:
        name, currency = info[ticker]
        try:
            # With group_by='ticker', columns are a MultiIndex (ticker, field).
            # df[ticker] slices to a simple DataFrame for that ticker.
            if isinstance(raw.columns, pd.MultiIndex):
                if ticker not in raw.columns.get_level_values(0):
                    raise KeyError(f"Ticker {ticker} missing from batch download result")
                df = raw[ticker].copy()
            else:
                # Fallback: single ticker returned simple columns
                df = raw.copy()

            df = df.dropna(how="all")
            if df.empty or len(df) < 5:
                raise ValueError(f"Insufficient data for {ticker}")

            results[ticker] = _extract_ohlcv(df, ticker, name, currency)

        except Exception as exc:
            logger.warning(f"yfinance processing failed for {ticker}: {exc}")
            results[ticker] = _make_error(ticker, name, currency, exc)

    return results


def fetch_extended_prices(
    tickers: list[str],
) -> dict[str, tuple[float, str] | None]:
    """
    Fetch the latest real-time / extended-hours price for a list of tickers.

    Uses a single batch yfinance call with 1-minute bars and prepost=True to capture
    after-hours (16:00–20:00 ET) and pre-market (04:00–09:30 ET) prices.

    Returns {ticker: (price, label)} where label is "after-hours" or "pre-market",
    or {ticker: None} if the last bar is within regular market hours or data is absent.
    """
    if not tickers:
        return {}

    results: dict[str, tuple[float, str] | None] = {}

    try:
        raw = yf.download(
            tickers,
            period="1d",
            interval="1m",
            prepost=True,
            progress=False,
            auto_adjust=True,
            group_by="ticker",
        )
    except Exception as exc:
        logger.warning(f"Extended price batch download failed: {exc}")
        return {t: None for t in tickers}

    if raw is None or (hasattr(raw, "empty") and raw.empty):
        logger.debug("Extended price batch returned empty DataFrame")
        return {t: None for t in tickers}

    for ticker in tickers:
        try:
            # Handle MultiIndex (multi-ticker) vs flat columns (single-ticker)
            if isinstance(raw.columns, pd.MultiIndex):
                if ticker not in raw.columns.get_level_values(0):
                    results[ticker] = None
                    continue
                df = raw[ticker].copy()
            else:
                df = raw.copy()

            df = df.dropna(how="all")
            if df.empty:
                results[ticker] = None
                continue

            last_price = float(df["Close"].iloc[-1])
            last_ts = df.index[-1]

            # Normalise timezone: yfinance intraday returns UTC-aware timestamps;
            # treat tz-naive timestamps as UTC (fallback, should not normally occur).
            if last_ts.tzinfo is None:
                from datetime import timezone as _tz
                last_ts = last_ts.replace(tzinfo=_tz.utc)
            last_et = last_ts.astimezone(_ET)
            t = last_et.time()

            if t < _MARKET_OPEN:
                label = "pre-market"
            elif t >= _MARKET_CLOSE:
                label = "after-hours"
            else:
                # Last bar is within regular market hours — not an extended price
                results[ticker] = None
                continue

            results[ticker] = (round(last_price, 4), label)
            logger.debug(f"Extended price {ticker}: {label} ${last_price:.4f} at {last_et.strftime('%H:%M ET')}")

        except Exception as exc:
            logger.debug(f"Extended price failed for {ticker}: {exc}")
            results[ticker] = None

    with_data = sum(1 for v in results.values() if v is not None)
    logger.info(f"Extended prices fetched: {with_data}/{len(tickers)} tickers with extended-hours data")
    return results
