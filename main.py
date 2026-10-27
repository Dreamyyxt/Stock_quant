"""
Stock Quant - Automated US Stock Quantitative Trading System
============================================================
Main entry point. Supports multiple run modes.

  python main.py --mode health        # Pre-open health check (API/account/prices)
  python main.py --mode probe         # What market data can this account actually get?
  python main.py --mode intraday-test # Intraday engine on synthetic bars (offline)
  python main.py --mode intraday-replay --date 2026-10-23   # Replay a real session
  python main.py --mode intraday      # Intraday session against the broker (paper)
  python main.py --mode dry           # Dry run rebalance (no trades)
  python main.py --mode rebalance     # Run one rebalancing cycle now
  python main.py --mode live          # Start live trading scheduler
  python main.py --mode monitor       # Run daily monitor once
  python main.py --mode sync-orders   # Sync broker order statuses to event log
  python main.py --mode backtest      # Run historical backtest
  python main.py --mode status        # Show current portfolio status

Every order-placing mode takes a single-instance lock for the configured
account, so a manual invocation cannot run alongside the launchd service.
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import (
    EXECUTION_CONFIG, INTRADAY_CONFIG, STATE_DIR, TIGER_CONFIG, TRADING_ENV,
    intraday_config, is_live, live_trading_allowed, LIVE_CONFIRM_TOKEN,
)
from core.state_store import StateStore
from core.trading_lock import SingleInstanceLock
from scheduler.task_scheduler import TradingScheduler, setup_logging

setup_logging()
logger = logging.getLogger(__name__)

LOCKED_MODES = {"live", "rebalance", "monitor", "sync-orders", "intraday"}


def check_config() -> bool:
    """Validate configuration and refuse to trade live without explicit consent."""
    if not TIGER_CONFIG.get("tiger_id") or not TIGER_CONFIG.get("account"):
        print("\n[ERROR] Tiger credentials missing.")
        print("Set TIGER_ID / TIGER_ACCOUNT in the environment (or config/.env).")
        return False

    key_path = TIGER_CONFIG.get("private_key_path", "")
    if not TIGER_CONFIG.get("private_key") and not os.path.exists(key_path):
        print(f"\n[ERROR] RSA private key not found at {key_path}")
        print("Set TIGER_PRIVATE_KEY_PATH, or generate one with config/generate_keys.py")
        return False

    if is_live() and not live_trading_allowed():
        print("\n" + "!" * 70)
        print("  LIVE TRADING BLOCKED")
        print(f"  TRADING_ENV=live but LIVE_TRADING_CONFIRM is not set to the token.")
        print(f"  To proceed, export: LIVE_TRADING_CONFIRM={LIVE_CONFIRM_TOKEN}")
        print("  Read docs/LIVE_READINESS.md before doing that.")
        print("!" * 70 + "\n")
        return False

    return True


def _make_rebalancer(data, orders):
    from config.settings import FIXED_PORTFOLIO
    if FIXED_PORTFOLIO.get("use_fixed_portfolio", False):
        from execution.fixed_rebalancer import FixedPortfolioRebalancer
        return FixedPortfolioRebalancer(data, orders)
    from execution.rebalancer import PortfolioRebalancer
    return PortfolioRebalancer(data, orders)


def _connect():
    from core.tiger_client import TigerClient
    from core.data_fetcher import DataFetcher
    from execution.order_manager import OrderManager

    tiger = TigerClient()
    tiger.connect()
    data = DataFetcher(tiger)
    orders = OrderManager(tiger, price_provider=data.get_current_price)
    return data, orders


def run_live():
    scheduler = TradingScheduler()
    scheduler.start()


def run_rebalance(dry: bool = False):
    data, orders = _connect()
    _make_rebalancer(data, orders).run_rebalance(dry_run=dry)


def run_monitor():
    data, orders = _connect()
    report = _make_rebalancer(data, orders).run_daily_monitor()

    print("\nRisk Monitor Report:")
    print(f"  Portfolio Value:   ${report.get('portfolio_value', 0):,.2f}")
    print(f"  Drawdown:          {report.get('portfolio_drawdown_pct', 0):.1%}")
    cooldowns = report.get("cooldowns") or {}
    if cooldowns:
        print(f"  Cooldowns:         {', '.join(f'{k} ({v}d)' for k, v in cooldowns.items())}")
    alerts = report.get("alerts", [])
    if alerts:
        print("\n  ALERTS:")
        for alert in alerts:
            print(f"    ! {alert}")
    else:
        print("  No risk alerts.")
    print()


def run_health():
    scheduler = TradingScheduler()
    scheduler.initialize()
    result = scheduler.run_health_check()
    print("\nHealth check:")
    for key, value in result.items():
        print(f"  {key}: {value}")
    print(f"\n  env: {TRADING_ENV.upper()}   account: {TIGER_CONFIG.get('account')}\n")
    return result


def run_probe():
    """Report what market data this account can actually retrieve."""
    import json
    from intraday.providers import probe_capabilities

    tiger = None
    try:
        from core.tiger_client import TigerClient
        tiger = TigerClient()
        tiger.connect()
    except Exception as exc:
        tiger = None
        print(f"\n[!] Broker connection unavailable: {exc}")
        print("    Probing public sources only. Check TIGER_* env vars / network.\n")

    report = probe_capabilities(tiger)
    print("\nMarket data capability report:")
    print(json.dumps(report, indent=2, default=str))
    print("\nInterpretation:")
    print("  tiger_1min.ok=true with a recent last_bar  -> real-time/near-real-time bars")
    print("  yfinance_1min.ok=true                      -> delayed bars, research/replay only")
    print("  alpaca_1min.ok=true                        -> IEX real-time feed (free key)")
    print()
    return report


def _connect_intraday(config: dict = None):
    """Broker wiring with intraday-specific execution overrides."""
    from core.data_fetcher import DataFetcher
    from core.tiger_client import TigerClient
    from execution.order_manager import OrderManager

    cfg = config or INTRADAY_CONFIG
    tiger = TigerClient()
    tiger.connect()
    data = DataFetcher(tiger)

    overrides = dict(EXECUTION_CONFIG)
    overrides.update({
        # Intraday legitimately trades the same name several times per session.
        # Duplicate protection falls back to the broker open-order check.
        "block_repeat_symbol_action_days": 0,
        # Each round trip costs two orders (entry + exit). The old *2 left zero
        # headroom: one retry or one partial-fill top-up would exhaust the cap
        # and the exit order would be rejected, leaving the position open
        # overnight. Add a small buffer so the exit is never the order that
        # gets blocked.
        "max_daily_trades": int(cfg.get("max_trades_per_day", 30)) * 2 + 4,
        "min_order_notional": cfg.get("min_order_notional", 200.0),
        "limit_slippage_pct": cfg.get("limit_slippage_pct", 0.0015),
        "fill_timeout_seconds": cfg.get("fill_timeout_seconds", 45),
        "trade_end_time": "15:59",
    })
    orders = OrderManager(tiger, config=overrides,
                          price_provider=data.get_current_price)
    return tiger, data, orders


def run_intraday(mode: str, session_date: str = None, speed: int = 1,
                 initial_cash: float = 100_000.0, dry: bool = False,
                 strategy: str = None):
    from intraday.engine import build_engine

    config = intraday_config(strategy)
    if mode == "live":
        # Shadow mode (`--dry`) goes through this branch too on purpose: it is
        # meant to rehearse *live* execution, so it has to face the same clock.
        # A live session must be flat before the bell. An order submitted at
        # 16:00 ET is either rejected (outside regular hours) or carried to the
        # next session, which silently turns an intraday strategy into an
        # overnight one — the exact risk the backtest does not model. The
        # backtest exits at the 15:59 close, so 15:58 is the closest
        # executable equivalent. Replay keeps 16:00 so it still reproduces the
        # validated 15:59 exit price.
        config["flatten_time"] = "15:58"
    client = None
    orders = None
    if mode in ("live", "dry"):
        client, _data, orders = _connect_intraday(config)

    engine = build_engine(
        mode, config,
        client=client, order_manager=orders,
        session_date=session_date, speed=speed,
        initial_cash=initial_cash, dry_run=dry,
    )
    return engine.run()


def run_status():
    data, _ = _connect()
    account = data.get_account_summary()
    positions = data.get_current_positions()
    risk_state = StateStore(f"{STATE_DIR}/risk_state.json").load()
    order_state = StateStore(f"{STATE_DIR}/order_state.json").load()

    print("\n" + "=" * 64)
    print(f"  PORTFOLIO STATUS        env={TRADING_ENV.upper()}  "
          f"account={TIGER_CONFIG.get('account')}")
    print("=" * 64)
    print(f"  Net Liquidation: ${account.get('net_liquidation', 0):>12,.2f}")
    print(f"  Cash:            ${account.get('cash', 0):>12,.2f}")
    print(f"  Buying Power:    ${account.get('buying_power', 0):>12,.2f}")
    print(f"  Unrealized P&L:  ${account.get('unrealized_pnl', 0):>12,.2f}")
    print(f"\n  Positions ({len(positions)}):")
    print(f"  {'Symbol':<8} {'Qty':>6} {'Avg Cost':>10} {'Mkt Val':>12} {'P&L':>10} {'P&L%':>8}")
    print("  " + "-" * 56)

    total_pnl = 0.0
    for sym, pos in sorted(positions.items()):
        pnl = pos.get("unrealized_pnl", 0)
        pnl_pct = pos.get("unrealized_pnl_pct") or 0
        total_pnl += pnl
        print(f"  {sym:<8} {pos.get('quantity', 0):>6} "
              f"${pos.get('avg_cost', 0):>9.2f} "
              f"${pos.get('market_value', 0):>11,.2f} "
              f"${pnl:>9,.2f} {pnl_pct:>7.1%}")

    print("  " + "-" * 56)
    print(f"  {'Total P&L:':<30} ${total_pnl:>9,.2f}")

    cooldowns = risk_state.get("cooldowns") or {}
    if cooldowns:
        print(f"\n  Stop-out cooldowns: {cooldowns}")
    print(f"  Portfolio peak:     ${risk_state.get('portfolio_peak', 0):,.2f}")
    daily = order_state.get("daily") or {}
    print(f"  Orders today:       {daily.get('count', 0)} ({daily.get('date', '-')})")
    print("=" * 64 + "\n")


def run_sync_orders():
    _, orders = _connect()
    events = orders.sync_order_statuses()
    print(f"\nOrder sync complete: {len(events)} status change(s) detected.\n")


def run_backtest():
    from backtest.run_backtest import main as backtest_main
    backtest_main()


def main():
    parser = argparse.ArgumentParser(
        description="Stock Quant - US Stock Quantitative Trading System"
    )
    parser.add_argument(
        "--mode",
        choices=["health", "probe", "intraday", "intraday-test", "intraday-replay",
                 "live", "rebalance", "dry", "monitor", "sync-orders", "backtest", "status"],
        default="dry",
        help="Run mode (default: dry)",
    )
    parser.add_argument("--date", help="Session date for intraday-replay (YYYY-MM-DD)")
    parser.add_argument("--speed", type=int, default=1,
                        help="Replay: simulated minutes per tick (default 1 = accurate)")
    parser.add_argument("--cash", type=float, default=100_000.0,
                        help="Starting cash for simulated intraday modes (default 100000)")
    parser.add_argument("--dry", action="store_true",
                        help="Intraday: simulate fills locally instead of sending orders")
    parser.add_argument("--strategy", default=None,
                        choices=["orb_breakout", "orb_tsla"],
                        help="Intraday strategy config (default: orb_breakout, "
                             "or $INTRADAY_STRATEGY)")
    args = parser.parse_args()

    handlers = {
        "health": run_health,
        "probe": run_probe,
        "intraday": lambda: run_intraday("live", dry=args.dry, strategy=args.strategy),
        "intraday-test": lambda: run_intraday("synthetic", initial_cash=args.cash,
                                             strategy=args.strategy),
        "intraday-replay": lambda: run_intraday(
            "replay", session_date=args.date, speed=args.speed,
            initial_cash=args.cash, dry=True, strategy=args.strategy),
        "live": run_live,
        "rebalance": lambda: run_rebalance(dry=False),
        "dry": lambda: run_rebalance(dry=True),
        "monitor": run_monitor,
        "sync-orders": run_sync_orders,
        "backtest": run_backtest,
        "status": run_status,
    }

    logger.info("Starting in mode: %s (env=%s)", args.mode, TRADING_ENV.upper())

    # Modes that neither touch the broker nor need credentials
    if args.mode in ("backtest", "intraday-test", "intraday-replay"):
        handlers[args.mode]()
        return

    if not check_config():
        sys.exit(2)

    lock = None
    if args.mode in LOCKED_MODES:
        lock = SingleInstanceLock(
            f"{STATE_DIR}/trading_{TIGER_CONFIG['account']}.lock"
        )
        if not lock.acquire():
            print("\n[ABORT] Another trading process is already running for this "
                  f"account ({TIGER_CONFIG['account']}).\n")
            sys.exit(3)

    try:
        handlers[args.mode]()
    finally:
        if lock is not None:
            lock.release()


if __name__ == "__main__":
    main()
