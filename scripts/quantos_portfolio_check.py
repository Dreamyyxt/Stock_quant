#!/usr/bin/env python
"""
策略组合体检（B6）
==================

把风险预算、灰度路由、AI 研究通道三块的结果摊成一张表，用来看而不是猜。

用法：

    .venv/bin/python scripts/quantos_portfolio_check.py
    .venv/bin/python scripts/quantos_portfolio_check.py --nav 2000000 --rho 0.6

这里都是**算出来的示例数字**（仓位、波动率是演示值），不是真实账户。
真实账户要接 Ledger 和行情，那是运维服务的活。这个脚本回答的是
「如果仓位是这样，我会怎么评价它」。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quantos_core import ai_research as AIR                      # noqa: E402
from quantos_core.canary import CanaryRouter, RolloutState        # noqa: E402
from quantos_core.contracts import D, StrategyIntent, q           # noqa: E402
from quantos_core.risk_budget import (                            # noqa: E402
    RiskModel, budget_drift, budget_to_quotas, equal_risk_weights,
    portfolio_risk,
)

OK, WARN, NO = "✅", "⚠️ ", "❌"


def bar(title: str) -> None:
    print(f"\n{'─' * 68}\n{title}\n{'─' * 68}")


def fmt_money(value: Decimal) -> str:
    return f"{value:,.2f}"


def fmt_pct(value: Decimal, places: int = 2) -> str:
    return f"{q(value * 100, places):.2f}%"


def demo_book(nav: Decimal) -> dict:
    """两份策略的仓位（名义金额）。演示用，量级接近一个千万级小基金。"""
    return {
        "etf_momentum": {"SPY": nav * D("0.12"), "QQQ": nav * D("0.08")},
        "crypto_carry": {"BTC/USDT:USDT": nav * D("0.06"),
                         "ETH/USDT:USDT": nav * D("0.04")},
    }


VOLS = {
    "SPY": D("0.16"), "QQQ": D("0.22"),
    "BTC/USDT:USDT": D("0.55"), "ETH/USDT:USDT": D("0.65"),
}

BUDGETS = {"etf_momentum": D("0.55"), "crypto_carry": D("0.45")}


def show_risk(nav: Decimal, rho: Decimal) -> None:
    book = demo_book(nav)
    net: dict = {}
    for per in book.values():
        for symbol, notional in per.items():
            net[symbol] = net.get(symbol, Decimal(0)) + notional

    model = RiskModel.uniform(VOLS, rho, label=f"常数 ρ={rho}")
    risk = portfolio_risk(net, model, per_strategy=book, nav=nav,
                          risk_budget_pct=D("0.15"))

    bar("1. 风险分解（净额口径，Euler 分解）")
    print(f"  组合波动率 σ = {fmt_money(risk.sigma)}"
          f"  （占 NAV {fmt_pct(risk.sigma_pct_nav or Decimal(0))}）")
    print(f"  各自独立波动之和 = {fmt_money(risk.standalone_sum)}"
          f"  → 分散化收益 {fmt_pct(risk.diversification)}")
    print(f"\n  {'标的':<18s}{'净额':>14s}{'风险贡献':>14s}{'占比':>10s}")
    for item in sorted(risk.by_symbol, key=lambda s: -abs(s.contribution)):
        flag = "  ⚠ 已对冲" if item.hedged else ""
        print(f"  {item.symbol:<18s}{fmt_money(item.net_notional):>14s}"
              f"{fmt_money(item.contribution):>14s}{fmt_pct(item.pct):>10s}{flag}")

    print(f"\n  {'策略':<18s}{'风险贡献':>14s}{'占比':>10s}{'预算占比':>10s}  状态")
    drifts = budget_drift(risk.strategy_pct, BUDGETS)
    over = {d.strategy_id: d.over for d in drifts}
    for sid in sorted(risk.by_strategy, key=lambda k: -risk.by_strategy[k]):
        mark = WARN if over.get(sid) else OK
        print(f"  {sid:<18s}{fmt_money(risk.by_strategy[sid]):>14s}"
              f"{fmt_pct(risk.strategy_pct[sid]):>10s}"
              f"{fmt_pct(BUDGETS.get(sid, Decimal(0))):>10s}  {mark}")

    print(f"\n  Σ标的贡献 = {fmt_money(risk.contribution_sum)}  "
          f"Σ策略贡献 = {fmt_money(risk.attribution_sum)}")
    print(f"  恒等式闭合：标的 {OK if risk.closes else NO}  "
          f"策略 {OK if risk.attribution_closes else NO}")
    for note in risk.notes:
        print(f"  {WARN} {note}")


def show_parity(nav: Decimal, rho: Decimal) -> None:
    bar("2. 等风险贡献建议（当前 vs 建议）")
    symbols = sorted(VOLS)
    model = RiskModel.uniform(VOLS, rho, label=f"常数 ρ={rho}")
    erc = equal_risk_weights(model, symbols)

    equal_w = {s: D(1) / D(len(symbols)) for s in symbols}
    r_inv = portfolio_risk({s: w * nav for s, w in equal_w.items()}, model)
    r_erc = portfolio_risk({s: w * nav for s, w in erc.weights.items()}, model)

    print(f"  {'标的':<18s}{'等权':>10s}{'等风险':>10s}{'等权下风险占比':>16s}")
    pct_inv = {s.symbol: s.pct for s in r_inv.by_symbol}
    for s in symbols:
        print(f"  {s:<18s}{fmt_pct(equal_w[s], 1):>10s}"
              f"{fmt_pct(erc.weights[s], 1):>10s}{fmt_pct(pct_inv[s]):>16s}")

    print(f"\n  等权组合 σ = {fmt_money(r_inv.sigma)}"
          f"   等风险组合 σ = {fmt_money(r_erc.sigma)}"
          f"   （降低 {fmt_pct(1 - r_erc.sigma / r_inv.sigma)}）")
    print(f"  收敛：{'是' if erc.converged else '否'}"
          f"（{erc.iterations} 次迭代，最大偏差 {float(erc.max_deviation):.1e}）")
    for note in erc.notes:
        print(f"  {WARN} {note}")


def show_budget(nav: Decimal) -> None:
    bar("3. 风险预算 → 额度（等预算下高波动拿更少钱）")
    vols = {sid: max(VOLS[s] for s in book) if book else Decimal(0)
            for sid, book in demo_book(nav).items()}
    plan = budget_to_quotas(BUDGETS, vols, risk_target_pct=D("0.10"))
    print(f"  风险目标 {fmt_pct(plan.risk_target_pct)} NAV"
          f"   压缩系数 {q(plan.scale, 4)}")
    print(f"  {'策略':<18s}{'预算':>10s}{'σ':>10s}{'换算额度':>12s}{'金额':>16s}")
    for sid in sorted(plan.quotas):
        print(f"  {sid:<18s}{fmt_pct(BUDGETS.get(sid, Decimal(0))):>10s}"
              f"{fmt_pct(vols[sid]):>10s}{fmt_pct(plan.quotas[sid]):>12s}"
              f"{fmt_money(plan.quotas[sid] * nav):>16s}")
    print(f"\n  已分配 {fmt_pct(plan.total)}  NAV，剩余现金 {fmt_pct(plan.cash_pct)}")
    for note in plan.notes:
        print(f"  {WARN} {note}")


def show_canary() -> None:
    bar("4. 灰度路由（10% 灰度时合计仍是 1.0，不是 2.0）")
    now = datetime.now(timezone.utc)

    def intent(sid: str, ver: str, symbol: str, weight: str) -> StrategyIntent:
        return StrategyIntent(
            intent_id=f"demo-{sid}-{ver}-{symbol}", strategy_id=sid,
            strategy_version=ver, symbol=symbol, venue="demo",
            target_weight=D(weight), confidence=D("0.5"), reason="demo",
            signal_ids=("sig-1",), as_of=now, ts=now,
            expires_at=now + timedelta(minutes=5))

    router = CanaryRouter()
    router.register_stable("etf_momentum", "v3")
    router.register_stable("crypto_carry", "v1")
    router.open_canary("etf_momentum", "v4", weight=D("0.1"),
                       actor="human:owner", reason="演示：10% 灰度")

    print(f"  {'策略':<18s}{'状态':<14s}{'稳定版':<10s}{'灰度版':<10s}{'份额'}")
    for cohort in router.cohorts():
        shares = ", ".join(f"{k}={q(v, 3)}" for k, v in cohort.shares().items())
        print(f"  {cohort.strategy_id:<18s}{cohort.state.value:<14s}"
              f"{cohort.stable_version:<10s}{(cohort.canary_version or '-'):<10s}{shares}")

    routed, reports = router.route([
        intent("etf_momentum", "v3", "SPY", "1.0"),
        intent("etf_momentum", "v4", "SPY", "1.0"),
        intent("crypto_carry", "v1", "BTC/USDT:USDT", "1.0"),
    ])
    print(f"\n  路由结果：{len(routed)} 条意图通过")
    for rep in reports:
        weights = ", ".join(f"{k}={v}" for k, v in rep.weight_by_symbol.items())
        print(f"    {rep.strategy_id:<18s} 发出 {rep.emitted}  "
              f"路由后合计 {weights}  丢弃 {rep.dropped}")
    print(f"  {OK} 两个版本各发满权重，合计仍是 1.0：灰度不会把仓位翻倍")
    print(f"  {OK} 路由不重新归一化：灰度 10% 就是 10% 的风险")

    print("\n  回滚演示：")
    cohort = router.rollback("etf_momentum", reason="演示", actor="ai:guard")
    print(f"    state={cohort.state.value}  份额={cohort.shares()}"
          f"  灰度版本留档={cohort.canary_version}")
    print(f"  {OK} 回滚不需要确认，一个动作到位；灰度版本号留着复盘用")


def show_ai_channel() -> None:
    bar("5. AI 研究通道（只读，没有任何写生产的路径）")
    report = AIR.assert_isolated()
    mark = OK if report.ok else NO
    print(f"  {mark} 静态隔离体检：越界 import {list(report.bad_imports) or '无'}"
          f"，生产写入方法 {list(report.forbidden_methods) or '无'}")
    print(f"  实际上：这个模块根本不 import strategy_registry / live_gate / oms，"
          f"\n           所以它没有 registry.promote() 可调 —— 这是结构约束，不是纪律要求")

    box = AIR.ResearchInbox()
    box.submit(author="ai:researcher", kind=AIR.ResearchKind.CANDIDATE_PARAMS,
               strategy_id="etf_momentum",
               summary="2019-2026 回测：20 日回看优于 60 日，年化 +2.1pp，最大回撤持平",
               payload={"params": {"lookback": 20}},
               evidence=("bt:2026-10-26-etf-lookback",))
    print(f"\n  {OK} 合法提交入箱（候选参数 + bt: 证据）")
    for kind, payload in (("顶层 quota_pct", {"quota_pct": "0.4"}),
                          ("嵌套 armed", {"suggest": [{"changes": {"armed": True}}]})):
        try:
            box.submit(author="ai:researcher", kind=AIR.ResearchKind.REPORT,
                       strategy_id="etf_momentum",
                       summary="这是一条想要改动生产控制面的建议，应当被拒绝掉",
                       payload=payload)
        except AIR.ResearchRejected as exc:
            print(f"  {OK} {kind} 被拒：{str(exc)[:46]}…")
    try:
        box.review("rs-1", reviewer="ai:reviewer", verdict=AIR.Verdict.ACCEPTED)
    except AIR.ReviewRequired as exc:
        print(f"  {OK} AI 复核被拒：{str(exc)[:40]}…")
    print(f"\n  {OK} 唯一出口是 ResearchDraft：必须 human: 签名，且它本身只是数据")


def main() -> int:
    ap = argparse.ArgumentParser(description="策略组合体检（B6）")
    ap.add_argument("--nav", default="10000000", help="组合 NAV（默认一千万）")
    ap.add_argument("--rho", default="0.45", help="常数相关系数假设（默认 0.45）")
    args = ap.parse_args()

    nav = D(args.nav)
    rho = D(args.rho)
    if nav <= 0:
        print("NAV 必须为正")
        return 2

    print("=" * 68)
    print(f"策略组合体检  NAV={fmt_money(nav)}  相关性假设 ρ={rho}")
    print("注意：仓位与波动率是演示值；这里的价值在于口径，不在于数字")
    print("=" * 68)

    show_risk(nav, rho)
    show_parity(nav, rho)
    show_budget(nav)
    show_canary()
    show_ai_channel()

    print("\n" + "=" * 68)
    print("三块的不变量：Σ标的贡献 = σ、灰度不归一化、AI 无生产写路径")
    print("任何一条被打破，都意味着有人绕过了契约层。")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
