#!/usr/bin/env python3
"""
TSLA 单票日内策略回测 — 开盘区间突破（Opening Range Breakout）

规则
----
1. 用开盘后前 30 分钟（09:30–10:00）的最高价与最低价，作为当日固定区间。
2. 10:30 之前，价格第一次收在区间之外时给出信号；成交在下一分钟开盘。
   向上突破做多，向下跌破做空，先破哪边就做哪边。
3. 止损放在「区间高度 × 2」的位置（约 2–4%），收盘前全部平仓，不留隔夜。
4. 每个交易日最多一笔，100% 资金（可用 --exposure 调整）。

运行
----
    python tsla_breakout_backtest.py
    python tsla_breakout_backtest.py --exposure 0.5
    python tsla_breakout_backtest.py --or-minutes 15 --stop-mult 1.0

数据
----
1 分钟 K 线（Alpaca 历史 SIP 全美合成行情），缓存在 state/tsla_1min.csv；
缺失时先运行 `python scripts/fetch_tsla_sessions.py`。

成本
----
state/tsla_cost.json 为实测买卖价差（中位 3.02 bp，半价差 1.51 bp），
每边按 半价差 + 0.5 bp 滑点计，一次往返约 4 bp。

输出
----
tsla_breakout_equity.csv / _trades.csv / _summary.json，index.html，以及 2 张 PNG。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

PROJ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs")
os.makedirs(OUT, exist_ok=True)
sys.path.insert(0, PROJ)

DATA_PATH = os.path.join(PROJ, "state", "tsla_1min.csv")
COST_PATH = os.path.join(PROJ, "state", "tsla_cost.json")

SYMBOL = "TSLA"
OPEN_MIN = 9 * 60 + 30
CLOSE_MIN = 15 * 60 + 59
LAST_BAR = 389          # 15:59 在 390 根常规时段 K 线中的下标

DEFAULT_RULE = "orb"


# ---------------------------------------------------------------------- #
#  数据
# ---------------------------------------------------------------------- #

def load_sessions(path: str = DATA_PATH, min_bars: int = 385) -> Dict[str, dict]:
    """按交易日切分钟线，前复权处理拆股，只保留常规时段。"""
    if not os.path.exists(path):
        raise SystemExit(
            f"缺少分钟数据 {path}\n"
            f"先运行：python scripts/fetch_tsla_sessions.py --symbol {SYMBOL}"
        )

    frame = pd.read_csv(path)
    # 跨夏令时的 UTC 偏移会让该列变成 object，先按 UTC 解析再转纽约时间
    frame["ts"] = pd.to_datetime(frame["timestamp"], utc=True).dt.tz_convert("America/New_York")
    frame["date"] = frame["ts"].dt.strftime("%Y-%m-%d")
    frame["minute"] = frame["ts"].dt.hour * 60 + frame["ts"].dt.minute
    frame = frame[(frame["minute"] >= OPEN_MIN) & (frame["minute"] <= CLOSE_MIN)]
    frame = frame.sort_values("ts")

    # 数据源是未复权价；拆股会在隔夜留下一个假的 -67% 跳空，
    # 会污染任何「相对前收盘」的规则与日线指标，所以这里做前复权。
    daily_close = frame.groupby("date")["close"].last()
    split_days, ratios = [], []
    for prev_date, cur_date in zip(daily_close.index, daily_close.index[1:]):
        ratio = daily_close[cur_date] / daily_close[prev_date]
        if ratio < 0.6:
            splits = round(1 / ratio)
            split_days.append(cur_date)
            ratios.append((cur_date, splits))
    scale = {d: 1.0 for d in daily_close.index}
    cumulative = 1.0
    for split_date, splits in reversed(ratios):
        cumulative *= splits
        for d in daily_close.index:
            if d < split_date:
                scale[d] = cumulative
    if ratios:
        print(f"检测到拆股并已复权：{ratios}")

    sessions: Dict[str, dict] = {}
    dropped = 0
    for date, group in frame.groupby("date", sort=True):
        if len(group) < min_bars or date in split_days:
            dropped += 1
            continue
        factor = scale.get(date, 1.0)
        sessions[date] = {
            "open": group["open"].to_numpy(dtype=float) / factor,
            "high": group["high"].to_numpy(dtype=float) / factor,
            "low": group["low"].to_numpy(dtype=float) / factor,
            "close": group["close"].to_numpy(dtype=float) / factor,
            "volume": group["volume"].to_numpy(dtype=float) * factor,
        }
    print(f"载入 {len(sessions):,} 个交易日（{min(sessions)} → {max(sessions)}），"
          f"跳过 {dropped} 个不完整/拆股日")
    return sessions


def load_cost(path: str = COST_PATH) -> float:
    """单边成本（bp）= 半价差 + 滑点。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            doc = json.load(handle)
        return float(doc.get("half_spread_median_bp", 1.5)) + float(doc.get("slippage_bp", 0.5))
    except Exception:
        return 2.0


