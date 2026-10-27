#!/usr/bin/env python3
"""
QuantOS 2.0 — Crypto 接入体检
==============================

一条命令回答四个问题：

    1. 哪些交易所现在就能自动交易？不能的是卡在哪一条？
    2. 现在这个时刻，哪几条 lane 在跑？（美股休市不影响币）
    3. 今天还有几次资金费结算？
    4. 一笔 0.0012345 BTC 的单，实际会被下成什么样？

    .venv/bin/python scripts/quantos_crypto_check.py
    .venv/bin/python scripts/quantos_crypto_check.py --venue okx --qty 0.05

不需要装 ccxt，不联网 —— 这里展示的是"系统认为可以做什么"，
真下单要等凭据接上、probe 实测过能力矩阵之后。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from quantos_core.contracts import D, Side  # noqa: E402
from quantos_core.funding import day_boundaries, next_settlement, window_of  # noqa: E402
from quantos_core.scheduler import (  # noqa: E402
    CryptoSession, Lane, LaneState, Scheduler, nyse_session,
)
from quantos_core.venues import (  # noqa: E402
    VENUE_PROFILES, CapabilityError, admit, profile,
)

UTC = timezone.utc
OK = "✅"
NO = "❌"
WARN = "⚠️"


def _bar(title: str) -> None:
    print(f"\n{title}")
    print("─" * 68)


def show_admissions(max_notional: Decimal) -> None:
    _bar(f"1. 场所准入（单笔上限 {max_notional} USDT）")
    for venue, caps in sorted(VENUE_PROFILES.items()):
        verdict = admit(caps, max_order_notional=max_notional)
        if not verdict.allowed:
            # 结构上就不该接自动交易，红灯
            mark, label = NO, "禁止自动交易"
        elif not caps.verified:
            # 声明值通过了，但一条都没实测 —— 这里绝不能打绿灯。
            # 运维看到绿灯就会以为可以上，而实际上参数是抄来的。
            mark, label = WARN, "结构准入通过，能力未实测"
        else:
            mark, label = OK, "可自动交易（能力已实测）"
        print(f"  {mark} {venue:<14s} {label}")
        for blocker in verdict.blockers:
            print(f"       阻断：{blocker}")
        for warning in verdict.warnings:
            if "未实测" in warning:
                continue          # 已经在标题里体现了，不重复刷屏
            print(f"       注意：{warning}")

    print("\n  说明：标 ⚠️ 的场所**现在还不能下真钱单**，必须先跑 probe() 用真实响应")
    print("       覆盖步长/精度/最小额，再实测「超时后能否按单号查回来」。")
    print("       把文档当参数用，是这类系统最常见的事故起点。")
    try:
        caps = profile("binanceusdm")
        sample = caps.validate_order("BTC/USDT:USDT", Side.BUY,
                                     D("0.0012345"), D("30123.456"),
                                     max_order_notional=max_notional)
        print(f"       例：binanceusdm 收 0.0012345 BTC @30123.456 → "
              f"{sample.qty} @ {sample.price}")
    except CapabilityError as exc:
        print(f"       例：样本单被拒 —— {exc}")


def show_lanes(now: datetime) -> None:
    _bar(f"2. 调度状态（{now.strftime('%Y-%m-%d %H:%M')} UTC）")

    equity = nyse_session()
    crypto = CryptoSession()
    sched = Scheduler([
        Lane("equity", equity, lambda t: None, 60),
        Lane("crypto", crypto, lambda t: None, 15),
    ])
    snapshot = sched.snapshot(now)
    for lane in snapshot["lanes"]:
        mark = OK if lane["open"] else "·"
        print(f"  {mark} {lane['name']:<8s} {lane['venue_class']:<7s} "
              f"每 {lane['interval_sec']:.0f}s  {lane['detail']}")
    print(f"\n  当前在跑：{', '.join(snapshot['open_lanes']) or '（全部休市）'}")
    print("  隔离保证：一条 lane 抛异常不会中断另一条（Scheduler.tick 逐条兜底）")


def show_funding(now: datetime) -> None:
    _bar("3. 资金费结算（8 小时周期，UTC）")
    win = window_of(now, 8)
    stamps = day_boundaries(now.date(), 8)
    nxt = next_settlement(now, 8)
    print(f"  当前窗口：{win.start.strftime('%m-%d %H:%M')} → "
          f"{win.end.strftime('%m-%d %H:%M')} UTC，已过 {win.progress(now) * 100:.0f}%")
    print(f"  下次结算：{nxt.strftime('%m-%d %H:%M')} UTC"
          f"（{(nxt - now).total_seconds() / 3600:.1f} 小时后）")
    print(f"  今日结算：{', '.join(s.strftime('%H:%M') for s in stamps)} UTC")
    print("\n  符号口径：多头 × 正费率 = 账户支出（账本记正数）")
    print("  计提口径：盘中计提只进展示，**不进账本**；账本只记已结算，按结算边界去重")


def show_order(caps_venue: str, symbol: str, qty: str, price: str,
               side: Side, max_notional: Decimal) -> None:
    _bar(f"4. 下单量化（{caps_venue} · {symbol}）")
    try:
        caps = profile(caps_venue)
    except CapabilityError as exc:
        print(f"  {NO} {exc}")
        return
    verdict = admit(caps, max_order_notional=max_notional)
    if not verdict.allowed:
        print(f"  {NO} 该场所不允许自动交易：")
        for blocker in verdict.blockers:
            print(f"       {blocker}")
        return
    try:
        check = caps.validate_order(symbol, side, D(qty), D(price),
                                    max_order_notional=max_notional)
    except CapabilityError as exc:
        print(f"  {NO} 这笔单会被本地拦下：{exc}")
        return

    print(f"  意图：{side.value} {qty} @ {price}")
    print(f"  实际：{side.value} {check.qty} @ {check.price}"
          f"   名义 {check.notional:.2f} USDT")
    for note in check.notes:
        print(f"       · {note}")
    if not check.notes:
        print("       · 无需取整")


def main() -> int:
    ap = argparse.ArgumentParser(description="QuantOS Crypto 接入体检")
    ap.add_argument("--venue", default="binanceusdm", help="展示哪家场所的下单量化")
    ap.add_argument("--symbol", default="BTC/USDT:USDT")
    ap.add_argument("--qty", default="0.0012345")
    ap.add_argument("--price", default="30123.456")
    ap.add_argument("--side", default="buy", choices=["buy", "sell"])
    ap.add_argument("--max-notional", default="5000",
                    help="单笔上限（USDT），与实盘开关第三级同口径")
    ap.add_argument("--at", default="", help="指定时刻（ISO8601，缺省用当前时间）")
    args = ap.parse_args()

    max_notional = D(args.max_notional)
    if args.at:
        now = datetime.fromisoformat(args.at.replace("Z", "+00:00"))
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
    else:
        now = datetime.now(UTC)

    print("QuantOS 2.0 · Crypto 接入体检")
    print("=" * 68)
    print(f"时间基准：{now.isoformat()}")

    show_admissions(max_notional)
    show_lanes(now)
    show_funding(now)
    show_order(args.venue, args.symbol, args.qty, args.price,
               Side.BUY if args.side == "buy" else Side.SELL, max_notional)

    print("\n" + "=" * 68)
    print("这份报告只说明「系统认为可以做什么」。真下单还差三件事：")
    print("  1. 装 ccxt + 配凭据（build_ccxt_transport）")
    print("  2. 跑 probe() 把声明值换成实测值（尤其 lag 和按单号回查）")
    print("  3. 过 B4 三级实盘开关 —— Crypto 一样要过，没有例外")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
