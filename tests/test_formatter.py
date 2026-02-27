"""Tests for the Telegram report formatter."""

import json
import pytest

from src.analysis.response_parser import parse, AnalysisResult
from src.reporting.formatter import render
from src.utils.config_loader import AppConfig


VALID_RESPONSE = {
    "run_date": "2026-02-23",
    "risk_profile": "moderate",
    "macro_summary": "Global equities under pressure.",
    "portfolio_bias": "bearish",
    "assets": [
        {
            "ticker": "AAPL",
            "name": "Apple Inc.",
            "asset_type": "stock",
            "signal": "HOLD",
            "confidence": 60,
            "current_price": "$175.00",
            "target_price": None,
            "stop_loss": None,
            "justification": "Neutral momentum.",
            "key_risks": ["Macro risk"],
            "time_horizon": "medium",
            "sentiment_score": "neutral",
        }
    ],
    "disclaimer": "For informational purposes only.",
}


def _make_config() -> AppConfig:
    return AppConfig()


def test_render_returns_list_of_strings():
    result = parse(json.dumps(VALID_RESPONSE))
    config = _make_config()
    parts = render(result, config)
    assert isinstance(parts, list)
    assert len(parts) >= 1
    for part in parts:
        assert isinstance(part, str)
        assert len(part) > 0


def test_render_respects_message_length_limit():
    result = parse(json.dumps(VALID_RESPONSE))
    config = _make_config()
    config.reporting.telegram.max_message_length = 200  # force splitting
    parts = render(result, config)
    for part in parts:
        assert len(part) <= 200 + 100  # allow small overshoot from individual lines


def test_render_contains_ticker():
    result = parse(json.dumps(VALID_RESPONSE))
    config = _make_config()
    parts = render(result, config)
    full_text = "\n".join(parts)
    assert "AAPL" in full_text


def test_render_contains_signal():
    result = parse(json.dumps(VALID_RESPONSE))
    config = _make_config()
    parts = render(result, config)
    full_text = "\n".join(parts)
    assert "HOLD" in full_text


def test_render_contains_disclaimer():
    result = parse(json.dumps(VALID_RESPONSE))
    config = _make_config()
    parts = render(result, config)
    full_text = "\n".join(parts)
    assert "informational purposes" in full_text.lower()
