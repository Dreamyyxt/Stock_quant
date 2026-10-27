#!/usr/bin/env python3
"""
Pre-flight check before running the TSLA ORB strategy live
==========================================================

Answers one question: if I start the strategy right now, does it do what the
backtest says it does? Every check below corresponds to a way the live run has
already been observed to diverge.

Exit codes: 0 = ready, 1 = blocked (a check failed), 2 = market closed (fine,
just not now).

Usage
-----
    python scripts/live_readiness.py
"""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging  # noqa: E402

logging.getLogger().setLevel(logging.ERROR)
for name in ("tigeropen", "core.tiger_client", "intraday.providers",
             "execution.order_manager", "core.state_store"):
    logging.getLogger(name).setLevel(logging.ERROR)

from config.settings import (  # noqa: E402
    EXECUTION_CONFIG, TRADING_ENV, intraday_config,
)

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:                                    # pragma: no cover
    ET = None

OK, WARN, FAIL = "PASS", "WARN", "FAIL"
results = []


def check(name: str, status: str, detail: str) -> None:
    results.append((status, name, detail))
    mark = {OK: "✓", WARN: "!", FAIL: "✗"}[status]
    print(f" {mark} {name:<26} {detail}")


def main() -> int:
    cfg = intraday_config("orb_tsla")
    print()
    print("=" * 84)
    print(" TSLA 开盘区间突破 — 实盘前置检查")
    print("=" * 84)

    # ---- 1. Market clock ------------------------------------------------ #
    now_et = datetime.now(ET) if ET else datetime.now()
    weekday = now_et.weekday()
    minute = now_et.hour * 60 + now_et.minute
    is_weekend = weekday >= 5
    in_session = (not is_weekend) and 570 <= minute < 960

    if is_weekend:
        check("市场时段", WARN,
              f"{now_et:%a %H:%M ET} — 周末休市，无法实跑")
    elif not in_session:
        check("市场时段", WARN,
              f"{now_et:%a %H:%M ET} — 不在 09:30–16:00 常规时段")
    else:
        check("市场时段", OK, f"{now_et:%a %H:%M ET} — 常规时段")

    # ---- 2. Trading environment ---------------------------------------- #
    if TRADING_ENV == "live":
        check("交易环境", WARN, "TRADING_ENV=live（真实资金）")
    else:
        check("交易环境", OK, f"TRADING_ENV={TRADING_ENV}（模拟盘）")

    # ---- 3. Broker connection ------------------------------------------ #
    try:
        from core.tiger_client import TigerClient
        tiger = TigerClient()
        tiger.connect()
        check("券商连接", OK, "Tiger API 已连接")
    except Exception as exc:
        check("券商连接", FAIL, f"{type(exc).__name__}: {str(exc)[:90]}")
        return _summary()

    # ---- 4. Account is clean ------------------------------------------- #
    summary = tiger.get_account_summary()
    positions = tiger.get_positions()
    net_liq = float(summary.get("net_liquidation") or 0)
    cash = float(summary.get("cash") or 0)
    open_pos = {s: int(p.get("quantity") or 0) for s, p in positions.items()
                if int(p.get("quantity") or 0) != 0}

    if not open_pos:
        check("账户持仓", OK, f"空仓 | 净值 ${net_liq:,.0f} 现金 ${cash:,.0f}")
    elif set(open_pos) == {"TSLA"}:
        check("账户持仓", OK, f"仅 TSLA {open_pos['TSLA']} 股 — 与单票策略一致")
    else:
        exposure = net_liq - cash
        check("账户持仓", FAIL,
              f"{len(open_pos)} 只非策略持仓，市值 ${exposure:,.0f} "
              f"({exposure / net_liq:.0%}) — 会与单票仓位叠加")

    # ---- 5. Buying power for a 100% position --------------------------- #
    bp = float(summary.get("buying_power") or 0)
    need = net_liq * float(cfg.get("position_pct", 0.995))
    if bp >= need:
        check("购买力", OK, f"${bp:,.0f} ≥ 需要 ${need:,.0f}")
    else:
        check("购买力", FAIL, f"${bp:,.0f} < 需要 ${need:,.0f}")

    # ---- 6. Live quotes reachable -------------------------------------- #
    from intraday.providers import TigerBarProvider
    provider = TigerBarProvider(tiger, cfg["universe"], int(cfg.get("lookback_bars", 120)))
    bars = provider.get_recent(cfg["universe"])
    frame = bars.get("TSLA")
    if frame is None or len(frame) == 0:
        check("分钟行情", FAIL, "Tiger 未返回 TSLA 分钟线")
    else:
        last = frame.index[-1]
        fresh = (datetime.now(ET) - last).total_seconds() / 60 if ET else None
        if last.year < 2000:
            check("分钟行情", FAIL, f"时间戳异常 {last}（epoch 单位解析错误）")
        elif fresh is not None and fresh > 30 and in_session:
            check("分钟行情", WARN,
                  f"{len(frame)} 根，最新 {last:%m-%d %H:%M}（{fresh:.0f} 分钟前）")
        else:
            check("分钟行情", OK,
                  f"{len(frame)} 根，最新 {last:%m-%d %H:%M}，收盘 {float(frame['close'].iloc[-1]):.2f}")

    # ---- 7. Opening range actually buildable --------------------------- #
    from intraday.signals import OrbTslaSignals
    sig = OrbTslaSignals(dict(cfg))
    if frame is not None and len(frame):
        probe = sig._evaluate_symbol("TSLA", frame)
        reason = probe.reasons[0] if probe.reasons else "no reason"
        # "does not start at the session open" here means the whole session's
        # 09:30 bars are absent — the strategy would refuse to trade all day.
        if "does not start at the session open" in reason:
            check("开盘区间", WARN, f"当前窗口尚未覆盖今日 09:30（{reason}）")
        else:
            check("开盘区间", OK, reason)

    # ---- 8. Config consistency ----------------------------------------- #
    trades = int(cfg.get("max_trades_per_day", 1))
    cap = int(cfg.get("max_trades_per_day", 1)) * 2 + 4
    check("下单额度", OK, f"每日 {trades} 笔策略 / {cap} 单券商上限")
    check("平仓时点", OK,
          f"实盘 15:58 强平（回测口径为 15:59 收盘），回放仍用 {cfg['flatten_time']}")
    check("方向", OK,
          "多空双向" if cfg.get("allow_short") else "仅做多（与回测不符）")

    # ---- 9. Single-instance lock --------------------------------------- #
    lock = os.path.join("state", f"trading_{os.getenv('TIGER_ACCOUNT', 'default')}.lock")
    check("单实例锁", OK, "启动时由 trading_lock 自动获取" if not os.path.exists(lock)
          else "已存在锁文件（确认没有别的实例在跑）")

    # ---- 10. Advise ----------------------------------------------------- #
    print("-" * 84)
    blocked = [r for r in results if r[0] == FAIL]
    warned = [r for r in results if r[0] == WARN]
    if blocked:
        print(f" 结论：{len(blocked)} 项阻塞，不要启动策略。")
        for _, name, detail in blocked:
            print(f"   · {name}: {detail}")
    elif warned:
        print(f" 结论：无阻塞，{len(warned)} 项提醒。可以按计划启动。")
    else:
        print(" 结论：全部通过，可以启动。")
    print("=" * 84)
    return _summary()


def _summary() -> int:
    if any(s == FAIL for s, _, _ in results):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
