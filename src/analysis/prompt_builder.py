"""
Prompt builder: assembles the system + user prompt sent to the AI.

Handles two prompt types:
  - Stage 1 (discovery): build_discovery_prompt() — broad market scan
  - Stage 2 (analysis): build() — deep-dive on discovered candidates

Reads prompt templates from config/prompts.yaml and fills in live market data
from the data objects produced by the fetcher.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional, Union

import yaml

from src.utils.config_loader import AppConfig
from src.utils.logger import get_logger
from src.data.fetcher import MarketSnapshot, AssetMarketData, BroadMarketData
from src.data.newsapi_client import NewsArticle
from src.data.rss_client import RSSArticle
from src.data.finnhub_client import FinnhubArticle
from src.data.marketaux_client import MarketauxArticle
from src.data.newsdata_client import NewsDataArticle
from src.data.fred_client import EconomicIndicators
from src.data.adanos_client import AdanosTickerSentiment
from src.data.apewisdom_client import ApeWisdomEntry

logger = get_logger("analysis.prompt_builder")

NewsItem = Union[NewsArticle, RSSArticle, FinnhubArticle, MarketauxArticle, NewsDataArticle]


def _load_prompts(path: str = "config/prompts.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _format_headlines(articles: list, max_items: int = 5) -> str:
    """Format a mixed list of news articles from any source."""
    if not articles:
        return "  (no recent headlines available)"
    lines = []
    for a in articles[:max_items]:
        pub = (
            getattr(a, "published_at", None)
            or getattr(a, "datetime", None)
            or getattr(a, "pub_date", None)
            or getattr(a, "published", "")
        )
        source = getattr(a, "source", "") or getattr(a, "source_name", "")
        lines.append(f"  - [{pub}] {a.title} ({source})")
    return "\n".join(lines)


def _format_price(value: float, currency: str = "USD", decimals: int = 4) -> str:
    symbols = {"USD": "$", "HKD": "HK$", "EUR": "€", "GBP": "£", "CNY": "¥"}
    sym = symbols.get(currency.upper(), currency + " ")
    return f"{sym}{value:,.{decimals}f}"


def _deduplicate_news(articles: list, max_items: int = 5) -> list:
    """
    Deduplicate articles from multiple sources by title prefix (first 60 chars).
    Keeps the first occurrence of each unique title.
    """
    seen: set[str] = set()
    result = []
    for a in articles:
        key = a.title[:60].lower().strip()
        if key not in seen:
            seen.add(key)
            result.append(a)
            if len(result) >= max_items:
                break
    return result


def _build_adanos_block(
    reddit: Optional[AdanosTickerSentiment],
    x_sent: Optional[AdanosTickerSentiment],
    polymarket: Optional[AdanosTickerSentiment],
) -> str:
    """Format Adanos social sentiment into a compact block."""
    lines = []
    if reddit:
        buzz = f"{reddit.buzz_score:.0f}/100" if reddit.buzz_score is not None else "N/A"
        score = f"{reddit.sentiment_score:+.2f}" if reddit.sentiment_score is not None else "N/A"
        bull = f"{reddit.bullish_pct:.0f}%" if reddit.bullish_pct is not None else ""
        bear = f"{reddit.bearish_pct:.0f}%" if reddit.bearish_pct is not None else ""
        trend = f" ({reddit.trend})" if reddit.trend else ""
        mentions = f", {reddit.mentions:,} mentions" if reddit.mentions else ""
        bull_bear = f", {bull} bull/{bear} bear" if bull else ""
        lines.append(f"  Reddit: Buzz {buzz}, Sentiment {score}{trend}{mentions}{bull_bear}")
    if x_sent:
        buzz = f"{x_sent.buzz_score:.0f}/100" if x_sent.buzz_score is not None else "N/A"
        score = f"{x_sent.sentiment_score:+.2f}" if x_sent.sentiment_score is not None else "N/A"
        trend = f" ({x_sent.trend})" if x_sent.trend else ""
        lines.append(f"  X.com:  Buzz {buzz}, Sentiment {score}{trend}")
    if polymarket:
        buzz = f"{polymarket.buzz_score:.0f}/100" if polymarket.buzz_score is not None else "N/A"
        score = f"{polymarket.sentiment_score:+.2f}" if polymarket.sentiment_score is not None else "N/A"
        lines.append(f"  Polymarket: Buzz {buzz}, Sentiment {score}")
    return "\n".join(lines) if lines else ""


def _build_apewisdom_line(entry: Optional[ApeWisdomEntry]) -> str:
    """Format ApeWisdom Reddit ranking into a single line."""
    if not entry:
        return ""
    rank_change = ""
    if entry.rank_24h_ago is not None:
        delta = entry.rank_24h_ago - entry.rank  # positive = climbed
        if delta > 0:
            rank_change = f" (↑ from #{entry.rank_24h_ago})"
        elif delta < 0:
            rank_change = f" (↓ from #{entry.rank_24h_ago})"
        else:
            rank_change = " (stable)"
    mentions_str = f", {entry.mentions:,} mentions" if entry.mentions else ""
    return f"  ApeWisdom Reddit rank: #{entry.rank}{rank_change}{mentions_str}"


def _build_stock_section(assets: list[AssetMarketData], prompts: dict) -> str:
    if not assets:
        return ""
    lines = [prompts.get("stock_section_header", "### STOCKS\n")]
    for amd in assets:
        if amd.data_unavailable:
            lines.append(f"**{amd.ticker} — {amd.name}**\n  DATA UNAVAILABLE — excluded from analysis\n")
            continue
        o = amd.ohlcv

        # Alpha Vantage sentiment string
        sentiment_str = "N/A"
        if amd.sentiment:
            s = amd.sentiment
            sentiment_str = (
                f"{s.overall_sentiment} (score: {s.overall_score:+.2f}, "
                f"bull/neut/bear: {s.bullish_count}/{s.neutral_count}/{s.bearish_count})"
            )

        # Merge all news sources; AV headlines first, then NewsAPI/RSS, Finnhub, Marketaux, NewsData
        all_news: list = []
        if amd.sentiment and amd.sentiment.top_headlines:
            for h in amd.sentiment.top_headlines:
                class _AVH:
                    title = h
                    published_at = ""
                    source = "AlphaVantage"
                all_news.append(_AVH())  # type: ignore[arg-type]
        all_news.extend(amd.news)
        all_news.extend(amd.finnhub_news)
        all_news.extend(amd.marketaux_news)
        all_news.extend(amd.newsdata_news)
        top_news = _deduplicate_news(all_news, max_items=5)

        # Finnhub fundamentals block
        fundamentals_str = ""
        if amd.finnhub_metrics:
            m = amd.finnhub_metrics
            parts = []
            if m.pe_ratio is not None:
                parts.append(f"P/E: {m.pe_ratio:.1f}x")
            if m.week_52_high is not None and m.week_52_low is not None:
                parts.append(
                    f"52W: {_format_price(m.week_52_high, o.currency, 2)}/"
                    f"{_format_price(m.week_52_low, o.currency, 2)}"
                )
            if m.beta is not None:
                parts.append(f"Beta: {m.beta:.2f}")
            analyst_total = m.analyst_buy + m.analyst_hold + m.analyst_sell
            if analyst_total > 0:
                parts.append(f"Analysts: {m.analyst_buy}B/{m.analyst_hold}H/{m.analyst_sell}S")
            if m.price_target_mean is not None:
                pt_str = f"Target: {_format_price(m.price_target_mean, o.currency, 2)}"
                if m.price_target_high and m.price_target_low:
                    pt_str += (
                        f" (H:{_format_price(m.price_target_high, o.currency, 2)}"
                        f"/L:{_format_price(m.price_target_low, o.currency, 2)})"
                    )
                parts.append(pt_str)
            if parts:
                fundamentals_str = f"Fundamentals: {' | '.join(parts)}\n"

        # Social sentiment block
        adanos_block = _build_adanos_block(amd.adanos_reddit, amd.adanos_x, amd.adanos_polymarket)
        ape_line = _build_apewisdom_line(amd.apewisdom)
        social_section = ""
        if adanos_block or ape_line:
            social_lines = ["Social Sentiment:"]
            if adanos_block:
                social_lines.append(adanos_block)
            if ape_line:
                social_lines.append(ape_line)
            social_section = "\n".join(social_lines) + "\n"

        # Build price header — put extended price first so AI uses it as current_price
        if o.extended_price is not None and o.extended_label:
            pct_str = (
                f", {o.extended_pct:+.2f}% vs ${o.current_price:,.2f} prev close"
                if o.extended_pct is not None else ""
            )
            price_header = (
                f"  Current price: {_format_price(o.extended_price, o.currency)} "
                f"({o.extended_label}{pct_str})\n"
                f"  Prev close: {_format_price(o.current_price, o.currency)} | "
                f"Open: {_format_price(o.open_price, o.currency)} | "
                f"High: {_format_price(o.day_high, o.currency)} | "
                f"Low: {_format_price(o.day_low, o.currency)}\n"
            )
        else:
            price_header = (
                f"  Current price: {_format_price(o.current_price, o.currency)} | "
                f"Open: {_format_price(o.open_price, o.currency)} | "
                f"High: {_format_price(o.day_high, o.currency)} | "
                f"Low: {_format_price(o.day_low, o.currency)}\n"
            )

        block = (
            f"**{o.ticker} — {o.name}** ({o.currency})\n"
            f"Price data:\n"
            f"{price_header}"
            f"  5d change: {o.pct_5d:+.2f}% | 20d change: {o.pct_20d:+.2f}%\n"
            f"  Volume vs 20d avg: {o.vol_ratio:.2f}x\n"
            f"  MA(20): {_format_price(o.ma20, o.currency)} | RSI(14): {o.rsi}\n"
            f"{fundamentals_str}"
            f"Alpha Vantage sentiment: {sentiment_str}\n"
            f"{social_section}"
            f"Recent headlines:\n{_format_headlines(top_news)}\n"
        )
        lines.append(block)
    return "\n".join(lines)


def _build_commodity_section(assets: list[AssetMarketData], prompts: dict) -> str:
    if not assets:
        return ""
    lines = [prompts.get("commodity_section_header", "### COMMODITIES\n")]
    for amd in assets:
        if amd.data_unavailable:
            lines.append(f"**{amd.name}**\n  DATA UNAVAILABLE — excluded from analysis\n")
            continue
        o = amd.ohlcv

        all_news: list = list(amd.news) + list(amd.marketaux_news) + list(amd.newsdata_news)
        top_news = _deduplicate_news(all_news, max_items=5)

        # Build commodity price line — put extended price first when available
        if o.extended_price is not None and o.extended_label:
            pct_str = (
                f", {o.extended_pct:+.2f}% vs ${o.current_price:,.2f} prev close"
                if o.extended_pct is not None else ""
            )
            commodity_price_line = (
                f"  Current price: {_format_price(o.extended_price, o.currency)} "
                f"({o.extended_label}{pct_str})\n"
                f"  Prev close: {_format_price(o.current_price, o.currency)} | "
                f"5d change: {o.pct_5d:+.2f}%\n"
            )
        else:
            commodity_price_line = (
                f"  Current price: {_format_price(o.current_price, o.currency)} | "
                f"5d change: {o.pct_5d:+.2f}%\n"
            )

        block = (
            f"**{o.name}** ({o.currency}/unit)\n"
            f"{commodity_price_line}"
            f"  20d high: {_format_price(o.day_high, o.currency)} | "
            f"20d low: {_format_price(o.day_low, o.currency)}\n"
            f"  MA(20): {_format_price(o.ma20, o.currency)} | RSI(14): {o.rsi}\n"
            f"Recent headlines:\n{_format_headlines(top_news)}\n"
        )
        lines.append(block)
    return "\n".join(lines)


def _build_macro_section(snapshot: MarketSnapshot) -> str:
    if not snapshot.macro_headlines:
        return "(no macro headlines available)"
    lines = []
    for a in snapshot.macro_headlines[:5]:
        pub = getattr(a, "published", "")
        lines.append(f"  - [{pub}] {a.title} ({a.source})")
    return "\n".join(lines)


def _build_economic_section(indicators: Optional[EconomicIndicators]) -> str:
    """Format FRED economic indicators into a compact table for the AI prompt."""
    if not indicators or indicators.error or not indicators.observations:
        return "(economic data unavailable)"

    lines = []
    for obs in indicators.observations:
        if obs.value is None:
            continue
        value_str = f"{obs.value:.2f}"
        prev_str = ""
        if obs.prev_value is not None:
            delta = obs.value - obs.prev_value
            prev_str = f" | prev: {obs.prev_value:.2f} ({delta:+.2f})"
        lines.append(f"  {obs.name}: {value_str} ({obs.date}){prev_str}")

    # Append yield curve spread if both 10Y and 2Y are available
    spread = indicators.yield_curve_spread
    if spread is not None:
        curve_label = "normal curve" if spread >= 0 else "INVERTED"
        lines.append(f"  Yield Curve (10Y-2Y): {spread:+.2f}% ({curve_label})")

    return "\n".join(lines) if lines else "(economic data unavailable)"


# ── Stage 1: Discovery prompt ──────────────────────────────────────────────────

def build_discovery_prompt(
    config: AppConfig,
    broad_data: BroadMarketData,
    previous_bad_response: str | None = None,
    prompts_path: str = "config/prompts.yaml",
) -> dict[str, str]:
    """
    Build the Stage 1 discovery prompt.

    The AI receives the ApeWisdom trending list, macro headlines, and FRED data,
    and is asked to select up to max_candidates stocks from the trending list.

    Returns:
        {"system": str, "user": str}
    """
    prompts = _load_prompts(prompts_path)
    max_candidates = config.discovery.max_candidates
    top_n = config.discovery.apewisdom_top_n

    # System prompt
    system = prompts["discovery_system_prompt"].format(
        max_candidates=max_candidates,
    )

    # Build ApeWisdom trending table (includes mention velocity + 5d price if available)
    trending_table = _build_trending_table(
        broad_data.trending_stocks, top_n, broad_data.candidate_prices
    )

    # Economic indicators
    economic_section = _build_economic_section(broad_data.economic_indicators)

    # Macro headlines
    if broad_data.macro_headlines:
        macro_lines = []
        for a in broad_data.macro_headlines[:5]:
            pub = getattr(a, "published", "")
            macro_lines.append(f"  - [{pub}] {a.title} ({a.source})")
        macro_headlines = "\n".join(macro_lines)
    else:
        macro_headlines = "  (no macro headlines available)"

    # Gold price context
    gold_context = "(Gold price data unavailable)"
    if broad_data.gold_ohlcv and not broad_data.gold_ohlcv.error:
        g = broad_data.gold_ohlcv
        if g.extended_price is not None and g.extended_label:
            pct_str = (
                f", {g.extended_pct:+.2f}% vs ${g.current_price:,.2f} prev close"
                if g.extended_pct is not None else ""
            )
            gold_context = (
                f"Gold (GC=F): ${g.extended_price:,.2f}/troy oz "
                f"(current — {g.extended_label}{pct_str})"
            )
        else:
            gold_context = f"Gold (GC=F): ${g.current_price:,.2f}/troy oz"
        gold_context += (
            f" | 5d change: {g.pct_5d:+.2f}% | "
            f"RSI(14): {g.rsi} | MA(20): ${g.ma20:,.2f}"
        )

    run_dt = datetime.now().strftime("%Y-%m-%d %H:%M")
    user = prompts["discovery_user_template"].format(
        run_datetime=run_dt,
        trending_stocks_table=trending_table,
        economic_indicators_section=economic_section,
        macro_headlines=macro_headlines,
        gold_context=gold_context,
        max_candidates=max_candidates,
    )

    # Append retry suffix if a previous bad response is provided
    if previous_bad_response:
        retry_suffix = prompts.get("retry_suffix", "")
        user += "\n" + retry_suffix.format(previous_response=previous_bad_response[:1000])

    logger.info(
        f"Discovery prompt built: system={len(system)} chars, user={len(user)} chars, "
        f"trending_stocks={len(broad_data.trending_stocks.data) if broad_data.trending_stocks else 0}"
        f"{' (retry)' if previous_bad_response else ''}"
    )
    return {"system": system, "user": user}


def _build_trending_table(
    snapshot: Optional["ApeWisdomSnapshot"],
    top_n: int,
    candidate_prices: dict[str, float] | None = None,
) -> str:
    """
    Format the ApeWisdom trending list as a markdown table.

    Columns: Rank | Ticker | Name | Mentions | MentionΔ% | RankΔ(24h) | 5d Price

    MentionΔ% measures mention acceleration vs 24h ago — high velocity signals
    early-stage buzz where price may not yet have reacted.
    5d Price shows recent price movement — flat/negative price + rising buzz = opportunity.
    """
    if not snapshot or snapshot.error or not snapshot.data:
        return "(trending data unavailable — no ApeWisdom data fetched)"

    entries = sorted(snapshot.data.values(), key=lambda e: e.rank)[:top_n]
    if not entries:
        return "(no trending stocks found)"

    rows = []
    for e in entries:
        # Rank change vs 24h ago
        if e.rank_24h_ago is not None:
            delta = e.rank_24h_ago - e.rank  # positive = climbed
            if delta > 0:
                rank_change = f"↑{delta}"
            elif delta < 0:
                rank_change = f"↓{abs(delta)}"
            else:
                rank_change = "—"
        else:
            rank_change = "—"

        # Mention velocity: % change in mentions vs 24h ago
        if e.mentions_24h_ago and e.mentions_24h_ago > 0:
            velocity = (e.mentions - e.mentions_24h_ago) / e.mentions_24h_ago * 100
            velocity_str = f"{velocity:+.0f}%"
        else:
            velocity_str = "—"

        # 5-day price change (from Phase 1 price check batch)
        pct_5d = (candidate_prices or {}).get(e.ticker)
        price_str = f"{pct_5d:+.1f}%" if pct_5d is not None else "—"

        rows.append(
            f"  {e.rank:>4} | {e.ticker:<8} | {e.name[:20]:<20} | "
            f"{e.mentions:>8,} | {velocity_str:>10} | {rank_change:>10} | {price_str:>9}"
        )
    return "\n".join(rows)


# ── Stage 2: Analysis prompt ───────────────────────────────────────────────────

def build(
    config: AppConfig,
    snapshot: MarketSnapshot,
    previous_bad_response: str | None = None,
    prompts_path: str = "config/prompts.yaml",
) -> dict[str, str]:
    """
    Build the Stage 2 analysis prompt for the discovered candidates.

    Returns:
        {"system": str, "user": str}
    """
    prompts = _load_prompts(prompts_path)
    appetite = config.risk.appetite
    max_recommendations = config.discovery.max_recommendations

    # System prompt
    risk_constraints = prompts["risk_constraints"][appetite]
    system = prompts["system_prompt"].format(
        risk_appetite=appetite,
        risk_constraints=risk_constraints,
        max_recommendations=max_recommendations,
    )

    # User message sections
    stock_section = _build_stock_section(snapshot.stocks, prompts)
    commodity_section = _build_commodity_section(snapshot.commodities, prompts)
    macro_headlines = _build_macro_section(snapshot)
    economic_indicators_section = _build_economic_section(snapshot.economic_indicators)

    # Asset count summary
    available = [a for a in snapshot.all_assets() if not a.data_unavailable]
    type_counts: dict[str, int] = {}
    for a in available:
        type_counts[a.asset_type] = type_counts.get(a.asset_type, 0) + 1
    asset_type_summary = ", ".join(f"{v} {k}(s)" for k, v in type_counts.items())

    run_dt = datetime.now().strftime("%Y-%m-%d %H:%M")

    user = prompts["user_message_template"].format(
        run_datetime=run_dt,
        risk_appetite=appetite,
        asset_count=len(available),
        asset_type_summary=asset_type_summary,
        max_recommendations=max_recommendations,
        stock_section=stock_section,
        commodity_section=commodity_section,
        macro_headlines=macro_headlines,
        economic_indicators_section=economic_indicators_section,
    )

    # Append retry suffix if a previous bad response is provided
    if previous_bad_response:
        retry_suffix = prompts.get("retry_suffix", "")
        user += "\n" + retry_suffix.format(previous_response=previous_bad_response[:1000])

    logger.info(
        f"Analysis prompt built: system={len(system)} chars, user={len(user)} chars, "
        f"assets={len(available)} (retry={'yes' if previous_bad_response else 'no'})"
    )
    return {"system": system, "user": user}
