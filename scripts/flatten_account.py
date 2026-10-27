#!/usr/bin/env python3
"""
Flatten every open position in the broker account
=================================================

One-off cleanup, not part of the strategy. The single-name ORB backtest assumes
100% of equity in one symbol and nothing else; a paper account that is 98%
invested in eight unrelated names turns that assumption into a ~2x leveraged
bet. This script is the bridge back to the validated exposure.

Safety
------
* Dry run by default. It prints the plan and touches nothing.
* `--execute` is required to actually submit sells.
* Refuses to fire outside the regular session: an order queued overnight fills
  at the next open, at a price nobody agreed to.
* Sell limit sits *below* the reference price so it crosses the spread. The
  goal is to be flat, not to earn the last cent.
* Every fill is confirmed, not assumed.

Usage
-----
    python scripts/flatten_account.py                 # show the plan
    python scripts/flatten_account.py --execute       # actually flatten

    python scripts/flatten_account.py --keep TSLA     # leave one name alone
"""

import argparse
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import EXECUTION_CONFIG, TRADING_ENV  # noqa: E402

logging.getLogger().setLevel(logging.WARNING)
for name in ("tigeropen", "core.tiger_client", "execution.order_manager"):
    logging.getLogger(name).setLevel(logging.WARNING)


def _plan(positions: dict, net_liq: float, keep: set) -> list:
    rows = []
    for symbol, p in sorted(positions.items()):
        qty = int(p.get("quantity") or 0)
        if qty == 0 or symbol.upper() in keep:
            continue
        market_value = float(p.get("market_value") or 0)
        avg_cost = float(p.get("avg_cost") or 0)
        # Broker-reported market value is the most reliable reference when the
        # market is closed and there is no live quote to ask for.
        ref = market_value / qty if qty else avg_cost
        rows.append({
            "symbol": symbol.upper(),
            "quantity": qty,
            "ref_price": ref,
            "market_value": market_value,
            "avg_cost": avg_cost,
            "unrealized": float(p.get("unrealized_pnl") or 0),
            "weight": market_value / net_liq if net_liq else 0.0,
        })
    rows.sort(key=lambda r: r["market_value"], reverse=True)
    return rows


def _print_plan(rows: list, summary: dict) -> None:
    print()
    print("=" * 78)
    print(" 清仓计划（模拟盘）")
    print("=" * 78)
    print(f" 环境        : {TRADING_ENV}")
    print(f" 净值        : ${summary.get('net_liquidation', 0):,.2f}")
    print(f" 现金        : ${summary.get('cash', 0):,.2f}")
    print(f" 待平仓      : {len(rows)} 只，合计 ${sum(r['market_value'] for r in rows):,.2f}")
    print("-" * 78)
    print(f" {'代码':<7}{'股数':>8}{'参考价':>11}{'市值':>15}{'占比':>8}{'浮动盈亏':>14}")
    print("-" * 78)
    for r in rows:
        print(f" {r['symbol']:<7}{r['quantity']:>8}{r['ref_price']:>11,.2f}"
              f"{r['market_value']:>15,.2f}{r['weight']:>7.1%}"
              f"{r['unrealized']:>14,.2f}")
    print("-" * 78)
    total_pnl = sum(r["unrealized"] for r in rows)
    print(f" 合计浮动盈亏: {total_pnl:>+14,.2f}")
    print("=" * 78)


