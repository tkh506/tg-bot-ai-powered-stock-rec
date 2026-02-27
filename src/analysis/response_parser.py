"""
Response parser: validates and converts the AI JSON response into structured dataclasses.

Handles two response types:
  - Stage 1 (discovery): parse_candidates() → DiscoveryResult
  - Stage 2 (analysis): parse() → AnalysisResult
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal, Optional

from src.utils.logger import get_logger

logger = get_logger("analysis.response_parser")

VALID_SIGNALS = {"BUY", "HOLD", "SELL"}
VALID_HORIZONS = {"short", "medium", "long"}
VALID_BIASES = {"bullish", "neutral", "bearish"}
VALID_SENTIMENTS = {"positive", "neutral", "negative"}


class ParseError(Exception):
    pass


# ── Stage 1: Discovery dataclasses ────────────────────────────────────────────

@dataclass
class Candidate:
    """A stock the AI has identified as worth deep-dive analysis."""
    ticker: str
    name: str
    exchange: str = "NASDAQ"   # ApeWisdom doesn't provide exchange; default US
    rationale: str = ""


@dataclass
class DiscoveryResult:
    """Parsed output from Stage 1 AI call."""
    discovery_summary: str
    candidates: list[Candidate] = field(default_factory=list)
    raw_json: str = ""


def parse_candidates(raw_response: str) -> DiscoveryResult:
    """
    Parse the Stage 1 AI discovery response.

    Raises ParseError if the response is invalid, so the caller can retry.
    """
    text = raw_response.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ParseError(f"Discovery response is not valid JSON: {e}")

    if not isinstance(data, dict):
        raise ParseError("Discovery response JSON is not an object")

    if "candidates" not in data:
        raise ParseError("Discovery response missing required field: 'candidates'")

    candidates_raw = data["candidates"]
    if not isinstance(candidates_raw, list):
        raise ParseError("'candidates' field must be a list")

    candidates: list[Candidate] = []
    for i, item in enumerate(candidates_raw):
        if not isinstance(item, dict):
            raise ParseError(f"Candidate #{i} is not an object")
        ticker = str(item.get("ticker", "")).strip().upper()
        if not ticker:
            raise ParseError(f"Candidate #{i} has no ticker")
        candidates.append(Candidate(
            ticker=ticker,
            name=str(item.get("name", ticker)),
            exchange=str(item.get("exchange", "NASDAQ")),
            rationale=str(item.get("rationale", "")),
        ))

    logger.info(
        f"Discovery parsed: {len(candidates)} candidates: {[c.ticker for c in candidates]}"
    )
    return DiscoveryResult(
        discovery_summary=str(data.get("discovery_summary", "")),
        candidates=candidates,
        raw_json=raw_response,
    )


@dataclass
class AssetSignal:
    ticker: str
    name: str
    asset_type: str
    signal: Literal["BUY", "HOLD", "SELL"]
    confidence: int
    current_price: str
    target_price: Optional[str]
    stop_loss: Optional[str]
    justification: str
    key_risks: list[str]
    time_horizon: str
    sentiment_score: str


@dataclass
class AnalysisResult:
    run_date: str
    risk_profile: str
    macro_summary: str
    portfolio_bias: str
    assets: list[AssetSignal] = field(default_factory=list)
    disclaimer: str = "For informational purposes only. Not financial advice."
    raw_json: str = ""


def parse(raw_response: str) -> AnalysisResult:
    """
    Parse the AI's JSON response into an AnalysisResult.

    Raises ParseError with a descriptive message if the response is invalid,
    so the caller can retry with the bad response included in the next prompt.
    """
    # Strip any accidental markdown code fences
    text = raw_response.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ParseError(f"Response is not valid JSON: {e}")

    if not isinstance(data, dict):
        raise ParseError("Response JSON is not an object")

    # Validate required top-level fields
    for field_name in ("run_date", "macro_summary", "portfolio_bias", "assets"):
        if field_name not in data:
            raise ParseError(f"Missing required field: '{field_name}'")

    portfolio_bias = str(data.get("portfolio_bias", "neutral")).lower()
    if portfolio_bias not in VALID_BIASES:
        logger.warning(f"Unexpected portfolio_bias '{portfolio_bias}', defaulting to 'neutral'")
        portfolio_bias = "neutral"

    assets_raw = data.get("assets", [])
    if not isinstance(assets_raw, list):
        raise ParseError("'assets' field must be a list")

    parsed_assets: list[AssetSignal] = []
    for i, item in enumerate(assets_raw):
        if not isinstance(item, dict):
            raise ParseError(f"Asset #{i} is not an object")

        ticker = str(item.get("ticker", "")).strip()
        if not ticker:
            raise ParseError(f"Asset #{i} has no ticker")

        signal = str(item.get("signal", "")).upper().strip()
        if signal not in VALID_SIGNALS:
            raise ParseError(f"Asset {ticker}: invalid signal '{signal}' (must be BUY/HOLD/SELL)")

        raw_conf = item.get("confidence")
        try:
            confidence = int(raw_conf)
        except (TypeError, ValueError):
            raise ParseError(f"Asset {ticker}: confidence '{raw_conf}' is not an integer")
        if not (0 <= confidence <= 100):
            raise ParseError(f"Asset {ticker}: confidence {confidence} out of range [0, 100]")

        time_horizon = str(item.get("time_horizon", "medium")).lower()
        if time_horizon not in VALID_HORIZONS:
            logger.warning(f"Asset {ticker}: unexpected time_horizon '{time_horizon}', using 'medium'")
            time_horizon = "medium"

        sentiment_score = str(item.get("sentiment_score", "neutral")).lower()
        if sentiment_score not in VALID_SENTIMENTS:
            sentiment_score = "neutral"

        key_risks = item.get("key_risks", [])
        if not isinstance(key_risks, list):
            key_risks = [str(key_risks)]

        parsed_assets.append(AssetSignal(
            ticker=ticker,
            name=str(item.get("name", ticker)),
            asset_type=str(item.get("asset_type", "stock")),
            signal=signal,  # type: ignore[arg-type]
            confidence=confidence,
            current_price=str(item.get("current_price", "N/A")),
            target_price=item.get("target_price"),
            stop_loss=item.get("stop_loss"),
            justification=str(item.get("justification", ""))[:500],
            key_risks=[str(r) for r in key_risks[:3]],
            time_horizon=time_horizon,
            sentiment_score=sentiment_score,
        ))

    logger.info(
        f"Parsed {len(parsed_assets)} asset signals: "
        f"{sum(1 for a in parsed_assets if a.signal == 'BUY')} BUY, "
        f"{sum(1 for a in parsed_assets if a.signal == 'HOLD')} HOLD, "
        f"{sum(1 for a in parsed_assets if a.signal == 'SELL')} SELL"
    )

    return AnalysisResult(
        run_date=str(data.get("run_date", "")),
        risk_profile=str(data.get("risk_profile", "")),
        macro_summary=str(data.get("macro_summary", "")),
        portfolio_bias=portfolio_bias,
        assets=parsed_assets,
        disclaimer=str(data.get("disclaimer", "For informational purposes only.")),
        raw_json=raw_response,
    )