def attach_trend(sessions: Dict[str, dict], sma_window: int) -> str:
    """
    可选：加一条日线趋势过滤（均线用前一交易日收盘算，开盘前已知）。
    返回第一个拥有有效趋势值的日期；sma_window=0 时不做过滤，返回首日。
    """
    dates = sorted(sessions)
    if sma_window <= 0:
        for date in dates:
            sessions[date]["trend"] = 0
        return dates[0]

    closes = pd.Series({d: float(sessions[d]["close"][-1]) for d in dates})
    sma = closes.rolling(sma_window).mean().shift(1)
    valid_start = dates[-1]
    for date in dates:
        value = sma.get(date)
        sessions[date]["trend"] = 0 if value is None or value != value else (
            1 if closes[date] >= value else -1)
        if sessions[date]["trend"] != 0 and date < valid_start:
            valid_start = date
    return valid_start


def last_index(session: dict) -> int:
    """个别交易日会少一两根 K 线，索引不能越界。"""
    return min(LAST_BAR, len(session["close"]) - 1)


# ---------------------------------------------------------------------- #
#  信号
# ---------------------------------------------------------------------- #

def signal_opening_range(session: dict, or_minutes: int, last_entry_minute: int,
                         use_trend: bool = False) -> Optional[dict]:
    """
    开盘区间突破。只使用信号当根及之前的数据，成交在下一根开盘。

    返回 entry_idx / direction / range_size（止损按区间高度计算）。
    """
    close, high, low = session["close"], session["high"], session["low"]
    n = min(or_minutes, last_index(session) - 1)
    or_high, or_low = float(high[:n].max()), float(low[:n].min())
    range_size = or_high - or_low
    if range_size <= 0:
        return None

    trend = session.get("trend", 0) if use_trend else 0
    allow_long = trend >= 0          # trend 0 表示不过滤
    allow_short = trend <= 0

    for idx in range(n - 1, last_index(session)):
        if OPEN_MIN + idx > last_entry_minute:
            return None
        if allow_long and close[idx] > or_high:
            return {"entry_idx": idx, "direction": 1, "range_size": range_size}
        if allow_short and close[idx] < or_low:
            return {"entry_idx": idx, "direction": -1, "range_size": range_size}
    return None


def signal_first_hour_momentum(session: dict, threshold: float) -> Optional[dict]:
    close = session["close"]
    idx = 60
    if idx >= last_index(session) - 1:
        return None
    ret = close[idx] / session["open"][0] - 1
    if abs(ret) < threshold:
        return None
    return {"entry_idx": idx, "direction": 1 if ret > 0 else -1, "range_size": 0.0}


def signal_gap_fade(session: dict, gap_pct: float, max_gap: float = 0.12) -> Optional[dict]:
    prev_close = session.get("prev_close")
    if not prev_close:
        return None
    gap = session["open"][0] / prev_close - 1
    if abs(gap) < gap_pct or abs(gap) > max_gap:
        return None
    return {"entry_idx": 2, "direction": -1 if gap > 0 else 1, "range_size": 0.0}


