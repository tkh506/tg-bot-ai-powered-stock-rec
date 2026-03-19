"""
Backtester: evaluates historical BUY/SELL signals against actual price outcomes.

Logic:
  1. Query the signals table for BUY/SELL entries ≥7 calendar days old (≈5 trading days)
     that have not yet been evaluated (no matching row in backtest_results).
  2. Fetch daily close-price history for all pending tickers in a single yfinance batch call.
  3. For each signal, find the closing price 5 trading days after the signal date.
  4. Compute outcome:
       BUY  → CORRECT if price rose, INCORRECT if fell
       SELL → CORRECT if price fell, INCORRECT if rose
       INCONCLUSIVE if price data is unavailable.
  5. Persist results to backtest_results table.
  6. Compute all-time accuracy stats and send a Telegram report (or print if dry_run).

Run:
    python -m src.main --backtest
    python -m src.main --backtest --dry-run   # print to stdout, skip Telegram
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import yfinance as yf

from src.utils.config_loader import AppConfig
from src.utils.logger import get_logger
from src.notifications import telegram_bot

logger = get_logger("analysis.backtester")

# Signals must be at least this many calendar days old before evaluation
# (~5 trading days accounting for weekends)
_MIN_DAYS_ELAPSED = 7

_BACKTEST_SCHEMA = """
CREATE TABLE IF NOT EXISTS backtest_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id       INTEGER UNIQUE REFERENCES signals(id),
    evaluated_at    TIMESTAMP NOT NULL,
    ticker          TEXT NOT NULL,
    signal          TEXT,
    price_at_signal REAL,
    price_5d_later  REAL,
    pct_change      REAL,
    outcome         TEXT CHECK(outcome IN ('CORRECT','INCORRECT','INCONCLUSIVE'))
);
CREATE INDEX IF NOT EXISTS idx_backtest_ticker ON backtest_results(ticker, evaluated_at);
"""


# ── Helpers ────────────────────────────────────────────────────────────────────

def _parse_price(price_str: str | None) -> Optional[float]:
    """Parse '$1,234.56' or '1234.56' to float. Returns None on failure."""
    if not price_str:
        return None
    try:
        clean = re.sub(r"[^\d.]", "", str(price_str))
        return float(clean) if clean else None
    except ValueError:
        return None


def _fetch_price_histories(
    tickers: list[str],
    earliest_date: datetime,
) -> dict[str, pd.Series]:
    """
    Fetch daily close prices for all tickers in a single yfinance batch call.

    Returns {ticker: pd.Series of Close prices indexed by date}.
    Sequential call only — never call this inside a thread pool (LESSON 4).
    """
    if not tickers:
        return {}

    # Start one day before the earliest signal to guarantee the signal date is included
    start_str = (earliest_date - timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        raw = yf.download(
            tickers,
            start=start_str,
            progress=False,
            auto_adjust=True,
            group_by="ticker",
        )
    except Exception as exc:
        logger.warning(f"Backtest price download failed: {exc}")
        return {}

    if raw is None or (hasattr(raw, "empty") and raw.empty):
        logger.warning("Backtest price download returned empty DataFrame")
        return {}

    result: dict[str, pd.Series] = {}
    for ticker in tickers:
        try:
            if isinstance(raw.columns, pd.MultiIndex):
                if ticker not in raw.columns.get_level_values(0):
                    logger.debug(f"Backtest: ticker {ticker} missing from download result")
                    continue
                closes = raw[ticker]["Close"].dropna()
            else:
                # Single-ticker download returns flat columns
                closes = raw["Close"].dropna()
            result[ticker] = closes
        except Exception as exc:
            logger.debug(f"Backtest price extraction failed for {ticker}: {exc}")

    logger.info(f"Backtest price histories fetched: {len(result)}/{len(tickers)} tickers")
    return result


def _get_price_n_trading_days_later(
    closes: pd.Series,
    signal_dt: datetime,
    n: int = 5,
) -> Optional[float]:
    """
    Return the closing price n trading days after signal_dt.
    Returns None if fewer than n trading days of data exist after signal_dt.
    """
    signal_date = signal_dt.date()
    future = closes[closes.index.normalize().date > signal_date]
    if len(future) < n:
        return None
    return float(future.iloc[n - 1])


# ── Main backtest runner ───────────────────────────────────────────────────────

def run_backtest(config: AppConfig, dry_run: bool = False) -> None:
    """
    Evaluate unevaluated BUY/SELL signals and send an accuracy report to Telegram.

    Args:
        config:  full AppConfig (secrets must be loaded)
        dry_run: if True, print report to stdout instead of sending to Telegram
    """
    db_path = config.reporting.archive.sqlite_db_path

    if not Path(db_path).exists():
        logger.info("Backtest: no database found — nothing to evaluate yet")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Ensure backtest_results table exists (handles DBs created before this feature)
    conn.executescript(_BACKTEST_SCHEMA)
    conn.commit()

    # Query unevaluated BUY/SELL signals old enough to have 5 trading days elapsed
    cutoff_dt = (datetime.now() - timedelta(days=_MIN_DAYS_ELAPSED)).isoformat()
    rows = conn.execute(
        """
        SELECT s.id, s.ticker, s.signal, s.current_price, s.run_at
        FROM signals s
        LEFT JOIN backtest_results b ON b.signal_id = s.id
        WHERE s.signal IN ('BUY', 'SELL')
          AND s.run_at <= ?
          AND b.id IS NULL
        """,
        (cutoff_dt,),
    ).fetchall()

    if not rows:
        logger.info("Backtest: no unevaluated signals found — nothing to do")
        conn.close()
        return

    logger.info(f"Backtest: {len(rows)} unevaluated signal(s) found")

    # Parse DB rows into workable dicts; skip rows with unparseable dates
    pending: list[dict] = []
    for row in rows:
        try:
            run_at = datetime.fromisoformat(row["run_at"])
        except (ValueError, TypeError):
            logger.warning(f"Backtest: skipping signal id={row['id']} — bad run_at '{row['run_at']}'")
            continue
        pending.append({
            "id": row["id"],
            "ticker": row["ticker"],
            "signal": row["signal"],
            "price_at_signal": _parse_price(row["current_price"]),
            "run_at": run_at,
        })

    if not pending:
        conn.close()
        return

    # Single batch price fetch for all affected tickers
    unique_tickers = list({p["ticker"] for p in pending})
    earliest = min(p["run_at"] for p in pending)
    histories = _fetch_price_histories(unique_tickers, earliest)

    # Evaluate each signal
    evaluated_at = datetime.now()
    new_results: list[dict] = []

    for p in pending:
        closes = histories.get(p["ticker"])
        price_5d = (
            _get_price_n_trading_days_later(closes, p["run_at"])
            if closes is not None
            else None
        )

        if p["price_at_signal"] is not None and price_5d is not None:
            pct = round((price_5d / p["price_at_signal"] - 1) * 100, 2)
            if p["signal"] == "BUY":
                outcome = "CORRECT" if pct > 0 else "INCORRECT"
            else:  # SELL
                outcome = "CORRECT" if pct < 0 else "INCORRECT"
        else:
            pct = None
            outcome = "INCONCLUSIVE"

        entry_str = f"${p['price_at_signal']:.2f}" if p["price_at_signal"] else "?"
        price_5d_str = f"${price_5d:.2f}" if price_5d is not None else "?"
        pct_str = f"{pct:+.1f}%" if pct is not None else "?"
        logger.info(
            f"  {p['ticker']} {p['signal']} @ {p['run_at'].date()}: "
            f"entry={entry_str}, 5d={price_5d_str}, chg={pct_str}, → {outcome}"
        )

        new_results.append({
            "signal_id": p["id"],
            "evaluated_at": evaluated_at.isoformat(),
            "ticker": p["ticker"],
            "signal": p["signal"],
            "price_at_signal": p["price_at_signal"],
            "price_5d_later": price_5d,
            "pct_change": pct,
            "outcome": outcome,
        })

    # Persist results to DB
    with conn:
        conn.executemany(
            """
            INSERT OR IGNORE INTO backtest_results
                (signal_id, evaluated_at, ticker, signal, price_at_signal,
                 price_5d_later, pct_change, outcome)
            VALUES (:signal_id, :evaluated_at, :ticker, :signal, :price_at_signal,
                    :price_5d_later, :pct_change, :outcome)
            """,
            new_results,
        )
    logger.info(f"Backtest: saved {len(new_results)} result(s) to DB")

    # Pull all-time stats for the summary report (exclude INCONCLUSIVE)
    all_rows = conn.execute(
        """
        SELECT signal, outcome, pct_change
        FROM backtest_results
        WHERE outcome != 'INCONCLUSIVE'
        """
    ).fetchall()
    conn.close()

    report = _format_report(new_results, all_rows, evaluated_at)

    if dry_run:
        print(report)
    else:
        telegram_bot.send_report([report], config)


# ── Report formatter ───────────────────────────────────────────────────────────

def _format_report(
    new_results: list[dict],
    all_rows: list,
    evaluated_at: datetime,
) -> str:
    """Build a Telegram-ready Markdown backtest accuracy report."""
    lines = [
        f"📊 *Weekly Backtest Report* — {evaluated_at.strftime('%Y-%m-%d')}",
        "",
        f"*This batch:* {len(new_results)} signal(s) evaluated",
        "",
    ]

    # Per-signal breakdown for this batch
    for r in sorted(new_results, key=lambda x: x["ticker"]):
        if r["outcome"] == "CORRECT":
            emoji = "✅"
        elif r["outcome"] == "INCORRECT":
            emoji = "❌"
        else:
            emoji = "⚪"
        pct_str = f"{r['pct_change']:+.1f}%" if r["pct_change"] is not None else "N/A"
        lines.append(f"  {emoji} {r['ticker']} {r['signal']}: {pct_str} \\(5d return\\)")

    # All-time accuracy stats
    if all_rows:
        lines.append("")
        lines.append("*All-time accuracy \\(excluding inconclusive\\):*")

        for sig in ("BUY", "SELL"):
            sig_rows = [r for r in all_rows if r["signal"] == sig]
            if not sig_rows:
                continue
            correct = sum(1 for r in sig_rows if r["outcome"] == "CORRECT")
            total = len(sig_rows)
            pcts = [r["pct_change"] for r in sig_rows if r["pct_change"] is not None]
            avg_pct = sum(pcts) / len(pcts) if pcts else 0.0
            lines.append(
                f"  {sig}: {correct}/{total} correct "
                f"\\({correct / total * 100:.0f}%\\) | avg 5d: {avg_pct:+.1f}%"
            )

        all_correct = sum(1 for r in all_rows if r["outcome"] == "CORRECT")
        overall = all_correct / len(all_rows) * 100
        lines.append(
            f"  *Overall: {all_correct}/{len(all_rows)} \\({overall:.0f}% accurate\\)*"
        )

    lines.append("")
    lines.append(
        "_5-day forward return measured from signal entry price\\. "
        "Past accuracy ≠ future results\\._"
    )

    return "\n".join(lines)
