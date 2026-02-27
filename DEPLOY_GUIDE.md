# DEPLOY_GUIDE.md — Deployment & Upgrade Guide

This guide covers the full lifecycle: setting up git, deploying to GCP for the first time,
and the routine workflow for testing and deploying future updates.

---

## Prerequisites

- GCP Compute Engine VM already provisioned (Debian 12, `e2-small`, `asia-east2`)
- SSH access to the VM via terminal: `ssh YOUR_USER@YOUR_VM_EXTERNAL_IP`
- GitHub account with the repo `tkh506/tg-bot/tg-bot-ai-powered-stock-rec`
- All API keys ready (see `.env.example`)

> **Multi-bot VM**: This bot installs to `/tg-bot/tg-bot-ai-powered-stock-rec/` and runs as
> system user `stockbot`. Each Telegram bot on the VM should have its own directory under
> `/tg-bot/` and its own dedicated system user, so services, logs, and secrets stay isolated.

---

## Part 1: First-Time Deployment

### Step 1 — Initialize git locally and push to GitHub

Run these commands in your local project directory:

```bash
# Initialize the git repo
git init
git add .
git commit -m "Initial commit"

# Connect to your GitHub repo
git remote add origin https://github.com/tkh506/tg-bot-ai-powered-stock-rec.git
git branch -M main
git push -u origin main
```

> **Important**: The `.gitignore` already excludes `.env`, `data/`, `logs/`, and `venv/`.
> Never commit `.env`. Verify with `git status` before every push.

---

### Step 2 — Confirm the repo URL in the bootstrap script

`deploy/setup_gcp.sh` already has the correct repo URL set:

```bash
REPO_URL="https://github.com/tkh506/tg-bot-ai-powered-stock-rec.git"
```

If you haven't pushed yet, this will be committed as part of Step 1.

---

### Step 3 — Bootstrap the GCP VM (run once as root)

SSH into the VM from your terminal:

```bash
# SSH into the VM
ssh YOUR_USER@YOUR_VM_EXTERNAL_IP

# On the VM — switch to root
sudo su -

# Clone the repo directly into the target folder
git clone https://github.com/tkh506/tg-bot-ai-powered-stock-rec.git /tg-bot/tg-bot-ai-powered-stock-rec

# Run the bootstrap script (installs Python 3.11, git, sqlite3, creates dirs)
bash /tg-bot/tg-bot-ai-powered-stock-rec/deploy/setup_gcp.sh
```

This installs Python 3.11, git, sqlite3, and clones the repo to `/tg-bot/tg-bot-ai-powered-stock-rec`.

---

### Step 4 — Create the `.env` secrets file on the VM

Still as root:

```bash
cd /tg-bot/tg-bot-ai-powered-stock-rec

# Copy the template
cp .env.example .env

# Fill in your secrets
nano .env
```

Minimum required keys:
```
OPENROUTER_API_KEY=sk-or-v1-...
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=-100123456789

# Your personal Telegram user ID — message @userinfobot to get it
TELEGRAM_OWNER_USER_ID=123456789
```

Add any optional source keys (NEWSAPI_KEY, FINNHUB_API_KEY, etc.) you have.

> `TELEGRAM_OWNER_USER_ID` is required for the bot listener. Without it, `install.sh`
> will still succeed but the listener service will refuse to start.

---

### Step 5 — Install the application and register systemd

Still as root:

```bash
bash /tg-bot/tg-bot-ai-powered-stock-rec/deploy/install.sh
```

This creates the `stockbot` system user, builds the Python venv, installs all dependencies,
registers + starts the systemd timer, adds the sudoers rule, and starts the bot listener.

Expected output ends with:
```
==> Installation complete!
Timer status: ai-investment-advisor.timer  active  ...
Listener: running
```

---

### Step 6 — Verify the first run

Trigger a manual run and watch the logs:

```bash
# Trigger the service now (don't wait for the timer)
sudo systemctl start ai-investment-advisor.service

# Stream live logs
journalctl -u ai-investment-advisor -f
```

A successful run ends with:
```
Pipeline completed successfully. Report sent to Telegram.
```

Alternatively, do a dry run (no Telegram message sent):

```bash
sudo -u stockbot /tg-bot/tg-bot-ai-powered-stock-rec/venv/bin/python -m src.main --dry-run
```

Also verify the bot listener is running:

```bash
systemctl status ai-investment-advisor-listener.service
# Should show: Active: active (running)

# Test /report: open a private chat with your bot on Telegram and send /report
# You should get: "Running pipeline... report will arrive in ~2 minutes."
```

---

## Part 2: Upgrade Workflow

Use this workflow every time you make changes to the code, config, or prompts.

---

### Phase A — Local: Develop and Test

**1. Make your changes**

Edit code, `config/config.yaml`, or `config/prompts.yaml` locally.

**2. Run the full test suite**

```bash
# From the project root with venv active
source venv/bin/activate
pytest tests/ -v
```

All tests must pass before proceeding. If any fail, fix them first.

**3. Run a dry run locally**

```bash
python -m src.main --dry-run
```

Read through the full output carefully:
- Stage 1 candidates discovered (check for sensible tickers)
- Stage 2 recommendations produced (check for BUY/HOLD/SELL signals)
- No ERROR or CRITICAL lines in the log
- All data sources either succeeded or degraded gracefully (WARNING at most)
- Token counts are reasonable (not spiking unexpectedly)

> A dry run uses real API calls and real AI calls — it only skips sending to Telegram.
> It costs a small amount in OpenRouter credits (~$0.005) per run.

---

### Phase B — Push Changes to GitHub

**4. Stage and commit your changes**

```bash
# Check what changed — review before staging
git diff
git status

# Stage specific files (preferred over git add -A)
git add src/data/some_client.py config/prompts.yaml

# Commit with a meaningful message
git commit -m "Fix: brief description of what changed and why"

# Push to GitHub
git push
```

> **Convention**: Use prefixes: `Fix:` for bug fixes, `Add:` for new features,
> `Update:` for enhancements, `Docs:` for documentation only.

---

### Phase C — Deploy to the VM

**5. SSH into the VM**

```bash
ssh YOUR_USER@YOUR_VM_EXTERNAL_IP
```

**6. Pull the latest code**

```bash
sudo git -C /tg-bot/tg-bot-ai-powered-stock-rec pull
```

You should see the list of changed files. Verify it looks correct.

**7. Install new dependencies (only if `requirements.txt` changed)**

```bash
sudo -u stockbot /tg-bot/tg-bot-ai-powered-stock-rec/venv/bin/pip install -r /tg-bot/tg-bot-ai-powered-stock-rec/requirements.txt --quiet
```

Skip this step if only `.py` or `.yaml` files changed.

**8. Reload systemd (only if `.service` or `.timer` files changed)**

```bash
sudo systemctl daemon-reload
```

Skip this step if only application code or config changed.

**9. Restart services to pick up changes**

```bash
sudo systemctl restart ai-investment-advisor.timer
sudo systemctl restart ai-investment-advisor-listener.service
```

> Both restarts are harmless even if nothing changed — safe to always run both.

**10. Verify the upgrade**

Trigger a manual run and confirm the new behaviour:

```bash
sudo systemctl start ai-investment-advisor.service
journalctl -u ai-investment-advisor -f
```

Check that the fix/feature you deployed is visible in the logs.

---

## Quick Reference Cheatsheet

### Local commands

| Task | Command |
|---|---|
| Run all tests | `pytest tests/ -v` |
| Run single test file | `pytest tests/test_response_parser.py -v` |
| Local dry run | `python -m src.main --dry-run` |
| Live local run | `python -m src.main` |
| Commit and push | `git add <files> && git commit -m "..." && git push` |

### VM commands (run via SSH)

| Task | Command |
|---|---|
| Pull latest code | `sudo git -C /tg-bot/tg-bot-ai-powered-stock-rec pull` |
| Install new deps | `sudo -u stockbot /tg-bot/tg-bot-ai-powered-stock-rec/venv/bin/pip install -r /tg-bot/tg-bot-ai-powered-stock-rec/requirements.txt` |
| Reload systemd units | `sudo systemctl daemon-reload` |
| Restart timer | `sudo systemctl restart ai-investment-advisor.timer` |
| Restart bot listener | `sudo systemctl restart ai-investment-advisor-listener.service` |
| Manual pipeline trigger | `sudo systemctl start ai-investment-advisor.service` |
| Stream pipeline logs | `journalctl -u ai-investment-advisor -f` |
| Stream listener logs | `journalctl -u ai-investment-advisor-listener -f` |
| Check timer schedule | `systemctl list-timers ai-investment-advisor.timer` |
| Tail app log file | `tail -f /tg-bot/tg-bot-ai-powered-stock-rec/logs/advisor.log` |
| Check recent reports | `sqlite3 /tg-bot/tg-bot-ai-powered-stock-rec/data/reports.db "SELECT run_at, portfolio_bias, error_flag FROM reports ORDER BY run_at DESC LIMIT 5;"` |
| Edit secrets | `sudo nano /tg-bot/tg-bot-ai-powered-stock-rec/.env` |