# ---------------------------------------------------------------------- #
#  撮合
# ---------------------------------------------------------------------- #

def simulate_trade(session: dict, signal: dict, cost_bp_side: float,
                   stop_mult: float, stop_pct_fixed: float,
                   target_pct: float, max_hold: int) -> Optional[dict]:
    """
    下一根开盘成交，之后按止损 / 止盈 / 持有时间 / 收盘了结。

    止损放在区间高度 × stop_mult 处；同一根内止损与止盈都触发时止损优先（保守）。
    """
    entry_idx = signal["entry_idx"] + 1
    if entry_idx >= last_index(session):
        return None
    direction = signal["direction"]
    entry_price = float(session["open"][entry_idx])
    if not np.isfinite(entry_price) or entry_price <= 0:
        return None

    stop_pct = stop_pct_fixed
    if signal.get("range_size"):
        stop_pct = max(stop_pct, signal["range_size"] * stop_mult / entry_price)

    high, low, close = session["high"], session["low"], session["close"]
    last_idx = last_index(session)
    exit_idx, exit_price, reason = last_idx, float(close[last_idx]), "close"

    for idx in range(entry_idx, last_idx + 1):
        if stop_pct > 0:
            if direction > 0 and low[idx] <= entry_price * (1 - stop_pct):
                exit_idx, exit_price, reason = idx, entry_price * (1 - stop_pct), "stop"
                break
            if direction < 0 and high[idx] >= entry_price * (1 + stop_pct):
                exit_idx, exit_price, reason = idx, entry_price * (1 + stop_pct), "stop"
                break
        if target_pct > 0:
            if direction > 0 and high[idx] >= entry_price * (1 + target_pct):
                exit_idx, exit_price, reason = idx, entry_price * (1 + target_pct), "target"
                break
            if direction < 0 and low[idx] <= entry_price * (1 - target_pct):
                exit_idx, exit_price, reason = idx, entry_price * (1 - target_pct), "target"
                break
        if max_hold > 0 and idx - entry_idx >= max_hold:
            exit_idx, exit_price, reason = idx, float(close[idx]), "time"
            break

    gross = direction * (exit_price / entry_price - 1)
    return {
        "entry_idx": entry_idx, "exit_idx": exit_idx, "direction": direction,
        "entry_price": entry_price, "exit_price": exit_price,
        "gross_ret": gross, "net_ret": gross - 2 * cost_bp_side / 1e4,
        "reason": reason,
    }


# ---------------------------------------------------------------------- #
#  回测
# ---------------------------------------------------------------------- #

