"""
Persistent Telegram bot listener for ad-hoc pipeline triggers.

Handles the /report command sent to the bot in a private chat.
Only the configured owner (TELEGRAM_OWNER_USER_ID) can trigger a run.
The resulting report is delivered to the group chat (TELEGRAM_CHAT_ID),
the same destination as scheduled runs.

Managed by: ai-investment-advisor-listener.service (systemd)
Run with:   python -m src.notifications.bot_listener
"""

from __future__ import annotations

import logging
import subprocess
import sys

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from src.utils.config_loader import Secrets
from src.utils.logger import get_logger, setup_logger

logger = get_logger("notifications.bot_listener")

_PIPELINE_SERVICE = "ai-investment-advisor.service"


async def _handle_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /report command — authorized owner only."""
    if update.message is None or update.effective_user is None:
        return

    user_id = update.effective_user.id
    owner_id: int = context.application.bot_data["owner_user_id"]

    if user_id != owner_id:
        # Silently ignore — don't reveal the bot is listening to strangers
        logger.warning(
            f"Unauthorized /report from user_id={user_id} "
            f"(@{update.effective_user.username}) — ignored"
        )
        return

    await update.message.reply_text(
        "Running pipeline... report will arrive in the group chat in ~2 minutes."
    )

    try:
        # --no-block: ask systemd to start the service then return immediately.
        # Without it, systemctl waits until the one-shot service finishes (~2 min),
        # which always exceeds any reasonable subprocess timeout.
        result = subprocess.run(
            ["sudo", "/usr/bin/systemctl", "start", "--no-block", _PIPELINE_SERVICE],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            logger.info(f"Pipeline triggered via /report (user_id={user_id})")
        else:
            stderr = (result.stderr or "").strip()
            # systemd exits 1 with a message if the service unit is already active
            if "already" in stderr.lower():
                logger.info("Pipeline already running — /report ignored duplicate trigger")
                await update.message.reply_text(
                    "Pipeline is already running. Check the group chat shortly."
                )
            else:
                logger.error(f"systemctl start failed (rc={result.returncode}): {stderr}")
                await update.message.reply_text(
                    f"Failed to start pipeline.\nError: {stderr[:200]}"
                )
    except subprocess.TimeoutExpired:
        logger.error("systemctl start --no-block timed out after 10s — sudoers issue?")
        await update.message.reply_text(
            "Timed out trying to start the pipeline. Check VM logs."
        )
    except Exception as e:
        logger.exception(f"Unexpected error triggering pipeline: {e}")
        await update.message.reply_text("Unexpected error. Check VM logs.")


def main() -> None:
    try:
        secrets = Secrets()
    except Exception as e:
        logger.critical(f"Failed to load secrets from .env: {e}")
        sys.exit(1)

    if not secrets.telegram_owner_user_id:
        logger.critical(
            "TELEGRAM_OWNER_USER_ID not set in .env — "
            "refusing to start (anyone could trigger pipeline runs)"
        )
        sys.exit(1)

    owner_id = secrets.telegram_owner_user_id
    logger.info(f"Bot listener starting. Authorized owner_user_id={owner_id}.")
    logger.info("Waiting for /report command in private chat...")

    app = Application.builder().token(secrets.telegram_bot_token).build()
    app.bot_data["owner_user_id"] = owner_id
    app.add_handler(CommandHandler("report", _handle_report))
    app.run_polling(
        drop_pending_updates=True,    # Ignore /report commands queued while the bot was offline
        allowed_updates=["message"],  # Only process message updates
    )


if __name__ == "__main__":
    setup_logger(name="advisor", log_level="INFO", log_file="logs/advisor.log")
    main()