---

## Common Upgrade Scenarios

### Config-only change (e.g. risk appetite, discovery settings)

```bash
# Local: edit config/config.yaml, verify with dry run, push
python -m src.main --dry-run
git add config/config.yaml && git commit -m "Update: ..." && git push

# VM: pull + restart timer (no pip install, no daemon-reload needed)
sudo git -C /tg-bot/tg-bot-ai-powered-stock-rec pull
sudo systemctl restart ai-investment-advisor.timer
```

### Prompt-only change (e.g. tuning discovery or analysis instructions)

```bash
# Local: edit config/prompts.yaml, verify with dry run, push
python -m src.main --dry-run

# VM: pull + restart timer (no pip install, no daemon-reload needed)
sudo git -C /tg-bot/tg-bot-ai-powered-stock-rec pull
sudo systemctl restart ai-investment-advisor.timer
```

### Code change (new feature or bug fix)

```bash
# Local: edit code → run tests → dry run → push
pytest tests/ -v
python -m src.main --dry-run
git add src/... && git commit -m "..." && git push

# VM: pull + optionally install deps + restart timer
sudo git -C /tg-bot/tg-bot-ai-powered-stock-rec pull
# Only if requirements.txt changed:
sudo -u stockbot /tg-bot/tg-bot-ai-powered-stock-rec/venv/bin/pip install -r /tg-bot/tg-bot-ai-powered-stock-rec/requirements.txt
sudo systemctl restart ai-investment-advisor.timer
```

### New API key added

```bash
# VM only — edit .env and restart both services
sudo nano /tg-bot/tg-bot-ai-powered-stock-rec/.env
sudo systemctl restart ai-investment-advisor.timer
sudo systemctl restart ai-investment-advisor-listener.service
# No git pull needed (secrets are never in git)
```

---

## Triggering an Ad-Hoc Report

Open a **private chat with your bot** on Telegram and send:
```
/report
```

The bot will reply "Running pipeline... report will arrive in the group chat in ~2 minutes."
The report is then sent to the group chat (`TELEGRAM_CHAT_ID`), same as scheduled runs.

**Authorization**: only works if your Telegram user ID matches `TELEGRAM_OWNER_USER_ID` in `.env`.
To find your user ID: message `@userinfobot` on Telegram.

If the bot doesn't respond at all, check the listener is running:
```bash
systemctl status ai-investment-advisor-listener.service
journalctl -u ai-investment-advisor-listener -f
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `git pull` on VM shows "not a git repository" | Wrong directory | Always use `sudo git -C /tg-bot/tg-bot-ai-powered-stock-rec pull` |
| Service fails immediately | `.env` missing or malformed | `sudo -u stockbot /tg-bot/tg-bot-ai-powered-stock-rec/venv/bin/python -m src.main --dry-run` to see the error |
| Timer never fires | Timer not enabled | `sudo systemctl enable --now ai-investment-advisor.timer` |
| `Import error` after pull | New dependency not installed | Run the `pip install` command in Step 7 above |
| Telegram "Chat not found" | Wrong `TELEGRAM_CHAT_ID` in `.env` | Add the bot to the group chat, send a message, then call `https://api.telegram.org/bot<TOKEN>/getUpdates` to find the correct chat ID |
| All sources empty in logs | API keys missing from `.env` | `sudo nano /tg-bot/tg-bot-ai-powered-stock-rec/.env` and verify all keys are set |
| `/report` gets no response | Listener not running or wrong user ID | Check `systemctl status ai-investment-advisor-listener.service`; verify `TELEGRAM_OWNER_USER_ID` matches what `@userinfobot` returned |
| Listener starts then immediately stops | `TELEGRAM_OWNER_USER_ID` not set in `.env` | Add it to `.env` and `sudo systemctl restart ai-investment-advisor-listener.service` |
| `/report` says "Failed to start pipeline" | Sudoers rule missing | Re-run `install.sh` as root, or manually add the rule (see `install.sh` comments) |
