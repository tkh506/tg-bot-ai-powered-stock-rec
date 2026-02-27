"""
AI Investment Advisor — main entry point.

Two-stage discovery pipeline:
  Stage 1 — broad market scan → AI identifies up to N candidate stocks from Reddit trending
  Stage 2 — deep-dive on candidates → AI produces final recommendations (max M)

Usage:
    python -m src.main                # normal run
    python -m src.main --dry-run      # fetch + analyze but skip Telegram send
    python -m src.main --schedule     # run on APScheduler (fallback for non-systemd envs)
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Ensure project root is on sys.path when run as a module
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.config_loader import load_config, ConfigError
from src.utils.logger import setup_logger, get_logger
from src.data import fetcher
from src.analysis import prompt_builder, ai_client, response_parser
from src.analysis.response_parser import Candidate
from src.reporting import formatter, archiver
from src.notifications import telegram_bot

logger = get_logger("main")


class PipelineError(Exception):
    pass


def _run_pipeline(config, dry_run: bool = False) -> None:
    """Execute one full two-stage analysis → report → notify cycle."""

    # ── PHASE 1: Broad market scan ─────────────────────────────────────────────
    logger.info("=== PHASE 1: Broad market scan ===")
    broad_data = fetcher.fetch_broad_market_data(config)

    if not broad_data.trending_stocks or broad_data.trending_stocks.error:
        raise PipelineError(
            "ApeWisdom fetch failed — no trending stock data available for discovery"
        )

    # Stage 1 AI call: identify up to max_candidates from ApeWisdom trending list
    logger.info("=== STAGE 1 AI: Discovering investment candidates ===")
    max_retries = config.ai.max_retries
    discovery_result = None
    raw_discovery: str = ""
    tok_in1 = tok_out1 = 0
    previous_bad_discovery: str | None = None

    for attempt in range(1, max_retries + 1):
        discovery_prompt = prompt_builder.build_discovery_prompt(
            config, broad_data, previous_bad_response=previous_bad_discovery
        )
        try:
            raw_discovery, tok_in1, tok_out1 = ai_client.call_with_retry(
                discovery_prompt, config
            )
            discovery_result = response_parser.parse_candidates(raw_discovery)
            break
        except response_parser.ParseError as e:
            logger.warning(f"Discovery parse error on attempt {attempt}/{max_retries}: {e}")
            previous_bad_discovery = raw_discovery
            if attempt == max_retries:
                raise PipelineError(
                    f"Stage 1 AI response could not be parsed after {max_retries} attempts: {e}"
                )
            time.sleep(config.ai.retry_delay_seconds)
        except ai_client.OpenRouterError as e:
            raise PipelineError(f"OpenRouter API failure in Stage 1: {e}")

    assert discovery_result is not None

    # Validate candidates: keep only tickers that actually appear in the ApeWisdom list.
    # This prevents the AI from hallucinating tickers not in the trending data.
    aw_tickers = set(broad_data.trending_stocks.data.keys())  # already uppercase
    validated = [
        c for c in discovery_result.candidates
        if c.ticker.upper() in aw_tickers
    ]
    validated = validated[:config.discovery.max_candidates]

    if len(validated) < len(discovery_result.candidates):
        dropped = len(discovery_result.candidates) - len(validated)
        logger.warning(
            f"Dropped {dropped} candidate(s) not found in ApeWisdom list "
            f"(AI hallucination guard)"
        )

    # Always add Gold as a separate commodity candidate (not from ApeWisdom)
    gold_candidate = Candidate(
        ticker="GC=F", name="Gold", exchange="COMMODITY", rationale="Always-on macro hedge"
    )
    if config.discovery.always_include_gold:
        validated.append(gold_candidate)

    logger.info(
        f"Stage 1 complete: {len(validated)} candidates selected "
        f"({len(validated)-1} stocks + Gold)"
    )
    for c in validated:
        logger.info(f"  Candidate: {c.ticker} — {c.name} [{c.rationale[:60]}]")

    # ── PHASE 2: Deep-dive data fetch for candidates ───────────────────────────
    logger.info("=== PHASE 2: Deep-dive data fetch ===")
    snapshot = fetcher.fetch_targeted_data(config, validated, broad_data)

    available = [a for a in snapshot.all_assets() if not a.data_unavailable]
    if not available:
        raise PipelineError("All candidate data sources failed — no data available for Stage 2 analysis")

    # Stage 2 AI call: analyse candidates and produce final recommendations
    logger.info("=== STAGE 2 AI: Generating final recommendations ===")
    result = None
    raw_analysis: str = ""
    tok_in2 = tok_out2 = 0
    previous_bad_analysis: str | None = None

    for attempt in range(1, max_retries + 1):
        analysis_prompt = prompt_builder.build(
            config, snapshot, previous_bad_response=previous_bad_analysis
        )
        try:
            raw_analysis, tok_in2, tok_out2 = ai_client.call_with_retry(
                analysis_prompt, config
            )
            result = response_parser.parse(raw_analysis)
            break
        except response_parser.ParseError as e:
            logger.warning(f"Analysis parse error on attempt {attempt}/{max_retries}: {e}")
            previous_bad_analysis = raw_analysis
            if attempt == max_retries:
                raise PipelineError(
                    f"Stage 2 AI response could not be parsed after {max_retries} attempts: {e}"
                )
            time.sleep(config.ai.retry_delay_seconds)
        except ai_client.OpenRouterError as e:
            raise PipelineError(f"OpenRouter API failure in Stage 2: {e}")

    assert result is not None

    total_input_tokens = tok_in1 + tok_in2
    total_output_tokens = tok_out1 + tok_out2
    logger.info(
        f"Stage 2 complete: {len(result.assets)} recommendation(s) generated. "
        f"Total tokens: {total_input_tokens} in / {total_output_tokens} out"
    )

    # ── PHASE 3: Format report ─────────────────────────────────────────────────
    logger.info("=== PHASE 3: Formatting report ===")
    report_parts = formatter.render(result, config, discovery_result=discovery_result)
    full_report_md = "\n\n---\n\n".join(report_parts)

    # ── PHASE 4: Send via Telegram ─────────────────────────────────────────────
    if dry_run:
        logger.info("DRY RUN — skipping Telegram send. Report preview:")
        for i, part in enumerate(report_parts):
            print(f"\n--- Part {i+1}/{len(report_parts)} ---\n{part}")
    else:
        logger.info("=== PHASE 4: Sending Telegram report ===")
        telegram_bot.send_report(report_parts, config)

    # ── PHASE 5: Archive ───────────────────────────────────────────────────────
    logger.info("=== PHASE 5: Archiving ===")
    all_sources = list(dict.fromkeys(
        broad_data.data_sources_used + snapshot.data_sources_used
    ))
    report_id = archiver.save(
        result=result,
        report_md=full_report_md,
        config=config,
        token_input=total_input_tokens,
        token_output=total_output_tokens,
        data_sources_used=all_sources,
    )
    logger.info(f"Run complete. Report #{report_id} saved.")


def run(dry_run: bool = False) -> None:
    """Load config and run the pipeline with top-level error handling."""
    try:
        config = load_config()
    except FileNotFoundError as e:
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"FATAL: Config validation failed: {e}", file=sys.stderr)
        sys.exit(1)

    setup_logger(log_level=config.app.log_level)
    logger.info(f"Starting AI Investment Advisor (dry_run={dry_run})")

    try:
        _run_pipeline(config, dry_run=dry_run)
    except PipelineError as e:
        logger.error(f"Pipeline failed: {e}", exc_info=True)
        if not dry_run:
            telegram_bot.send_error_alert(str(e), config)
        sys.exit(0)   # exit 0 so systemd timer keeps running
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        if not dry_run:
            try:
                telegram_bot.send_error_alert(f"Unexpected error: {e}", config)
            except Exception:
                pass
        sys.exit(0)


def run_scheduled(config) -> None:
    """APScheduler-based runner (fallback for non-systemd environments)."""
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    import pytz

    tz = pytz.timezone(config.app.timezone)
    sched_cfg = config.schedule

    scheduler = BlockingScheduler(timezone=tz)
    scheduler.add_job(
        lambda: _run_pipeline(config),
        CronTrigger(
            hour=sched_cfg.cron_hour,
            minute=sched_cfg.cron_minute,
            day_of_week=sched_cfg.cron_days_of_week,
            timezone=tz,
        ),
        id="advisor_run",
        name="AI Investment Advisor",
        misfire_grace_time=3600,
    )

    logger.info(
        f"APScheduler started: {sched_cfg.cron_days_of_week} "
        f"at {sched_cfg.cron_hour:02d}:{sched_cfg.cron_minute:02d} {config.app.timezone}"
    )
    scheduler.start()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI Investment Advisor")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run analysis but print report to stdout instead of sending to Telegram"
    )
    parser.add_argument(
        "--schedule", action="store_true",
        help="Run on APScheduler (use on non-systemd systems)"
    )
    args = parser.parse_args()

    if args.schedule:
        try:
            cfg = load_config()
        except Exception as e:
            print(f"FATAL: {e}", file=sys.stderr)
            sys.exit(1)
        setup_logger(log_level=cfg.app.log_level)
        run_scheduled(cfg)
    else:
        run(dry_run=args.dry_run)
