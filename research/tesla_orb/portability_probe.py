#!/usr/bin/env python3
"""portability_probe.py — ORB 参数可移植性探针

问题：现在这套开盘区间突破（30 分钟区间 / 2×区间止损 / 10:00–10:31 入场 /
持到收盘）是不是"放到任何股票都能用"？

做法：参数一个字不改，只换 universe，在几只不同波动特征的票上跑同一段
session，看每笔交易的平均收益有没有系统性差异。这是**冒烟测试**，不是
统计证据 —— 17 个 session 的样本量说明不了 alpha 是否存在，但足以说明
"同一组参数在不同标的上表现差异有多大"。

用法：.venv/bin/python research/tesla_orb/portability_probe.py
"""
from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import sys
from datetime import date, timedelta

import pandas as pd

PROJ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs")
sys.path.insert(0, PROJ)

from config.settings import ORB_TSLA_CONFIG, STATE_DIR          # noqa: E402
from intraday.engine import build_engine                        # noqa: E402
from intraday.providers import AlpacaBarProvider, cache_path    # noqa: E402

SYMBOLS = ["TSLA", "NVDA", "AMD", "AAPL", "SPY"]
START, END = "2026-09-29", "2026-10-23"
CASH = 100_000.0
PROBE_DIR = os.path.join(STATE_DIR, "portability_probe")


def weekdays(start: str, end: str) -> list[str]:
    days, cur = [], date.fromisoformat(start)
    stop = date.fromisoformat(end)
    while cur <= stop:
        if cur.weekday() < 5:
            days.append(cur.isoformat())
        cur += timedelta(days=1)
    return days


def ensure(symbol: str, session: str) -> bool:
    if os.path.exists(cache_path(symbol, session)):
        return True
    for feed in ("sip", "iex"):
        try:
            provider = AlpacaBarProvider([symbol], lookback=400,
                                        session_date=session, feed=feed)
            frame = provider.get_recent([symbol]).get(symbol)
        except Exception:
            continue
        if frame is not None and len(frame) >= 100:
            frame.to_csv(cache_path(symbol, session))
            return True
    return False


def run_one(symbol: str, session: str) -> list[dict]:
    journal_dir = os.path.join(PROBE_DIR, "journal", symbol)
    os.makedirs(journal_dir, exist_ok=True)
    journal = os.path.join(journal_dir, f"{session}.jsonl")
    if os.path.exists(journal):
        os.remove(journal)
    config = dict(ORB_TSLA_CONFIG)
    config.update({"universe": [symbol], "journal_dir": journal_dir,
                   "journal_filename": f"{session}.jsonl",
                   "summary_dir": os.path.join(PROBE_DIR, "summary")})
    engine = build_engine("replay", config, session_date=session, speed=1,
                          initial_cash=CASH, dry_run=True)
    with contextlib.redirect_stdout(io.StringIO()):
        engine.run()
    trades, entries = [], {}
    if not os.path.exists(journal):
        return trades
    for line in open(journal, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "entry":
            entries[ev.get("session_date")] = ev
        elif ev.get("type") == "exit":
            ent = entries.get(ev.get("session_date"), {})
            trades.append({
                "symbol": symbol, "date": ev.get("session_date"),
                "side": "long" if int(ent.get("side", 1) or 1) > 0 else "short",
                "return_pct": float(ev.get("return_pct", 0) or 0) * 100,
                "pnl": float(ev.get("pnl", 0) or 0),
                "reason": ev.get("reason", ""),
            })
    return trades


def main() -> int:
    logging.getLogger().setLevel(logging.ERROR)
    for name in ("intraday.engine", "intraday.providers", "intraday.signals",
                 "intraday.execution", "core.notifier", "core.state_store"):
        logging.getLogger(name).setLevel(logging.ERROR)

    sessions = weekdays(START, END)
    print(f"样本：{len(sessions)} 个交易日 {sessions[0]} → {sessions[-1]}，"
          f"{len(SYMBOLS)} 只标的，参数完全相同\n")

    rows, missing = [], {}
    for symbol in SYMBOLS:
        ok = [s for s in sessions if ensure(symbol, s)]
        gone = [s for s in sessions if s not in ok]
        if gone:
            missing[symbol] = gone
        for session in ok:
            rows.extend(run_one(symbol, session))
        print(f"[{symbol}] 拉数 {len(ok)}/{len(sessions)} 个 session", flush=True)

    df = pd.DataFrame(rows)
    os.makedirs(OUT, exist_ok=True)
    df.to_csv(os.path.join(OUT, "portability_probe_trades.csv"), index=False)

    print("\n标的     成交笔数  胜率     平均每笔%   平均盈利%  平均亏损%   最长连亏  净盈亏$")
    stats = []
    for symbol in SYMBOLS:
        sub = df[df.symbol == symbol] if len(df) else pd.DataFrame()
        if not len(sub):
            print(f"{symbol:8s} 0         —        —           —          —          —         —")
            continue
        wins = sub[sub.return_pct > 0]
        losses = sub[sub.return_pct <= 0]
        streak = mx = 0
        for r in sub.sort_values("date").return_pct:
            streak = streak + 1 if r <= 0 else 0
            mx = max(mx, streak)
        print(f"{symbol:8s} {len(sub):<9d} {len(wins)/len(sub)*100:6.1f}%  "
              f"{sub.return_pct.mean():+8.3f}  "
              f"{(wins.return_pct.mean() if len(wins) else 0):+8.3f}  "
              f"{(losses.return_pct.mean() if len(losses) else 0):+8.3f}  "
              f"{mx:<9d} {sub.pnl.sum():+9.2f}")
        stats.append({"symbol": symbol, "trades": len(sub),
                      "win_rate": round(len(wins) / len(sub) * 100, 2),
                      "avg_bp": round(sub.return_pct.mean() * 100, 2)})

    if missing:
        print("\n缺数（接口没给）：")
        for symbol, days in missing.items():
            print(f"  {symbol}: {len(days)} 个 {days[:5]}{'…' if len(days) > 5 else ''}")
    print(f"\n明细: {os.path.join(OUT, 'portability_probe_trades.csv')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
