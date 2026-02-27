"""
Report formatter: converts an AnalysisResult into Telegram-compatible Markdown.

Splits into multiple parts automatically if the report exceeds Telegram's 4096-char limit.
Includes a discovery summary showing which candidates were identified in Stage 1.
"""

from __future__ import annotations

from typing import Optional

from src.analysis.response_parser import AnalysisResult, AssetSignal, DiscoveryResult
from src.data.yfinance_client import OHLCVData
from src.utils.config_loader import AppConfig
from src.utils.logger import get_logger

logger = get_logger("reporting.formatter")

SIGNAL_EMOJI = {"BUY": "🟢", "HOLD": "🟡", "SELL": "🔴"}
BIAS_EMOJI = {"bullish": "📈", "neutral": "➡️", "bearish": "📉"}
HORIZON_LABEL = {"short": "Short-term", "medium": "Medium-term", "long": "Long-term"}
TYPE_ORDER = ["stock", "commodity"]
TYPE_LABEL = {"stock": "Stocks", "commodity": "Commodities"}


def _fmt_price(value: float) -> str:
    """Format a numeric price with dollar sign and comma separators."""
    return f"${value:,.2f}"


def _signal_line(
    asset: AssetSignal,
    ohlcv_map: Optional[dict[str, OHLCVData]] = None,
) -> str:
    emoji = SIGNAL_EMOJI.get(asset.signal, "⚪")
    conf = f"{asset.confidence}%"
    horizon = HORIZON_LABEL.get(asset.time_horizon, asset.time_horizon)
    target = f"  Target: {asset.target_price}" if asset.target_price else ""
    stop = f"  Stop: {asset.stop_loss}" if asset.stop_loss else ""
    risks = ""
    if asset.key_risks:
        risk_str = " | ".join(asset.key_risks[:2])
        risks = f"\n  Risks: {risk_str}"

    # Build price display — show extended-hours price + prev close when available
    price_str = asset.current_price or "N/A"
    if ohlcv_map:
        ohlcv = ohlcv_map.get(asset.ticker)
        if ohlcv and ohlcv.extended_price is not None and ohlcv.extended_label:
            pct_str = (
                f" ({ohlcv.extended_pct:+.2f}%)"
                if ohlcv.extended_pct is not None else ""
            )
            price_str = (
                f"{_fmt_price(ohlcv.extended_price)}{pct_str} "
                f"*{ohlcv.extended_label}* | Prev close: {_fmt_price(ohlcv.current_price)}"
            )

    return (
        f"{emoji} *{asset.ticker}* — {asset.signal} ({conf} conf)\n"
        f"  Price: {price_str}{target}{stop}\n"
        f"  {asset.justification}\n"
        f"  Horizon: {horizon}{risks}"
    )


def _discovery_section(discovery_result: Optional[DiscoveryResult]) -> str:
    """
    Render a compact 'Discovered candidates' block showing Stage 1 selections.
    Gives the user transparency into what the AI scanned vs. what it recommended.
    """
    if not discovery_result or not discovery_result.candidates:
        return ""

    # Separate stocks from Gold (exchange == "COMMODITY")
    stocks = [c for c in discovery_result.candidates if c.exchange != "COMMODITY"]
    tickers = [c.ticker for c in stocks]

    lines = ["*Scanned candidates:* " + ", ".join(tickers)]
    if discovery_result.discovery_summary:
        lines.append(f"_{discovery_result.discovery_summary}_")

    return "\n".join(lines) + "\n\n"


def render(
    result: AnalysisResult,
    config: AppConfig,
    discovery_result: Optional[DiscoveryResult] = None,
    ohlcv_map: Optional[dict[str, OHLCVData]] = None,
) -> list[str]:
    """
    Build the full report as a list of Telegram-ready message strings.
    Each string is <= max_message_length characters.

    ohlcv_map: optional {ticker: OHLCVData} for showing real-time/extended-hours
               prices alongside the previous regular-session close.
    """
    max_len = config.reporting.telegram.max_message_length
    parts: list[str] = []
    current_block = ""

    def flush(text: str) -> None:
        nonlocal current_block
        if len(current_block) + len(text) > max_len:
            if current_block.strip():
                parts.append(current_block.strip())
            current_block = text
        else:
            current_block += text

    # ── Header ────────────────────────────────────────────────────────────────
    bias_emoji = BIAS_EMOJI.get(result.portfolio_bias, "➡️")
    header = (
        f"*AI Investment Advisor*\n"
        f"📅 {result.run_date} | Risk: *{result.risk_profile.capitalize()}* | "
        f"Bias: {bias_emoji} *{result.portfolio_bias.capitalize()}*\n\n"
    )
    if config.notifications.send_summary_header and result.macro_summary:
        header += f"*Macro:* {result.macro_summary}\n\n"

    # Discovery candidates block (Stage 1 transparency)
    discovery_block = _discovery_section(discovery_result)
    if discovery_block:
        header += discovery_block

    header += "━━━━━━━━━━━━━━━━━━━━━━\n\n"
    flush(header)

    # ── Assets grouped by type ─────────────────────────────────────────────────
    assets_by_type: dict[str, list[AssetSignal]] = {}
    for asset in result.assets:
        assets_by_type.setdefault(asset.asset_type, []).append(asset)

    for atype in TYPE_ORDER:
        group = assets_by_type.get(atype, [])
        if not group:
            continue
        label = TYPE_LABEL.get(atype, atype.capitalize())
        flush(f"*{label}*\n")
        for asset in group:
            flush(_signal_line(asset, ohlcv_map=ohlcv_map) + "\n\n")
        flush("━━━━━━━━━━━━━━━━━━━━━━\n\n")

    # ── Footer ────────────────────────────────────────────────────────────────
    flush(f"_{result.disclaimer}_")

    if current_block.strip():
        parts.append(current_block.strip())

    logger.info(f"Report formatted into {len(parts)} Telegram message(s)")
    return parts
