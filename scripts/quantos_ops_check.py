#!/usr/bin/env python3
"""
QuantOS 2.0 — 运维中心端到端抽检（T-302 的 DoD）
==============================================

DoD 原文：**页面显示的数字与进程内状态一致（抽检 5 项）**。

本脚本真正起一个 HTTP 服务，从 `/api/ops/snapshot` 取数，再和进程内的
Ledger / OMS / RiskState / Registry 逐项比对，打印对照表。不是"接口能返回
200 就算过"，而是每个数字都要有出处。

    .venv/bin/python scripts/quantos_ops_check.py
    .venv/bin/python scripts/quantos_ops_check.py --fault stale-market
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from quantos_core.contracts import D, OrderState, q  # noqa: E402
from quantos_core.ops_server import OpsHttpServer, OpsService  # noqa: E402
from quantos_ops_server import build_runtime  # noqa: E402


def fetch(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fault", default="", help="注入故障后再抽检")
    ap.add_argument("--port", type=int, default=0)
    args = ap.parse_args(argv)

    rt = build_runtime(fault=args.fault)
    service = OpsService(metrics=rt["metrics"], registry=rt["registry"],
                         prices_provider=lambda: rt["prices"], token="check")

    ledger, prices = rt["ledger"], rt["prices"]
    oms, risk_state, registry = rt["oms"], rt["risk_state"], rt["registry"]

    # ---- 进程内真值（直接问对象，不问接口） ---------------------------- #
    nav_true = ledger.nav(prices)
    gross_true = ledger.gross_exposure(prices)
    exposure_true = q(gross_true / nav_true * 100, 2) if nav_true else Decimal(0)
    dd_true = D(risk_state.drawdown_pct)
    strategies_true = len(list(registry))
    unknown_true = len([o for o in oms.registry.orders()
                        if o.state is OrderState.UNKNOWN])

    with OpsHttpServer(service, port=args.port, quiet=True) as srv:
        base = srv.url
        snap = fetch(base, "/api/ops/snapshot")
        strategies = fetch(base, "/api/ops/strategies")
        audit = fetch(base, "/api/ops/audit?limit=5")

    by_key = {m["key"]: m for m in snap["metrics"]}

    def num(key):
        m = by_key.get(key)
        return None if m is None else m.get("value")

    rows = [
        ("基金净资产 NAV", "portfolio.nav", num("portfolio.nav"),
         float(nav_true), "api.portfolio.nav vs ledger.nav()"),
        ("名义敞口 %", "portfolio.gross_exposure", num("portfolio.gross_exposure"),
         float(exposure_true), "api vs gross_exposure()/nav"),
        ("当前回撤 %", "risk.drawdown", num("risk.drawdown"),
         float(dd_true), "api vs RiskState.drawdown_pct"),
        ("策略实例数", "facts.strategies", len(snap["facts"].get("strategies") or []),
         strategies_true, "api.facts vs len(StrategyRegistry)"),
        ("待对账订单", "reconcile.pending", num("reconcile.pending"),
         unknown_true, "api vs OMS 中 UNKNOWN 订单数"),
        ("订单总数", "facts.all_orders", len(snap["facts"].get("all_orders") or []),
         len(oms.registry.orders()), "api.facts vs OMS 订单数"),
    ]

    print(f"抽取 {len(rows)} 项，比对「HTTP 接口」与「进程内对象」")
    print("-" * 78)
    print(f"{'指标':<16}{'接口值':>14}{'进程内真值':>14}  {'结果':<6}来源")
    print("-" * 78)
    bad = 0
    for label, key, got, want, source in rows:
        ok = False
        if isinstance(got, (int, float)) and isinstance(want, (int, float)):
            ok = abs(float(got) - float(want)) <= 0.01
        else:
            ok = got == want
        if not ok:
            bad += 1
        print(f"{label:<16}{str(got):>14}{str(want):>14}  "
              f"{'✅ 一致' if ok else '❌ 不一致':<6}{source}")

    print("-" * 78)

    # ---- 异常定位能力（B3 的 DoD 本体） -------------------------------- #
    if args.fault:
        causes = snap.get("root_causes") or []
        print(f"\n故障注入：{args.fault}")
        print(f"  overall = {snap.get('overall')}")
        print(f"  headline = {snap.get('headline')}")
        for c in causes[:4]:
            print(f"  · [{c['severity']}] {c['key']} ← {c['component']}"
                  f" | {c['detail'][:60]}")
        located = bool(causes) and snap.get("overall") in ("warn", "critical")
        if not located:
            bad += 1
            print("  ❌ 注入故障后没有定位到具体环节")
        else:
            print("  ✅ 能定位到具体环节（不是一句「系统异常」）")

    # ---- 只追加审计 ---------------------------------------------------- #
    print(f"\n审计：{audit['count']} 条，digest {audit['digest']}")
    newest = (audit.get("records") or [{}])[0]
    print(f"  最新：{newest.get('action')} {newest.get('strategy_id')} "
          f"{newest.get('result')} by {newest.get('actor')}")

    print("\n" + ("全部一致 ✅" if bad == 0 else f"{bad} 项不一致 ❌"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
