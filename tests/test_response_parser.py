"""Tests for the AI response parser."""

import json
import pytest

from src.analysis.response_parser import parse, ParseError, AnalysisResult


VALID_RESPONSE = {
    "run_date": "2026-02-23",
    "risk_profile": "moderate",
    "macro_summary": "Markets are cautious amid inflation data.",
    "portfolio_bias": "neutral",
    "assets": [
        {
            "ticker": "AAPL",
            "name": "Apple Inc.",
            "asset_type": "stock",
            "signal": "HOLD",
            "confidence": 65,
            "current_price": "$175.20",
            "target_price": "$185.00",
            "stop_loss": "$165.00",
            "justification": "Stable but facing headwinds. RSI neutral. Volume average.",
            "key_risks": ["Macro slowdown", "iPhone demand uncertainty"],
            "time_horizon": "medium",
            "sentiment_score": "neutral",
        },
        {
            "ticker": "bitcoin",
            "name": "Bitcoin",
            "asset_type": "crypto",
            "signal": "BUY",
            "confidence": 72,
            "current_price": "$68,000",
            "target_price": "$80,000",
            "stop_loss": "$60,000",
            "justification": "Momentum strong after ETF inflows. RSI not overbought.",
            "key_risks": ["Regulatory risk", "Market sentiment flip"],
            "time_horizon": "short",
            "sentiment_score": "positive",
        },
    ],
    "disclaimer": "For informational purposes only.",
}


def test_parse_valid_response():
    result = parse(json.dumps(VALID_RESPONSE))
    assert isinstance(result, AnalysisResult)
    assert result.run_date == "2026-02-23"
    assert result.portfolio_bias == "neutral"
    assert len(result.assets) == 2
    aapl = result.assets[0]
    assert aapl.ticker == "AAPL"
    assert aapl.signal == "HOLD"
    assert aapl.confidence == 65
    btc = result.assets[1]
    assert btc.signal == "BUY"
    assert btc.confidence == 72


def test_parse_strips_markdown_fences():
    wrapped = "```json\n" + json.dumps(VALID_RESPONSE) + "\n```"
    result = parse(wrapped)
    assert len(result.assets) == 2


def test_parse_invalid_json_raises():
    with pytest.raises(ParseError, match="not valid JSON"):
        parse("not valid json at all {")


def test_parse_missing_field_raises():
    bad = {**VALID_RESPONSE}
    del bad["assets"]
    with pytest.raises(ParseError, match="Missing required field"):
        parse(json.dumps(bad))


def test_parse_invalid_signal_raises():
    bad = json.loads(json.dumps(VALID_RESPONSE))
    bad["assets"][0]["signal"] = "MAYBE"
    with pytest.raises(ParseError, match="invalid signal"):
        parse(json.dumps(bad))


def test_parse_confidence_out_of_range_raises():
    bad = json.loads(json.dumps(VALID_RESPONSE))
    bad["assets"][0]["confidence"] = 150
    with pytest.raises(ParseError, match="out of range"):
        parse(json.dumps(bad))


def test_parse_confidence_not_integer_raises():
    bad = json.loads(json.dumps(VALID_RESPONSE))
    bad["assets"][0]["confidence"] = "high"
    with pytest.raises(ParseError, match="not an integer"):
        parse(json.dumps(bad))


def test_parse_unknown_portfolio_bias_defaults():
    bad = json.loads(json.dumps(VALID_RESPONSE))
    bad["portfolio_bias"] = "sideways"
    result = parse(json.dumps(bad))
    assert result.portfolio_bias == "neutral"


def test_parse_missing_optional_fields():
    minimal_asset = {
        "ticker": "GC=F",
        "name": "Gold",
        "asset_type": "commodity",
        "signal": "SELL",
        "confidence": 55,
        "current_price": "$2300",
        "justification": "Overbought.",
        "key_risks": [],
        "time_horizon": "short",
        "sentiment_score": "negative",
    }
    response = {**VALID_RESPONSE, "assets": [minimal_asset]}
    result = parse(json.dumps(response))
    assert result.assets[0].target_price is None
    assert result.assets[0].stop_loss is None
