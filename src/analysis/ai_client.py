"""
OpenRouter API client.

Sends prompts to any model via OpenRouter and returns the raw response string.
Handles rate limiting (HTTP 429) with exponential backoff.

Stage 1 (discovery) and Stage 2 (analysis) may use different models.
Pass model_override to call() / call_with_retry() to select the Stage 1 model.
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
    model_override: str | None = None,
) -> tuple[str, int, int]:
    """
    Send a prompt to the configured model via OpenRouter.

    Args:
        prompt: {"system": str, "user": str}
        config: AppConfig with ai settings and secrets
        model_override: if provided, use this model instead of config.ai.model
                        (used for Stage 1 which runs config.ai.stage1_model)

    Returns:
        (response_text, input_tokens, output_tokens)

    Raises:
        OpenRouterRateLimitError: on HTTP 429
        OpenRouterError: on other API failures
    """
    ai_cfg = config.ai
    secrets = config.secrets
    assert secrets, "Secrets not loaded"

    model = model_override or ai_cfg.model

    headers = {
        "Authorization": f"Bearer {secrets.openrouter_api_key}",
        "HTTP-Referer": APP_SITE,
        "X-Title": APP_TITLE,
        "Content-Type": "application/json",
    }

    payload: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt["system"]},
            {"role": "user", "content": prompt["user"]},
        ],
        "temperature": ai_cfg.temperature,
        "max_tokens": ai_cfg.max_tokens,
        "response_format": {"type": ai_cfg.response_format},
    }

    logger.info(f"Calling OpenRouter: model={model} temp={ai_cfg.temperature}")

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
        choice = data["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError) as e:
        raise OpenRouterError(f"Unexpected response structure: {e} — {str(data)[:300]}")

    # content can be None when the model hits max_tokens, a content filter, or
    # returns an empty completion. Raise clearly rather than letting .strip() crash later.
    if content is None:
        finish_reason = data.get("choices", [{}])[0].get("finish_reason", "unknown")
        raise OpenRouterError(
            f"Model returned null content (finish_reason='{finish_reason}'). "
            f"Model: {model}. This may be a max_tokens limit or content filter."
        )

    # Log token usage
    usage = data.get("usage", {})
    input_tokens = usage.get("prompt_tokens", 0)
    output_tokens = usage.get("completion_tokens", 0)
    logger.info(f"OpenRouter response: {input_tokens} input tokens, {output_tokens} output tokens")

    return content, input_tokens, output_tokens


def call_with_retry(
    prompt: dict[str, str],
    config: AppConfig,
    model_override: str | None = None,
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
            return call(prompt, config, model_override=model_override)
        except OpenRouterRateLimitError as e:
            if attempt < max_retries:
                wait = delay * (2 ** (attempt - 1))
                logger.warning(f"Rate limited, retrying in {wait}s (attempt {attempt}/{max_retries})")
                time.sleep(wait)
            else:
                raise
        except OpenRouterError:
            raise
