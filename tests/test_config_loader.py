"""Tests for configuration loading and validation."""

import os
import tempfile
import pytest
import yaml

from src.utils.config_loader import AppConfig, load_config


MINIMAL_CONFIG = {
    "risk": {"appetite": "conservative"},
    "discovery": {"max_candidates": 8, "max_recommendations": 3},
}


def test_default_appconfig():
    """AppConfig with no arguments should use sensible defaults."""
    cfg = AppConfig()
    assert cfg.risk.appetite == "moderate"
    assert cfg.ai.model == "google/gemini-3.1-pro-preview"
    assert cfg.ai.stage1_model == "openai/gpt-5.4-mini"
    assert cfg.ai.temperature == 0.3
    assert cfg.reporting.telegram.max_message_length == 4096


def test_load_config_from_file(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(MINIMAL_CONFIG), encoding="utf-8")

    # Provide dummy env vars so Secrets() doesn't fail
    env = {
        "OPENROUTER_API_KEY": "sk-test",
        "TELEGRAM_BOT_TOKEN": "123:ABC",
        "TELEGRAM_CHAT_ID": "-100123",
        "NEWSAPI_KEY": "test",
        "ALPHAVANTAGE_KEY": "test",
    }
    original = {k: os.environ.get(k) for k in env}
    os.environ.update(env)

    try:
        cfg = load_config(config_file)
        assert cfg.risk.appetite == "conservative"
        assert cfg.discovery.max_candidates == 8
        assert cfg.discovery.max_recommendations == 3
    finally:
        for k, v in original.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_load_config_missing_file():
    with pytest.raises(FileNotFoundError):
        load_config("nonexistent/config.yaml")


def test_invalid_risk_appetite():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"risk": {"appetite": "ultra-aggressive"}})


def test_discovery_defaults():
    cfg = AppConfig()
    assert cfg.discovery.max_candidates == 10
    assert cfg.discovery.max_recommendations == 5
    assert cfg.discovery.always_include_gold is True
    assert cfg.discovery.apewisdom_top_n == 100
