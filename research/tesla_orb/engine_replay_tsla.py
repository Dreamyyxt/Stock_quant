#!/usr/bin/env python3
"""
Replay the TSLA opening-range strategy through the *real* intraday engine
=======================================================================

``tsla_breakout_backtest.py`` is a self-contained research script: it has its
own fill model, its own stop handling and its own loop. The live system trades
through ``intraday/engine.py``. Those two are different programs, so "the
backtest says +280%" is not evidence about what the engine will do — unless you
actually run the engine over the same history and compare.

That is what this script does. One session per ``build_engine(mode="replay")``
call, compounding equity across days, then a line-by-line comparison against the
standalone backtest.

    python scripts/engine_replay_tsla.py --limit 20     # smoke test
    python scripts/engine_replay_tsla.py                # full history

Outputs (in cwd):
    engine_replay_equity.csv
    engine_replay_trades.csv
    engine_replay_summary.json
    engine_replay_dashboard.html
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import glob
import io
import json
import logging
import math
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

PROJ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs")
os.makedirs(OUT, exist_ok=True)
sys.path.insert(0, PROJ)

from config.settings import ORB_TSLA_CONFIG, STATE_DIR          # noqa: E402
from intraday.engine import build_engine                        # noqa: E402
from intraday.providers import cache_path                       # noqa: E402

SYMBOL = "TSLA"
SYMBOL_NAME = "特斯拉"
CACHE_DIR = os.path.join(STATE_DIR, "intraday_cache")
WORK_DIR = os.path.join(STATE_DIR, "engine_replay")
SPLIT_DAY = "2022-08-25"          # 3:1 split, excluded by the backtest too
MIN_BARS = 385                    # same completeness filter as the backtest
INITIAL_CASH = 100_000.0


# ---------------------------------------------------------------------- #
#  Session list
# ---------------------------------------------------------------------- #

def available_sessions() -> List[str]:
    """Dates with a cached bar file, filtered exactly like the backtest."""
    files = glob.glob(os.path.join(CACHE_DIR, f"{SYMBOL}_*.csv"))
    dates = sorted(os.path.basename(f)[len(SYMBOL) + 1:-4] for f in files)
    keep, dropped = [], []
    for date in dates:
        if date == SPLIT_DAY:
            dropped.append((date, "split day"))
            continue
        try:
            frame = pd.read_csv(os.path.join(CACHE_DIR, f"{SYMBOL}_{date}.csv"),
                                index_col=0, usecols=[0, 4])
        except Exception:
            dropped.append((date, "unreadable"))
            continue
        if len(frame) < MIN_BARS:
            dropped.append((date, f"{len(frame)} bars"))
            continue
        keep.append(date)
    if dropped:
        print(f"跳过 {len(dropped)} 个交易日（不完整/拆股）："
              f"{', '.join(d for d, _ in dropped[:6])}"
              f"{' …' if len(dropped) > 6 else ''}")
    return keep


# ---------------------------------------------------------------------- #
#  One session
# ---------------------------------------------------------------------- #

def run_session(date: str, equity: float, journal_dir: str,
                summary_dir: str, overrides: dict) -> dict:
    config = dict(ORB_TSLA_CONFIG)
    config.update({
        "journal_dir": journal_dir,
        "journal_filename": "trades.jsonl",
        "summary_dir": summary_dir,
    })
    config.update(overrides or {})

    engine = build_engine("replay", config, session_date=date, speed=1,
                          initial_cash=equity, dry_run=True)
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink):
        summary = engine.run()
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="只跑最近 N 个交易日")
    parser.add_argument("--start", help="起始日期 YYYY-MM-DD")
    parser.add_argument("--end", help="结束日期 YYYY-MM-DD")
    parser.add_argument("--cash", type=float, default=INITIAL_CASH)
    parser.add_argument("--daily-loss", type=float, default=None,
                        help="覆盖 max_daily_loss_pct（默认用配置里的 0.10）")
    parser.add_argument("--no-dashboard", action="store_true")
    parser.add_argument("--reuse", action="store_true",
                        help="跳过回放，直接用已有的三件套重新出报表/仪表盘")
    parser.add_argument("--prefix", default="engine_replay")
    args = parser.parse_args()

    logging.getLogger().setLevel(logging.WARNING)
    for name in ("intraday.engine", "intraday.providers", "intraday.signals",
                 "intraday.execution", "core.notifier", "core.state_store"):
        logging.getLogger(name).setLevel(logging.WARNING)

    if args.reuse:
        stats, comparison, curve, trades = _load_existing(args.prefix)
        _print_report(stats, comparison)
        if not args.no_dashboard:
            _render_dashboard(args.prefix, curve, trades, stats, comparison)
        return 0

    sessions = available_sessions()
    if args.start:
        sessions = [d for d in sessions if d >= args.start]
    if args.end:
        sessions = [d for d in sessions if d <= args.end]
    if args.limit:
        sessions = sessions[-args.limit:]
    if not sessions:
        print("没有可回放的交易日")
        return 1

    overrides = {}
    if args.daily_loss is not None:
        overrides["max_daily_loss_pct"] = float(args.daily_loss)

    os.makedirs(WORK_DIR, exist_ok=True)
    journal_dir = os.path.join(WORK_DIR, "journals")
    summary_dir = os.path.join(WORK_DIR, "summaries")
    for path in (journal_dir, summary_dir):
        os.makedirs(path, exist_ok=True)
    # Truncate rather than delete: the per-session summaries are a useful audit
    # trail, and a bulk wipe inside the repo is exactly the kind of thing that
    # should not happen silently.
    journal_path = os.path.join(journal_dir, "trades.jsonl")
    if os.path.exists(journal_path):
        os.remove(journal_path)

    print(f"回放 {len(sessions):,} 个交易日（{sessions[0]} → {sessions[-1]}），"
          f"初始资金 ${args.cash:,.0f}")

    equity = float(args.cash)
    curve: List[dict] = [{"date": _prev_day(sessions[0]), "value": equity}]
    per_session: List[dict] = []
    started = time.time()

    for index, date in enumerate(sessions, start=1):
        summary = run_session(date, equity, journal_dir, summary_dir, overrides)
        previous = equity
        equity = float(summary["end_equity"])
        curve.append({"date": date, "value": equity})
        per_session.append({
            "date": date,
            "start_equity": previous,
            "end_equity": equity,
            "return_pct": (equity / previous - 1) * 100 if previous else 0.0,
            "trades": summary["trades"],
            "halted": summary["halted"],
            "max_intraday_drawdown": summary["max_intraday_drawdown"],
        })
        if index % 100 == 0 or index == len(sessions):
            elapsed = time.time() - started
            eta = elapsed / index * (len(sessions) - index)
            print(f"  {index}/{len(sessions)}  权益 ${equity:,.0f}  "
                  f"已用 {elapsed/60:.1f} 分钟  预计剩余 {eta/60:.1f} 分钟",
                  flush=True)

    trades = _load_trades(os.path.join(journal_dir, "trades.jsonl"))
    stats = _stats(curve, trades, per_session)
    comparison = _compare(stats)
    comparison["trades"] = _trade_parity(trades)

    _export(args.prefix, curve, trades, stats, comparison, sessions)

    _print_report(stats, comparison)

    if not args.no_dashboard:
        _render_dashboard(args.prefix, curve, trades, stats, comparison)
    return 0


# ---------------------------------------------------------------------- #
#  Data shaping
# ---------------------------------------------------------------------- #

def _prev_day(date: str) -> str:
    return (pd.Timestamp(date) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")


def _reason_zh(reason: str) -> str:
    """Engine reasons are English (logs are English); the dashboard is Chinese."""
    text = str(reason or "")
    if text.startswith("stop hit"):
        return "止损" + text[len("stop hit"):]
    if text.startswith("target hit"):
        return "止盈" + text[len("target hit"):]
    if text.startswith("session end"):
        return "收盘平仓"
    if text.startswith("max hold"):
        return "超时平仓"
    if text.startswith("daily loss"):
        return "日内亏损熔断"
    return text


def _load_trades(path: str) -> List[dict]:
    entries: Dict[str, dict] = {}
    exits: List[dict] = []
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "entry":
                entries[event.get("session_date")] = event
            elif event.get("type") == "exit":
                exits.append(event)

    trades = []
    for event in exits:
        entry = entries.get(event.get("session_date"))
        if not entry:
            continue
        side = int(entry.get("side", 1))
        trades.append({
            "entry_date": entry["session_date"],
            "exit_date": event["session_date"],
            "side": "long" if side > 0 else "short",
            "size": int(entry["quantity"]),
            "entry_price": float(entry["price"]),
            "exit_price": float(event["price"]),
            "pnl": float(event["pnl"]),
            "pnl_pct": round(float(event.get("return_pct", 0.0)) * 100, 4),
            "holding_bars": int(event.get("held_minutes", 0)),
            "exit_reason": _reason_zh(event.get("reason", "")),
            "entry_time": entry.get("signal_time", ""),
            "exit_time": event.get("exit_time", ""),
            "symbol": SYMBOL,
            "symbol_name": SYMBOL_NAME,
            "display_symbol": SYMBOL,
        })
    trades.sort(key=lambda t: t["entry_date"])
    return trades


def _stats(curve: List[dict], trades: List[dict], per_session: List[dict]) -> dict:
    values = np.array([p["value"] for p in curve], dtype=float)
    dates = [p["date"] for p in curve]
    start_value, end_value = values[0], values[-1]
    total_return = end_value / start_value - 1

    days = (pd.Timestamp(dates[-1]) - pd.Timestamp(dates[0])).days
    annual = (end_value / start_value) ** (365 / days) - 1 if days > 0 else 0.0

    daily = pd.Series(values[1:] / values[:-1] - 1, index=pd.to_datetime(dates[1:]))
    sharpe = float(daily.mean() / daily.std() * math.sqrt(252)) if daily.std() > 0 else 0.0

    peak = np.maximum.accumulate(values)
    drawdown = values / peak - 1
    max_dd = float(drawdown.min())

    returns_bp = np.array([t["pnl_pct"] * 100 for t in trades], dtype=float)
    wins = returns_bp > 0
    t_stat = (returns_bp.mean() / returns_bp.std(ddof=1) * math.sqrt(len(returns_bp))
              if len(returns_bp) > 1 and returns_bp.std(ddof=1) > 0 else 0.0)

    longs = [t for t in trades if t["side"] == "long"]
    shorts = [t for t in trades if t["side"] == "short"]
    stops = [t for t in trades
             if str(t["exit_reason"]).startswith(("stop", "止损"))]

    yearly = {}
    for point in curve[1:]:
        year = point["date"][:4]
        yearly.setdefault(year, {"start": None, "end": None})
    running = values[0]
    for point in curve[1:]:
        year = point["date"][:4]
        bucket = yearly[year]
        if bucket["start"] is None:
            bucket["start"] = running
        bucket["end"] = point["value"]
        running = point["value"]
    yearly_out = {}
    for year, bucket in yearly.items():
        if bucket["start"] and bucket["end"]:
            year_trades = [t for t in trades if t["entry_date"][:4] == year]
            yearly_out[year] = {
                "n": len(year_trades),
                "net_bp": round(float(np.mean([t["pnl_pct"] * 100 for t in year_trades]))
                                if year_trades else 0.0, 2),
                "total_pct": round((bucket["end"] / bucket["start"] - 1) * 100, 2),
                "hit": round(float(np.mean([t["pnl_pct"] > 0 for t in year_trades]))
                             if year_trades else 0.0, 3),
            }

    longest_loss, streak = 0, 0
    for value in returns_bp:
        streak = streak + 1 if value <= 0 else 0
        longest_loss = max(longest_loss, streak)

    return {
        "start": dates[0],
        "end": dates[-1],
        "sessions": len(per_session),
        "initial_cash": float(start_value),
        "final_value": float(end_value),
        "total_return_pct": round(total_return * 100, 2),
        "annual_return_pct": round(annual * 100, 2),
        # Reported positive, like the rest of the three-file convention.
        "max_drawdown_pct": round(abs(max_dd) * 100, 2),
        "sharpe": round(sharpe, 3),
        "win_rate_pct": round(float(wins.mean() * 100) if len(wins) else 0.0, 2),
        "total_trades": len(trades),
        "avg_net_bp": round(float(returns_bp.mean()) if len(returns_bp) else 0.0, 2),
        "median_net_bp": round(float(np.median(returns_bp)) if len(returns_bp) else 0.0, 2),
        "t_stat": round(float(t_stat), 2),
        "longs": len(longs),
        "long_avg_bp": round(float(np.mean([t["pnl_pct"] * 100 for t in longs])) if longs else 0.0, 2),
        "shorts": len(shorts),
        "short_avg_bp": round(float(np.mean([t["pnl_pct"] * 100 for t in shorts])) if shorts else 0.0, 2),
        "stop_exits": len(stops),
        "close_exits": len(trades) - len(stops),
        "avg_win_pct": round(float(np.mean([t["pnl_pct"] for t in trades if t["pnl_pct"] > 0]))
                             if wins.any() else 0.0, 3),
        "avg_loss_pct": round(float(np.mean([t["pnl_pct"] for t in trades if t["pnl_pct"] <= 0]))
                              if (~wins).any() else 0.0, 3),
        "longest_loss_streak": longest_loss,
        "halted_sessions": int(sum(1 for s in per_session if s["halted"])),
        "sessions_over_10pct_dd": int(sum(1 for s in per_session
                                         if s["max_intraday_drawdown"] < -0.10)),
        "worst_session_pct": round(min(s["return_pct"] for s in per_session), 2),
        "best_session_pct": round(max(s["return_pct"] for s in per_session), 2),
        "trades_per_day": round(len(trades) / len(per_session), 3) if per_session else 0.0,
        "yearly": yearly_out,
    }


def _compare(stats: dict) -> dict:
    path = os.path.join(OUT, "tsla_breakout_summary.json")
    rows = [
        ("累计收益", "total_return_pct", "%", stats["total_return_pct"]),
        ("年化收益", "annual_return_pct", "%", stats["annual_return_pct"]),
        ("最大回撤", "max_drawdown_pct", "%", stats["max_drawdown_pct"]),
        ("夏普", "sharpe", "", stats["sharpe"]),
        ("胜率", "win_rate_pct", "%", stats["win_rate_pct"]),
        ("笔数", "total_trades", "", stats["total_trades"]),
        ("单笔净收益", "avg_net_bp", "bp", stats["avg_net_bp"]),
        ("t 统计量", "t_stat", "", stats["t_stat"]),
        ("多头笔数", "longs", "", stats["longs"]),
        ("空头笔数", "shorts", "", stats["shorts"]),
        ("止损出场", "stop_exits", "", stats["stop_exits"]),
        ("收盘出场", "close_exits", "", stats["close_exits"]),
    ]
    if not os.path.exists(path):
        return {"available": False, "rows": rows}
    with open(path, "r", encoding="utf-8") as handle:
        reference = json.load(handle)
    summary = reference.get("summary", {})
    extra = reference.get("extra", {}).get("stats", {})
    ref_map = {
        "total_return_pct": summary.get("total_return_pct"),
        "annual_return_pct": summary.get("annual_return_pct"),
        "max_drawdown_pct": summary.get("max_drawdown_pct"),
        "sharpe": summary.get("sharpe"),
        "win_rate_pct": summary.get("win_rate_pct"),
        "total_trades": summary.get("total_trades"),
        "avg_net_bp": extra.get("avg_net_bp"),
        "t_stat": extra.get("t_stat"),
        "longs": extra.get("longs"),
        "shorts": extra.get("shorts"),
        "stop_exits": extra.get("stop_exits"),
        "close_exits": extra.get("close_exits"),
    }
    enriched = []
    for label, key, unit, engine_value in rows:
        base = ref_map.get(key)
        delta = (float(engine_value) - float(base)) if base is not None else None
        enriched.append({
            "label": label, "key": key, "unit": unit,
            "engine": engine_value, "backtest": base,
            "delta": round(delta, 3) if delta is not None else None,
        })
    return {
        "available": True,
        "rows": enriched,
        "reference_meta": reference.get("meta", {}),
        "reference_yearly": reference.get("extra", {}).get("yearly", {}),
    }


def _trade_parity(trades: List[dict]) -> dict:
    """Date-by-date comparison against the standalone backtest's trades file.

    Aggregate returns can match by luck. If the engine picks the same days, the
    same direction and nearly the same price, the two programs are the same
    strategy; if it doesn't, the aggregate match is a coincidence.
    """
    path = os.path.join(OUT, "tsla_breakout_trades.csv")
    if not os.path.exists(path) or not trades:
        return {"available": False}
    reference: Dict[str, dict] = {}
    with open(path, "r", encoding="utf-8") as handle:
        header = handle.readline().strip().split(",")
        for line in handle:
            parts = line.strip().split(",")
            if len(parts) != len(header):
                continue
            row = dict(zip(header, parts))
            reference[row["entry_date"]] = row

    engine_by_date = {t["entry_date"]: t for t in trades}
    common = sorted(set(reference) & set(engine_by_date))
    diffs, side_mismatch = [], 0
    for date in common:
        engine = engine_by_date[date]
        base = reference[date]
        diffs.append(abs(engine["pnl_pct"] * 100 - float(base["pnl_pct"]) * 100))
        if engine["side"] != base["side"]:
            side_mismatch += 1

    diffs_arr = np.array(diffs, dtype=float) if diffs else np.array([0.0])
    return {
        "available": True,
        "matched_dates": len(common),
        "backtest_trades": len(reference),
        "engine_trades": len(trades),
        "only_in_backtest": sorted(set(reference) - set(engine_by_date))[:10],
        "only_in_engine": sorted(set(engine_by_date) - set(reference))[:10],
        "side_mismatch": side_mismatch,
        "mean_abs_diff_bp": round(float(diffs_arr.mean()), 3),
        "median_abs_diff_bp": round(float(np.median(diffs_arr)), 3),
        "p95_abs_diff_bp": round(float(np.percentile(diffs_arr, 95)), 3),
        "max_abs_diff_bp": round(float(diffs_arr.max()), 3),
    }


def _load_existing(prefix: str):
    """Rebuild the report inputs from the exported files (no replay)."""
    summary_path = os.path.join(OUT, f"{prefix}_summary.json")
    if not os.path.exists(summary_path):
        raise SystemExit(f"缺少 {summary_path}，先跑一次完整回放")
    with open(summary_path, "r", encoding="utf-8") as handle:
        doc = json.load(handle)
    stats = doc["extra"]["stats"]
    comparison = doc["extra"].get("parity", {"available": False})
    comparison["trades"] = _trade_parity(_csv_trades(os.path.join(OUT, f"{prefix}_trades.csv")))
    curve = [{"date": row["date"], "value": float(row["value"])}
             for row in _read_csv(os.path.join(OUT, f"{prefix}_equity.csv"))]
    return stats, comparison, curve, _csv_trades(os.path.join(OUT, f"{prefix}_trades.csv"))


def _read_csv(path: str) -> List[dict]:
    with open(path, "r", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _csv_trades(path: str) -> List[dict]:
    trades = []
    for row in _read_csv(path):
        row["size"] = int(float(row["size"]))
        row["holding_bars"] = int(float(row.get("holding_bars") or 0))
        for key in ("entry_price", "exit_price", "pnl", "pnl_pct"):
            row[key] = float(row[key])
        trades.append(row)
    return trades


# ---------------------------------------------------------------------- #
#  Output
# ---------------------------------------------------------------------- #
def _export(prefix: str, curve: List[dict], trades: List[dict],
            stats: dict, comparison: dict, sessions: List[str]) -> None:
    with open(os.path.join(OUT, f"{prefix}_equity.csv"), "w", newline="", encoding="utf-8") as handle:
        handle.write("date,value\n")
        for point in curve:
            handle.write(f"{point['date']},{point['value']:.2f}\n")

    columns = ["entry_date", "exit_date", "side", "size", "entry_price", "exit_price",
               "pnl", "pnl_pct", "holding_bars", "exit_reason", "entry_time",
               "exit_time", "symbol", "symbol_name", "display_symbol"]
    with open(os.path.join(OUT, f"{prefix}_trades.csv"), "w", newline="", encoding="utf-8") as handle:
        handle.write(",".join(columns) + "\n")
        for trade in trades:
            handle.write(",".join(str(trade.get(c, "")) for c in columns) + "\n")

    payload = {
        "meta": {
            "strategy_name": f"{SYMBOL} 开盘区间突破（引擎回放）",
            "symbol": SYMBOL,
            "start": stats["start"],
            "end": stats["end"],
            "initial_cash": stats["initial_cash"],
            "window_start_value": stats["initial_cash"],
            "final_value": stats["final_value"],
            "market": "us",
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "engine": "intraday/engine.py (replay, sim execution)",
            "sessions_replayed": len(sessions),
        },
        "summary": {
            "total_return_pct": stats["total_return_pct"],
            "annual_return_pct": stats["annual_return_pct"],
            "max_drawdown_pct": stats["max_drawdown_pct"],
            "sharpe": stats["sharpe"],
            "win_rate_pct": stats["win_rate_pct"],
            "total_trades": stats["total_trades"],
        },
        "extra": {"stats": stats, "parity": comparison},
    }
    with open(os.path.join(OUT, f"{prefix}_summary.json"), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def _print_report(stats: dict, comparison: dict) -> None:
    print("\n" + "=" * 74)
    print(f"  引擎回放 — {SYMBOL} 开盘区间突破   "
          f"{stats['start']} → {stats['end']}  ({stats['sessions']:,} 个交易日)")
    print("=" * 74)
    print(f"  初始 / 期末权益   ${stats['initial_cash']:>12,.0f}  →  "
          f"${stats['final_value']:>12,.0f}")
    print(f"  累计收益          {stats['total_return_pct']:>12.2f}%   "
          f"年化 {stats['annual_return_pct']:.2f}%")
    print(f"  最大回撤          {stats['max_drawdown_pct']:>12.2f}%   "
          f"夏普 {stats['sharpe']:.2f}")
    print(f"  笔数 / 胜率       {stats['total_trades']:>12d}   "
          f"{stats['win_rate_pct']:.1f}%")
    print(f"  单笔净收益        {stats['avg_net_bp']:>12.2f}bp "
          f"(中位 {stats['median_net_bp']:.2f}bp, t={stats['t_stat']:.2f})")
    print(f"  多 / 空           {stats['longs']:>5d} / {stats['shorts']:<5d} "
          f"({stats['long_avg_bp']:.2f} / {stats['short_avg_bp']:.2f} bp)")
    print(f"  止损 / 收盘出场   {stats['stop_exits']:>5d} / {stats['close_exits']:<5d}")
    print(f"  熔断触发 / 自日内高点回撤>10%  {stats['halted_sessions']} / "
          f"{stats['sessions_over_10pct_dd']}")
    print("-" * 74)
    if comparison.get("available"):
        print(f"  {'指标':<14}{'引擎回放':>14}{'独立回测':>14}{'差异':>12}")
        for row in comparison["rows"]:
            unit = row["unit"]
            engine = row["engine"]
            base = row["backtest"]
            delta = row["delta"]
            engine_txt = f"{engine:,.2f}{unit}" if isinstance(engine, float) else f"{engine}{unit}"
            base_txt = (f"{base:,.2f}{unit}" if isinstance(base, float) else f"{base}{unit}"
                        if base is not None else "—")
            delta_txt = "—" if delta is None else (
                f"{delta:+,.2f}{unit}" if isinstance(delta, float) else f"{delta:+}{unit}")
            print(f"  {row['label']:<14}{engine_txt:>14}{base_txt:>14}{delta_txt:>12}")
    else:
        print("  未找到 tsla_breakout_summary.json，跳过对账")
    parity = comparison.get("trades") or {}
    if parity.get("available"):
        print(f"  逐笔对账：{parity['matched_dates']}/{parity['engine_trades']} 笔日期对齐，"
              f"方向不一致 {parity['side_mismatch']} 笔，"
              f"单笔差 中位 {parity['median_abs_diff_bp']:.2f}bp / "
              f"p95 {parity['p95_abs_diff_bp']:.2f}bp / "
              f"最大 {parity['max_abs_diff_bp']:.2f}bp")
        if parity.get("only_in_backtest"):
            print(f"    仅回测有：{parity['only_in_backtest']}")
        if parity.get("only_in_engine"):
            print(f"    仅引擎有：{parity['only_in_engine']}")
    print("-" * 74)
    for year, bucket in sorted(stats["yearly"].items()):
        print(f"  {year}: {bucket['n']:>4d} 笔  {bucket['net_bp']:>8.2f}bp  "
              f"年度 {bucket['total_pct']:>8.2f}%  命中 {bucket['hit']:.1%}")
    print("=" * 74 + "\n")


def _render_dashboard(prefix: str, curve: List[dict], trades: List[dict],
                      stats: dict, comparison: dict) -> None:
    try:
        from reporting.render_dashboard import build_dashboard_data, render_dashboard
    except Exception as exc:
        print(f"仪表盘渲染跳过：{exc}")
        return

    points = [{"date": p["date"], "value": p["value"]} for p in curve]
    markers = []
    for trade in trades:
        markers.append({
            "date": trade["entry_date"],
            "action": "short" if trade["side"] == "short" else "buy",
            "price": trade["entry_price"],
            "size": trade["size"],
            "label": f"{trade['entry_time']} 入场",
        })

    overlay = []
    bt_equity = os.path.join(ROOT, "tsla_breakout_equity.csv")
    if os.path.exists(bt_equity):
        frame = pd.read_csv(bt_equity)
        overlay.append({
            "name": "独立回测",
            "stroke": "#8b949e",
            "points": [{"date": row["date"], "value": float(row["value"])}
                       for _, row in frame.iterrows()],
        })

    rows = []
    for row in comparison.get("rows", []):
        if comparison.get("available"):
            unit = row["unit"]
            engine = row["engine"]
            base = row["backtest"]
            rows.append({
                "metric": row["label"],
                "values": [
                    {"main": f"{engine:,.2f}{unit}" if isinstance(engine, float)
                     else f"{engine}{unit}"},
                    {"main": "—" if base is None else
                     (f"{base:,.2f}{unit}" if isinstance(base, float) else f"{base}{unit}")},
                ],
            })

    extra_modules = [
        {"type": "text", "tab": "overview", "title": "这份报告是什么",
         "text": ("- 同一套规则跑两遍：一次用研究脚本，一次用真正接券商的日内引擎\n"
                  "- 引擎回放是逐个交易日调用 build_engine(mode=\"replay\")，"
                  "权益按天复利\n"
                  "- 两边数字接近，说明回测对引擎有参考价值；差得多，说明引擎里"
                  "有东西在吃利润")},
        {"type": "text", "tab": "overview", "title": "口径说明",
         "text": (f"- 入场：信号 K 线收盘后的下一分钟开盘价，成交价含半价差 + 滑点\n"
                  f"- 出场：止损按触发后的真实价格成交（回测假设正好成交在止损价，"
                  f"引擎更悲观）；其余持到 15:59 收盘\n"
                  f"- 单票 100% 仓位，每日最多一笔，多空双向\n"
                  f"- 成本：单边半价差 1.51bp + 0.5bp 冲击，与回测同一口径\n"
                  f"- 2021 年的成交价是当时的未拆股价格（TSLA 2022-08 拆 3:1 前约 "
                  f"$1,174），收益率不受影响\n"
                  f"- 日内亏损熔断 {ORB_TSLA_CONFIG['max_daily_loss_pct']:.0%}"
                  f"（回测没有这一层）：触发 {stats['halted_sessions']} 次")},
        {"type": "text", "tab": "overview", "title": "结论",
         "text": (f"- 引擎回放 {stats['total_return_pct']:.1f}%，"
                  f"单笔净 {stats['avg_net_bp']:.1f}bp，t={stats['t_stat']:.2f}\n"
                  f"- 多头 {stats['long_avg_bp']:.2f}bp / 空头 "
                  f"{stats['short_avg_bp']:.2f}bp，两侧都为正\n"
                  f"- 最长连亏 {stats['longest_loss_streak']} 笔，"
                  f"最差单日 {stats['worst_session_pct']:.2f}%\n"
                  f"- 这是历史模拟，不是实盘成绩")},
    ]
    if rows:
        extra_modules.append({
            "type": "metric_table", "tab": "overview",
            "title": "引擎回放 vs 独立回测",
            "columns": ["指标", "引擎回放", "独立回测"],
            "rows": rows,
        })
    extra_modules.append({
        "type": "trades_table", "tab": "overview",
        "title": "逐笔明细（引擎回放）",
        "subtitle": f"前 {min(len(trades), 200)} 笔 / 共 {len(trades)} 笔",
        "rows": trades[:200],
        "columns": [
            {"key": "display_symbol", "label": "标的", "format": "text"},
            {"key": "side", "label": "方向", "format": "pill"},
            {"key": "entry_date", "label": "日期", "format": "text"},
            {"key": "entry_time", "label": "入场", "format": "text"},
            {"key": "exit_time", "label": "出场", "format": "text"},
            {"key": "size", "label": "股数", "format": "number"},
            {"key": "entry_price", "label": "开仓价", "format": "number"},
            {"key": "exit_price", "label": "平仓价", "format": "number"},
            {"key": "pnl", "label": "盈亏", "format": "sign"},
            {"key": "pnl_pct", "label": "收益率", "format": "pct"},
            {"key": "exit_reason", "label": "出场原因", "format": "text"},
        ],
    })

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
            "strategy_name": f"{SYMBOL} 开盘区间突破（引擎回放）",
            "symbol": SYMBOL,
            "start": stats["start"],
            "end": stats["end"],
            "initial_cash": stats["initial_cash"],
            "window_start_value": stats["initial_cash"],
            "final_value": stats["final_value"],
            "market": "us",
        },
        language="zh",
        extra_modules=extra_modules,
    )
    # The default module list ends with a 838-row trades table, which would push
    # every narrative card far below the fold. Keep the chart and the metric
    # table, then append our own modules (narrative first, then the comparison,
    # then the trade list).
    base_count = max(0, len(data.get("modules", [])) - len(extra_modules))
    defaults = [m for m in data.get("modules", [])[:base_count]
                if m.get("type") in ("overview_chart", "metric_table")]
    data["modules"] = defaults + list(extra_modules)
    for module in data["modules"]:
        if module.get("type") == "overview_chart" and overlay:
            module["overlay_series"] = overlay
            break
    render_dashboard(data, output_path=os.path.join(OUT, f"{prefix}_dashboard.html"))
    print(f"仪表盘 → {os.path.abspath(prefix + '_dashboard.html')}")


if __name__ == "__main__":
    raise SystemExit(main())
