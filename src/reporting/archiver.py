"""
Archiver: persists each report run to SQLite and a Markdown file.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from src.analysis.response_parser import AnalysisResult
from src.utils.config_loader import AppConfig
from src.utils.logger import get_logger

logger = get_logger("reporting.archiver")

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    risk_profile        TEXT,
    macro_summary       TEXT,
    portfolio_bias      TEXT,
    report_md           TEXT NOT NULL,
    token_input         INTEGER DEFAULT 0,
    token_output        INTEGER DEFAULT 0,
    data_sources_used   TEXT,
    error_flag          BOOLEAN DEFAULT 0
);

CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id       INTEGER REFERENCES reports(id),
    run_at          TIMESTAMP NOT NULL,
    ticker          TEXT NOT NULL,
    asset_type      TEXT,
    signal          TEXT CHECK(signal IN ('BUY','HOLD','SELL')),
    confidence      INTEGER,
    current_price   TEXT,
    target_price    TEXT,
    stop_loss       TEXT,
    justification   TEXT,
    time_horizon    TEXT,
    sentiment_score TEXT
);

CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals(ticker, run_at);
CREATE INDEX IF NOT EXISTS idx_reports_run_at ON reports(run_at);
"""


def _get_connection(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def save(
    result: AnalysisResult,
    report_md: str,
    config: AppConfig,
    token_input: int = 0,
    token_output: int = 0,
    data_sources_used: list[str] | None = None,
) -> int:
    """
    Persist the report to SQLite + a Markdown file.
    Returns the report ID (SQLite primary key).
    """
    db_path = config.reporting.archive.sqlite_db_path
    md_dir = config.reporting.archive.markdown_dir
    run_at = datetime.now()
    timestamp = run_at.strftime("%Y-%m-%d_%H%M%S")

    # ── Markdown file ─────────────────────────────────────────────────────────
    md_path = Path(md_dir)
    md_path.mkdir(parents=True, exist_ok=True)
    md_file = md_path / f"{timestamp}.md"
    md_file.write_text(report_md, encoding="utf-8")
    logger.info(f"Report archived to {md_file}")

    # ── SQLite ────────────────────────────────────────────────────────────────
    conn = _get_connection(db_path)
    with conn:
        cur = conn.execute(
            """
            INSERT INTO reports
                (run_at, risk_profile, macro_summary, portfolio_bias,
                 report_md, token_input, token_output, data_sources_used)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_at.isoformat(),
                result.risk_profile,
                result.macro_summary,
                result.portfolio_bias,
                report_md,
                token_input,
                token_output,
                json.dumps(data_sources_used or []),
            ),
        )
        report_id = cur.lastrowid

        for asset in result.assets:
            conn.execute(
                """
                INSERT INTO signals
                    (report_id, run_at, ticker, asset_type, signal, confidence,
                     current_price, target_price, stop_loss, justification,
                     time_horizon, sentiment_score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    run_at.isoformat(),
                    asset.ticker,
                    asset.asset_type,
                    asset.signal,
                    asset.confidence,
                    asset.current_price,
                    asset.target_price,
                    asset.stop_loss,
                    asset.justification,
                    asset.time_horizon,
                    asset.sentiment_score,
                ),
            )

    logger.info(f"Report #{report_id} saved to SQLite with {len(result.assets)} signals")
    conn.close()

    # ── Purge old markdown files ───────────────────────────────────────────────
    _purge_old_archives(md_path, config.reporting.archive.retention_days)

    return report_id


def _purge_old_archives(md_dir: Path, retention_days: int) -> None:
    """Delete markdown archive files older than retention_days."""
    cutoff = datetime.now().timestamp() - retention_days * 86400
    deleted = 0
    for f in md_dir.glob("*.md"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            deleted += 1
    if deleted:
        logger.info(f"Purged {deleted} old archive file(s)")
