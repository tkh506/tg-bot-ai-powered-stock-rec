"""
OpenRouter API client.

Sends prompts to Claude Sonnet 4.6 via OpenRouter and returns the raw response string.
Handles rate limiting (HTTP 429) with exponential backoff.
"""

from __future__ import annotations

import httpx

from src.utils.config_loader import AppConfig
from src.utils.logger import get_logger

logger = get_logger("analysis.ai_client")

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
APP_SITE = "https://github.com/ai-investment-advisor"   # shown in OpenRouter dashboard
APP_TITLE = "AI Investment Advisor"


class OpenRouterError(Exception):
    pass


class OpenRouterRateLimitError(OpenRouterError):
    pass


def call(
    prompt: dict[str, str],
    config: AppConfig,
) -> tuple[str, int, int]:
    """
    Send a prompt to the configured model via OpenRouter.

    Args:
        prompt: {"system": str, "user": str}
        config: AppConfig with ai settings and secrets

    Returns:
        (response_text, input_tokens, output_tokens)

    Raises:
        OpenRouterRateLimitError: on HTTP 429
        OpenRouterError: on other API failures
    """
    ai_cfg = config.ai
    secrets = config.secrets
    assert secrets, "Secrets not loaded"

    headers = {
        "Authorization": f"Bearer {secrets.openrouter_api_key}",
        "HTTP-Referer": APP_SITE,
        "X-Title": APP_TITLE,
        "Content-Type": "application/json",
    }

    payload: dict = {
        "model": ai_cfg.model,
        "messages": [
            {"role": "system", "content": prompt["system"]},
            {"role": "user", "content": prompt["user"]},
        ],
        "temperature": ai_cfg.temperature,
        "max_tokens": ai_cfg.max_tokens,
        "response_format": {"type": ai_cfg.response_format},
    }

    logger.info(f"Calling OpenRouter: model={ai_cfg.model} temp={ai_cfg.temperature}")

    with httpx.Client(timeout=120.0) as client:
        response = client.post(
            f"{OPENROUTER_BASE}/chat/completions",
            headers=headers,
            json=payload,
        )

    if response.status_code == 429:
        raise OpenRouterRateLimitError(f"OpenRouter rate limited (HTTP 429): {response.text[:200]}")

    if response.status_code != 200:
        raise OpenRouterError(
            f"OpenRouter API error {response.status_code}: {response.text[:400]}"
        )

    data = response.json()

    # Extract content
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise OpenRouterError(f"Unexpected response structure: {e} — {str(data)[:300]}")

    # Log token usage
    usage = data.get("usage", {})
    input_tokens = usage.get("prompt_tokens", 0)
    output_tokens = usage.get("completion_tokens", 0)
    logger.info(f"OpenRouter response: {input_tokens} input tokens, {output_tokens} output tokens")

    return content, input_tokens, output_tokens


def call_with_retry(
    prompt: dict[str, str],
    config: AppConfig,
    previous_bad_response: str | None = None,
) -> tuple[str, int, int]:
    """
    Call OpenRouter with automatic retry on rate limit errors.
    Passes previous_bad_response back to caller so prompt_builder can use it.
    Retry logic is handled externally (in main.py) for parse errors.
    """
    import time

    max_retries = config.ai.max_retries
    delay = config.ai.retry_delay_seconds

    for attempt in range(1, max_retries + 1):
        try:
            return call(prompt, config)
        except OpenRouterRateLimitError as e:
            if attempt < max_retries:
                wait = delay * (2 ** (attempt - 1))
                logger.warning(f"Rate limited, retrying in {wait}s (attempt {attempt}/{max_retries})")
                time.sleep(wait)
            else:
                raise
        except OpenRouterError:
            raise
