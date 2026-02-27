"""
Telegram bot notifier.

Sends formatted report messages and error alerts via python-telegram-bot (sync mode).
"""

from __future__ import annotations

import telegram
from telegram.error import TelegramError

from src.utils.config_loader import AppConfig
from src.utils.logger import get_logger

logger = get_logger("notifications.telegram")


def _get_bot(config: AppConfig) -> telegram.Bot:
    assert config.secrets, "Secrets not loaded"
    return telegram.Bot(token=config.secrets.telegram_bot_token)


def send_report(parts: list[str], config: AppConfig) -> None:
    """
    Send a multi-part Telegram report.
    Each part is sent as a separate message to stay within the 4096-char limit.
    """
    if not config.reporting.telegram.enabled:
        logger.info("Telegram reporting disabled — skipping send")
        return

    assert config.secrets, "Secrets not loaded"
    chat_id = config.secrets.telegram_chat_id
    parse_mode = config.reporting.telegram.parse_mode
    bot = _get_bot(config)

    import asyncio

    async def _send_all() -> None:
        for i, part in enumerate(parts):
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=part,
                    parse_mode=parse_mode,
                )
                logger.info(f"Telegram: sent part {i+1}/{len(parts)}")
            except TelegramError as e:
                # Retry without parse_mode (handles malformed Markdown)
                logger.warning(f"Telegram send failed with {parse_mode}, retrying as plain text: {e}")
                try:
                    await bot.send_message(chat_id=chat_id, text=part)
                except TelegramError as e2:
                    logger.error(f"Telegram send failed entirely for part {i+1}: {e2}")

    asyncio.run(_send_all())


def send_error_alert(message: str, config: AppConfig) -> None:
    """Send a plain-text error alert to the Telegram chat."""
    if not config.notifications.send_on_error:
        return
    assert config.secrets, "Secrets not loaded"

    alert = f"AI Investment Advisor ERROR\n\n{message[:3000]}"

    import asyncio

    async def _send() -> None:
        try:
            bot = _get_bot(config)
            await bot.send_message(
                chat_id=config.secrets.telegram_chat_id,
                text=alert,
            )
            logger.info("Telegram error alert sent")
        except TelegramError as e:
            logger.error(f"Failed to send Telegram error alert: {e}")

    try:
        asyncio.run(_send())
    except Exception as e:
        logger.error(f"send_error_alert raised: {e}")
