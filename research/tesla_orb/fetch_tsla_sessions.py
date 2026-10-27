#!/usr/bin/env python3
"""
Fetch TSLA 1-minute bars session by session, then assemble one CSV.

The bulk endpoint paginates 10,000 bars at a time and is fragile over multi-year
windows on a free data plan; asking for one session at a time is ~390 bars per
call and has proven reliable. Per-session files also resume for free.

Usage:
    python scripts/fetch_tsla_sessions.py --start 2021-11-01 --end 2026-10-23
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from config.settings import STATE_DIR  # noqa: E402
from intraday.providers import AlpacaBarProvider, CACHE_DIR, cache_path  # noqa: E402


def weekdays(start: str, end: str):
    cursor = date.fromisoformat(start)
    stop = date.fromisoformat(end)
    while cursor <= stop:
        if cursor.weekday() < 5:
            yield cursor.isoformat()
        cursor += timedelta(days=1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="TSLA")
    parser.add_argument("--start", default="2021-11-01")
    parser.add_argument("--end", default="2026-10-23")
    parser.add_argument("--workers", type=int, default=1,
                        help="Parallel sessions (4 keeps us under the data API rate limit)")
    parser.add_argument("--assemble-only", action="store_true")
    parser.add_argument("--reverse", action="store_true",
                        help="Fetch newest sessions first")
    args = parser.parse_args()

    symbol = args.symbol.upper()
    os.makedirs(CACHE_DIR, exist_ok=True)
    days = list(weekdays(args.start, args.end))
    todo = [d for d in days if not os.path.exists(cache_path(symbol, d))]
    if args.reverse:
        todo.reverse()
    print(f"{len(days)} candidate sessions {days[0]} -> {days[-1]}; "
          f"{len(days) - len(todo)} already cached, {len(todo)} to fetch", flush=True)

    def grab(session: str) -> tuple:
        provider = AlpacaBarProvider([symbol], lookback=400, session_date=session, feed="sip")
        try:
            frame = provider.get_recent([symbol]).get(symbol)
        except Exception as exc:
            return session, -1, str(exc)
        if frame is None or len(frame) < 100:
            return session, 0, "empty"
        frame.to_csv(cache_path(symbol, session))
        return session, len(frame), ""

    if not args.assemble_only and todo:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        done = empty = failed = 0
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {pool.submit(grab, d): d for d in todo}
            for index, future in enumerate(as_completed(futures), 1):
                session, count, note = future.result()
                if count > 0:
                    done += 1
                elif count == 0:
                    empty += 1
                else:
                    failed += 1
                    print(f"  {session}: {note}", flush=True)
                if index % 50 == 0:
                    print(f"  [{index}/{len(todo)}] fetched {done}, empty {empty}, "
                          f"failed {failed}", flush=True)
        print(f"\nFetched {done}, empty {empty}, failed {failed}", flush=True)

    # Assemble. Timestamps stay in UTC; the research code converts to ET.
    frames = []
    sessions = sorted({f.split("_")[-1].replace(".csv", "")
                       for f in os.listdir(CACHE_DIR)
                       if f.startswith(f"{symbol}_") and f.endswith(".csv")})
    for session in sessions:
        if not (args.start <= session <= args.end):
            continue
        frame = pd.read_csv(cache_path(symbol, session), index_col=0)
        frames.append(frame)
    if not frames:
        print("Nothing to assemble.")
        return 1

    combined = pd.concat(frames).sort_index()
    combined.index.name = "timestamp"
    out = os.path.join(STATE_DIR, f"{symbol.lower()}_1min.csv")
    combined.to_csv(out)
    print(f"Assembled {len(combined):,} bars over {len(frames):,} sessions -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
