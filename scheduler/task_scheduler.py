"""
Task Scheduler
==============
Automates the fixed portfolio strategy on a schedule (all times = US Eastern).

  - Pre-open health check: Mon-Fri 09:00 ET
  - Daily monitor:         Mon-Fri 10:00 ET and 14:00 ET
  - Rebalance:             per STRATEGY_CONFIG (daily 09:40 ET by default)

Operational hardening:
  * misfire_grace_time + coalesce — a sleeping laptop no longer silently drops a
    session. Previously APScheduler's 1-second default grace meant "missed by
    8:30" turned into "skipped entirely, no alert".
  * fail-closed market status — an API error skips trading instead of assuming
    the market is open.
  * rotating logs — the old single file grew across months.
"""

import json
import logging
import logging.handlers
import os
from datetime import datetime, timedelta
from typing import Optional

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from config.settings import (
    EXECUTION_CONFIG, FIXED_PORTFOLIO, LOG_CONFIG, STATE_DIR, STRATEGY_CONFIG,
    TIGER_CONFIG, TRADING_ENV,
)
from core.data_fetcher import DataFetcher
from core.notifier import Notifier
from core.tiger_client import TigerClient
from execution.order_manager import OrderManager

logger = logging.getLogger(__name__)

_ET = "America/New_York"
MISFIRE_GRACE_SECONDS = 900      # run up to 15 minutes late
MAX_INSTANCES = 1


