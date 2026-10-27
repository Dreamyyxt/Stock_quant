#!/usr/bin/env python3
"""Turn a westock `kline` Markdown dump into a clean daily OHLCV CSV.

westock returns newest-row-first with columns
    date | open | last | high | low | volume | amount | exchange | change_pct
where `last` is the close, `volume` is in lots (手, 100 shares for A shares /
100 units for ETFs) and `exchange` is turnover in percent.

Two things this script deliberately does:
  * drop the last row when the market is still trading — a partial bar has a
    fake low volume and would poison every volume-based factor;
  * verify there are no duplicate dates and report the gap structure instead of
    silently forward filling.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def parse_markdown_table(path: str) -> pd.DataFrame:
    rows: list[list[str]] = []
    header: list[str] | None = None
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if header is None:
                header = [c.lower() for c in cells]
                continue
            if all(set(c) <= set("-: ") for c in cells):
                continue
            if len(cells) != len(header):
                continue
            rows.append(cells)

    if header is None or not rows:
        raise SystemExit(f"no table found in {path}")

    df = pd.DataFrame(rows, columns=header)
    df = df.rename(columns={"last": "close"})
    keep = ["date", "open", "high", "low", "close", "volume", "amount"]
    for col in keep:
        if col not in df.columns:
            raise SystemExit(f"missing column {col!r} in {path}: {list(df.columns)}")
    out = df[keep].copy()

    for col in ("open", "high", "low", "close", "volume", "amount"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out = out.dropna(subset=["date", "close"])
    out = out.sort_values("date").drop_duplicates(subset=["date"], keep="last")
    return out.reset_index(drop=True)


def audit(df: pd.DataFrame, label: str) -> None:
    print(f"[{label}] rows={len(df)}  {df['date'].iloc[0].date()} → {df['date'].iloc[-1].date()}")
    gaps = df["date"].diff().dt.days
    long_gaps = df.loc[gaps > 10, "date"]
    print(f"  calendar gaps > 10 days: {len(long_gaps)}  (春节/国庆长假属正常)")
    bad = df[(df[["open", "high", "low", "close"]] <= 0).any(axis=1)]
    print(f"  non-positive prices: {len(bad)}")
    no_vol = df[df["volume"] <= 0]
    print(f"  zero-volume bars (suspension proxy): {len(no_vol)}")
    span = (df["date"].iloc[-1] - df["date"].iloc[0]).days
    print(f"  span {span} days ≈ {span / 365.25:.1f} years")
    first, last = df.iloc[0], df.iloc[-1]
    print(f"  first {first['date'].date()} close={first['close']:.3f}")
    print(f"  last  {last['date'].date()} close={last['close']:.3f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default=os.path.join(PROJECT_ROOT, "state/ashare/raw_510300_qfq.md"))
    ap.add_argument("--out", default=os.path.join(PROJECT_ROOT, "state/ashare/510300_daily.csv"))
    ap.add_argument("--drop-today", action="store_true", default=True)
    ap.add_argument("--keep-today", dest="drop_today", action="store_false")
    args = ap.parse_args()

    df = parse_markdown_table(args.raw)
    audit(df, "raw")

    if args.drop_today:
        today = pd.Timestamp(datetime.now().date())
        if len(df) and df["date"].iloc[-1].normalize() == today:
            # Today's bar is still forming whenever the call happens before the
            # 15:00 close; keeping it would understate volume and distort any
            # volume-ratio factor on the freshest row.
            now = datetime.now()
            if now.hour < 15:
                df = df.iloc[:-1].reset_index(drop=True)
                print(f"  dropped incomplete bar for {today.date()} (before 15:00 local)")
            else:
                print(f"  kept bar for {today.date()} (after 15:00 local)")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    df.to_csv(args.out, index=False)
    print(f"\nwrote {args.out}  ({len(df)} rows)")
    audit(df, "clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
