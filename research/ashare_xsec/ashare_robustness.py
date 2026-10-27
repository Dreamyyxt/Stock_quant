#!/usr/bin/env python3
"""A 股横截面因子的稳健性审计（robustness audit）。

为什么单独写这一层
------------------
一个因子在样本内跑出 +280%，最容易的三种解释是：真 alpha、参数恰好挑中噪声、
或者样本重叠把 t 值放大了十倍。回测本身**无法区分**这三者——只有稳健性检验能。

本脚本对每个因子给出四张"测谎"结果：

A. 参数面（parameter surface）
   把回看窗口从 5 摆到 250。真因子的收益应该在这个纬度上**连续**：20 天好、40 天也还
   可以、10 天不至于崩。如果只有一个点尖起来，那是噪声的指纹，不是 alpha。
   上一次 mom_5d 就是这么被否掉的——它最好的持有期恰好是九种里唯一被挑中的那种。

B. 随机组合零分布（random-portfolio null）
   每期把因子值随机打乱再取同一分位——等于"随机挑同样多的股票"。打 500 次得到零分布。
   策略落在 99 分位之外才算过。这一步防的是"整个市场这几年就在涨，随便买都赚"。

C. 成本敏感性（cost sensitivity）
   把双边成本从 10.2bp 乘到 3 倍。A 股小微盘的冲击成本远高于名义佣金，
   一个"加两倍成本就归零"的因子，实盘不可能活。

D. 分段稳定性（regime stability）
   逐年看收益，并且把封存段（最后 20%）单独算。只在某一年赚钱的因子不能上线。

输出
----
state/ashare/robustness.json   全量结果
state/ashare/robustness.csv    参数面表格
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

STRAT = Path(__file__).resolve().parent      # research/ashare_xsec
ROOT = STRAT.parent.parent                   # 项目根：state/ 在这里
sys.path.insert(0, str(ROOT))

import ashare_xsec_backtest as E  # noqa: E402

OUT_JSON = ROOT / "state" / "ashare" / "robustness.json"
OUT_CSV = ROOT / "state" / "ashare" / "robustness.csv"

# 参数面：反转看短端，动量看长端。两端都摆出来，看的是"坡"不是"峰"
REV_LOOKBACKS = [5, 10, 15, 20, 30, 40, 60]
MOM_LOOKBACKS = [60, 120, 180, 250]
QUANTILES = [0.05, 0.10, 0.20]
COST_MULTIPLES = [1.0, 2.0, 3.0]
N_RANDOM = 300
SEED = 20261026


# --------------------------------------------------------------------------- #
def forward_open_return(open_px: pd.DataFrame, bars: int) -> pd.DataFrame:
    """信号日 i 的远期收益：T+1 开盘买入、持有 bars 个交易日后开盘卖出。

    与引擎的成交口径一致（开盘价进、开盘价出），这样解析法和引擎能互相校验。
    """
    entry = open_px.shift(-1)
    exit_ = open_px.shift(-(1 + bars))
    return exit_ / entry - 1.0


def period_returns(
    factor: pd.DataFrame,
    fwd: pd.DataFrame,
    signal_idx: list[int],
    direction: int,
    quantile: float,
    cost_per_period: float,
    shuffle: np.random.Generator | None = None,
) -> np.ndarray:
    """逐期等权分位组合的净收益序列。

    `shuffle` 不为 None 时，每期把因子值随机置换后再取分位——即随机选股零假设。
    置换保留当期横截面的因子分布宽度，只破坏排序信息，这是最干净的对照。
    """
    out: list[float] = []
    for i in signal_idx:
        s = factor.iloc[i]
        f = fwd.iloc[i]
        ok = s.notna() & f.notna()
        if ok.sum() < E.MIN_CROSS_SECTION:
            continue
        s2 = s[ok]
        f2 = f[ok]
        if shuffle is not None:
            s2 = pd.Series(shuffle.permutation(s2.to_numpy()), index=s2.index)
        k = max(1, int(round(len(s2) * quantile)))
        sel = s2.nlargest(k).index if direction > 0 else s2.nsmallest(k).index
        out.append(float(f2[sel].mean()) - cost_per_period)
    return np.asarray(out, dtype=float)


def curve_stats(period_rets: np.ndarray, bars: int = E.REBALANCE_BARS) -> dict:
    """把逐期收益折成一条净值曲线再算指标（与引擎口径可比）。"""
    if period_rets.size == 0:
        return {}
    nav = np.cumprod(1.0 + period_rets)
    total = float(nav[-1] - 1.0)
    years = period_rets.size * bars / 252.0
    ann = float(nav[-1] ** (1.0 / years) - 1.0) if years > 0 and nav[-1] > 0 else float("nan")
    peak = np.maximum.accumulate(nav)
    mdd = float((nav / peak - 1.0).min())
    per_year = 252.0 / bars
    sharpe = float(period_rets.mean() / period_rets.std(ddof=1) * np.sqrt(per_year)) \
        if period_rets.std(ddof=1) > 0 else float("nan")
    return {
        "total_pct": round(total * 100, 2),
        "annual_pct": round(ann * 100, 2),
        "mdd_pct": round(mdd * 100, 2),
        "sharpe": round(sharpe, 2),
        "periods": int(period_rets.size),
    }


def main() -> int:
    t0 = pd.Timestamp.now()
    panel = E.load_panel()
    cal = panel.calendar
    print(f"[data] {panel.fetched}/{panel.requested} 只，{cal[0].date()} → {cal[-1].date()}，{len(cal)} 个交易日")

    # 评估窗口与主回测保持一致
    base = E.build_factor(panel.close, panel.volume, "mom120")
    ready = (base.notna() & (panel.volume > 0)).sum(axis=1)
    ok_days = ready[ready >= E.MIN_CROSS_SECTION]
    eval_start = int(cal.get_loc(ok_days.index[0]))
    eval_end = len(cal) - 1
    holdout_idx = int(eval_start + (eval_end - eval_start) * (1 - E.HOLDOUT_FRAC))
    holdout_date = cal[holdout_idx].strftime("%Y-%m-%d")
    signal_idx = [i for i in range(eval_start, eval_end, E.REBALANCE_BARS)]
    in_sample = [i for i in signal_idx if i < holdout_idx]
    print(f"[window] {cal[eval_start].date()} → {cal[eval_end].date()}，"
          f"封存 {holdout_date} 起，共 {len(signal_idx)} 期（样本内 {len(in_sample)}）")

    fwd = forward_open_return(panel.open, E.REBALANCE_BARS)
    round_trip = E.BUY_COST + E.SELL_COST     # 单期全换手的双边成本
    rng = np.random.default_rng(SEED)

    specs: list[tuple[str, int, int]] = (
        [(f"rev{n}", n, -1) for n in REV_LOOKBACKS] + [(f"mom{n}", n, +1) for n in MOM_LOOKBACKS]
    )

    rows: list[dict] = []
    detail: dict[str, dict] = {}

    for name, look, direction in specs:
        E.LOOKBACK[name] = look
        factor = E.build_factor(panel.close, panel.volume, name)

        # ---- A. 参数面（本期数固定 10% 分位） ---------------------------
        pr = period_returns(factor, fwd, signal_idx, direction, 0.10, round_trip)
        st = curve_stats(pr)
        row = {"factor": name, "lookback": look, "side": "long_top" if direction > 0 else "long_bottom",
               **st}
        rows.append(row)

        # ---- C. 成本敏感性 ---------------------------------------------
        cost_sens = {}
        for mult in COST_MULTIPLES:
            p = period_returns(factor, fwd, signal_idx, direction, 0.10, round_trip * mult)
            cost_sens[f"x{mult:g}"] = curve_stats(p)

        # ---- B. 随机组合零分布 -----------------------------------------
        null_totals = np.empty(N_RANDOM)
        for b in range(N_RANDOM):
            p = period_returns(factor, fwd, signal_idx, direction, 0.10, round_trip, shuffle=rng)
            null_totals[b] = float(np.prod(1.0 + p) - 1.0)
        pct_rank = float((null_totals < st["total_pct"] / 100.0).mean())
        null_mean = float(null_totals.mean())

        # ---- D. 分段稳定性 ---------------------------------------------
        pr_in = period_returns(factor, fwd, in_sample, direction, 0.10, round_trip)
        pr_out = period_returns(factor, fwd, [i for i in signal_idx if i >= holdout_idx],
                                direction, 0.10, round_trip)
        by_year: dict[int, float] = {}
        nav = 1.0
        for i, r in zip(signal_idx, pr):
            nav *= 1.0 + r
            by_year[cal[i].year] = nav
        yearly = {}
        prev = 1.0
        for y in sorted(by_year):
            yearly[y] = round((by_year[y] / prev - 1.0) * 100, 2)
            prev = by_year[y]

        detail[name] = {
            "lookback": look,
            "direction": direction,
            "full": st,
            "in_sample": curve_stats(pr_in),
            "holdout": curve_stats(pr_out),
            "cost_sensitivity": cost_sens,
            "random_null": {
                "draws": N_RANDOM,
                # 复合净值分布是**重尾**的（94 期噪声被复利放大），均值会被少数幸运
                # 抽样拉高，必须同时看中位数和分位。只看均值会误判成"策略没超额"。
                "null_mean_total_pct": round(null_mean * 100, 2),
                "null_median_total_pct": round(float(np.median(null_totals)) * 100, 2),
                "null_p05_total_pct": round(float(np.percentile(null_totals, 5)) * 100, 2),
                "null_p95_total_pct": round(float(np.percentile(null_totals, 95)) * 100, 2),
                "strategy_total_pct": st["total_pct"],
                "percentile_vs_null": round(pct_rank, 4),
                "beats_median_by_pct_points": round(st["total_pct"] - float(np.median(null_totals)) * 100, 2),
            },
            "yearly_pct": yearly,
        }
        x3 = detail[name]["cost_sensitivity"].get("x3", {}).get("total_pct", float("nan"))
        flag = "PASS" if (pct_rank > 0.99 and x3 > 0) else "FAIL"
        detail[name]["verdict"] = flag
        print(f"[{name:8s}] 总 {st['total_pct']:8.2f}% 年化 {st['annual_pct']:6.2f}% "
              f"MDD {st['mdd_pct']:7.2f}% Sharpe {st['sharpe']:5.2f} | "
              f"随机零假设 中位 {np.median(null_totals)*100:6.2f}% 分位 {pct_rank*100:5.1f}% | "
              f"x3成本 {x3:8.2f}% | 封存 {detail[name]['holdout'].get('total_pct', 0):7.2f}% | {flag}",
              flush=True)

    # ---- 分位敏感性：同一因子在不同分位的表现 -------------------------
    quant_sens: dict[str, dict] = {}
    for name, look, direction in specs:
        if name not in ("rev20", "mom120"):
            continue
        E.LOOKBACK[name] = look
        factor = E.build_factor(panel.close, panel.volume, name)
        quant_sens[name] = {
            f"q{q:g}": curve_stats(period_returns(factor, fwd, signal_idx, direction, q, round_trip))
            for q in QUANTILES
        }

    # ---- 等权全池基准（同样用开盘口径，可比） ---------------------------
    ew_periods = []
    for i in signal_idx:
        f = fwd.iloc[i]
        f = f[f.notna()]
        if len(f) >= E.MIN_CROSS_SECTION:
            ew_periods.append(float(f.mean()))
    ew = curve_stats(np.asarray(ew_periods))

    payload = {
        "generated_at": t0.strftime("%Y-%m-%d %H:%M:%S"),
        "universe": {"fetched": panel.fetched, "requested": panel.requested,
                     "note": "当前中证800成分股，含幸存者偏差"},
        "window": {"start": cal[eval_start].strftime("%Y-%m-%d"),
                   "end": cal[eval_end].strftime("%Y-%m-%d"),
                   "holdout_start": holdout_date,
                   "rebalance_bars": E.REBALANCE_BARS,
                   "cost_round_trip_bp": round(round_trip * 1e4, 2)},
        "benchmark_equal_weight_open": ew,
        "factor_surface": rows,
        "detail": detail,
        "quantile_sensitivity": quant_sens,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(rows).to_csv(OUT_CSV, index=False)
    print(f"\n[bench] 等权全池 总 {ew['total_pct']:.2f}% 年化 {ew['annual_pct']:.2f}% Sharpe {ew['sharpe']}")
    print(f"[out] {OUT_JSON}\n[out] {OUT_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
