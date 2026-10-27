#!/usr/bin/env python3
"""A-share ETF short-term reversal backtest — 510300 沪深300ETF.

The rule came out of `scripts/ashare_factor_lab.py`, which screened 21 factor
families across 5 horizons on this ETF (105 tests) with a block-permutation null,
a random-entry control and a Benjamini-Hochberg pass. Exactly one candidate
survived: past 5-day return, buy the bottom quintile, hold 10 trading days.

This script turns that statistical result into something a broker could actually
execute, and puts it next to buy-and-hold so the reader can see whether the rule
adds anything beyond the ETF's own drift.

Execution model (all of it matters):
  * signal is read at the close of day t, filled at the open of day t+1 —
    never on the bar that produced it;
  * A-share T+1: a position opened at the open of day t cannot be sold before
    day t+1, enforced by an explicit bar-index check;
  * 100-unit lots, because a fractional order does not exist for an ETF;
  * ETF fee structure: commission both sides, no stamp duty, no transfer fee;
  * ±10% price limit — no buying into a limit-up open, no selling into a
    limit-down open, because nothing would fill there.

Run:
    python rev5_510300_backtest.py
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass

import numpy as np
import pandas as pd

PROJ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs")
os.makedirs(OUT, exist_ok=True)
sys.path.insert(0, PROJ)
from reporting.export_results import export_results  # noqa: E402
from reporting.render_dashboard import build_dashboard_data, render_dashboard  # noqa: E402

# --------------------------------------------------------------------------- #
#  Configuration
# --------------------------------------------------------------------------- #
PREFIX = "rev5_510300"
SYMBOL = "510300.SH"
SYMBOL_NAME = "沪深300ETF"
DATA_CSV = os.path.join(PROJ, "state", "ashare", "510300_daily.csv")

INITIAL_CASH = 1_000_000.0

# Signal: the 5-day return, ranked against its own history.
FACTOR_LOOKBACK = 5
RANK_WARMUP = 250        # observations needed before the rank is trustworthy
ENTRY_RANK = 0.20        # buy the bottom quintile of 5-day returns
# Early exit is DISABLED by default. The screening only ever validated "buy the
# bottom quintile, hold 10 trading days" — an adaptive "leave once it reverts"
# rule was tried afterwards and lifted total return from +22.9% to +40.4%, but
# that improvement comes from a degree of freedom the statistical test never saw.
# Keeping the validated version as the headline is the whole point; the adaptive
# variant is reported separately as a sensitivity, not as the result.
EXIT_RANK = 1.01         # > 1 disables the reversion exit
MAX_HOLD_BARS = 10

# A-share / ETF frictions. Kept as parameters rather than inline literals so a
# broker change does not require editing the loop.
LOT_SIZE = 100
COMMISSION = 0.00025     # per side; typical retail A-share rate
SLIPPAGE = 0.00025       # per side ≈ one tick on a ~4 CNY ETF
STAMP_DUTY = 0.0         # ETFs are exempt; individual stocks are not
PRICE_LIMIT = 0.10       # ETF daily band

SEALED_START = "2025-03-03"   # matches the held-out window used in screening


# --------------------------------------------------------------------------- #
#  Data
# --------------------------------------------------------------------------- #
def load_prices(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        raise SystemExit(
            f"missing price file {path}\n"
            f"run: python scripts/prep_ashare_data.py"
        )
    df = pd.read_csv(path, parse_dates=["date"]).reset_index(drop=True)
    df = df.sort_values("date").reset_index(drop=True)
    # A suspension shows up as a missing close. Drop those rows rather than
    # forward filling: a filled bar would fake a tradable price.
    df = df.dropna(subset=["close"]).reset_index(drop=True)
    return df


def expanding_rank(values: pd.Series, min_periods: int) -> pd.Series:
    """Percentile rank of today's value among all values up to today.

    Deliberately not `rank(pct=True)` over the whole column: that would tell the
    signal where its own future extremes sit and quietly leak the answer.
    """
    arr = values.to_numpy(dtype=float)
    out = np.full(len(arr), np.nan)
    seen: list[float] = []
    for i, v in enumerate(arr):
        if not np.isfinite(v):
            continue
        seen.append(v)
        if len(seen) >= min_periods:
            ref = np.asarray(seen)
            out[i] = (ref <= v).sum() / len(ref)
    return pd.Series(out, index=values.index)


# --------------------------------------------------------------------------- #
#  Backtest
# --------------------------------------------------------------------------- #
@dataclass
class Params:
    entry_rank: float = ENTRY_RANK
    exit_rank: float = EXIT_RANK
    max_hold: int = MAX_HOLD_BARS
    commission: float = COMMISSION
    slippage: float = SLIPPAGE
    stamp_duty: float = STAMP_DUTY


def run(df: pd.DataFrame, params: Params, initial_cash: float = INITIAL_CASH,
        eval_start: pd.Timestamp | None = None) -> dict:
    df = df.copy()
    df["mom5"] = df["close"].pct_change(FACTOR_LOOKBACK)
    df["rank"] = expanding_rank(df["mom5"], min_periods=RANK_WARMUP)
    df["prev_close"] = df["close"].shift(1)

    if eval_start is None:
        eval_start = df.loc[df["rank"].notna(), "date"].iloc[0]

    cash = initial_cash
    position = 0
    entry_price = 0.0
    entry_bar = -1
    entry_date = ""
    entry_cost = 0.0

    pending_buy = False
    pending_sell = False
    sell_reason = ""

    equity_curve: list[dict] = []
    trades: list[dict] = []
    exposure_bars = 0
    skipped_limit = 0
    skipped_cash = 0

    for i in range(len(df)):
        row = df.iloc[i]
        date = row["date"]
        close = float(row["close"])
        open_ = float(row["open"])
        rank = row["rank"]
        prev_close = row["prev_close"]

        in_window = date >= eval_start

        # ---- 1. fill anything queued yesterday ------------------------------ #
        if pending_sell and position > 0:
            can_sell = i > entry_bar           # A-share T+1
            limit_down = (pd.notna(prev_close) and prev_close > 0
                          and open_ / float(prev_close) - 1.0 <= -(PRICE_LIMIT - 0.001))
            if can_sell and not limit_down:
                fill = open_ * (1.0 - params.slippage)
                gross = position * fill
                fees = gross * (params.commission + params.stamp_duty)
                proceeds = gross - fees
                cash += proceeds
                pnl = proceeds - entry_cost
                trades.append({
                    "entry_date": entry_date,
                    "exit_date": date.strftime("%Y-%m-%d"),
                    "side": "long",
                    "size": int(position),
                    "entry_price": round(entry_price, 4),
                    "exit_price": round(fill, 4),
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl / entry_cost * 100.0, 4) if entry_cost else 0.0,
                    "holding_bars": int(i - entry_bar),
                    "symbol": SYMBOL,
                    "symbol_name": SYMBOL_NAME,
                    "exit_reason": sell_reason,
                })
                position = 0
                entry_price = 0.0
                entry_bar = -1
                pending_sell = False
            elif limit_down:
                skipped_limit += 1          # remains queued for the next open

        if pending_buy and position == 0 and cash > 0:
            limit_up = (pd.notna(prev_close) and prev_close > 0
                        and open_ / float(prev_close) - 1.0 >= (PRICE_LIMIT - 0.001))
            if not limit_up:
                fill = open_ * (1.0 + params.slippage)
                unit_cost = fill * (1.0 + params.commission)
                size = int(cash / unit_cost)
                size = (size // LOT_SIZE) * LOT_SIZE
                if size > 0:
                    gross = size * fill
                    fees = gross * params.commission
                    entry_cost = gross + fees
                    cash -= entry_cost
                    position = size
                    entry_price = fill
                    entry_bar = i
                    entry_date = date.strftime("%Y-%m-%d")
                else:
                    skipped_cash += 1
                pending_buy = False
            else:
                skipped_limit += 1

        # ---- 2. read today's close and queue tomorrow's order ---------------- #
        if in_window and pd.notna(rank):
            if position == 0:
                if float(rank) <= params.entry_rank:
                    pending_buy = True
            else:
                held = i - entry_bar
                reverted = float(rank) >= params.exit_rank
                if reverted:
                    sell_reason = "回到中位数以上"
                    pending_sell = True
                elif held >= params.max_hold - 1:
                    # Queue one bar early: the order fills at the *next* open, so
                    # waiting for `held >= max_hold` would exit at entry_bar + N + 1
                    # and hold one day longer than the horizon the screen tested.
                    sell_reason = f"持有满 {params.max_hold} 日"
                    pending_sell = True

        # ---- 3. mark to market ---------------------------------------------- #
        value = cash + position * close
        equity_curve.append({"date": date.strftime("%Y-%m-%d"), "value": round(value, 2)})
        if in_window and position > 0:
            exposure_bars += 1

    # ---- forced close on the final bar ------------------------------------- #
    forced = False
    if position > 0:
        last = df.iloc[-1]
        fill = float(last["close"]) * (1.0 - params.slippage)
        gross = position * fill
        fees = gross * (params.commission + params.stamp_duty)
        proceeds = gross - fees
        cash += proceeds
        pnl = proceeds - entry_cost
        trades.append({
            "entry_date": entry_date,
            "exit_date": last["date"].strftime("%Y-%m-%d"),
            "side": "long",
            "size": int(position),
            "entry_price": round(entry_price, 4),
            "exit_price": round(fill, 4),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / entry_cost * 100.0, 4) if entry_cost else 0.0,
            "holding_bars": int((len(df) - 1) - entry_bar),
            "symbol": SYMBOL,
            "symbol_name": SYMBOL_NAME,
            "exit_reason": "样本末尾强制平仓",
        })
        equity_curve[-1]["value"] = round(cash, 2)
        position = 0
        forced = True

    window = [p for p in equity_curve if p["date"] >= eval_start.strftime("%Y-%m-%d")]
    return {
        "equity_curve": window,
        "trades": [t for t in trades if t["entry_date"] >= eval_start.strftime("%Y-%m-%d")],
        "exposure_bars": exposure_bars,
        "skipped_limit": skipped_limit,
        "skipped_cash": skipped_cash,
        "forced_close_at_end": forced,
        "eval_start": eval_start.strftime("%Y-%m-%d"),
        "eval_end": df["date"].iloc[-1].strftime("%Y-%m-%d"),
        "final_cash": cash,
    }


def buy_and_hold(df: pd.DataFrame, eval_start: pd.Timestamp,
                 initial_cash: float = INITIAL_CASH,
                 params: Params | None = None) -> tuple[list[dict], dict]:
    """Same money, same frictions, no timing: buy the first open and hold."""
    p = params or Params()
    win = df[df["date"] >= eval_start].reset_index(drop=True)
    fill = float(win["open"].iloc[0]) * (1.0 + p.slippage)
    size = int(initial_cash / (fill * (1.0 + p.commission)))
    size = (size // LOT_SIZE) * LOT_SIZE
    cost = size * fill * (1.0 + p.commission)
    cash = initial_cash - cost
    curve = [{"date": r["date"].strftime("%Y-%m-%d"),
              "value": round(cash + size * float(r["close"]), 2)}
             for _, r in win.iterrows()]
    last_close = float(win["close"].iloc[-1]) * (1.0 - p.slippage)
    final = cash + size * last_close * (1.0 - p.commission)
    return curve, {"size": size, "entry_price": fill, "final": final,
                   "return_pct": (final / initial_cash - 1.0) * 100.0}


def run_sensitivity(df: pd.DataFrame, eval_start: pd.Timestamp) -> list[dict]:
    """Vary the two parameters nobody validated, and report the whole surface.

    A single headline number hides whether a result is a plateau or a spike. If
    neighbouring settings collapse, the configuration was fitted to noise.
    """
    rows = []
    for exit_rank in (1.01, 0.80, 0.50):
        for max_hold in (5, 10, 20):
            p = Params(exit_rank=exit_rank, max_hold=max_hold)
            r = run(df, p, eval_start=eval_start)
            s = summarise(r["equity_curve"], r["trades"])
            exposure = r["exposure_bars"] / max(len(r["equity_curve"]), 1) * 100.0
            sealed = [x for x in r["equity_curve"] if x["date"] >= SEALED_START]
            sealed_ret = 0.0
            if len(sealed) > 1:
                sv = pd.Series([x["value"] for x in sealed])
                sealed_ret = float((sv.iloc[-1] / sv.iloc[0] - 1.0) * 100.0)
            rows.append({
                "exit": "不提前平仓" if exit_rank > 1 else f"回到 {exit_rank:.0%} 分位平仓",
                "hold": max_hold,
                "trades": s["total_trades"],
                "total": s["total_return_pct"],
                "mdd": s["max_drawdown_pct"],
                "sharpe": s["sharpe"],
                "exposure": round(exposure, 1),
                "sealed": round(sealed_ret, 2),
            })
    return rows


# --------------------------------------------------------------------------- #
#  Reporting helpers
# --------------------------------------------------------------------------- #
def yearly_returns(equity: list[dict]) -> dict[str, float]:
    """Calendar-year returns that chain exactly to the total.

    Each year is measured against the *previous year's closing equity*, not its
    own first bar. Using the year's own first bar drops the move across the New
    Year gap, and the yearly figures then no longer multiply back to the total
    return — a discrepancy that looks like a bug to anyone who checks.
    """
    s = pd.Series({p["date"]: p["value"] for p in equity})
    s.index = pd.to_datetime(s.index)
    out: dict[str, float] = {}
    prev_close: float | None = None
    for year, group in s.groupby(s.index.year):
        base = prev_close if prev_close is not None else float(group.iloc[0])
        out[str(year)] = round((float(group.iloc[-1]) / base - 1.0) * 100.0, 2)
        prev_close = float(group.iloc[-1])
    return out


def max_drawdown(equity: list[dict]) -> float:
    v = np.array([p["value"] for p in equity], dtype=float)
    peak = np.maximum.accumulate(v)
    dd = (v - peak) / peak
    return float(dd.min() * 100.0)


def summarise(equity: list[dict], trades: list[dict]) -> dict:
    v = np.array([p["value"] for p in equity], dtype=float)
    rets = pd.Series(v).pct_change().dropna()
    years = max(len(v) / 244.0, 1e-9)
    total = v[-1] / v[0] - 1.0
    annual = (1.0 + total) ** (1.0 / years) - 1.0
    vol = float(rets.std(ddof=1) * np.sqrt(244)) if len(rets) > 1 else 0.0
    sharpe = float(annual / vol) if vol > 0 else 0.0
    wins = [t for t in trades if t["pnl"] > 0]
    return {
        "start": equity[0]["date"], "end": equity[-1]["date"],
        "initial": round(v[0], 2), "final": round(v[-1], 2),
        "total_return_pct": round(total * 100.0, 2),
        "annual_return_pct": round(annual * 100.0, 2),
        "max_drawdown_pct": round(max_drawdown(equity), 2),
        "sharpe": round(sharpe, 3),
        "win_rate_pct": round(len(wins) / len(trades) * 100.0, 2) if trades else 0.0,
        "total_trades": len(trades),
        "avg_pnl_pct": round(float(np.mean([t["pnl_pct"] for t in trades])), 4) if trades else 0.0,
        "avg_holding_bars": round(float(np.mean([t["holding_bars"] for t in trades])), 1) if trades else 0.0,
        "n_buys": len(trades),
        "n_sells": len(trades),
    }


# --------------------------------------------------------------------------- #
#  Main
# --------------------------------------------------------------------------- #
def main() -> int:
    import logging
    logging.getLogger().setLevel(logging.WARNING)

    df = load_prices(DATA_CSV)
    print(f"data: {len(df)} bars  {df['date'].iloc[0].date()} → {df['date'].iloc[-1].date()}")

    probe = df.copy()
    probe["mom5"] = probe["close"].pct_change(FACTOR_LOOKBACK)
    probe["rank"] = expanding_rank(probe["mom5"], min_periods=RANK_WARMUP)
    eval_start = probe.loc[probe["rank"].notna(), "date"].iloc[0]
    print(f"evaluation window: {eval_start.date()} → {df['date'].iloc[-1].date()} "
          f"(first {RANK_WARMUP} bars used as warmup)")

    params = Params()
    result = run(df, params, eval_start=eval_start)
    equity, trades = result["equity_curve"], result["trades"]
    stats = summarise(equity, trades)

    bench_curve, bench = buy_and_hold(df, eval_start, params=params)
    bench_stats = summarise(bench_curve, [])

    stats["exposure_pct"] = round(
        result["exposure_bars"] / max(len(equity), 1) * 100.0, 2)
    stats["benchmark_return_pct"] = round(bench["return_pct"], 2)
    stats["bench_mdd"] = bench_stats["max_drawdown_pct"]
    stats["bench_sharpe"] = bench_stats["sharpe"]
    stats["excess_vs_benchmark_pct"] = round(stats["total_return_pct"] - bench["return_pct"], 2)
    stats["skipped_limit"] = result["skipped_limit"]
    stats["skipped_cash"] = result["skipped_cash"]

    print(f"\nstrategy : {stats['total_return_pct']:+.2f}%  "
          f"annual {stats['annual_return_pct']:+.2f}%  "
          f"MDD {stats['max_drawdown_pct']:.2f}%  Sharpe {stats['sharpe']:.2f}")
    print(f"buy&hold : {bench['return_pct']:+.2f}%")
    print(f"trades {stats['total_trades']}  win {stats['win_rate_pct']:.1f}%  "
          f"avg {stats['avg_pnl_pct']:+.3f}%  exposure {stats['exposure_pct']:.1f}%")
    print(f"excess vs buy&hold: {stats['excess_vs_benchmark_pct']:+.2f}pp")

    yearly = yearly_returns(equity)
    bench_yearly = yearly_returns(bench_curve)
    print("\nyear  strategy  buy&hold")
    for y in sorted(yearly):
        print(f"{y}  {yearly[y]:+8.2f}%  {bench_yearly.get(y, 0.0):+8.2f}%")

    sealed = [p for p in equity if p["date"] >= SEALED_START]
    if len(sealed) > 1:
        sealed_trades = [t for t in trades if t["entry_date"] >= SEALED_START]
        s = pd.Series([p["value"] for p in sealed])
        print(f"\nsealed window ({SEALED_START} →): "
              f"{(s.iloc[-1] / s.iloc[0] - 1) * 100:+.2f}%  "
              f"{len(sealed_trades)} trades  "
              f"avg {np.mean([t['pnl_pct'] for t in sealed_trades]):+.3f}%"
              if sealed_trades else
              f"\nsealed window ({SEALED_START} →): no trades")
        stats["sealed_return_pct"] = round(float((s.iloc[-1] / s.iloc[0] - 1) * 100), 2)
        stats["sealed_trades"] = len(sealed_trades)
        b = [p for p in bench_curve if p["date"] >= SEALED_START]
        if len(b) > 1:
            bs = pd.Series([p["value"] for p in b])
            stats["sealed_benchmark_pct"] = round(float((bs.iloc[-1] / bs.iloc[0] - 1) * 100), 2)

    # ---- parameter surface (nobody validated these two knobs) ------------- #
    sens = run_sensitivity(df, eval_start)
    print("\n参数敏感性（未参与筛选的两个自由度）:")
    print(f"{'平仓方式':<20}{'持有':>5}{'笔数':>6}{'收益%':>9}{'回撤%':>9}"
          f"{'Sharpe':>8}{'持仓%':>7}{'封存段%':>9}")
    for s in sens:
        print(f"{s['exit']:<20}{s['hold']:>5}{s['trades']:>6}{s['total']:>9.2f}"
              f"{s['mdd']:>9.2f}{s['sharpe']:>8.2f}{s['exposure']:>7.1f}{s['sealed']:>9.2f}")

    # ---- exports --------------------------------------------------------- #
    export_results(
        output_dir=OUT,
        equity_curve=equity,
        trade_history=trades,
        prefix=PREFIX,
        initial_cash=INITIAL_CASH,
        start=stats["start"],
        end=stats["end"],
        market="china_a",
        strategy_name="5日跌幅最大档买入（沪深300ETF 逆势）",
        symbol=SYMBOL,
    )
    print(f"\nexported {PREFIX}_equity.csv / {PREFIX}_trades.csv / {PREFIX}_summary.json")

    # ---- charts ---------------------------------------------------------- #
    make_charts(equity, bench_curve, trades, yearly, bench_yearly, result)

    # ---- dashboard ------------------------------------------------------- #
    render_html(equity, trades, stats, bench_curve, yearly, bench_yearly, result, bench, sens)

    with open(os.path.join(OUT, f"{PREFIX}_summary.json"), "r", encoding="utf-8") as fh:
        summary = json.load(fh)
    print(f"\nfinal: {summary['summary']['total_return_pct']}%  "
          f"trades {summary['summary']['total_trades']}")
    return 0


def make_charts(equity, bench_curve, trades, yearly, bench_yearly, result) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    # A CJK font must exist or every label renders as tofu boxes.
    for candidate in ("PingFang SC", "Hiragino Sans GB", "Heiti SC",
                      "Songti SC", "Arial Unicode MS", "STHeiti"):
        if any(f.name == candidate for f in font_manager.fontManager.ttflist):
            plt.rcParams["font.sans-serif"] = [candidate]
            break
    plt.rcParams["axes.unicode_minus"] = False

    dates = pd.to_datetime([p["date"] for p in equity])
    vals = np.array([p["value"] for p in equity], dtype=float)

    # 1. equity vs buy & hold
    fig, ax = plt.subplots(figsize=(11, 4.6))
    ax.plot(dates, vals, color="#c62828", lw=1.5, label="策略（5日跌幅档买入）")
    ax.plot(pd.to_datetime([p["date"] for p in bench_curve]),
            [p["value"] for p in bench_curve], color="#9e9e9e", lw=1.3, label="买入持有")
    ax.set_title("沪深300ETF：逆势策略 vs 买入持有（含手续费与滑点）")
    ax.set_ylabel("账户净值（元）")
    ax.legend(loc="upper left")
    ax.grid(alpha=0.25)
    fig.tight_layout()
    plt.show()
    fig.savefig(os.path.join(OUT, f"{PREFIX}_equity_vs_hold.png"), dpi=130)
    plt.close(fig)

    # 2. drawdown
    peak = np.maximum.accumulate(vals)
    dd = (vals - peak) / peak * 100
    fig, ax = plt.subplots(figsize=(11, 3.2))
    ax.fill_between(dates, dd, 0, color="#c62828", alpha=0.35)
    ax.plot(dates, dd, color="#c62828", lw=0.9)
    ax.set_title("策略回撤")
    ax.set_ylabel("%")
    ax.grid(alpha=0.25)
    fig.tight_layout()
    plt.show()
    fig.savefig(os.path.join(OUT, f"{PREFIX}_drawdown.png"), dpi=130)
    plt.close(fig)

    # 3. yearly comparison
    years = sorted(yearly)
    x = np.arange(len(years))
    fig, ax = plt.subplots(figsize=(9, 3.8))
    ax.bar(x - 0.2, [yearly[y] for y in years], width=0.38, color="#c62828", label="策略")
    ax.bar(x + 0.2, [bench_yearly.get(y, 0.0) for y in years], width=0.38,
           color="#9e9e9e", label="买入持有")
    ax.set_xticks(x)
    ax.set_xticklabels(years)
    ax.axhline(0, color="#333", lw=0.8)
    ax.set_title("分年收益对比")
    ax.set_ylabel("%")
    ax.legend()
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    plt.show()
    fig.savefig(os.path.join(OUT, f"{PREFIX}_yearly.png"), dpi=130)
    plt.close(fig)

    # 4. trade P&L distribution
    if trades:
        pnls = [t["pnl_pct"] for t in trades]
        fig, ax = plt.subplots(figsize=(9, 3.6))
        ax.hist(pnls, bins=32, color="#c62828", alpha=0.75, edgecolor="white")
        ax.axvline(0, color="#333", lw=1.0)
        ax.axvline(float(np.mean(pnls)), color="#1565c0", lw=1.4, ls="--",
                   label=f"均值 {np.mean(pnls):+.2f}%")
        ax.set_title(f"单笔收益分布（{len(pnls)} 笔）")
        ax.set_xlabel("%")
        ax.legend()
        ax.grid(axis="y", alpha=0.25)
        fig.tight_layout()
        plt.show()
        fig.savefig(os.path.join(OUT, f"{PREFIX}_trade_pnl.png"), dpi=130)
        plt.close(fig)

    print(f"charts → {PREFIX}_*.png")


def render_html(equity, trades, stats, bench_curve, yearly, bench_yearly, result, bench,
                sens) -> None:
    points = [{"date": p["date"], "value": p["value"]} for p in equity]
    overlay = [{"name": "买入持有", "stroke": "#9e9e9e",
                "points": [{"date": p["date"], "value": p["value"]} for p in bench_curve]}]

    cmp_rows = [
        {"metric": "累计收益", "values": [{"main": f"{stats['total_return_pct']:+.2f}%"},
                                          {"main": f"{bench['return_pct']:+.2f}%"}]},
        {"metric": "年化收益", "values": [{"main": f"{stats['annual_return_pct']:+.2f}%"},
                                          {"main": "—"}]},
        {"metric": "最大回撤", "values": [{"main": f"{stats['max_drawdown_pct']:.2f}%"},
                                          {"main": "—"}]},
        {"metric": "夏普", "values": [{"main": f"{stats['sharpe']:.2f}"},
                                      {"main": "—"}]},
        {"metric": "持仓时间占比", "values": [{"main": f"{stats['exposure_pct']:.1f}%"},
                                              {"main": "100%"}]},
        {"metric": "交易笔数", "values": [{"main": f"{stats['total_trades']}"},
                                          {"main": "1"}]},
    ]

    text_modules = [
        {"type": "text", "tab": "overview", "title": "这份报告是什么",
         "text": ("这是一次**因子验证后的落地回测**，不是又一次参数搜索。\n"
                  "先在沪深300ETF 上跑 21 类因子 × 5 个持有期共 105 个组合，"
                  "用分位数组合、置换检验、随机入场对照和多重检验校正筛一遍，"
                  "只有「过去 5 日跌幅最大的一档」通过。本页把这个唯一通过检验的规则"
                  "做成可下单的策略，并放在买入持有旁边，看它到底有没有额外贡献。")},
        {"type": "text", "tab": "overview", "title": "筛选阶段看到了什么",
         "text": ("- 105 个检验，Bonferroni 阈值 |t| ≈ 3.49\n"
                  "- 唯一通过：过去 5 日跌幅最低 20% 分位，持有 10 日，"
                  "样本内单笔 **+155.9bp**（扣 10bp 成本后 +145.9bp）\n"
                  "- 置换检验 p=0.007，过 BH-FDR\n"
                  "- 随机入场对照：信号 +155.9bp vs 随机 +26.4bp，**p=0.000** —— "
                  "这一步很重要，它证明收益不是「任何一天买入都涨」的漂移\n"
                  "- **但封存的最后 20% 只剩 +19.5bp**，命中率 58.3%。"
                  "统计上成立过的效应，强度衰减了一大截")},
        {"type": "text", "tab": "overview", "title": "交易口径",
         "text": (f"- 方向：仅做多（A 股个股与 ETF 不能裸卖空）\n"
                  f"- 信号：收盘后计算过去 5 日涨跌幅，位于历史最低 20% 分位即买入\n"
                  f"- 成交：**次日开盘价**，买入上浮、卖出下调各 {SLIPPAGE*1e4:.1f}bp 滑点\n"
                  f"- 费用：佣金单边 {COMMISSION*1e4:.1f}bp；ETF 免印花税、免过户费\n"
                  f"- 手数：100 份整数倍，零股不下单\n"
                  f"- T+1：买入当日不可卖出，最早次日\n"
                  f"- 平仓：**固定持有 {MAX_HOLD_BARS} 个交易日**（不做提前平仓，见下方敏感性）\n"
                  f"- 涨跌停：开盘触及 ±{PRICE_LIMIT:.0%} 时不成交"
                  f"（本次触发 {stats['skipped_limit']} 次）")},
        {"type": "text", "tab": "overview", "title": "筛选结论为什么不能直接当收益",
         "text": ("筛选报出「底部 20% 分位、持 10 日、单笔 +155.9bp」，但那个数字是**重叠样本**算的："
                  "市场连续下跌时，连续十几天每天都落在底部区间，每一次都共享同一段反弹，"
                  "同一笔反弹被数了十遍。\n"
                  "真正能下单的版本一笔只能进一次，而且进的是**下跌开始那天**，不是底部那天。"
                  "把 72 笔实际入场日按同样持有窗口重算，毛收益只有 **14.5bp**，"
                  "扣掉约 10bp 双边摩擦后所剩无几。\n"
                  "这不是代码错误，是因子研究最常见的口径陷阱："
                  "**信号日的平均前瞻收益，和策略能拿到的收益，不是一回事。**")},
        {"type": "text", "tab": "overview", "title": "结论",
         "text": (f"- 策略 {stats['total_return_pct']:+.2f}%，买入持有 "
                  f"{bench['return_pct']:+.2f}%，落后 "
                  f"{abs(stats['excess_vs_benchmark_pct']):.2f} 个百分点\n"
                  f"- 用了 {stats['exposure_pct']:.1f}% 的持仓时间，回撤 "
                  f"{stats['max_drawdown_pct']:.2f}%（买入持有 {stats['bench_mdd']:.2f}%）\n"
                  f"- 风险调整后同样没优势：夏普 {stats['sharpe']:.2f} vs "
                  f"买入持有 {stats['bench_sharpe']:.2f}\n"
                  f"- 封存段（{SEALED_START} 起）{stats.get('sealed_return_pct', 0):+.2f}%，"
                  f"买入持有 {stats.get('sealed_benchmark_pct', 0):+.2f}%\n"
                  f"- **最该看的是下面那张敏感性表**：同一个因子，"
                  f"把持有期从 10 天改成 5 天，收益从 {stats['total_return_pct']:+.2f}% 变成 +80.46%；"
                  f"改成 20 天是 +39.57%。被统计检验认证的那个设置，"
                  f"恰好是九种组合里最差的一个。\n"
                  f"- 一句话：**这个逆势因子在沪深300ETF 上不足以构成可交易的优势**，"
                  f"不是参数没调好，是参数面本身没有平台")},
    ]

    cmp_table = {"type": "metric_table", "tab": "overview",
                 "title": "策略 vs 买入持有",
                 "columns": ["指标", "逆势策略", "买入持有"],
                 "rows": cmp_rows}

    sens_table = {"type": "metric_table", "tab": "overview",
                  "title": "参数敏感性（筛选未验证的两个自由度）",
                  "columns": ["平仓方式", "持有天数", "笔数", "累计收益", "最大回撤",
                              "夏普", "持仓占比", "封存段收益"],
                  "rows": [{"metric": f"{s['exit']} / {s['hold']}日",
                            "values": [{"main": f"{s['total']:+.2f}%"},
                                       {"main": f"{s['mdd']:.2f}%"},
                                       {"main": f"{s['sharpe']:.2f}"},
                                       {"main": f"{s['exposure']:.1f}%"},
                                       {"main": f"{s['sealed']:+.2f}%"}]}
                           for s in sens]}

    year_rows = []
    for y in sorted(yearly):
        diff = yearly[y] - bench_yearly.get(y, 0.0)
        year_rows.append({"metric": f"{y} 年", "values": [
            {"main": f"{yearly[y]:+.2f}%"},
            {"main": f"{bench_yearly.get(y, 0.0):+.2f}%"},
            {"main": f"{diff:+.2f}pp"}]})
    year_table = {"type": "metric_table", "tab": "overview", "title": "分年收益",
                  "columns": ["年度", "逆势策略", "买入持有", "超额"],
                  "rows": year_rows}

    trade_table = {"type": "trades_table", "tab": "trades", "title": "逐笔明细",
                   "rows": trades}

    data = build_dashboard_data(
        equity_curve=points,
        trade_history=trades,
        summary={
            "total_return_pct": stats["total_return_pct"],
            "annual_return_pct": stats["annual_return_pct"],
            "max_drawdown_pct": stats["max_drawdown_pct"],
            "sharpe": stats["sharpe"],
            "win_rate_pct": stats["win_rate_pct"],
            "total_trades": stats["total_trades"],
        },
        meta={
            "strategy_name": "沪深300ETF 5日逆势策略",
            "symbol": SYMBOL,
            "start": stats["start"],
            "end": stats["end"],
            "initial_cash": INITIAL_CASH,
            "window_start_value": equity[0]["value"],
            "final_value": equity[-1]["value"],
            "market": "cn",
        },
        language="zh",
        extra_modules=text_modules,
    )

    for module in data.get("modules", []):
        if module.get("type") == "overview_chart":
            module["overlay_series"] = overlay
            break

    # The default module list ends with a full trade table, which would push the
    # narrative cards far below the fold. Keep chart + metrics, then our own.
    defaults = [m for m in data.get("modules", [])
                if m.get("type") in ("overview_chart", "metric_table")]
    data["modules"] = defaults + text_modules + [cmp_table, year_table, sens_table, trade_table]

    out = render_dashboard(data, output_path=os.path.join(OUT, "rev5_510300_dashboard.html"))
    print(f"dashboard → {os.path.abspath(out)}")


if __name__ == "__main__":
    sys.exit(main())
