#!/bin/bash
# =============================================================================
# GCP VM bootstrap script — run ONCE on a fresh Debian 12 instance as root.
# Installs system dependencies and clones the project.
# =============================================================================
set -euo pipefail

APP_DIR="/opt/ai-investment-advisor"
REPO_URL="https://github.com/tkh506/tg-bot-ai-powered-stock-rec.git"   # update this

echo "==> Updating system packages"
apt-get update && apt-get upgrade -y

echo "==> Installing Python 3.11 and dependencies"
apt-get install -y python3.11 python3.11-venv python3-pip git sqlite3 curl wget

echo "==> Cloning repository"
git clone "$REPO_URL" "$APP_DIR"

echo "==> Creating logs and data directories"
mkdir -p "$APP_DIR"/{logs,data/archive}

echo ""
echo "==> Next step: copy your .env file to $APP_DIR/.env"
echo "    Then run: $APP_DIR/deploy/install.sh"
