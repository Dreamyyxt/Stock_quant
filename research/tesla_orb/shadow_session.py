#!/usr/bin/env python3
"""shadow_session.py — TSLA ORB 影子盘（当日 session 实时记账）

与 engine_replay_tsla.py 的区别：那个是历史批量回放（完整 session），
这个是"今天的盘跑到哪儿算到哪儿"：

  1. 盘中运行：抓当日已完成的 1 分钟 bar（Alpaca，SIP 拒了自动降 IEX），
     用同一套真实引擎（intraday/engine.py, replay 模式）跑这个残缺 session，
     引擎会在最后一根 bar 平仓 —— 所以**盘中跑出来的只是快照**，
     收盘后（北京时间次日 04:05 起）必须重跑一次覆盖账本才是终值。
  2. 权益延续：默认接着历史回放曲线的终点（$367,694.35，2021-11→2026-10-23），
     账本 state/tsla_paper_book.json 按 session 日期 upsert，可重复运行。

用法：
    .venv/bin/python research/tesla_orb/shadow_session.py                # 今天(ET)
    .venv/bin/python research/tesla_orb/shadow_session.py --date 2026-10-26
    .venv/bin/python research/tesla_orb/shadow_session.py --cash 100000  # 重置起点

⚠️ 以上内容由 AI 基于公开信息整理生成，仅供参考，不构成任何投资建议或个股推荐。
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import os
import sys
from datetime import datetime, timezone

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs")
SHADOW_DIR = os.path.join(ROOT, "state", "tsla_paper")
BOOK = os.path.join(SHADOW_DIR, "tsla_paper_book.json")
sys.path.insert(0, ROOT)

from config.settings import ORB_TSLA_CONFIG                      # noqa: E402
from intraday.engine import build_engine                         # noqa: E402
from intraday.providers import (AlpacaBarProvider, cache_path,   # noqa: E402
                                ReplayBarProvider)

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    ET = None

SYMBOL = "TSLA"
REPLAY_FINAL_EQUITY = 367694.35     # 历史引擎回放 2026-10-23 收盘净值
SPLIT_DAY = "2022-08-25"


def default_today() -> str:
    now = datetime.now(ET) if ET else datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%d")


def ensure_bars(date: str) -> pd.DataFrame:
    """当日 bar 已在缓存就直接用；否则抓（SIP → IEX 降级），写入缓存。"""
    path = cache_path(SYMBOL, date)
    if os.path.exists(path):
        return pd.read_csv(path, index_col=0, parse_dates=True)
    frame = None
    for feed in ("sip", "iex"):
        provider = AlpacaBarProvider([SYMBOL], lookback=400, session_date=date, feed=feed)
        frame = provider.get_recent([SYMBOL]).get(SYMBOL)
        if frame is not None and len(frame):
            print(f"[data] {len(frame)} bars via {feed.upper()}")
            break
    if frame is None or not len(frame):
        raise SystemExit(f"[data] {date} 拿不到分钟线（休市日？接口挂了？）")
    frame.to_csv(path)
    return frame


def run_engine(date: str, start_equity: float) -> tuple[dict, str]:
    os.makedirs(SHADOW_DIR, exist_ok=True)
    journal_dir = os.path.join(SHADOW_DIR, "journals")
    summary_dir = os.path.join(SHADOW_DIR, "summaries")
    os.makedirs(journal_dir, exist_ok=True)
    os.makedirs(summary_dir, exist_ok=True)
    journal = os.path.join(journal_dir, f"trades_{date}.jsonl")
    if os.path.exists(journal):
        os.remove(journal)

    config = dict(ORB_TSLA_CONFIG)
    config.update({
        "journal_dir": journal_dir,
        "journal_filename": f"trades_{date}.jsonl",
        "summary_dir": summary_dir,
    })
    engine = build_engine("replay", config, session_date=date, speed=1,
                          initial_cash=start_equity, dry_run=True)
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink):
        summary = engine.run()
    return summary, journal


def parse_journal(path: str) -> list[dict]:
    entries, trades = {}, []
    if not os.path.exists(path):
        return trades
    with open(path, encoding="utf-8") as fh:
        for line in fh:
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
                side = int(ent.get("side", ev.get("side", 1)) or 1)
                trades.append({
                    "side": "long" if side > 0 else "short",
                    "qty": int(ent.get("quantity", ev.get("quantity", 0)) or 0),
                    "entry_price": float(ent.get("price", 0) or 0),
                    "entry_time": ent.get("signal_time", ""),
                    "exit_price": float(ev.get("price", 0) or 0),
                    "exit_time": ev.get("exit_time", ""),
                    "pnl": round(float(ev.get("pnl", 0) or 0), 2),
                    "return_pct": round(float(ev.get("return_pct", 0) or 0) * 100, 3),
                    "held_minutes": int(ev.get("held_minutes", 0) or 0),
                    "reason": ev.get("reason", ""),
                })
    trades.sort(key=lambda t: t["entry_time"])
    return trades


def load_book() -> dict:
    if os.path.exists(BOOK):
        return json.loads(open(BOOK, encoding="utf-8").read())
    return {"created_at": datetime.now().isoformat(timespec="seconds"),
            "seed_note": "起点 = 历史引擎回放(2021-11→2026-10-23)终点净值",
            "equity": REPLAY_FINAL_EQUITY, "sessions": []}


def main() -> int:
    ap = argparse.ArgumentParser(description="TSLA ORB 影子盘当日记账")
    ap.add_argument("--date", default=default_today())
    ap.add_argument("--cash", type=float, default=None,
                    help="覆盖账本净值（重置起点）")
    args = ap.parse_args()

    logging.getLogger().setLevel(logging.WARNING)
    for name in ("intraday.engine", "intraday.providers", "intraday.signals",
                 "intraday.execution", "core.notifier", "core.state_store"):
        logging.getLogger(name).setLevel(logging.WARNING)

    if args.date == SPLIT_DAY:
        raise SystemExit("拆股日，跳过")

    book = load_book()
    start_equity = float(args.cash or book["equity"])

    frame = ensure_bars(args.date)
    et = frame.index.tz_convert("America/New_York")
    bars_today = int((et.date == pd.Timestamp(args.date).date()).sum())

    summary, journal = run_engine(args.date, start_equity)
    end_equity = float(summary["end_equity"])
    trades = parse_journal(journal)

    # 当日 bar 少于 385 根 = 收盘后重跑才有效的完整日；否则是盘中快照
    complete = bars_today >= 385
    session_rec = {
        "date": args.date,
        "start_equity": round(start_equity, 2),
        "end_equity": round(end_equity, 2),
        "return_pct": round((end_equity / start_equity - 1) * 100, 4),
        "bars": bars_today,
        "complete": complete,
        "trades": trades,
        "halted": bool(summary.get("halted")),
        "run_at": datetime.now().isoformat(timespec="seconds"),
    }
    book["sessions"] = [s for s in book["sessions"] if s["date"] != args.date]
    book["sessions"].append(session_rec)
    book["sessions"].sort(key=lambda s: s["date"])
    # 只有完整日的净值才推进账本；盘中快照只展示不落权益
    if complete:
        book["equity"] = round(end_equity, 2)
    book["last_run"] = session_rec
    os.makedirs(SHADOW_DIR, exist_ok=True)
    with open(BOOK, "w", encoding="utf-8") as fh:
        json.dump(book, fh, ensure_ascii=False, indent=2)

    # ---- 报告 ----
    print(f"\n=== TSLA ORB 影子盘 · {args.date} · {'完整日' if complete else '盘中快照（收盘后需重跑）'} ===")
    print(f"bar 数: {bars_today}  覆盖至 ET {et[-1].strftime('%H:%M')}")
    print(f"起始权益 ${start_equity:,.2f} → 当前 ${end_equity:,.2f}"
          f"（{session_rec['return_pct']:+.2f}%）")
    if not trades:
        print("今日无成交（开盘区间未被突破，或入场窗口内无信号）")
    for t in trades:
        print(f"  {t['side'].upper():5s} {t['qty']} 股 @ {t['entry_price']:.2f} ({t['entry_time']}) "
              f"→ 平仓 {t['exit_price']:.2f}  盈亏 {t['pnl']:+,.2f} ({t['return_pct']:+.2f}%)  "
              f"原因 {t['reason']}")
    print(f"账本: {BOOK}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
