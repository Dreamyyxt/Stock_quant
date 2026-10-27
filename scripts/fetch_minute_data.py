"""
Fetch 1-minute bars into the replay cache.

    .venv/bin/python scripts/fetch_minute_data.py                 # today (ET)
    .venv/bin/python scripts/fetch_minute_data.py --date 2026-10-23
    .venv/bin/python scripts/fetch_minute_data.py --source alpaca

Writes state/intraday_cache/<SYMBOL>_<DATE>.csv so `--mode intraday-replay` is
repeatable and offline. Prefers Alpaca (needs ALPACA_API_KEY / ALPACA_API_SECRET)
because yfinance rate-limits aggressively — the same HTTP 429 that was filling
the old logs.

If both sources fail you can still supply the data yourself: export minute bars
from the broker and drop them in the same directory with columns
time,open,high,low,close,volume.
"""

import argparse
import os
import pathlib
import sys
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from config.settings import INTRADAY_CONFIG  # noqa: E402
from intraday.providers import (  # noqa: E402
    CACHE_DIR, ET, AlpacaBarProvider, ReplayBarProvider, cache_path,
)


def _today() -> str:
    return (datetime.now(ET) if ET else datetime.now()).strftime("%Y-%m-%d")


def fetch_alpaca(symbols, date) -> dict:
    provider = AlpacaBarProvider(symbols, lookback=500, session_date=date)
    if not provider.configured:
        print("  alpaca: not configured (ALPACA_API_KEY / ALPACA_API_SECRET)")
        return {}
    bars = provider.get_recent(symbols)
    for symbol, frame in bars.items():
        frame.to_csv(cache_path(symbol, date))
    return bars


def fetch_yfinance(symbols, date) -> dict:
    try:
        provider = ReplayBarProvider(symbols, date, lookback=500)
        return provider._data
    except RuntimeError as exc:
        print(f"  yfinance: {exc}")
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch minute bars for replay")
    parser.add_argument("--date", default=_today(), help="Session date (YYYY-MM-DD)")
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument("--source", choices=["auto", "alpaca", "yfinance"], default="auto")
    args = parser.parse_args()

    symbols = [s.upper() for s in (args.symbols or INTRADAY_CONFIG["universe"])]
    os.makedirs(CACHE_DIR, exist_ok=True)

    print(f"\nFetching 1-minute bars for {len(symbols)} symbols on {args.date}")
    print(f"Cache directory: {CACHE_DIR}\n")

    got = {}
    if args.source in ("auto", "alpaca"):
        got = fetch_alpaca(symbols, args.date)
        if got:
            print(f"  alpaca: {len(got)}/{len(symbols)} symbols")

    if not got and args.source in ("auto", "yfinance"):
        got = fetch_yfinance(symbols, args.date)
        if got:
            print(f"  yfinance: {len(got)}/{len(symbols)} symbols")

    if not got:
        print("\n[FAILED] No data retrieved.\n")
        print("Options:")
        print("  1) Configure ALPACA_API_KEY / ALPACA_API_SECRET in config/.env (free tier, IEX feed)")
        print("  2) Retry later — yfinance throttles by IP and blocks for a while")
        print("  3) Export minute bars from your broker and place CSVs at:")
        print(f"       {CACHE_DIR}/<SYMBOL>_{args.date}.csv")
        print("     required columns: time,open,high,low,close,volume")
        return 1

    print(f"\n{'symbol':<8} {'bars':>6} {'first':>20} {'last':>20}")
    print("-" * 58)
    for symbol in sorted(got):
        frame = got[symbol]
        first = str(frame.index[0])[:19] if len(frame) else "-"
        last = str(frame.index[-1])[:19] if len(frame) else "-"
        print(f"{symbol:<8} {len(frame):>6} {first:>20} {last:>20}")

    missing = [s for s in symbols if s not in got]
    if missing:
        print(f"\nMissing: {', '.join(missing)}")

    print(f"\nDone. Replay with:\n  .venv/bin/python main.py --mode intraday-replay --date {args.date}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
