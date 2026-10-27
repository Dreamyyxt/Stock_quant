#!/usr/bin/env python3
"""
Measure TSLA's real trading cost from SIP quotes.

The lab and the backtest both need one number: what does crossing the spread
cost, per side? Assuming it (the old code assumed 15 bp entry / 40 bp flatten)
overstates cost by two orders of magnitude for a liquid mega-cap and turns a
break-even strategy into a guaranteed loser.

Writes state/tsla_cost.json.
"""
from __future__ import annotations

import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import STATE_DIR  # noqa: E402
from scripts.measure_spread import sample_day  # noqa: E402

OUT_PATH = os.path.join(STATE_DIR, "tsla_cost.json")
SESSIONS = ["2026-10-23", "2026-10-22", "2026-10-21", "2026-10-20",
            "2026-10-19", "2026-10-16", "2026-10-15", "2026-10-14"]


def main() -> int:
    key = (os.getenv("ALPACA_API_KEY") or "").strip()
    secret = (os.getenv("ALPACA_API_SECRET") or "").strip()
    if not key or not secret:
        print("Alpaca keys missing (config/.env)")
        return 1

    spreads = []
    for session in SESSIONS:
        try:
            day = sample_day("TSLA", session, key, secret)
        except Exception as exc:
            print(f"  {session}: {exc}")
            continue
        print(f"  {session}: {len(day)} quotes, median {statistics.median(day)*1e4:.2f} bp"
              if day else f"  {session}: no quotes")
        spreads.extend(day)

    if len(spreads) < 50:
        print("Too few quotes to build a cost model.")
        return 1

    spreads.sort()
    median = statistics.median(spreads)
    p95 = spreads[int(len(spreads) * 0.95) - 1]
    model = {
        "symbol": "TSLA",
        "samples": len(spreads),
        "sessions": SESSIONS,
        "feed": "sip",
        "spread_median_bp": median * 1e4,
        "spread_p95_bp": p95 * 1e4,
        "half_spread_median_bp": median * 1e4 / 2,
        "half_spread_p95_bp": p95 * 1e4 / 2,
        # Extra allowance per side on top of the half-spread: the fill is not
        # always at the touch, and a marketable order pays through one level.
        "slippage_bp": 0.5,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as handle:
        json.dump(model, handle, indent=2)

    print(f"\nquotes={len(spreads):,}  median spread {median*1e4:.2f} bp  "
          f"p95 {p95*1e4:.2f} bp")
    print(f"half-spread median {median*1e4/2:.2f} bp  ->  round trip ≈ "
          f"{(median*1e4/2)*2 + 1.0:.2f} bp incl. 0.5 bp/side slippage")
    print(f"Saved → {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
