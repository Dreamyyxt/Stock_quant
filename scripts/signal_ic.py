"""
Signal information test: does the entry condition predict anything?

This deliberately does NOT simulate trading. It asks one question only: when the
entry filter says yes, is the next 5/15/30/60 minutes' return better than the
unconditional return over the same horizon?

Why this comes first: a trading simulation mixes signal, sizing, exits and cost
assumptions, so a flat equity curve cannot tell you which one is broken. The
forward-return spread isolates the signal. If it is ~0, no amount of cost tuning
or risk management will make the strategy work.

    .venv/bin/python scripts/signal_ic.py --days 10
    .venv/bin/python scripts/signal_ic.py --days 10 --horizons 5 15 30 60

Reads the same cached bars the replay uses, and calls the *same*
IntradaySignals.evaluate(), so the measurement cannot drift from live behaviour.
"""

import argparse
import json
import os
import statistics
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

from config.settings import INTRADAY_CONFIG  # noqa: E402
from intraday.providers import cache_path  # noqa: E402
from intraday.signals import IntradaySignals  # noqa: E402


def cached_dates(universe, limit: int) -> list:
    """Session dates with a full cache, most recent first."""
    dates = sorted({d.split("_")[-1].replace(".csv", "")
                    for d in os.listdir(os.path.dirname(cache_path("X", "X")))
                    if d.endswith(".csv")}, reverse=True)
    full = [d for d in dates
            if all(os.path.exists(cache_path(s, d)) for s in universe)]
    return sorted(full[:limit])


def load_frames(universe, session_date) -> dict:
    frames = {}
    for symbol in universe:
        path = cache_path(symbol, session_date)
        if not os.path.exists(path):
            continue
        try:
            frame = pd.read_csv(path, index_col=0, parse_dates=True)
        except Exception:
            continue
        if len(frame):
            frames[symbol] = frame
    return frames


def measure(universe, dates, horizons, config) -> dict:
    signals = IntradaySignals(config)
    lookback = int(config.get("lookback_bars", 90))
    min_bars = int(config.get("min_bars_for_signal", 35))

    # events: list of (session_date, ts, entry_ok, score, momentum, rsi, forward dict)
    events = []
    for session_date in dates:
        frames = load_frames(universe, session_date)
        if len(frames) < 2:
            continue
        index = sorted(set().union(*[set(f.index) for f in frames.values()]))
        for position, ts in enumerate(index):
            if position < min_bars or position + max(horizons) >= len(index):
                continue

            windows = {}
            for symbol, frame in frames.items():
                window = frame[frame.index <= ts].tail(lookback)
                if len(window) >= min_bars:
                    windows[symbol] = window
            if len(windows) < 2:
                continue

            ranked = {s.symbol: s for s in signals.evaluate(windows)}
            for symbol, window in windows.items():
                signal = ranked.get(symbol)
                if signal is None:
                    continue
                entry_price = float(window["close"].iloc[-1])
                if entry_price <= 0:
                    continue
                frame = frames[symbol]
                future = frame[frame.index > ts]
                if len(future) == 0:
                    continue
                forward = {}
                for horizon in horizons:
                    if len(future) >= horizon:
                        exit_price = float(future["close"].iloc[horizon - 1])
                        forward[horizon] = exit_price / entry_price - 1
                if not forward:
                    continue
                events.append({
                    "date": session_date, "ts": ts, "symbol": symbol,
                    "entry_ok": signal.entry_ok, "score": signal.score,
                    "momentum": signal.momentum, "rsi": signal.rsi,
                    "vwap_dev": signal.vwap_dev, "rel_volume": signal.rel_volume,
                    "forward": forward,
                })
    return {"events": events}


def summarise(events, horizons, label) -> dict:
    out = {"label": label, "n": len(events), "horizons": {}}
    for horizon in horizons:
        values = [e["forward"][horizon] for e in events if horizon in e["forward"]]
        if not values:
            continue
        mean = statistics.mean(values)
        stdev = statistics.stdev(values) if len(values) > 1 else 0.0
        t_stat = (mean / (stdev / len(values) ** 0.5)) if stdev and len(values) > 2 else 0.0
        out["horizons"][horizon] = {
            "n": len(values),
            "mean": mean,
            "median": statistics.median(values),
            "hit_rate": sum(1 for v in values if v > 0) / len(values),
            "t_stat": t_stat,
        }
    return out


