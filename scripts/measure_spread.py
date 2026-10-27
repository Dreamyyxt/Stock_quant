"""
Measure the real cost of crossing the spread, per symbol.

The replay engine used a flat 0.15% entry / 0.40% flatten slippage assumption.
Measured against actual SIP quotes for this universe the median half-spread is
~0.013%, i.e. the old numbers overstated cost by 10-15x — enough to turn a
break-even strategy into a guaranteed loser.

This script samples historical quotes and writes state/cost_model.json:

    .venv/bin/python scripts/measure_spread.py --days 10
    .venv/bin/python scripts/measure_spread.py --days 20 --symbols AAPL AMD

SimExecution reads that file per symbol. Anything not in the file falls back to
`limit_slippage_pct` in INTRADAY_CONFIG, so a new symbol never trades with a
silently optimistic cost.
"""

import argparse
import json
import os
import statistics
import sys
import urllib.parse
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import INTRADAY_CONFIG, STATE_DIR  # noqa: E402
from intraday.providers import cache_path  # noqa: E402

OUT_PATH = os.path.join(STATE_DIR, "cost_model.json")


def get(url, params, key, secret):
    request = urllib.request.Request(
        f"{url}?{urllib.parse.urlencode(params)}",
        headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
                 "accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def sample_day(symbol, session_date, key, secret, limit=2000):
    """Quotes for one symbol/session, spread expressed as % of mid."""
    start = f"{session_date}T14:00:00Z"       # 10:00 ET, spread has settled
    end = f"{session_date}T19:30:00Z"         # 15:30 ET, before the close
    payload = get(f"https://data.alpaca.markets/v2/stocks/{symbol}/quotes",
                  {"start": start, "end": end, "limit": limit, "feed": "sip"},
                  key, secret)
    spreads = []
    for quote in payload.get("quotes") or []:
        bid, ask = quote.get("bp"), quote.get("ap")
        if not bid or not ask or ask <= bid:
            continue
        mid = (ask + bid) / 2.0
        spreads.append((ask - bid) / mid)
    return spreads


def cached_sessions(limit):
    """Sessions we already have bars for — avoids probing empty days."""
    cache_dir = os.path.dirname(cache_path("X", "X"))
    if not os.path.isdir(cache_dir):
        return []
    dates = sorted({f.split("_")[-1].replace(".csv", "")
                    for f in os.listdir(cache_dir) if f.endswith(".csv")}, reverse=True)
    return sorted(dates[:limit])


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure per-symbol spread cost")
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--symbols", nargs="*", default=None)
    args = parser.parse_args()

    key = (os.getenv("ALPACA_API_KEY") or "").strip()
    secret = (os.getenv("ALPACA_API_SECRET") or "").strip()
    if not key or not secret:
        print("Alpaca keys missing — set ALPACA_API_KEY / ALPACA_API_SECRET in config/.env")
        return 1

    symbols = [s.upper() for s in (args.symbols or INTRADAY_CONFIG["universe"])]
    sessions = cached_sessions(args.days)
    if not sessions:
        print("No cached sessions found. Fetch bars first (scripts/fetch_minute_data.py).")
        return 1

    print(f"\nMeasuring spread over {len(sessions)} sessions "
          f"({sessions[0]} → {sessions[-1]})\n")
    print(f"{'symbol':<8}{'quotes':>8}{'median%':>10}{'p95%':>9}{'half med%':>11}{'half p95%':>11}")
    print("-" * 57)

    model, medians, p95s = {}, [], []
    for symbol in symbols:
        spreads = []
        for session in sessions:
            try:
                spreads.extend(sample_day(symbol, session, key, secret))
            except Exception as exc:
                print(f"  {symbol} {session}: {exc}")
        if len(spreads) < 50:
            print(f"{symbol:<8}{len(spreads):>8}   (too few samples, skipped)")
            continue
        spreads.sort()
        median = statistics.median(spreads)
        p95 = spreads[int(len(spreads) * 0.95) - 1]
        model[symbol] = {
            "samples": len(spreads),
            "spread_median": round(median, 8),
            "spread_p95": round(p95, 8),
            # Half-spread = cost of crossing once with a marketable order.
            # SimExecution applies it per side, so a round trip pays ~1 spread.
            "half_spread_median": round(median / 2, 8),
            "half_spread_p95": round(p95 / 2, 8),
        }
        medians.append(median / 2)
        p95s.append(p95 / 2)
        print(f"{symbol:<8}{len(spreads):>8}{median:>9.4%}{p95:>9.4%}"
              f"{median/2:>11.4%}{p95/2:>11.4%}")

    if not model:
        print("\n[FAILED] No usable samples.\n")
        return 1

    universe_median = statistics.median(medians)
    universe_p95 = statistics.median(p95s)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "sessions": sessions,
        "feed": "sip",
        "symbols": model,
        "universe_half_spread_median": round(universe_median, 8),
        "universe_half_spread_p95": round(universe_p95, 8),
    }
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)

    print(f"\nUniverse half-spread: median {universe_median:.4%}  p95 {universe_p95:.4%}")
    print(f"  config limit_slippage_pct   = {INTRADAY_CONFIG['limit_slippage_pct']:.4%}  "
          f"({INTRADAY_CONFIG['limit_slippage_pct']/universe_median:.0f}x median)")
    print(f"  config flatten_slippage_pct = {INTRADAY_CONFIG['flatten_slippage_pct']:.4%}  "
          f"({INTRADAY_CONFIG['flatten_slippage_pct']/universe_p95:.0f}x p95)")
    print(f"\nSaved → {OUT_PATH}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
