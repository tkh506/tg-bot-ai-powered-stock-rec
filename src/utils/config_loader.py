"""
Configuration loader: reads config.yaml + .env and validates with Pydantic v2.
Returns a single AppConfig object used throughout the application.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# ── Custom exceptions ─────────────────────────────────────────────────────────

class ConfigError(Exception):
    """Raised when configuration loading or validation fails."""


# ── Secret settings (from .env) ───────────────────────────────────────────────

class Secrets(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    openrouter_api_key: str = Field(..., alias="OPENROUTER_API_KEY")
    telegram_bot_token: str = Field(..., alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(..., alias="TELEGRAM_CHAT_ID")
    newsapi_key: str = Field(..., alias="NEWSAPI_KEY")
    alphavantage_key: str = Field(..., alias="ALPHAVANTAGE_KEY")
    gcp_project_id: str | None = Field(None, alias="GCP_PROJECT_ID")
    # New data source keys — all optional; sources degrade gracefully when absent
    finnhub_key: str | None = Field(None, alias="FINNHUB_API_KEY")
    marketaux_key: str | None = Field(None, alias="MARKETAUX_API_KEY")
    newsdata_key: str | None = Field(None, alias="NEWSDATA_API_KEY")
    fred_key: str | None = Field(None, alias="FRED_API_KEY")
    adanos_key: str | None = Field(None, alias="ADANOS_API_KEY")
    # ApeWisdom requires no API key
    # Bot listener: owner's Telegram user ID — only this user can trigger /report
    telegram_owner_user_id: int | None = Field(None, alias="TELEGRAM_OWNER_USER_ID")


# ── YAML config models ────────────────────────────────────────────────────────

class AppMeta(BaseModel):
    name: str = "AI Investment Advisor"
    timezone: str = "Asia/Hong_Kong"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


class ScheduleConfig(BaseModel):
    systemd_oncalendar: str = "Mon-Fri 00:00:00 UTC"
    cron_hour: int = 0
    cron_minute: int = 0
    cron_days_of_week: str = "mon-fri"


class RiskConfig(BaseModel):
    appetite: Literal["conservative", "moderate", "aggressive"] = "moderate"


class DiscoveryConfig(BaseModel):
    """Controls the two-stage discovery pipeline."""
    max_candidates: int = 10       # Stage 1 AI limit: tickers to deep-dive
    max_recommendations: int = 5   # Stage 2 AI limit: final recommendations
    always_include_gold: bool = True
    apewisdom_top_n: int = 100     # How many ApeWisdom trending entries to show Stage 1 AI


class YFinanceConfig(BaseModel):
    enabled: bool = True
    lookback_days: int = 20
    interval: str = "1d"


class CoinGeckoConfig(BaseModel):
    enabled: bool = True
    vs_currency: str = "usd"
    include_7d_change: bool = True


class NewsAPIConfig(BaseModel):
    enabled: bool = True
    max_articles_per_asset: int = 5
    hours_lookback: int = 24
    language: str = "en"
    sort_by: str = "relevancy"


class AlphaVantageConfig(BaseModel):
    enabled: bool = True
    sentiment_limit: int = 50


class RSSFeed(BaseModel):
    name: str
    url: str


class RSSConfig(BaseModel):
    enabled: bool = True
    feeds: list[RSSFeed] = [
        RSSFeed(name="Reuters Business", url="https://feeds.reuters.com/reuters/businessNews"),
        RSSFeed(name="Reuters Markets", url="https://feeds.reuters.com/reuters/financialNews"),
    ]
    max_items_per_feed: int = 10
    keywords_from_asset_names: bool = True


class FinnhubConfig(BaseModel):
    enabled: bool = True
    max_news_per_stock: int = 5
    hours_lookback: int = 24
    include_metrics: bool = True


class MarketauxConfig(BaseModel):
    enabled: bool = True
    max_articles_per_asset: int = 5


class NewsDataConfig(BaseModel):
    enabled: bool = True
    max_articles_per_asset: int = 5
    timeframe_hours: int = 24


class FredSeriesConfig(BaseModel):
    id: str
    name: str


class FredConfig(BaseModel):
    enabled: bool = True
    series: list[FredSeriesConfig] = [
        FredSeriesConfig(id="CPIAUCSL", name="CPI (Inflation)"),
        FredSeriesConfig(id="UNRATE",   name="Unemployment Rate"),
        FredSeriesConfig(id="FEDFUNDS", name="Fed Funds Rate"),
        FredSeriesConfig(id="DGS10",    name="10Y Treasury Yield"),
        FredSeriesConfig(id="DGS2",     name="2Y Treasury Yield"),
        FredSeriesConfig(id="GDP",      name="US GDP (Quarterly)"),
    ]


class AdanosConfig(BaseModel):
    enabled: bool = True
    days_lookback: int = 7
    include_reddit_stocks: bool = True
    include_x_stocks: bool = True
    include_polymarket: bool = True
    include_reddit_crypto: bool = True


class ApeWisdomConfig(BaseModel):
    enabled: bool = True


class DataSourcesConfig(BaseModel):
    yfinance: YFinanceConfig = YFinanceConfig()
    coingecko: CoinGeckoConfig = CoinGeckoConfig()
    newsapi: NewsAPIConfig = NewsAPIConfig()
    alphavantage: AlphaVantageConfig = AlphaVantageConfig()
    rss: RSSConfig = RSSConfig()
    finnhub: FinnhubConfig = FinnhubConfig()
    marketaux: MarketauxConfig = MarketauxConfig()
    newsdata: NewsDataConfig = NewsDataConfig()
    fred: FredConfig = FredConfig()
    adanos: AdanosConfig = AdanosConfig()
    apewisdom: ApeWisdomConfig = ApeWisdomConfig()


class AIConfig(BaseModel):
    model: str = "anthropic/claude-sonnet-4-6"
    temperature: float = 0.3
    max_tokens: int = 4096
    max_retries: int = 3
    retry_delay_seconds: int = 5
    response_format: str = "json_object"


class TelegramReportConfig(BaseModel):
    enabled: bool = True
    parse_mode: str = "Markdown"
    max_message_length: int = 4096


class ArchiveConfig(BaseModel):
    sqlite_db_path: str = "data/reports.db"
    markdown_dir: str = "data/archive"
    retention_days: int = 365


class ReportingConfig(BaseModel):
    telegram: TelegramReportConfig = TelegramReportConfig()
    archive: ArchiveConfig = ArchiveConfig()


class NotificationsConfig(BaseModel):
    send_on_error: bool = True
    send_summary_header: bool = True


# ── Root config ───────────────────────────────────────────────────────────────

class AppConfig(BaseModel):
    app: AppMeta = AppMeta()
    schedule: ScheduleConfig = ScheduleConfig()
    risk: RiskConfig = RiskConfig()
    discovery: DiscoveryConfig = DiscoveryConfig()
    data_sources: DataSourcesConfig = DataSourcesConfig()
    ai: AIConfig = AIConfig()
    reporting: ReportingConfig = ReportingConfig()
    notifications: NotificationsConfig = NotificationsConfig()

    # Secrets injected at load time (not from YAML)
    secrets: Secrets | None = None


# ── Loader ────────────────────────────────────────────────────────────────────

def load_config(config_path: str | Path = "config/config.yaml") -> AppConfig:
    """
    Load and validate the full application configuration.
    Merges config.yaml with secrets from .env.
    """
    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}. "
            "Copy config/config.example.yaml to config/config.yaml and edit it."
        )

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    config = AppConfig.model_validate(raw)
    config.secrets = Secrets()  # loads from .env / environment variables
    return config