def main() -> int:
    ap = argparse.ArgumentParser(description="Flatten all open positions")
    ap.add_argument("--execute", action="store_true",
                    help="actually submit sell orders (default: dry run)")
    ap.add_argument("--keep", default="", help="comma-separated symbols to leave open")
    ap.add_argument("--slippage", type=float, default=0.005,
                    help="sell limit sits this far below the reference price (default 0.5%%)")
    args = ap.parse_args()

    keep = {s.strip().upper() for s in args.keep.split(",") if s.strip()}

    from core.data_fetcher import DataFetcher
    from core.tiger_client import TigerClient
    from execution.order_manager import OrderManager

    tiger = TigerClient()
    tiger.connect()
    data = DataFetcher(tiger)

    summary = tiger.get_account_summary()
    positions = tiger.get_positions()
    rows = _plan(positions, float(summary.get("net_liquidation") or 0), keep)

    if not rows:
        print("\n账户已经是空仓，无需操作。")
        return 0

    _print_plan(rows, summary)

    if not args.execute:
        print("\n[DRY RUN] 未提交任何订单。确认无误后加 --execute 执行。")
        print("注意：必须在美股常规时段 09:30–16:00 ET 之间执行。")
        return 0

    overrides = dict(EXECUTION_CONFIG)
    overrides.update({
        # This is a one-off cleanup, not the strategy: no duplicate blocking and
        # a cap generous enough for every name in the book.
        "block_repeat_symbol_action_days": 0,
        "max_daily_trades": max(30, len(rows) * 3),
        "enforce_trading_window": True,
        "max_price_deviation_pct": 0.50,   # reference prices are stale on purpose
    })
    orders = OrderManager(tiger, config=overrides, price_provider=data.get_current_price)

    allowed, reason = orders.trading_window_ok()
    if not allowed:
        print(f"\n[REFUSED] 不在可交易时段：{reason}")
        print("盘前/盘后/周末提交的卖单会挂到下一个开盘价成交，风险不可控。")
        return 2

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n开始执行清仓 @ {now}\n")

    results = []
    for r in rows:
        symbol, qty = r["symbol"], r["quantity"]
        if qty < 0:
            results.append({**r, "status": "skipped",
                            "note": "short position — flatten manually"})
            print(f" ! {symbol:6s} 空头 {qty} 股，本脚本不处理，请手动回补")
            continue

        limit = round(r["ref_price"] * (1 - args.slippage), 2)
        submitted = orders.sell(symbol, qty, limit, exact_price=True)
        if not submitted:
            results.append({**r, "status": "rejected", "note": "order not submitted"})
            print(f" ✗ {symbol:6s} {qty:>6} 股 @ {limit:>9,.2f}  未提交（被规则拦截）")
            continue

        result = orders.wait_for_fill(submitted["order_id"])
        status = result.get("status")
        filled = int(float(result.get("filled_qty") or 0))
        avg = result.get("avg_price")

        if filled > 0 and status in ("filled", "partial_fill"):
            results.append({**r, "status": status, "filled": filled,
                            "fill_price": avg, "limit": limit})
            print(f" ✓ {symbol:6s} {filled:>6} 股 @ {float(avg or 0):>9,.2f}"
                  f"  ({status})")
        else:
            try:
                orders.cancel_order(submitted["order_id"])
            except Exception:
                pass
            results.append({**r, "status": "unfilled", "limit": limit})
            print(f" ✗ {symbol:6s} {qty:>6} 股 @ {limit:>9,.2f}  未成交（{status}），已撤单")

    # Re-read the book: never trust the local tally over the broker's.
    print("\n" + "-" * 78)
    after = tiger.get_positions()
    after_summary = tiger.get_account_summary()
    remaining = {s: int(p.get("quantity") or 0) for s, p in after.items()
                 if int(p.get("quantity") or 0) != 0}
    filled_n = sum(1 for r in results if r.get("status") in ("filled", "partial_fill"))
    print(f" 已成交 {filled_n}/{len(rows)} 笔")
    print(f" 净值 ${after_summary.get('net_liquidation', 0):,.2f} | "
          f"现金 ${after_summary.get('cash', 0):,.2f} | "
          f"购买力 ${after_summary.get('buying_power', 0):,.2f}")
    if remaining:
        print(f" 仍有持仓: {remaining}")
        print(" → 不要直接启动策略，先确认这些是未成交还是主动保留。")
        return 1
    print(" 账户已全部平仓，可以启动策略。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