def bucket_by(events, key, edges, horizon) -> list:
    rows = []
    for low, high in zip(edges[:-1], edges[1:]):
        subset = [e for e in events
                  if low <= e[key] < high and horizon in e["forward"]]
        if not subset:
            continue
        values = [e["forward"][horizon] for e in subset]
        rows.append({
            "bucket": f"{low:g}..{high:g}", "n": len(values),
            "mean": statistics.mean(values),
            "hit_rate": sum(1 for v in values if v > 0) / len(values),
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Forward-return test for entry signals")
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--horizons", nargs="*", type=int, default=[5, 15, 30, 60])
    parser.add_argument("--out", default="logs/signal_ic.json")
    args = parser.parse_args()

    config = dict(INTRADAY_CONFIG)
    universe = config["universe"]
    dates = cached_dates(universe, args.days)
    if not dates:
        print("No cached sessions. Run scripts/batch_replay.py first (or fetch data).")
        return 1

    print(f"\nSignal test over {len(dates)} sessions: {dates[0]} → {dates[-1]}")
    print(f"Calling IntradaySignals.evaluate() on {len(universe)} symbols, "
          f"entry_score={config.get('entry_score')}\n")

    events = measure(universe, dates, args.horizons, config)["events"]
    if not events:
        print("[FAILED] No observations produced.")
        return 1

    entries = [e for e in events if e["entry_ok"]]
    print(f"Observations: {len(events):,}  (entry_ok: {len(entries):,} = "
          f"{len(entries)/len(events):.1%} of bars)\n")

    baseline = summarise(events, args.horizons, "all bars")
    signal = summarise(entries, args.horizons, "entry_ok")

    print(f"{'horizon':>8}{'baseline':>12}{'entry_ok':>12}{'edge':>11}"
          f"{'hit%':>8}{'t':>8}{'n':>8}")
    print("-" * 67)
    for horizon in args.horizons:
        base = baseline["horizons"].get(horizon)
        sig = signal["horizons"].get(horizon)
        if not base or not sig:
            continue
        edge = sig["mean"] - base["mean"]
        print(f"{horizon:>6}m {base['mean']:>11.3%} {sig['mean']:>11.3%} "
              f"{edge:>10.3%} {sig['hit_rate']:>7.1%} {sig['t_stat']:>7.2f} "
              f"{sig['n']:>7d}")

    print("\nEntry edge by score bucket (30-minute forward return):")
    horizon = 30 if 30 in args.horizons else args.horizons[-1]
    print(f"  {'score':<14}{'n':>8}{'mean':>11}{'hit%':>9}")
    print("  " + "-" * 40)
    for row in bucket_by(entries, "score", [0.0, 0.5, 0.62, 0.72, 0.82, 0.92, 1.01], horizon):
        print(f"  {row['bucket']:<14}{row['n']:>8}{row['mean']:>10.3%}{row['hit_rate']:>8.1%}")

    print("\nEntry edge by momentum bucket (30-minute forward return):")
    print(f"  {'momentum':<14}{'n':>8}{'mean':>11}{'hit%':>9}")
    print("  " + "-" * 40)
    for row in bucket_by(entries, "momentum",
                         [0.0, 0.001, 0.002, 0.003, 0.005, 0.01, 1.0], horizon):
        print(f"  {row['bucket']:<14}{row['n']:>8}{row['mean']:>10.3%}{row['hit_rate']:>8.1%}")

    # ---- Conditional tests: the same bars sliced by *other* entry ideas ---- #
    # The score table above says high momentum is followed by weakness, so the
    # natural counter-hypothesis is mean reversion: buy weakness instead.
    print(f"\nHypothesis tests at {horizon}m (all bars, not just entry_ok):")
    print(f"  {'condition':<34}{'n':>8}{'mean':>11}{'hit%':>9}{'t':>7}")
    print("  " + "-" * 68)

    conditions = [
        ("momentum < -0.10%", lambda e: e["momentum"] < -0.001),
        ("momentum < -0.20%", lambda e: e["momentum"] < -0.002),
        ("momentum < -0.30%", lambda e: e["momentum"] < -0.003),
        ("RSI < 30", lambda e: e["rsi"] < 30),
        ("RSI < 25", lambda e: e["rsi"] < 25),
        ("RSI 70-80", lambda e: 70 <= e["rsi"] < 80),
        ("RSI >= 80", lambda e: e["rsi"] >= 80),
        ("below VWAP > 0.3%", lambda e: e.get("vwap_dev", 0) < -0.003),
        ("above VWAP > 0.3%", lambda e: e.get("vwap_dev", 0) > 0.003),
        ("vol >= 2x", lambda e: e["rel_volume"] >= 2.0),
        ("reversal: mom<-0.15% & RSI<40",
         lambda e: e["momentum"] < -0.0015 and e["rsi"] < 40),
        ("reversal: mom<-0.15% & RSI<40 & vol>=1.5x",
         lambda e: e["momentum"] < -0.0015 and e["rsi"] < 40 and e["rel_volume"] >= 1.5),
        ("reversal: mom<-0.25% & RSI<35",
         lambda e: e["momentum"] < -0.0025 and e["rsi"] < 35),
    ]

    hypothesis_rows = []
    for label, predicate in conditions:
        subset = [e for e in events if horizon in e["forward"] and predicate(e)]
        if len(subset) < 20:
            print(f"  {label:<34}{len(subset):>8}   (too few)")
            continue
        values = [e["forward"][horizon] for e in subset]
        mean = statistics.mean(values)
        stdev = statistics.stdev(values) if len(values) > 1 else 0.0
        t_stat = mean / (stdev / len(values) ** 0.5) if stdev else 0.0
        hit = sum(1 for v in values if v > 0) / len(values)
        print(f"  {label:<34}{len(values):>8}{mean:>10.3%}{hit:>8.1%}{t_stat:>7.2f}")
        hypothesis_rows.append({"condition": label, "n": len(values),
                                "mean": mean, "hit_rate": hit, "t_stat": t_stat})

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "sessions": dates,
        "config": {k: v for k, v in config.items() if k != "universe"},
        "baseline": baseline,
        "entry": signal,
        "score_buckets": bucket_by(entries, "score",
                                   [0.0, 0.5, 0.62, 0.72, 0.82, 0.92, 1.01], horizon),
        "momentum_buckets": bucket_by(entries, "momentum",
                                      [0.0, 0.001, 0.002, 0.003, 0.005, 0.01, 1.0], horizon),
        "hypotheses": hypothesis_rows,
    }
    os.makedirs("logs", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    print(f"\nSaved → {args.out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