def setup_logging():
    """Configure rotating file logging and console output."""
    log_dir = LOG_CONFIG.get("log_dir", "logs")
    os.makedirs(log_dir, exist_ok=True)

    handlers = []
    if LOG_CONFIG.get("log_to_console", True):
        handlers.append(logging.StreamHandler())
    if LOG_CONFIG.get("log_to_file", True):
        path = os.path.join(log_dir, "trading.log")
        handler = logging.handlers.TimedRotatingFileHandler(
            path,
            when=LOG_CONFIG.get("log_rotate_when", "midnight"),
            backupCount=LOG_CONFIG.get("log_backup_count", 30),
            encoding="utf-8",
        )
        handler.suffix = "%Y%m%d"
        handlers.append(handler)

    logging.basicConfig(
        level=getattr(logging, LOG_CONFIG.get("log_level", "INFO")),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


class TradingScheduler:
    """Schedules and runs all automated trading tasks."""

    def __init__(self):
        self.tiger = TigerClient()
        self.data_fetcher: Optional[DataFetcher] = None
        self.order_manager: Optional[OrderManager] = None
        self.notifier = Notifier(state_dir=STATE_DIR)
        self._rebalancer = None
        self.scheduler = BlockingScheduler(
            timezone=_ET,
            job_defaults={
                "coalesce": True,
                "max_instances": MAX_INSTANCES,
                "misfire_grace_time": MISFIRE_GRACE_SECONDS,
            },
        )

    # ------------------------------------------------------------------ #
    #  Initialization
    # ------------------------------------------------------------------ #

    def initialize(self):
        logger.info("Initializing trading system (env=%s, account=%s)...",
                    TRADING_ENV.upper(), TIGER_CONFIG.get("account"))
        self.tiger.connect()
        self.data_fetcher = DataFetcher(self.tiger)
        self.order_manager = OrderManager(
            self.tiger,
            notifier=self.notifier,
            price_provider=self.data_fetcher.get_current_price,
        )

        if FIXED_PORTFOLIO.get("use_fixed_portfolio", False):
            from execution.fixed_rebalancer import FixedPortfolioRebalancer
            self._rebalancer = FixedPortfolioRebalancer(
                self.data_fetcher, self.order_manager
            )
        else:
            from execution.rebalancer import PortfolioRebalancer
            self._rebalancer = PortfolioRebalancer(
                self.data_fetcher, self.order_manager
            )

        logger.info("Trading system initialized")

    # ------------------------------------------------------------------ #
    #  Market Status Check
    # ------------------------------------------------------------------ #

    def is_market_open(self) -> bool:
        """
        Check whether the US market is open via the Tiger API.

        Fails closed by default: if the status endpoint is unreachable we do NOT
        trade. Set market_check_fail_open=True only if you accept that risk.
        """
        fail_open = bool(EXECUTION_CONFIG.get("market_check_fail_open", False))
        try:
            statuses = self.tiger.get_market_status()
            for status in statuses or []:
                market = str(getattr(status, "market", "") or "")
                state = str(getattr(status, "status", "") or "")
                if "US" in market.upper():
                    is_open = "open" in state.lower() or "trading" in state.lower()
                    if not is_open:
                        logger.info("Market status: %s (%s) — skipping", state, market)
                    return is_open
            logger.warning("Market status response had no US entry: %s", statuses)
        except Exception as exc:
            logger.error("Market status check failed: %s", exc)
            self.notifier.warning("Market status unknown",
                                  f"{exc} — trading skipped (fail-closed)")

        return fail_open

    # ------------------------------------------------------------------ #
    #  Jobs
    # ------------------------------------------------------------------ #

    def run_health_check(self) -> dict:
        """Pre-open sanity check: API, account, positions, prices, state."""
        checks = {}
        try:
            account = self.data_fetcher.get_account_summary()
            checks["account"] = bool(account.get("net_liquidation"))
            checks["net_liquidation"] = account.get("net_liquidation", 0)

            positions = self.data_fetcher.get_current_positions()
            checks["positions"] = len(positions)

            if positions:
                prices = self.data_fetcher.get_quotes(list(positions.keys()))
                missing = [s for s in positions if not (prices.get(s) or {}).get("last")]
                checks["missing_prices"] = missing
            else:
                checks["missing_prices"] = []

            checks["state_dir"] = os.path.isdir(STATE_DIR)
            checks["ok"] = bool(checks["account"]) and not checks["missing_prices"]
        except Exception as exc:
            checks["ok"] = False
            checks["error"] = str(exc)

        if checks.get("ok"):
            logger.info("Health check OK — net liq $%.0f, %d positions",
                        checks.get("net_liquidation", 0), checks.get("positions", 0))
        else:
            logger.error("Health check FAILED: %s", checks)
            self.notifier.critical(
                "Pre-open health check failed",
                f"env={TRADING_ENV} account={TIGER_CONFIG.get('account')} details={checks}",
            )
        return checks

    def run_daily_monitor(self):
        logger.info("--- Daily Monitor Starting ---")
        if not self.is_market_open():
            return
        try:
            report = self._rebalancer.run_daily_monitor()
            if report.get("should_notify"):
                report_path = f"logs/daily_monitor_{datetime.now():%Y%m%d_%H%M}.json"
                os.makedirs("logs", exist_ok=True)
                with open(report_path, "w", encoding="utf-8") as f:
                    json.dump(report, f, indent=2, default=str)
                events = report.get("actionable_events", {})
                logger.info(
                    "Daily monitor activity → cancelled=%d stops=%d alerts=%d | %s",
                    events.get("cancelled_orders", 0),
                    events.get("stop_orders_submitted", 0),
                    events.get("alerts", 0), report_path,
                )
        except Exception as exc:
            logger.error("Daily monitor failed: %s", exc, exc_info=True)
            self.notifier.critical("Daily monitor crashed", str(exc))

    def _run_rebalance(self, label: str):
        logger.info("=== %s Rebalance Starting ===", label)
        if not self.is_market_open():
            return
        try:
            summary = self._rebalancer.run_rebalance(dry_run=False)
            report_path = f"logs/rebalance_{datetime.now():%Y%m%d_%H%M}.json"
            os.makedirs("logs", exist_ok=True)
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(summary, f, indent=2, default=str)
            logger.info("%s rebalance complete → %d planned | report %s",
                        label, summary.get("trades_count", 0), report_path)
        except Exception as exc:
            logger.error("%s rebalance failed: %s", label, exc, exc_info=True)
            self.notifier.critical(f"{label} rebalance crashed", str(exc))

    def run_scheduled_rebalance(self):
        self._run_rebalance("Scheduled")

    def run_monthly_rebalance(self):
        self._run_rebalance("Monthly")

    # ------------------------------------------------------------------ #
    #  Schedule Configuration
    # ------------------------------------------------------------------ #

    def _get_rebalance_schedule(self):
        rebalance_time = STRATEGY_CONFIG.get("rebalance_time", "09:40")
        try:
            hour_str, minute_str = rebalance_time.split(":")
            hour, minute = int(hour_str), int(minute_str)
        except Exception:
            logger.warning("Invalid rebalance_time=%r, falling back to 09:40 ET", rebalance_time)
            hour, minute = 9, 40

        freq = (STRATEGY_CONFIG.get("rebalance_frequency", "monthly") or "monthly").lower()
        rebal_day = STRATEGY_CONFIG.get("rebalance_day", 1)
        return freq, rebal_day, hour, minute

    def setup_schedules(self):
        """Configure all scheduled jobs (all times are US Eastern)."""
        freq, rebal_day, hour, minute = self._get_rebalance_schedule()

        self.scheduler.add_job(
            self.run_health_check,
            trigger=CronTrigger(day_of_week="mon-fri", hour=9, minute=0, timezone=_ET),
            id="health_check", name="Pre-open Health Check", replace_existing=True,
        )

        self.scheduler.add_job(
            self.run_daily_monitor,
            trigger=CronTrigger(day_of_week="mon-fri", hour=10, minute=0, timezone=_ET),
            id="daily_monitor", name="Fixed Portfolio Daily Monitor", replace_existing=True,
        )

        self.scheduler.add_job(
            self.run_daily_monitor,
            trigger=CronTrigger(day_of_week="mon-fri", hour=14, minute=0, timezone=_ET),
            id="midday_monitor", name="Fixed Portfolio Midday Monitor", replace_existing=True,
        )

        if freq == "daily":
            self.scheduler.add_job(
                self.run_scheduled_rebalance,
                trigger=CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute, timezone=_ET),
                id="daily_rebalance", name="Daily Strategy Rebalance", replace_existing=True,
            )
        elif freq == "weekly":
            weekdays = ["mon", "tue", "wed", "thu", "fri"]
            day_index = rebal_day if isinstance(rebal_day, int) else 0
            day_of_week = weekdays[max(0, min(day_index, len(weekdays) - 1))]
            self.scheduler.add_job(
                self.run_scheduled_rebalance,
                trigger=CronTrigger(day_of_week=day_of_week, hour=hour, minute=minute, timezone=_ET),
                id="weekly_rebalance", name="Weekly Strategy Rebalance", replace_existing=True,
            )
        elif freq == "monthly":
            self.scheduler.add_job(
                self.run_scheduled_rebalance,
                trigger=CronTrigger(day=rebal_day, hour=hour, minute=minute,
                                    day_of_week="mon-fri", timezone=_ET),
                id="monthly_rebalance", name="Monthly Strategy Rebalance", replace_existing=True,
            )
        else:
            logger.warning("Unsupported rebalance_frequency=%r, no rebalance job scheduled", freq)

        logger.info("Scheduled jobs (US Eastern), env=%s:", TRADING_ENV.upper())
        for job in self.scheduler.get_jobs():
            next_run = getattr(job, "next_run_time", None) or "(pending start)"
            logger.info("  %-32s next: %s", job.name, next_run)

    # ------------------------------------------------------------------ #
    #  Start
    # ------------------------------------------------------------------ #

    def start(self):
        setup_logging()
        self.initialize()
        self.setup_schedules()
        self.notifier.info(
            "Trading scheduler started",
            f"env={TRADING_ENV.upper()} account={TIGER_CONFIG.get('account')}",
        )
        logger.info("Scheduler running. Press Ctrl+C to stop.")
        try:
            self.scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            logger.info("Scheduler stopped.")
            self.notifier.info("Trading scheduler stopped", "process exited")
