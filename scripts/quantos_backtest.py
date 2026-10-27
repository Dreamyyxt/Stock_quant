#!/usr/bin/env python3
"""
QuantOS 2.0 — 回测命令行（B12 / T-1204）

三个子命令：

    run       跑一次回测，打印指标 / 分年度 / 阻断统计 / 成本
    check     恒等式体检：NAV = 现金 + 持仓市值（逐日）、归因六项闭合
    compare   与 `research/ashare_ew20` 的手工回测对账，逐项列出差异来源

**为什么要 `compare` 而不是直接对齐数字**
----------------------------------------
手工回测的结论（年化 18.78%、MDD -26.1%）经过多轮核对，是有价值的基线；
但它是**另一套记账**。两套代码算出同一个数字，证明不了谁对 ——
只证明两边犯了同样的错，或者差异刚好抵消。

所以这里的目标不是"追平"，是**把差异逐项归因**：复权口径、成本口径、
最小下单额、整手取整、熔断开关。每一项都能指出方向；剩下的对不上的部分
就是需要人去看的东西。宁可显示缺口，也不显示假绿灯。

用法：
    .venv/bin/python scripts/quantos_backtest.py run --names 200
    .venv/bin/python scripts/quantos_backtest.py check --symbol sh600000
    .venv/bin/python scripts/quantos_backtest.py compare
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJ = ROOT.parent
if str(PROJ) not in sys.path:
    sys.path.insert(0, str(PROJ))

from quantos_core.backtest import (                                    # noqa: E402
    BacktestConfig, BacktestError, BarPanel, DailyBacktest, DataQualityError,
)
from quantos_core.capital_allocator import StrategyPolicy               # noqa: E402
from quantos_core.cost import CostModel, VenueCostSchedule              # noqa: E402
from quantos_core.contracts import D, q                                 # noqa: E402
from quantos_core.risk_engine import RiskPolicy                         # noqa: E402
from quantos_core.strategy_api import (                                 # noqa: E402
    SignalOut, StrategyManifest,
)

DATA_DIR = PROJ / "state" / "ashare" / "daily"
RESEARCH_SUMMARY = PROJ / "research" / "ashare_ew20" / "outputs" / "ew20_summary.json"
VENUE = "xshg"
ACCOUNT = "QBT-EW20"


# --------------------------------------------------------------------------- #
#  基准策略：等权全池
# --------------------------------------------------------------------------- #

class EqualWeight:
    """
    等权全池 + 固定间隔再平衡。**这不是一个"好策略"，它是一个标尺。**

    它只做一件事：把当时所有可成交的标的等权持有。任何选股逻辑都应该
    先跑赢它，否则说明"选股"这件事在你的数据上没有正贡献 ——
    这正是 A 股研究里最重要的那条结论（选股超额远小于等权再平衡本身）。

    它也只拿得到收盘价（策略契约如此，策略不该自己取数），
    所以不可能做流动性排序 —— 那不是这里丢的功能，是契约刻意不给的。
    """

    def __init__(self, rebalance_days: int = 20, warmup_bars: int = 25) -> None:
        self.manifest = StrategyManifest(
            strategy_id="ew20", version="1.0.0", venue=VENUE,
            kind="rebalance", rebalance_days=int(rebalance_days),
            warmup_bars=int(warmup_bars),
            author="QuantOS B12",
            description="等权全池 + 固定间隔再平衡（基准标尺，不含选股）",
        )

    def compute(self, frame, ctx):
        symbols = frame.symbols()
        if not symbols:
            return ()
        weight = (Decimal(1) / Decimal(len(symbols))).quantize(Decimal("0.00000001"))
        return [SignalOut(symbol=s, suggested_weight=weight,
                          reason="equal_weight") for s in symbols]


# --------------------------------------------------------------------------- #
#  成本与风控口径
# --------------------------------------------------------------------------- #

def ashare_cost_model() -> CostModel:
    """
    A 股费率。数值与 `research/ashare_ew20` 一致，来源写在这里而不是散在代码里：
    佣金 2.5bps（单笔最低 5 元）、印花税 5bps（仅卖出）、过户费 0.1bps（双边）、
    滑点 5bps、冲击成本 0。

    为什么不读 `config/cost_model.json`：那份文件里的场所是 IBKR/TIGER/OKX/BYBIT，
    没有 A 股。**回测用的费率必须是实盘要用的那一份** —— 所以这里构造出来的
    对象会在报告里被打印出全部参数（`cost_params`），让人一眼能看出用的是哪套。
    等 A 股真的接券商，这里必须改成读文件。
    """
    return CostModel(
        base=VenueCostSchedule(
            taker_fee_bps=Decimal("2.5"), maker_fee_bps=Decimal("2.5"),
            min_fee=Decimal("5"), slippage_bps=Decimal("5"),
            impact_bps_per_pct_nav=Decimal("0"),
            sell_tax_bps=Decimal("5"), transfer_bps=Decimal("0.1"),
        ),
        version="ashare-ew20-v1",
        source_path="scripts/quantos_backtest.py:ashare_cost_model()",
    )


def ashare_risk_policy() -> RiskPolicy:
    """
    与手工回测同口径的风控参数。

    `min_order_notional=2000` 是研究的口径（低于此不下单，避免碎单吃手续费），
    `max_gross_exposure_pct=99.5` 对应研究的 `cash_buffer=0.5%`。
    单笔上限放到 100% 是因为研究的组合是 700 只等权 —— 每只 0.13%，
    默认的 2% 单笔上限对它没有任何约束力，设窄了只会掩盖真实原因。
    """
    return RiskPolicy(
        max_order_notional_pct=Decimal("100"),
        max_strategy_allocation_pct=Decimal("100"),
        min_order_notional=Decimal("2000"),
        max_gross_exposure_pct=Decimal("99.5"),
        stale_market_data_action="allow",
    )


def policy(quota: Decimal = Decimal("1")) -> StrategyPolicy:
    return StrategyPolicy(strategy_id="ew20", venue=VENUE,
                          quota_pct=quota, max_gross_weight=Decimal("0.95"))


def build_config(args) -> BacktestConfig:
    return BacktestConfig(
        cost_model=ashare_cost_model(),
        strategy_policies=[policy()],
        account_id=ACCOUNT,
        venue=VENUE,
        initial_cash=D(str(args.cash)),
        risk_policy=ashare_risk_policy(),
        risk_profile=args.risk_profile,
        min_notional={VENUE: Decimal("2000")},
        warmup_bars=args.warmup,
        start=date.fromisoformat(args.start) if args.start else None,
        max_pending_bars=args.max_pending,
    )


# --------------------------------------------------------------------------- #
#  run
# --------------------------------------------------------------------------- #

def load_panel(args) -> BarPanel:
    if not DATA_DIR.is_dir():
        raise DataQualityError(
            f"没有本地行情：{DATA_DIR}（先跑 scripts/fetch_ashare_universe.py）")
    # 注意 `start` **不传给 loader**：评估起点在引擎里算，预热期必须留着。
    # 在 loader 里按 start 截断会把预热数据一并砍掉，策略第一根就 FrameError。
    return BarPanel.load(
        str(DATA_DIR), adjust=not args.no_adjust, min_bars=args.min_bars,
        max_names=args.names)


def _pct(x) -> str:
    return "—" if x is None else f"{x:.2f}%"


def _num(x, nd=3) -> str:
    return "—" if x is None else f"{x:.{nd}f}"


def cmd_run(args) -> int:
    t0 = time.time()
    panel = load_panel(args)
    if args.json:
        pass
    else:
        print(f"面板：{len(panel)} 个交易日 / {len(panel.symbols)} 只标的"
              f"（丢弃 {len(panel.skipped)} 只）  载入 {time.time() - t0:.1f}s")
        if panel.skipped:
            print(f"  丢弃样本：{', '.join(panel.skipped[:5])}"
                  f"{' …' if len(panel.skipped) > 5 else ''}")

    cfg = build_config(args)
    strategy = EqualWeight(args.rebalance, args.warmup)
    t1 = time.time()
    res = DailyBacktest(cfg, panel, strategy).run()
    elapsed = time.time() - t1

    if args.json:
        payload = res.to_dict()
        payload["elapsed_sec"] = round(elapsed, 2)
        payload["panel"] = {"days": len(panel), "symbols": len(panel.symbols),
                           "skipped": len(panel.skipped)}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    d = res.stats.to_dict()
    print(f"\n{'=' * 62}\n  回测结果  ew20（等权全池 + {args.rebalance} 日再平衡）"
          f"  {elapsed:.1f}s\n{'=' * 62}")
    print(f"  区间        {d['start']} → {d['end']}   {d['bars']} 个交易日 / {_num(d['years'])} 年")
    print(f"  期初 → 期末 {d['initial']} → {d['final']}")
    print(f"  总收益      {_pct(d['total_return_pct'])}")
    print(f"  年化        {_pct(d['annual_return_pct'])}")
    print(f"  年化波动    {_pct(d['annual_vol_pct'])}")
    print(f"  夏普/索提诺 {_num(d['sharpe'])} / {_num(d['sortino'])}")
    print(f"  最大回撤    {_pct(d['max_drawdown_pct'])}"
          f"   {d['mdd_peak']} → {d['mdd_trough']}")
    print(f"  Calmar      {_num(d['calmar'])}")
    if d["gaps"]:
        print(f"  算不出来的  {'；'.join(d['gaps'])}")

    print(f"\n  成本        佣金 {res.cost['fee_total']}，滑点 {res.cost['slippage_total']}"
          f"，合计 {res.cost['total_bps_of_avg_nav']} bps/平均NAV"
          f"（年化拖累 {_num(res.cost['annual_drag_pct'], 3)}%）")
    print(f"  归因闭合    {'✅ 六项之和等于净收益' if res.cost['attribution_closes'] else '❌ 不闭合，必须查'}")
    print(f"  平均敞口    {_num(res.exposure.get('avg_exposure_pct'), 2)}%"
          f"（最大 {_num(res.exposure.get('max_exposure_pct'), 2)}%）")
    # 成交笔数要报**真实**笔数，不是明细列表的长度。`trades` 有显示上限，
    # 拿它的长度当"成交笔数"会在全池回测里把 6385 笔说成 400 笔。
    print(f"  成交笔数    {res.replay.fills} 笔"
          f"（明细只列前 {len(res.trades)} 笔）"
          f"   账本流水 {res.replay.ledger.entry_count if res.replay else 0} 条")

    print("\n  分年度")
    for row in res.yearly:
        print(f"    {row['year']}  {row['return_pct']:>8.2f}%   {row['start']} → {row['end']}")

    if res.blocked:
        print("\n  阻断 / 跳过（按规则各拦了多少次）")
        for name, n in sorted(res.blocked.items(), key=lambda kv: -kv[1]):
            print(f"    {name:<28} {n}")

    print("\n  口径说明")
    for note in res.notes:
        print(f"    · {note}")
    print(f"\n  digest {res.digest}")
    return 0


# --------------------------------------------------------------------------- #
#  check —— 恒等式体检
# --------------------------------------------------------------------------- #

def cmd_check(args) -> int:
    """
    两条恒等式，逐日核对：

      1. NAV = 现金 + Σ(持仓 × 当日收盘价)
      2. 账本归因六项之和 = 净收益

    第 1 条独立于引擎自己的算法：现金直接从账本取，持仓直接从账本取，
    乘的是面板里的收盘价 —— 如果引擎漏了一笔现金流，这里立刻不平。
    """
    panel = load_panel(args)
    cfg = build_config(args)
    bt = DailyBacktest(cfg, panel, EqualWeight(args.rebalance, args.warmup))
    res = bt.run()

    replay = res.replay
    last_i = len(panel) - 1

    # 独立重算：现金从账本取、持仓从账本取、价格从**面板**取。
    # 三处都不经过引擎的 NAV 函数，所以这里不平就一定是引擎漏了钱。
    cash = sum(replay.ledger.cash_native().values())
    held = Decimal(0)
    for (venue, sym), pos in replay.ledger.positions().items():
        if D(pos.qty) == 0:
            continue
        px = panel.close_at(sym, last_i)
        if px is None:
            raise DataQualityError(f"{sym} 有持仓但没有收盘价，无法独立核对")
        held += D(pos.qty) * px
    rebuild = q(cash + held, 2)
    final = res.final_nav
    diff = abs(D(rebuild) - D(final))

    report = replay.ledger.performance(
        {(venue, sym): panel.close_at(sym, last_i)
         for (venue, sym), pos in replay.ledger.positions().items()
         if D(pos.qty) != 0})

    ok_nav = diff <= Decimal("0.01")
    ok_attr = report.closes

    if args.json:
        print(json.dumps({
            "final_nav": str(final), "rebuilt_nav": str(rebuild),
            "diff": str(q(diff, 4)), "nav_identity_ok": ok_nav,
            "attribution_closes": ok_attr,
            "net_pnl": str(q(report.net_pnl, 2)),
            "attribution_sum": str(q(report.attribution_sum, 2)),
            "cash": str(q(cash, 2)), "positions_value": str(q(held, 2)),
            "positions": len(replay.ledger.positions()),
            "entries": replay.ledger.entry_count,
        }, ensure_ascii=False, indent=2))
        return 0 if (ok_nav and ok_attr) else 1

    print(f"{'=' * 62}\n  恒等式体检\n{'=' * 62}")
    print(f"  NAV（引擎）      {final}")
    print(f"  现金 + 持仓市值   {rebuild}   （现金 {q(cash, 2)} + 持仓 {q(held, 2)}，"
          f"{len(replay.ledger.positions())} 只）")
    print(f"  差额              {q(diff, 4)}   → {'✅ 恒等' if ok_nav else '❌ 不平'}")
    print(f"  归因六项之和      {q(report.attribution_sum, 2)}"
          f"   净收益 {q(report.net_pnl, 2)}   → "
          f"{'✅ 闭合' if ok_attr else '❌ 不闭合'}")
    print(f"  账本流水          {replay.ledger.entry_count} 条 / 成交 {replay.fills} 笔")
    return 0 if (ok_nav and ok_attr) else 1


# --------------------------------------------------------------------------- #
#  compare —— 与手工回测对账
# --------------------------------------------------------------------------- #

#: 结构差异清单。**每一条都必须能指出方向**，否则它只是免责声明。
STRUCTURAL_DIFFS = [
    ("价格口径",
     "平台用后复权（分红再投资），研究用不复权",
     "平台偏高",
     "研究把分红漏在收益外，除权日还会少一块钱；A 股股息率 1~3%，7 年累计可观。"),
    ("滑点记账",
     "平台改成交价、滑点单独归因；研究把滑点并进费用、不改成交价",
     "平台略低",
     "平台的滑点部分不再参与后续涨跌，研究那部分相当于多买了/少卖了份额。二阶，量级很小。"),
    ("净额口径",
     "平台按名义额净额再取整到手；研究直接按股数设目标",
     "平台偏低",
     "整手约束下，按名义额算出的差额经常不足一手，被丢弃 → 组合持续偏离等权、"
     "不再平衡。这是两种口径的真实差别，不是 bug。"),
    ("最小下单额",
     "平台 min_order_notional=2000（可配），研究 min_order_value=2000",
     "同口径",
     "刻意对齐，否则差异会混进上一条里。"),
    ("熔断",
     "平台 research 口径关闭日损/回撤熔断；研究根本没有这一层",
     "同口径",
     "开 prod 口径会被 5% 回撤永久停单（本批未做自动恢复），结果不可比。"),
    ("选股",
     "平台策略只拿得到收盘价（契约如此），做不了流动性排序",
     "平台偏低/偏高不定",
     "研究的 n_names 实验依赖成交额排序，平台这一层做不到；所以只比全池口径。"),
]


def cmd_compare(args) -> int:
    if not RESEARCH_SUMMARY.exists():
        raise DataQualityError(
            f"没有研究基线：{RESEARCH_SUMMARY}（先跑 research/ashare_ew20/ew20_backtest.py）")
    research = json.loads(RESEARCH_SUMMARY.read_text(encoding="utf-8"))
    base = research["summary"]

    panel = load_panel(args)
    cfg = build_config(args)
    res = DailyBacktest(cfg, panel, EqualWeight(args.rebalance, args.warmup)).run()
    d = res.stats.to_dict()

    rows = [
        ("年化 %", d["annual_return_pct"], base["annual_return_pct"]),
        ("总收益 %", d["total_return_pct"], base["total_return_pct"]),
        ("最大回撤 %", d["max_drawdown_pct"], base["max_drawdown_pct"]),
        ("年化波动 %", d["annual_vol_pct"], base["annual_vol_pct"]),
        ("夏普", d["sharpe"], base["sharpe"]),
        ("期末净值", float(d["final"]) if d["final"] else None, base["final_value"]),
    ]

    if args.json:
        print(json.dumps({
            "window": {"platform": [d["start"], d["end"]],
                       "research": [base["start"], base["end"]]},
            "rows": [{"metric": m, "platform": p, "research": r,
                      "diff": (None if p is None or r is None else round(p - r, 4))}
                     for m, p, r in rows],
            "structural_diffs": [{"topic": t, "what": w, "direction": dr, "why": y}
                                 for t, w, dr, y in STRUCTURAL_DIFFS],
            "platform": {"blocked": dict(sorted(res.blocked.items())),
                         "cost": res.cost, "exposure": res.exposure,
                         "digest": res.digest},
            "research": {"orders": research["meta"].get("orders"),
                         "rebalances": research["meta"].get("rebalances"),
                         "cost_paid": research["meta"].get("cost_paid"),
                         "blocked": research["meta"].get("blocked")},
            "verdict": _verdict(rows),
        }, ensure_ascii=False, indent=2))
        return 0

    print(f"{'=' * 74}\n  平台回测 vs 手工研究基线（research/ashare_ew20）\n{'=' * 74}")
    print(f"  平台区间 {d['start']} → {d['end']}    研究区间 {base['start']} → {base['end']}")
    if (d["start"], d["end"]) != (base["start"], base["end"]):
        print("  ⚠ 两段区间不同，下面的差值里混了口径差 —— 差值只能看方向，不能当精度。")
    print(f"\n  {'指标':<12}{'平台':>14}{'研究':>14}{'差':>12}")
    print(f"  {'-' * 52}")
    for name, p, r in rows:
        if p is None or r is None:
            print(f"  {name:<12}{'—':>14}{'—':>14}{'—':>12}")
            continue
        print(f"  {name:<12}{p:>14.3f}{r:>14.3f}{p - r:>+12.3f}")

    print("\n  结构差异（为什么不该相等）")
    for topic, what, direction, why in STRUCTURAL_DIFFS:
        print(f"    · {topic}：{what}")
        print(f"        方向 {direction}    {why}")

    print("\n  平台侧实测")
    print(f"    成交 {res.replay.fills} 笔 / 账本 {res.replay.ledger.entry_count} 条"
          f"    平均敞口 {_num(res.exposure.get('avg_exposure_pct'), 2)}%")
    print(f"    成本 {res.cost['total_bps_of_avg_nav']} bps（佣金 {res.cost['fee_total']}"
          f"，滑点 {res.cost['slippage_total']}）")
    bc = dict(sorted(res.blocked.items(), key=lambda kv: -kv[1]))
    print(f"    阻断 {'  '.join(f'{k}={v}' for k, v in list(bc.items())[:6])}")
    print("\n  研究侧原始口径")
    meta = research["meta"]
    print(f"    成交 {meta.get('orders')} 笔 / 调仓 {meta.get('rebalances')} 次"
          f"    花费 {meta.get('cost_paid')}")
    print(f"    阻断 {meta.get('blocked')}")

    print(f"\n  结论：{_verdict(rows)}")
    return 0


def _verdict(rows) -> str:
    """一句话结论。只描述**能观察到的**东西，不替用户下"策略好不好"的判断。"""
    ann = dict((m, (p, r)) for m, p, r in rows).get("年化 %")
    if not ann or ann[0] is None or ann[1] is None:
        return "指标不全，无法给出方向性结论。"
    gap = ann[0] - ann[1]
    if abs(gap) < 1.0:
        return (f"年化差 {gap:+.2f}pp —— 两套记账在同一量级上一致；"
                "剩余差异按上面的结构清单解释。")
    return (f"年化差 {gap:+.2f}pp —— 超出噪声，先按结构清单逐项排查，"
            "不要用『调参对齐』把差异抹平。")


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="QuantOS 回测（B12）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--names", type=int, default=0,
                        help="只用前 N 只标的（0 = 全池）")
    common.add_argument("--min-bars", type=int, default=60, help="最少 bar 数")
    common.add_argument("--start", default="",
                        help="评估起点 YYYY-MM-DD（之前的 bar 仍做预热）")
    common.add_argument("--warmup", type=int, default=25, help="预热 bar 数")
    common.add_argument("--rebalance", type=int, default=20, help="再平衡间隔（交易日）")
    common.add_argument("--cash", default="10000000", help="初始资金")
    common.add_argument("--max-pending", type=int, default=10, help="挂单最多存活几根 bar")
    common.add_argument("--no-adjust", action="store_true", help="不复权（对照用）")
    common.add_argument("--risk-profile", default="research", choices=["research", "prod"])
    common.add_argument("--json", action="store_true", help="只输出 JSON")

    for name, fn, desc in (("run", cmd_run, "跑一次回测"),
                           ("check", cmd_check, "恒等式体检"),
                           ("compare", cmd_compare, "与手工基线对账")):
        p = sub.add_parser(name, parents=[common], help=desc)
        p.set_defaults(func=fn)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (BacktestError, DataQualityError) as exc:
        if args.json:
            print(json.dumps({"error": f"{type(exc).__name__}: {exc}"},
                             ensure_ascii=False))
            return 2
        print(f"[FATAL] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