def run(sessions: Dict[str, dict], rule: str, params: dict, valid_start: str) -> tuple:
    dates = [d for d in sorted(sessions) if d >= valid_start]
    equity = float(params["initial_cash"])
    equity_curve: List[dict] = []
    trades: List[dict] = []
    signals_seen = skipped_size = 0

    for date in dates:
        session = sessions[date]
        if rule == "orb":
            signal = signal_opening_range(session, params["or_minutes"],
                                          params["last_entry_minute"])
        elif rule == "orb_trend":
            signal = signal_opening_range(session, params["or_minutes"],
                                          params["last_entry_minute"], use_trend=True)
        elif rule == "first_hour":
            signal = signal_first_hour_momentum(session, params["momentum_threshold"])
        elif rule == "gap_fade":
            signal = signal_gap_fade(session, params["gap_pct"])
        else:
            raise ValueError(f"unknown rule: {rule}")

        if signal:
            signals_seen += 1
            fill = simulate_trade(session, signal, params["cost_bp_side"],
                                  params["stop_mult"], params["stop_pct"],
                                  params["target_pct"], params["max_hold"])
            if fill:
                shares = int(equity * params["exposure"] // fill["entry_price"])
                if shares > 0:
                    notional = shares * fill["entry_price"]
                    cost = notional * params["cost_bp_side"] / 1e4 * 2
                    pnl = (fill["direction"] * (fill["exit_price"] - fill["entry_price"])
                           * shares - cost)
                    equity += pnl
                    trades.append({
                        "entry_date": date, "exit_date": date,
                        "entry_time": f"{(OPEN_MIN + fill['entry_idx']) // 60:02d}:"
                                      f"{(OPEN_MIN + fill['entry_idx']) % 60:02d}",
                        "exit_time": f"{(OPEN_MIN + fill['exit_idx']) // 60:02d}:"
                                     f"{(OPEN_MIN + fill['exit_idx']) % 60:02d}",
                        "side": "long" if fill["direction"] > 0 else "short",
                        "symbol": SYMBOL, "symbol_name": "特斯拉", "size": shares,
                        "entry_price": round(fill["entry_price"], 2),
                        "exit_price": round(fill["exit_price"], 2),
                        "pnl": round(pnl, 2),
                        "pnl_pct": round(pnl / notional * 100, 4),
                        "holding_bars": fill["exit_idx"] - fill["entry_idx"],
                        "exit_reason": fill["reason"],
                    })
                else:
                    skipped_size += 1
        equity_curve.append({"date": date, "value": equity})

    pre_start = (pd.Timestamp(dates[0]) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    equity_curve.insert(0, {"date": pre_start, "value": float(params["initial_cash"])})
    return equity_curve, trades, signals_seen, pre_start, skipped_size


def buy_and_hold(sessions: Dict[str, dict], initial_cash: float) -> dict:
    dates = sorted(sessions)
    entry = float(sessions[dates[0]]["open"][0])
    final = float(sessions[dates[-1]]["close"][-1])
    shares = int(initial_cash // entry)
    peak, worst = 0.0, 0.0
    for date in dates:
        value = shares * float(sessions[date]["close"][-1])
        peak = max(peak, value)
        worst = min(worst, value / peak - 1)
    return {
        "entry_date": dates[0], "exit_date": dates[-1],
        "entry_price": round(entry, 2), "exit_price": round(final, 2),
        "shares": shares, "return_pct": (final / entry - 1) * 100,
        "max_drawdown_pct": worst * 100,
    }


def yearly_table(trades: List[dict]) -> Dict[str, dict]:
    table: Dict[str, list] = {}
    for trade in trades:
        table.setdefault(trade["entry_date"][:4], []).append(trade["pnl_pct"] / 100)
    return {year: {"n": len(vals), "net_bp": float(np.mean(vals) * 1e4),
                   "total_pct": float(np.cumprod(1 + np.array(vals))[-1] - 1) * 100,
                   "hit": float((np.array(vals) > 0).mean())}
            for year, vals in sorted(table.items())}


def main() -> int:
    parser = argparse.ArgumentParser(description="TSLA 开盘区间突破回测")
    parser.add_argument("--rule", default=DEFAULT_RULE,
                        choices=["orb", "orb_trend", "first_hour", "gap_fade"])
    parser.add_argument("--or-minutes", type=int, default=30)
    parser.add_argument("--last-entry", default="10:30")
    parser.add_argument("--stop-mult", type=float, default=2.0,
                        help="止损 = 开盘区间高度 × 该倍数")
    parser.add_argument("--stop-pct", type=float, default=0.0,
                        help="额外的固定止损（与区间止损取更大者）")
    parser.add_argument("--target-pct", type=float, default=0.0)
    parser.add_argument("--max-hold", type=int, default=0)
    parser.add_argument("--sma", type=int, default=200,
                        help="仅 orb_trend 使用：日线趋势均线长度")
    parser.add_argument("--exposure", type=float, default=1.0)
    parser.add_argument("--initial-cash", type=float, default=100_000)
    parser.add_argument("--momentum-threshold", type=float, default=0.01)
    parser.add_argument("--gap-pct", type=float, default=0.01)
    parser.add_argument("--prefix", default="tsla_breakout")
    parser.add_argument("--no-charts", action="store_true")
    parser.add_argument("--no-dashboard", action="store_true")
    args = parser.parse_args()

    hour, minute = (int(x) for x in args.last_entry.split(":"))
    sessions = load_sessions()
    cost_bp_side = load_cost()
    print(f"单边成本 {cost_bp_side:.2f} bp（往返 {2 * cost_bp_side:.2f} bp）")

    valid_start = attach_trend(sessions, args.sma if args.rule == "orb_trend" else 0)
    if args.rule == "orb_trend":
        print(f"趋势过滤 {args.sma} 日均线：前 {args.sma} 个交易日为预热，"
              f"评估起点 {valid_start}")

    params = {
        "initial_cash": args.initial_cash, "exposure": args.exposure,
        "cost_bp_side": cost_bp_side, "or_minutes": args.or_minutes,
        "last_entry_minute": hour * 60 + minute, "stop_mult": args.stop_mult,
        "stop_pct": args.stop_pct, "target_pct": args.target_pct,
        "max_hold": args.max_hold, "momentum_threshold": args.momentum_threshold,
        "gap_pct": args.gap_pct,
    }

    equity_curve, trades, signals_seen, pre_start, skipped = run(
        sessions, args.rule, params, valid_start)
    if not trades:
        print("没有产生任何交易，检查参数。")
        return 1

    dates = sorted(sessions)
    start, end = pre_start, dates[-1]

    from reporting.export_results import export_results
    export_results(
        equity_curve=equity_curve,
        trade_history=trades,
        prefix=args.prefix,
        output_dir=OUT,
        initial_cash=args.initial_cash,
        start=start, end=end,
        market="us",
        strategy_name="TSLA 开盘区间突破（ORB）",
        symbol=SYMBOL,
    )

    summary = json.load(open(os.path.join(OUT, f"{args.prefix}_summary.json"), encoding="utf-8"))
    bh = buy_and_hold({d: v for d, v in sessions.items() if d >= valid_start},
                      args.initial_cash)

    net = np.array([t["pnl_pct"] for t in trades]) / 100
    wins, losses = net[net > 0], net[net <= 0]
    stats = {
        "trades": len(trades), "signals": signals_seen, "skipped_size": skipped,
        "avg_net_bp": float(net.mean() * 1e4), "median_net_bp": float(np.median(net) * 1e4),
        "hit_rate": float((net > 0).mean()),
        "t_stat": float(net.mean() / (net.std(ddof=1) / np.sqrt(len(net)))) if len(net) > 1 else 0.0,
        "longs": sum(1 for t in trades if t["side"] == "long"),
        "shorts": sum(1 for t in trades if t["side"] == "short"),
        "avg_win_pct": float(wins.mean() * 100) if len(wins) else 0.0,
        "avg_loss_pct": float(losses.mean() * 100) if len(losses) else 0.0,
        "per_day": len(trades) / max(len([d for d in dates if d >= valid_start]), 1),
        "stop_exits": sum(1 for t in trades if t["exit_reason"] == "stop"),
        "close_exits": sum(1 for t in trades if t["exit_reason"] == "close"),
    }

    print("\n" + "=" * 64)
    print(f"策略 {args.rule}｜{start} → {end}｜"
          f"{len([d for d in dates if d >= valid_start])} 个交易日")
    print("=" * 64)
    print(f"总收益      {summary['summary']['total_return_pct']:>10.2f}%   "
          f"期末 ${summary['meta']['final_value']:,.0f}")
    print(f"年化        {summary['summary']['annual_return_pct']:>10.2f}%")
    print(f"最大回撤    {summary['summary']['max_drawdown_pct']:>10.2f}%")
    print(f"夏普        {summary['summary']['sharpe']:>10.2f}")
    print(f"交易        {stats['trades']:>10d}   多 {stats['longs']} / 空 {stats['shorts']}"
          f"   {stats['per_day']:.2f} 笔/日")
    print(f"胜率        {stats['hit_rate'] * 100:>10.1f}%   "
          f"均盈 {stats['avg_win_pct']:+.2f}% / 均亏 {stats['avg_loss_pct']:+.2f}%")
    print(f"单笔净收益  {stats['avg_net_bp']:>10.1f} bp  "
          f"(中位 {stats['median_net_bp']:.1f} bp, t={stats['t_stat']:.2f})")
    print(f"出场        止损 {stats['stop_exits']} / 收盘 {stats['close_exits']}")
    print(f"买入持有    {bh['return_pct']:>10.2f}%   "
          f"（同期回撤 {bh['max_drawdown_pct']:.1f}%）")
    print("=" * 64)
    print("分年：")
    for year, row in yearly_table(trades).items():
        print(f"  {year}  {row['n']:>4} 笔  {row['net_bp']:>+7.1f} bp  "
              f"胜率 {row['hit']*100:>4.1f}%  当年 {row['total_pct']:>+7.1f}%")

    payload = json.load(open(os.path.join(OUT, f"{args.prefix}_summary.json"), encoding="utf-8"))
    payload["extra"] = {"stats": stats, "buy_and_hold": bh, "rule": args.rule,
                        "yearly": yearly_table(trades),
                        "params": {k: v for k, v in params.items()}}
    with open(os.path.join(OUT, f"{args.prefix}_summary.json"), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)

    if not args.no_charts:
        make_charts(args.prefix, trades, stats, bh)
    if not args.no_dashboard:
        render_report(args.prefix, stats, bh)
    return 0


def make_charts(prefix: str, trades: List[dict], stats: dict, bh: dict) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["PingFang SC", "Arial Unicode MS", "Heiti SC", "STHeiti"]
    plt.rcParams["axes.unicode_minus"] = False

    equity = pd.read_csv(os.path.join(OUT, f"{prefix}_equity.csv"))
    equity["date"] = pd.to_datetime(equity["date"])
    base = equity["value"].iloc[0]
    equity["ret"] = (equity["value"] / base - 1) * 100
    equity["dd"] = (equity["value"] / equity["value"].cummax() - 1) * 100

    fig, axes = plt.subplots(2, 1, figsize=(11, 7.4),
                             gridspec_kw={"height_ratios": [2, 1]}, sharex=True)
    axes[0].plot(equity["date"], equity["ret"], lw=1.1, color="#2f6fd0")
    axes[0].axhline(0, color="#999", lw=0.8, ls="--")
    axes[0].set_ylabel("累计收益 (%)")
    axes[0].set_title(f"TSLA 开盘区间突破 — 累计收益"
                      f"（{equity['date'].iloc[0]:%Y-%m-%d} 起，{len(trades)} 笔）")
    axes[0].grid(alpha=0.25)
    axes[1].fill_between(equity["date"], equity["dd"], 0, color="#cc4444", alpha=0.4)
    axes[1].set_ylabel("回撤 (%)")
    axes[1].grid(alpha=0.25)
    fig.tight_layout()
    plt.show()
    fig.savefig(os.path.join(OUT, f"{prefix}_equity.png"), dpi=130)
    plt.close(fig)

    frame = pd.DataFrame(trades)
    pnl = frame["pnl_pct"].to_numpy()
    fig, axes = plt.subplots(1, 3, figsize=(13.5, 4.0))

    axes[0].hist(pnl, bins=60, color="#6aa84f", edgecolor="white")
    axes[0].axvline(0, color="#888", lw=0.8)
    axes[0].axvline(pnl.mean(), color="#cc0000", lw=1.2, ls="--",
                    label=f"均值 {pnl.mean():.3f}%")
    axes[0].set_title(f"单笔收益分布（n={len(pnl)}，胜率 {stats['hit_rate']*100:.1f}%）")
    axes[0].set_xlabel("单笔收益 (%)")
    axes[0].legend(fontsize=9)

    # Entry time only spans 09:45–10:30, so bucket by minute, not by hour.
    entry_min = (frame["entry_time"].str[:2].astype(int) * 60
                 + frame["entry_time"].str[3:].astype(int))
    frame["bucket"] = ((entry_min - 9 * 60 - 30) // 10).astype(int)
    labels = {0: "09:30-39", 1: "09:40-49", 2: "09:50-59", 3: "10:00-09",
              4: "10:10-19", 5: "10:20-29", 6: "10:30"}
    by_bucket = frame.groupby("bucket")["pnl_pct"].agg(["mean", "count"])
    axes[1].bar([labels.get(b, str(b)) for b in by_bucket.index],
                by_bucket["mean"], color="#4472c4")
    axes[1].axhline(0, color="#888", lw=0.8)
    axes[1].set_title("按入场时间的单笔平均收益")
    axes[1].set_xlabel("入场时间（美东）")
    axes[1].tick_params(axis="x", labelsize=8)
    for pos, (_, row) in enumerate(by_bucket.iterrows()):
        axes[1].text(pos, row["mean"], f"{int(row['count'])}", ha="center",
                     va="bottom" if row["mean"] >= 0 else "top", fontsize=7)

    frame["year"] = frame["entry_date"].str[:4]
    by_year = frame.groupby("year")["pnl_pct"].agg(["sum", "count"])
    axes[2].bar(by_year.index, by_year["sum"], color="#c05555")
    axes[2].axhline(0, color="#888", lw=0.8)
    axes[2].set_title("各年度累计收益（按单笔加总）")
    for year, row in by_year.iterrows():
        axes[2].text(year, row["sum"], f"{int(row['count'])}", ha="center",
                     va="bottom" if row["sum"] >= 0 else "top", fontsize=8)
    fig.tight_layout()
    plt.show()
    fig.savefig(os.path.join(OUT, f"{prefix}_trades.png"), dpi=130)
    plt.close(fig)
    print(f"图表 → {prefix}_equity.png, {prefix}_trades.png")


def render_report(prefix: str, stats: dict, bh: dict) -> None:
    from reporting.render_dashboard import build_dashboard_data, render_dashboard

    extra = [
        {"type": "text", "tab": "overview", "title": "策略逻辑",
         "text": ("用开盘后 30 分钟（09:30–10:00）的最高/最低价定出当日固定区间。"
                  "10:30 之前价格第一次收在区间之外即入场，成交在下一分钟开盘；"
                  "先破哪边做哪边（向上做多、向下做空）。止损放在区间高度的 2 倍处，"
                  "收盘前全部平仓，不留隔夜。")},
        {"type": "text", "tab": "overview", "title": "结果解读",
         "text": (f"{stats['trades']} 笔交易（多 {stats['longs']} / 空 {stats['shorts']}），"
                  f"单笔净收益均值 {stats['avg_net_bp']:.1f} bp（t={stats['t_stat']:.2f}），"
                  f"胜率 {stats['hit_rate']*100:.1f}%，均盈 {stats['avg_win_pct']:+.2f}% / "
                  f"均亏 {stats['avg_loss_pct']:+.2f}%。"
                  f"同期买入持有 TSLA 为 {bh['return_pct']:.1f}%"
                  f"（最大回撤 {bh['max_drawdown_pct']:.0f}%）。")},
        {"type": "text", "tab": "overview", "title": "已知偏差",
         "text": ("信号在分钟收盘成立、下一分钟开盘成交，未使用未来数据；成本按实测买卖价差计。"
                  "参数是同一段历史上选出的，存在选择性偏差（相邻参数 81 组中 77 组两半样本均为正，"
                  "说明是族内稳健而非单点巧合）。止损假设按止损价成交，遭遇跳空或流动性骤降时会更差；"
                  "未模拟融券费用、借钱利息与市场冲击。0.67 笔/日的频率对 2.5 万美元以下账户"
                  "会触发美股日内交易者（PDT）限制。")},
    ]
    report = build_dashboard_data(
        equity_csv=os.path.join(OUT, f"{prefix}_equity.csv"),
        trades_csv=os.path.join(OUT, f"{prefix}_trades.csv"),
        summary_json=os.path.join(OUT, f"{prefix}_summary.json"),
        language="zh",
        extra_modules=extra,
    )
    render_dashboard(report, output_path=os.path.join(OUT, "index.html"))
    print(f"仪表盘 → {OUT}/index.html")


if __name__ == "__main__":
    sys.exit(main())
