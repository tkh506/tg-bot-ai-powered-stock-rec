#!/bin/bash
# =============================================================================
# Application installer — run after setup_gcp.sh.
# Creates the advisor system user, sets up the venv, and registers systemd units.
# =============================================================================
set -euo pipefail

APP_DIR="/tg-bot/tg-bot-ai-powered-stock-rec"
APP_USER="stockbot"

# ── Create dedicated system user ──────────────────────────────────────────────
echo "==> Creating system user: $APP_USER"
id "$APP_USER" &>/dev/null || useradd --system --shell /bin/false --home-dir "$APP_DIR" "$APP_USER"

# ── Set ownership ─────────────────────────────────────────────────────────────
echo "==> Setting directory ownership"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ── Python virtual environment ────────────────────────────────────────────────
echo "==> Creating virtual environment"
sudo -u "$APP_USER" python3.11 -m venv "$APP_DIR/venv"

echo "==> Installing Python dependencies"
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --upgrade pip --quiet
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt" --quiet

# ── Verify .env exists ────────────────────────────────────────────────────────
if [ ! -f "$APP_DIR/.env" ]; then
    echo "ERROR: .env file not found at $APP_DIR/.env"
    echo "Copy .env.example to .env and fill in your API keys before running this script."
    exit 1
fi
chmod 600 "$APP_DIR/.env"
chown "$APP_USER:$APP_USER" "$APP_DIR/.env"

# ── systemd: pipeline timer ───────────────────────────────────────────────────
echo "==> Registering systemd units (pipeline)"
cp "$APP_DIR/deploy/ai-investment-advisor.service" /etc/systemd/system/
cp "$APP_DIR/deploy/ai-investment-advisor.timer"   /etc/systemd/system/

systemctl daemon-reload
systemctl enable ai-investment-advisor.timer
systemctl start  ai-investment-advisor.timer

# ── Sudoers rule — allows advisor user to trigger the pipeline from the bot listener ──
# Both with and without --no-block are whitelisted (bot_listener uses --no-block).
echo "==> Adding sudoers rule for pipeline trigger"
cat > /etc/sudoers.d/advisor-trigger << EOF
$APP_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl start ai-investment-advisor.service
$APP_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl start --no-block ai-investment-advisor.service
EOF
chmod 440 /etc/sudoers.d/advisor-trigger

# ── systemd: Telegram bot listener ───────────────────────────────────────────
echo "==> Registering systemd units (bot listener)"
cp "$APP_DIR/deploy/ai-investment-advisor-listener.service" /etc/systemd/system/

systemctl daemon-reload
systemctl enable ai-investment-advisor-listener.service
systemctl start  ai-investment-advisor-listener.service

# ── Verification ──────────────────────────────────────────────────────────────
echo ""
echo "==> Installation complete!"
echo ""
echo "Timer status:"
systemctl list-timers ai-investment-advisor.timer --no-pager

echo ""
echo "Listener status:"
systemctl is-active ai-investment-advisor-listener.service \
    && echo "  Listener: running" \
    || echo "  Listener: NOT running — check: journalctl -u ai-investment-advisor-listener"

echo ""
echo "Run a manual pipeline test:"
echo "  sudo systemctl start --no-block ai-investment-advisor.service"
echo "  journalctl -u ai-investment-advisor -f"
echo ""
echo "Or dry-run locally:"
echo "  cd $APP_DIR && venv/bin/python -m src.main --dry-run"
echo ""
echo "IMPORTANT: Ensure TELEGRAM_OWNER_USER_ID is set in $APP_DIR/.env"
echo "  Get your Telegram user ID by messaging @userinfobot on Telegram"
