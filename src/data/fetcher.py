"""
Parallel data fetcher orchestrator.

Two-stage discovery pipeline:

  Phase 1 — fetch_broad_market_data()
    Fetches non-asset-specific market overview data for the Stage 1 AI discovery call.
    Returns BroadMarketData: ApeWisdom trending list, macro RSS, FRED, Gold OHLCV.

  Phase 2 — fetch_targeted_data()
    Accepts the list of Candidate tickers discovered in Stage 1.
    Fetches full deep-dive data (OHLCV, news, sentiment, fundamentals) per candidate.
    Returns MarketSnapshot for the Stage 2 AI analysis call.

Graceful degradation: a failed source logs a warning and contributes empty data;
the pipeline continues with whatever data is available.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional

from src.utils.config_loader import AppConfig
from src.utils.logger import get_logger
from src.data.yfinance_client import OHLCVData, fetch_ohlcv_batch, fetch_extended_prices
from src.data.newsapi_client import NewsArticle, fetch_news_for_asset, NewsAPIQuotaExhausted
from src.data.alphavantage_client import SentimentData, fetch_sentiment
from src.data.rss_client import RSSArticle, fetch_asset_news_from_rss, fetch_macro_headlines
from src.data.finnhub_client import FinnhubArticle, FinnhubMetrics, fetch_finnhub_news, fetch_finnhub_metrics
from src.data.marketaux_client import MarketauxArticle, fetch_marketaux_news
from src.data.newsdata_client import NewsDataArticle, fetch_newsdata_news
from src.data.fred_client import EconomicIndicators, fetch_economic_indicators
from src.data.adanos_client import AdanosTickerSentiment, AdanosBatch, fetch_adanos_batch
from src.data.apewisdom_client import ApeWisdomEntry, ApeWisdomSnapshot, fetch_apewisdom

# Imported here to avoid circular imports: Candidate is defined in response_parser
from src.analysis.response_parser import Candidate

logger = get_logger("data.fetcher")

GOLD_TICKER = "GC=F"
GOLD_NAME = "Gold"
GOLD_CURRENCY = "USD"


@dataclass
class AssetMarketData:
    """All collected data for a single asset."""
    ticker: str
    name: str
    asset_type: str                              # stock | commodity
    ohlcv: Optional[OHLCVData] = None            # from yfinance
    news: list[NewsArticle | RSSArticle] = field(default_factory=list)
    sentiment: Optional[SentimentData] = None    # from Alpha Vantage (stocks only)
    # Finnhub
    finnhub_news: list[FinnhubArticle] = field(default_factory=list)
    finnhub_metrics: Optional[FinnhubMetrics] = None
    # Additional news
    marketaux_news: list[MarketauxArticle] = field(default_factory=list)
    newsdata_news: list[NewsDataArticle] = field(default_factory=list)
    # Social sentiment (stocks only)
    adanos_reddit: Optional[AdanosTickerSentiment] = None
    adanos_x: Optional[AdanosTickerSentiment] = None
    adanos_polymarket: Optional[AdanosTickerSentiment] = None
    apewisdom: Optional[ApeWisdomEntry] = None
    data_unavailable: bool = False               # True if ALL price sources failed


@dataclass
class MarketSnapshot:
    """Full snapshot of all fetched data for a single pipeline run."""
    stocks: list[AssetMarketData] = field(default_factory=list)
    commodities: list[AssetMarketData] = field(default_factory=list)
    macro_headlines: list[RSSArticle] = field(default_factory=list)
    economic_indicators: Optional[EconomicIndicators] = None
    data_sources_used: list[str] = field(default_factory=list)

    def all_assets(self) -> list[AssetMarketData]:
        return self.stocks + self.commodities


@dataclass
class BroadMarketData:
    """Phase 1 data — non-asset-specific market overview for Stage 1 AI discovery."""
    trending_stocks: Optional[ApeWisdomSnapshot] = None   # ApeWisdom top N
    macro_headlines: list[RSSArticle] = field(default_factory=list)
    economic_indicators: Optional[EconomicIndicators] = None
    gold_ohlcv: Optional[OHLCVData] = None                # GC=F price context for Stage 1
    data_sources_used: list[str] = field(default_factory=list)


def _merge_extended_prices(
    ohlcv_map: dict[str, OHLCVData],
    extended: dict[str, tuple[float, str] | None],
) -> None:
    """Merge extended-hours price data into OHLCVData objects in place."""
    for ticker, ext in extended.items():
        if ext is None:
            continue
        ohlcv = ohlcv_map.get(ticker)
        if not ohlcv or ohlcv.error:
            continue
        price, label = ext
        ohlcv.extended_price = price
        ohlcv.extended_label = label
        if ohlcv.current_price and ohlcv.current_price != 0:
            ohlcv.extended_pct = round((price / ohlcv.current_price - 1) * 100, 2)


def fetch_broad_market_data(config: AppConfig) -> BroadMarketData:
    """
    Phase 1: fetch broad, non-asset-specific market data.

    Fetches in parallel:
      - ApeWisdom top trending stocks (discovery universe for Stage 1 AI)
      - RSS macro headlines
      - FRED economic indicators
      - Gold OHLCV (GC=F) — provides price context in the discovery prompt

    Returns BroadMarketData.
    """
    broad = BroadMarketData()
    ds = config.data_sources
    secrets = config.secrets

    futures_map: dict = {}
    with ThreadPoolExecutor(max_workers=8) as executor:

        # ApeWisdom trending stocks — the discovery universe
        if ds.apewisdom.enabled:
            futures_map[executor.submit(fetch_apewisdom, "all-stocks")] = ("apewisdom", "stocks")

        # Macro RSS headlines
        if ds.rss.enabled:
            rss_feeds = [{"name": f.name, "url": f.url} for f in ds.rss.feeds]
            futures_map[executor.submit(
                fetch_macro_headlines, rss_feeds, ds.rss.max_items_per_feed, 5
            )] = ("rss", "macro")

        # FRED economic indicators
        if ds.fred.enabled and secrets and secrets.fred_key:
            futures_map[executor.submit(
                fetch_economic_indicators, secrets.fred_key, ds.fred.series
            )] = ("fred", "indicators")

        # Gold OHLCV — single-ticker batch for context
        if ds.yfinance.enabled:
            futures_map[executor.submit(
                fetch_ohlcv_batch,
                [(GOLD_TICKER, GOLD_NAME, GOLD_CURRENCY)],
                ds.yfinance.lookback_days, ds.yfinance.interval,
            )] = ("yfinance", "gold")

        # Collect results
        raw: dict = {}
        for future in as_completed(futures_map):
            key = futures_map[future]
            try:
                raw[key] = future.result()
            except Exception as e:
                logger.warning(f"Broad fetch failed for {key}: {e}")
                raw[key] = None

    # ApeWisdom
    aw_snap: ApeWisdomSnapshot | None = raw.get(("apewisdom", "stocks"))
    if aw_snap and not aw_snap.error:
        broad.trending_stocks = aw_snap
        broad.data_sources_used.append("ApeWisdom")
        logger.info(f"ApeWisdom: {len(aw_snap.data)} trending stocks fetched")
    else:
        logger.warning("ApeWisdom fetch failed or returned no data — Stage 1 discovery will be limited")

    # RSS macro headlines
    rss_result = raw.get(("rss", "macro"))
    if rss_result:
        broad.macro_headlines = rss_result
        broad.data_sources_used.append("RSS")

    # FRED
    fred_result: EconomicIndicators | None = raw.get(("fred", "indicators"))
    if fred_result and not fred_result.error:
        broad.economic_indicators = fred_result
        broad.data_sources_used.append("FRED")

    # Gold OHLCV
    yf_batch: dict[str, OHLCVData] = raw.get(("yfinance", "gold")) or {}
    gold_ohlcv = yf_batch.get(GOLD_TICKER)
    if gold_ohlcv and not gold_ohlcv.error:
        broad.gold_ohlcv = gold_ohlcv
        broad.data_sources_used.append("yfinance")
        # Enrich Gold with latest extended-hours price (sequential — never concurrent)
        try:
            extended = fetch_extended_prices([GOLD_TICKER])
            _merge_extended_prices({GOLD_TICKER: gold_ohlcv}, extended)
        except Exception as e:
            logger.warning(f"Gold extended price fetch failed: {e}")

    logger.info(f"Broad market data fetched. Sources: {broad.data_sources_used}")
    return broad


def fetch_targeted_data(
    config: AppConfig,
    candidates: list[Candidate],
    broad_data: BroadMarketData,
) -> MarketSnapshot:
    """
    Phase 2: fetch deep-dive data for the discovered candidates.

    For each stock candidate: yfinance OHLCV, NewsAPI, Alpha Vantage, Finnhub news+metrics,
    Marketaux, NewsData, Adanos (Reddit/X/Polymarket), ApeWisdom lookup (from broad_data).
    For Gold: yfinance OHLCV (already in broad_data), NewsAPI, Marketaux, NewsData.

    Returns a MarketSnapshot ready for Stage 2 AI analysis.
    """
    snapshot = MarketSnapshot()
    # Carry over Phase 1 macro data (already fetched — no re-fetch needed)
    snapshot.macro_headlines = broad_data.macro_headlines
    snapshot.economic_indicators = broad_data.economic_indicators
    if broad_data.macro_headlines:
        snapshot.data_sources_used.append("RSS")
    if broad_data.economic_indicators:
        snapshot.data_sources_used.append("FRED")

    ds = config.data_sources
    secrets = config.secrets

    # Split candidates into stock candidates and Gold
    stock_candidates = [c for c in candidates if c.exchange != "COMMODITY"]
    gold_included = any(c.exchange == "COMMODITY" for c in candidates)

    if not stock_candidates and not gold_included:
        logger.warning("No candidates to fetch targeted data for")
        return snapshot

    # ApeWisdom data from Phase 1 — use for O(1) per-ticker lookup
    aw_stocks_data = (
        broad_data.trending_stocks.data
        if broad_data.trending_stocks and not broad_data.trending_stocks.error
        else {}
    )
    rss_feeds = [{"name": f.name, "url": f.url} for f in ds.rss.feeds]

    futures_map: dict = {}
    newsapi_quota_exhausted = False

    with ThreadPoolExecutor(max_workers=20) as executor:

        # ── yfinance: single batch for all stocks + Gold ───────────────────────
        if ds.yfinance.enabled:
            yf_assets = [(c.ticker, c.name, "USD") for c in stock_candidates]
            if gold_included:
                yf_assets.append((GOLD_TICKER, GOLD_NAME, GOLD_CURRENCY))
            if yf_assets:
                futures_map[executor.submit(
                    fetch_ohlcv_batch, yf_assets,
                    ds.yfinance.lookback_days, ds.yfinance.interval,
                )] = ("yfinance", "batch")

        # ── Per-stock sources ──────────────────────────────────────────────────

        # NewsAPI — stocks
        if ds.newsapi.enabled and secrets and secrets.newsapi_key:
            for cand in stock_candidates:
                futures_map[executor.submit(
                    fetch_news_for_asset, cand.name, secrets.newsapi_key,
                    ds.newsapi.max_articles_per_asset,
                    ds.newsapi.hours_lookback, ds.newsapi.language, ds.newsapi.sort_by,
                )] = ("newsapi", cand.ticker, "stock")
            # NewsAPI — Gold
            if gold_included:
                futures_map[executor.submit(
                    fetch_news_for_asset, GOLD_NAME, secrets.newsapi_key,
                    ds.newsapi.max_articles_per_asset,
                    ds.newsapi.hours_lookback, ds.newsapi.language, ds.newsapi.sort_by,
                )] = ("newsapi", GOLD_TICKER, "commodity")

        # Alpha Vantage sentiment — all discovered stocks are US-listed
        if ds.alphavantage.enabled and secrets and secrets.alphavantage_key:
            for cand in stock_candidates:
                futures_map[executor.submit(
                    fetch_sentiment, cand.ticker, secrets.alphavantage_key,
                    ds.alphavantage.sentiment_limit,
                )] = ("alphavantage", cand.ticker)

        # Finnhub news — all stocks
        if ds.finnhub.enabled and secrets and secrets.finnhub_key:
            for cand in stock_candidates:
                futures_map[executor.submit(
                    fetch_finnhub_news, cand.ticker, secrets.finnhub_key,
                    ds.finnhub.hours_lookback, ds.finnhub.max_news_per_stock,
                )] = ("finnhub_news", cand.ticker)
            # Finnhub metrics — all discovered stocks are US-listed
            if ds.finnhub.include_metrics:
                for cand in stock_candidates:
                    futures_map[executor.submit(
                        fetch_finnhub_metrics, cand.ticker, secrets.finnhub_key,
                    )] = ("finnhub_metrics", cand.ticker)

        # Marketaux — stocks and Gold
        if ds.marketaux.enabled and secrets and secrets.marketaux_key:
            for cand in stock_candidates:
                futures_map[executor.submit(
                    fetch_marketaux_news, cand.ticker, secrets.marketaux_key,
                    ds.marketaux.max_articles_per_asset,
                )] = ("marketaux", cand.ticker)
            if gold_included:
                futures_map[executor.submit(
                    fetch_marketaux_news, GOLD_TICKER, secrets.marketaux_key,
                    ds.marketaux.max_articles_per_asset,
                )] = ("marketaux", GOLD_TICKER)

        # NewsData — stocks and Gold
        if ds.newsdata.enabled and secrets and secrets.newsdata_key:
            for cand in stock_candidates:
                futures_map[executor.submit(
                    fetch_newsdata_news, cand.name, secrets.newsdata_key,
                    ds.newsdata.max_articles_per_asset, ds.newsdata.timeframe_hours,
                )] = ("newsdata", cand.ticker)
            if gold_included:
                futures_map[executor.submit(
                    fetch_newsdata_news, GOLD_NAME, secrets.newsdata_key,
                    ds.newsdata.max_articles_per_asset, ds.newsdata.timeframe_hours,
                )] = ("newsdata", GOLD_TICKER)

        # Adanos — batch calls for all discovered US stocks (3 sources)
        if ds.adanos.enabled and secrets and secrets.adanos_key and stock_candidates:
            us_tickers = [c.ticker for c in stock_candidates]
            if ds.adanos.include_reddit_stocks:
                futures_map[executor.submit(
                    fetch_adanos_batch, us_tickers, secrets.adanos_key,
                    "reddit_stocks", ds.adanos.days_lookback,
                )] = ("adanos", "reddit_stocks")
            if ds.adanos.include_x_stocks:
                futures_map[executor.submit(
                    fetch_adanos_batch, us_tickers, secrets.adanos_key,
                    "x_stocks", ds.adanos.days_lookback,
                )] = ("adanos", "x_stocks")
            if ds.adanos.include_polymarket:
                futures_map[executor.submit(
                    fetch_adanos_batch, us_tickers, secrets.adanos_key,
                    "polymarket_stocks", ds.adanos.days_lookback,
                )] = ("adanos", "polymarket_stocks")

        # ── Collect all results ────────────────────────────────────────────────
        raw_results: dict = {}
        for future in as_completed(futures_map):
            key = futures_map[future]
            try:
                raw_results[key] = future.result()
            except NewsAPIQuotaExhausted as e:
                logger.warning(f"NewsAPI quota exhausted: {e} — falling back to RSS for news")
                newsapi_quota_exhausted = True
                raw_results[key] = []
            except Exception as e:
                logger.warning(f"Targeted fetch failed for {key}: {e}")
                raw_results[key] = None

    # ── Expand yfinance batch result ───────────────────────────────────────────
    yf_batch: dict[str, OHLCVData] = raw_results.pop(("yfinance", "batch"), None) or {}
    for ticker, ohlcv in yf_batch.items():
        raw_results[("yfinance", ticker)] = ohlcv
    if yf_batch:
        snapshot.data_sources_used.append("yfinance")

    # ── Enrich with extended-hours prices (sequential — never concurrent) ──────
    if yf_batch and ds.yfinance.enabled:
        try:
            extended = fetch_extended_prices(list(yf_batch.keys()))
            # yf_batch values are the same OHLCVData objects referenced by raw_results,
            # so in-place modification propagates to all downstream assembly.
            _merge_extended_prices(yf_batch, extended)
        except Exception as e:
            logger.warning(f"Extended price fetch failed: {e}")

    # ── Extract Adanos batch results ───────────────────────────────────────────
    def _adanos_dict(source_key: str) -> dict[str, AdanosTickerSentiment]:
        batch: AdanosBatch | None = raw_results.get(("adanos", source_key))
        return batch.data if (batch and not batch.error) else {}

    adanos_reddit = _adanos_dict("reddit_stocks")
    adanos_x = _adanos_dict("x_stocks")
    adanos_polymarket = _adanos_dict("polymarket_stocks")
    if any([adanos_reddit, adanos_x, adanos_polymarket]):
        snapshot.data_sources_used.append("Adanos")

    # Source attribution flags (append at most once each)
    newsapi_used = finnhub_used = marketaux_used = newsdata_used = av_used = False

    # ── Assemble stock candidates ──────────────────────────────────────────────
    for cand in stock_candidates:
        amd = AssetMarketData(ticker=cand.ticker, name=cand.name, asset_type="stock")

        yf_result = raw_results.get(("yfinance", cand.ticker))
        if yf_result and not yf_result.error:
            amd.ohlcv = yf_result

        news_result = raw_results.get(("newsapi", cand.ticker, "stock"))
        if news_result:
            amd.news = news_result
            if not newsapi_used:
                snapshot.data_sources_used.append("NewsAPI")
                newsapi_used = True
        elif newsapi_quota_exhausted and ds.rss.enabled:
            amd.news = _rss_fallback(cand.name, cand.ticker, rss_feeds, ds.rss.max_items_per_feed)

        sent_result = raw_results.get(("alphavantage", cand.ticker))
        if sent_result and not sent_result.error:
            amd.sentiment = sent_result
            if not av_used:
                snapshot.data_sources_used.append("AlphaVantage")
                av_used = True

        fh_news = raw_results.get(("finnhub_news", cand.ticker))
        if fh_news:
            amd.finnhub_news = fh_news
            if not finnhub_used:
                snapshot.data_sources_used.append("Finnhub")
                finnhub_used = True

        fh_metrics = raw_results.get(("finnhub_metrics", cand.ticker))
        if fh_metrics and not fh_metrics.error:
            amd.finnhub_metrics = fh_metrics

        mx_news = raw_results.get(("marketaux", cand.ticker))
        if mx_news:
            amd.marketaux_news = mx_news
            if not marketaux_used:
                snapshot.data_sources_used.append("Marketaux")
                marketaux_used = True

        nd_news = raw_results.get(("newsdata", cand.ticker))
        if nd_news:
            amd.newsdata_news = nd_news
            if not newsdata_used:
                snapshot.data_sources_used.append("NewsData")
                newsdata_used = True

        # Adanos and ApeWisdom — all discovered stocks are US-listed
        amd.adanos_reddit = adanos_reddit.get(cand.ticker.upper())
        amd.adanos_x = adanos_x.get(cand.ticker.upper())
        amd.adanos_polymarket = adanos_polymarket.get(cand.ticker.upper())
        amd.apewisdom = aw_stocks_data.get(cand.ticker.upper())

        amd.data_unavailable = (amd.ohlcv is None or amd.ohlcv.error is not None)
        snapshot.stocks.append(amd)

    # ── Assemble Gold ──────────────────────────────────────────────────────────
    if gold_included:
        amd = AssetMarketData(ticker=GOLD_TICKER, name=GOLD_NAME, asset_type="commodity")

        # Prefer Phase 2 yfinance result; fall back to Phase 1 result
        gold_yf = raw_results.get(("yfinance", GOLD_TICKER)) or broad_data.gold_ohlcv
        if gold_yf and not gold_yf.error:
            amd.ohlcv = gold_yf

        news_result = raw_results.get(("newsapi", GOLD_TICKER, "commodity"))
        if news_result:
            amd.news = news_result
            if not newsapi_used:
                snapshot.data_sources_used.append("NewsAPI")
                newsapi_used = True

        mx_news = raw_results.get(("marketaux", GOLD_TICKER))
        if mx_news:
            amd.marketaux_news = mx_news
            if not marketaux_used:
                snapshot.data_sources_used.append("Marketaux")
                marketaux_used = True

        nd_news = raw_results.get(("newsdata", GOLD_TICKER))
        if nd_news:
            amd.newsdata_news = nd_news
            if not newsdata_used:
                snapshot.data_sources_used.append("NewsData")
                newsdata_used = True

        amd.data_unavailable = (amd.ohlcv is None or amd.ohlcv.error is not None)
        snapshot.commodities.append(amd)

    available = [a for a in snapshot.all_assets() if not a.data_unavailable]
    unavailable = [a for a in snapshot.all_assets() if a.data_unavailable]
    logger.info(
        f"Targeted fetch complete: {len(available)} assets with data, "
        f"{len(unavailable)} unavailable. Sources: {snapshot.data_sources_used}"
    )
    return snapshot


def _rss_fallback(
    name: str,
    ticker: str,
    feeds: list[dict],
    max_items: int,
) -> list[RSSArticle]:
    """Fetch RSS articles as fallback news for an asset."""
    return fetch_asset_news_from_rss(
        feeds=feeds,
        keywords=[name, ticker],
        max_items_per_feed=max_items,
        max_results=3,
    )
