#!/usr/bin/env python3
"""
QuantOS 风险模型体检（T-701）
=============================

估一次协方差，把诊断信息打出来，并落盘给运维面读。

```bash
.venv/bin/python scripts/quantos_risk_check.py                 # 用仓库里的行情
.venv/bin/python scripts/quantos_risk_check.py --demo          # 合成数据演示
.venv/bin/python scripts/quantos_risk_check.py --demo --shrinkage 0.5 --halflife 20
.venv/bin/python scripts/quantos_risk_check.py --demo --drop-from 300   # 故意样本不足
```

看这个脚本输出的正确姿势：**先看"能不能用"，再看"数字是多少"**。
一个 trustworthy=false 的估计，数字再漂亮也不该进风险预算。
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys
from datetime import date, timedelta
from decimal import Decimal

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from quantos_core.covariance import (                       # noqa: E402
    TRADING_DAYS_CRYPTO, TRADING_DAYS_EQUITY, EstimationConfig, PriceSeries,
    RiskModelStore, describe_inputs, detect_price_jumps, estimate,
    intersect_dates,
)

D = lambda x: Decimal(str(x))


def demo_series(n: int = 160):
    """合成一组股票 + 债券价格，带共同因子（否则相关性会假性接近 0）。"""
    rng = random.Random(2026)
    factor = [rng.gauss(0.0, 1.0) for _ in range(n)]

    def build(symbol: str, sigma_d: float, seed: int, beta: float,
              drift: float = 0.0) -> PriceSeries:
        r = random.Random(seed)
        raw = [beta * factor[i] + math.sqrt(max(0.0, 1 - beta * beta))
               * r.gauss(0.0, 1.0) for i in range(n)]
        mean = sum(raw) / len(raw)
        c = [x - mean for x in raw]
        sd = math.sqrt(sum(v * v for v in c) / (len(c) - 1))
        px = [100.0]
        for z in c:
            px.append(max(1.0, px[-1] * (1.0 + drift + sigma_d * z / sd)))
        start = date(2026, 5, 1)
        return PriceSeries(symbol, tuple(
            (start + timedelta(days=i), D(f"{p:.10f}")) for i, p in enumerate(px)))

    return [
        build("QQQ", 0.012, 11, 0.95, 0.0006),
        build("SPY", 0.009, 22, 0.90, 0.0004),
        build("TLT", 0.007, 33, -0.30, -0.0002),
    ]


def load_ashare(symbols, tail_days: int, adjusted: bool = True):
    """
    读 `state/ashare/daily/*.csv`（A 股日线，带 adj_factor）。

    **这份缓存里的 `close` 已经是前复权价**，直接用就对了。
    `adj_factor` 是累计比值（sh600000 从 0.7457 涨到 1.0），
    未复权价 = `close / adj_factor`。

    这个方向搞反的代价很具体：把复权价再除一次 adj_factor，等于凭空制造
    几十处除权跳变，σ 会被抬到离谱的水平 —— 而它看起来仍然是个"正常"的
    波动率数字，不会报任何错。

    所以 `adjusted=False` 故意保留成对照：脚本会同时打印两种口径的跳变数，
    让「必须复权」这句话有实证。
    """
    import csv
    out = []
    daily = os.path.join(_ROOT, "state", "ashare", "daily")
    for symbol in symbols:
        path = os.path.join(daily, f"{symbol}.csv")
        if not os.path.isfile(path):
            raise FileNotFoundError(f"找不到 {path}")
        rows = []
        with open(path, "r", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                close = D(row["close"])
                adj = D(row.get("adj_factor") or 1)
                if close <= 0:
                    continue
                # close 本身已是前复权价；还原未复权才需要除以 adj_factor。
                px = close if adjusted or adj <= 0 else close / adj
                rows.append((date.fromisoformat(row["date"]), px))
        rows.sort()
        if tail_days > 0:
            rows = rows[-tail_days:]
        out.append(PriceSeries(symbol, tuple(rows)))
    return out


def _adj_factor_changes(symbol: str) -> int:
    """复权因子变动次数 = 分红/送股事件的次数。"""
    import csv
    path = os.path.join(_ROOT, "state", "ashare", "daily", f"{symbol}.csv")
    if not os.path.isfile(path):
        return 0
    last, n = None, 0
    with open(path, "r", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            v = row.get("adj_factor")
            if last is not None and v != last:
                n += 1
            last = v
    return n


def _single_sigma(series: PriceSeries, annualization: int, min_obs: int):
    """单标的 σ，估不出来就返回 None（不抛错，对照表要能继续打）。"""
    try:
        est = estimate([series], EstimationConfig(
            annualization=annualization, min_obs=min(min_obs, 60)))
        return est.vols.get(series.symbol)
    except Exception:                               # noqa: BLE001
        return None


def bar(text: str) -> None:
    print("\n" + "─" * 72)
    print(text)
    print("─" * 72)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="QuantOS 风险模型体检")
    ap.add_argument("--source", default="demo", choices=["demo", "ashare"],
                    help="demo=合成数据；ashare=读 state/ashare/daily 真实日线")
    ap.add_argument("--symbols", default="sh600000,sh600004,sh600009",
                    help="--source ashare 时要读的标的（逗号分隔）")
    ap.add_argument("--tail", type=int, default=250,
                    help="--source ashare 时只取最近 N 天")
    ap.add_argument("--annualization", type=int, default=TRADING_DAYS_EQUITY,
                    choices=[TRADING_DAYS_EQUITY, TRADING_DAYS_CRYPTO])
    ap.add_argument("--shrinkage", type=float, default=0.3)
    ap.add_argument("--halflife", type=int, default=0)
    ap.add_argument("--min-obs", type=int, default=120)
    ap.add_argument("--drop-from", type=int, default=0,
                    help="只保留最近 N 天，用来演示样本不足")
    ap.add_argument("--out", default=os.path.join(_ROOT, "state/risk/model.json"))
    ap.add_argument("--no-write", action="store_true", help="只算不落盘")
    args = ap.parse_args(argv)

    if args.source == "ashare":
        wanted = [x.strip() for x in args.symbols.split(",") if x.strip()]
        bar("0. 复权口径对照（实证：为什么必须复权）")
        raw_series = load_ashare(wanted, args.tail, adjusted=False)
        series = load_ashare(wanted, args.tail, adjusted=True)
        print("  同一份数据、两种价格口径，各估一次年化 σ：\n")
        for raw, adj_s in zip(raw_series, series):
            j_raw = detect_price_jumps([p for _, p in raw.points],
                                       [d for d, _ in raw.points])
            adj_changes = _adj_factor_changes(raw.symbol)
            sig_raw = _single_sigma(raw, args.annualization, args.min_obs)
            sig_adj = _single_sigma(adj_s, args.annualization, args.min_obs)
            delta = (f"{float(sig_raw)/float(sig_adj) - 1:+.1%}"
                     if sig_raw and sig_adj else "—")
            print(f"  {adj_s.symbol:10s} 复权因子变动 {adj_changes:3d} 次"
                  f"  |  σ 未复权 {str(sig_raw):>10s}"
                  f"  σ 前复权 {str(sig_adj):>10s}  差 {delta}")
            if j_raw:
                print(f"  {'':10s} ⚠ 未复权口径检出 {len(j_raw)} 处 >35% 跳变，"
                      f"最剧烈 {max(j_raw, key=lambda j: abs(j[1]))[1]:+.2%}")
        print("\n  这份样本只有分红除权（幅度 2~3%），没有拆股，所以两种口径")
        print("  都不会触发 35% 阈值 —— 上表的 σ 差异就是分红口径不一致的代价。")
        print("  真遇到拆股（2:1 就是 -50%）会触发跳变告警，那时必须换口径。")
    else:
        series = demo_series()
    if args.drop_from > 0:
        series = [PriceSeries(s.symbol, s.points[-args.drop_from:]) for s in series]

    bar("1. 输入体检（先看这个，比看结果有用）")
    for line in describe_inputs(series):
        print("  " + line)
    common = intersect_dates(series)
    print(f"\n  共同交易日 {len(common)} 天 → 收益观测 {max(0, len(common) - 1)} 个")
    for s in series:
        jumps = detect_price_jumps([p for _, p in s.points],
                                   [d for d, _ in s.points])
        if jumps:
            print(f"  ⚠ {s.symbol} 有 {len(jumps)} 处疑似未复权跳变：{jumps[:3]}")

    cfg = EstimationConfig(
        annualization=args.annualization,
        halflife=args.halflife or None,
        shrinkage=D(args.shrinkage),
        min_obs=args.min_obs,
    )
    bar(f"2. 估计参数")
    print(f"  年化因子   {cfg.annualization} 日/年"
          f"（{'股票口径' if cfg.annualization == 252 else 'Crypto 7×24 口径'}）")
    print(f"  半衰期     {cfg.halflife or '不用（等权）'}")
    print(f"  收缩强度   δ={cfg.shrinkage}")
    print(f"  最少观测   {cfg.min_obs}")

    try:
        est = estimate(series, cfg, label="体检脚本估计")
    except Exception as exc:                        # noqa: BLE001
        bar("3. 估计失败")
        print(f"  {type(exc).__name__}: {exc}")
        print("\n  这是**预期行为**：样本不够就直接拒绝，不给一个「凑合能用」的模型。")
        return 1

    bar("3. 波动率（年化）")
    for sym, vol in sorted(est.vols.items()):
        print(f"  {sym:10s} σ = {str(vol):>12s}   ({float(vol)*100:5.1f}%)")

    bar("4. 相关系数（收缩前 → 收缩后）")
    if est.raw_corr:
        print("  收缩会把极端 ρ 拉向均值 ρ̄。看 0.62 时要记得它原本是 0.87 ——")
        print("  只报收缩后的值，会让人以为 0.62 是估出来的。\n")
        for (a, b), v in sorted(est.corr.items()):
            raw = est.raw_corr.get((a, b))
            arrow = f"  ← 样本 {raw}" if raw is not None and raw != v else ""
            print(f"  ρ({a},{b}) = {v}{arrow}")
    else:
        for (a, b), v in sorted(est.corr.items()):
            print(f"  ρ({a},{b}) = {v}")
        print("\n  （未做收缩）")

    bar("5. 可信度诊断")
    print(f"  观测数       {est.n_obs}")
    print(f"  待估参数     {est.n_params}（{est.n_assets} 个 σ + "
          f"{est.n_assets*(est.n_assets-1)//2} 个 ρ）")
    print(f"  自由度比     {est.dof_ratio}   (阈值 ≥ 5)")
    print(f"  ρ 分辨率     ±{est.corr_stderr()}  (标准误上界：小于它的差异无意义)")
    print(f"  结论         {'可以用' if est.trustworthy else '仅供参考，不要拿去做风险预算'}")

    if est.warnings:
        bar(f"6. 告警（{len(est.warnings)} 条）")
        for w in est.warnings:
            print(f"  ⚠ {w}")

    if args.no_write:
        bar("落盘")
        print("  已跳过（--no-write）")
    else:
        store = RiskModelStore(args.out)
        store.save(est)
        bar("落盘")
        print(f"  已写入 {args.out}")
        print(f"  运维面 /api/ops/portfolio 会读到它，并显示年龄 "
              f"{store.age_days():.2f} 天")

    bar("怎么用")
    print("  model = est.to_risk_model()      # default_corr 故意留 None")
    print("  risk  = portfolio_risk(net, model)   # B6 的风险分解")
    print("\n  注意：to_risk_model() 不填 default_corr。没估过的标的对会在")
    print("  risk_budget 里抛错，而不是静默拿到一个「看起来合理」的 ρ。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
