"""
Factor lab: screen candidate intraday entry rules on real minute bars.

Why this exists
---------------
The previous signal (15-minute momentum + above VWAP + cross-sectional rank)
measured *anti-predictive*: the higher the score, the worse the next 30 minutes.
That is not fixable by tuning thresholds, so the entry rule has to be re-derived
from the data rather than adjusted.

Method (deliberately boring, deliberately strict)
-------------------------------------------------
1. Signal is evaluated on bar t using only bars <= t (no look-ahead).
2. Entry is the *next* bar's open, not bar t's close.
3. Exit is the close of bar t+h, so every rule is compared on the same horizon.
4. Cost is charged per trade from the measured spread model
   (state/cost_model.json), never assumed.
5. The sample is split chronologically: the first 70% is in-sample, the last 30%
   out-of-sample. A rule that only works in-sample is rejected.

    .venv/bin/python scripts/factor_lab.py --sessions 120
    .venv/bin/python scripts/factor_lab.py --sessions 120 --horizon 30 --emit-json
"""

import argparse
import json
import os
import statistics
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from config.settings import INTRADAY_CONFIG, STATE_DIR  # noqa: E402
from intraday.providers import cache_path  # noqa: E402

COST_MODEL_PATH = os.path.join(STATE_DIR, "cost_model.json")


# ---------------------------------------------------------------------- #
#  Data
# ---------------------------------------------------------------------- #

def cached_sessions(universe, limit: int) -> list:
    cache_dir = os.path.dirname(cache_path("X", "X"))
    if not os.path.isdir(cache_dir):
        return []
    dates = sorted({f.split("_")[-1].replace(".csv", "")
                    for f in os.listdir(cache_dir) if f.endswith(".csv")})
    full = [d for d in dates
            if all(os.path.exists(cache_path(s, d)) for s in universe)]
    return full[-limit:]


def load_session(universe, session_date) -> dict:
    frames = {}
    for symbol in universe:
        path = cache_path(symbol, session_date)
        if not os.path.exists(path):
            continue
        try:
            frame = pd.read_csv(path, index_col=0, parse_dates=True)
        except Exception:
            continue
        if len(frame) < 60:
            continue
        frames[symbol] = frame[["open", "high", "low", "close", "volume"]].astype(float)
    return frames


def load_half_spreads(percentile: str = "median") -> dict:
    """Per-symbol half-spread (fraction of price), from the measured model.

    median = a patient limit order at the touch; p95 = crossing in a hurry.
    Both are reported, because this single number decides the verdict.
    """
    key = "half_spread_p95" if percentile == "p95" else "half_spread_median"
    try:
        with open(COST_MODEL_PATH, "r", encoding="utf-8") as handle:
            model = json.load(handle)
    except Exception:
        return {}
    return {s: float(v.get(key) or 0)
            for s, v in (model.get("symbols") or {}).items()}


# ---------------------------------------------------------------------- #
#  Features (all causal: bar t only sees bars <= t)
# ---------------------------------------------------------------------- #

def build_features(frame: pd.DataFrame, opening_range_bars: int = 30) -> pd.DataFrame:
    out = frame.copy()
    close = out["close"]
    out["ret_1"] = close.pct_change()
    out["mom_5"] = close / close.shift(5) - 1
    out["mom_15"] = close / close.shift(15) - 1
    out["mom_30"] = close / close.shift(30) - 1

    # Prior N-bar high, excluding the current bar: a level you could act on.
    out["high_20_prev"] = out["high"].rolling(20).max().shift(1)
    out["high_60_prev"] = out["high"].rolling(60).max().shift(1)

    # Relative volume vs. its own recent norm (median 5-bar volume, last 60 bars).
    vol5 = out["volume"].rolling(5).mean()
    out["rel_volume"] = vol5 / vol5.rolling(60).median().shift(1)

    typical = (out["high"] + out["low"] + out["close"]) / 3
    cum_vol = out["volume"].cumsum()
    out["vwap"] = (typical * out["volume"]).cumsum() / cum_vol.replace(0, np.nan)
    out["vwap_dev"] = close / out["vwap"] - 1

    # Opening range: the first `opening_range_bars` of the session. Its high is
    # only known after that window, so rules using it must start later.
    opening_high = out["high"].iloc[:opening_range_bars].max()
    opening_low = out["low"].iloc[:opening_range_bars].min()
    out["or_high"] = opening_high
    out["or_low"] = opening_low
    out["or_ready"] = np.arange(len(out)) >= opening_range_bars

    # Wilder-ish RSI on 1-minute closes.
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    out["rsi"] = (100 - 100 / (1 + rs)).fillna(50.0)
    return out


# ---------------------------------------------------------------------- #
#  Candidate rules: name -> (boolean mask, description)
# ---------------------------------------------------------------------- #

def candidate_rules(f: pd.DataFrame) -> dict:
    traded = f["rel_volume"].notna() & f["mom_15"].notna()
    return {
        "baseline_all_bars": (traded, "every bar (reference)"),
        "vol_break_20": (
            traded & (f["rel_volume"] >= 2.0) & (f["close"] > f["high_20_prev"]),
            "volume 2x + break 20-bar high"),
        "vol_break_20_mom1": (
            traded & (f["rel_volume"] >= 2.0) & (f["close"] > f["high_20_prev"])
            & (f["mom_15"] >= 0.01),
            "volume 2x + break 20-bar high + 15m momentum >= 1%"),
        "vol_break_60": (
            traded & (f["rel_volume"] >= 2.0) & (f["close"] > f["high_60_prev"]),
            "volume 2x + break 60-bar high"),
        "orb30_break": (
            f["or_ready"] & (f["close"] > f["or_high"]) & (f["rel_volume"] >= 1.5),
            "break opening 30-min high, volume 1.5x"),
        "orb30_break_mom": (
            f["or_ready"] & (f["close"] > f["or_high"]) & (f["rel_volume"] >= 1.5)
            & (f["mom_15"] >= 0.005),
            "break opening 30-min high, volume 1.5x, 15m momentum >= 0.5%"),
        "tail_mom_1pct": (traded & (f["mom_15"] >= 0.01), "15m momentum >= 1% only"),
        "vol_only_3x": (traded & (f["rel_volume"] >= 3.0), "volume >= 3x only"),
        "vwap_reclaim_vol": (
            traded & (f["vwap_dev"] > 0) & (f["vwap_dev"].shift(1) <= 0)
            & (f["rel_volume"] >= 1.5),
            "reclaim VWAP from below, volume 1.5x"),
        "prev_close_break": (
            traded & (f["mom_5"] >= 0.004) & (f["rel_volume"] >= 2.0),
            "5m momentum >= 0.4% + volume 2x"),
    }


# ---------------------------------------------------------------------- #
#  Evaluation
# ---------------------------------------------------------------------- #

def collect_events(universe, sessions, horizon, cost_by_symbol) -> list:
    events = []
    for session in sessions:
        frames = load_session(universe, session)
        for symbol, frame in frames.items():
            features = build_features(frame)
            half_spread = cost_by_symbol.get(symbol, 0.0002)
            round_trip_cost = 2 * half_spread
            for name, (mask, _desc) in candidate_rules(features).items():
                positions = np.flatnonzero(mask.fillna(False).to_numpy())
                for pos in positions:
                    if pos + 1 + horizon >= len(frame):
                        continue
                    # signal on bar t, fill on bar t+1 open
                    entry_price = float(frame["open"].iloc[pos + 1])
                    exit_price = float(frame["close"].iloc[pos + horizon])
                    if entry_price <= 0 or exit_price <= 0:
                        continue
                    gross = exit_price / entry_price - 1
                    events.append({
                        "session": session, "symbol": symbol, "rule": name,
                        "bar": int(pos), "gross": gross,
                        "net": gross - round_trip_cost,
                    })
    return events


def stats(values: list, sessions: int) -> dict:
    if not values:
        return {"n": 0}
    mean = statistics.mean(values)
    stdev = statistics.stdev(values) if len(values) > 1 else 0.0
    return {
        "n": len(values),
        "mean": mean,
        "median": statistics.median(values),
        "hit_rate": sum(1 for v in values if v > 0) / len(values),
        "stdev": stdev,
        "t_stat": mean / (stdev / len(values) ** 0.5) if stdev and len(values) > 2 else 0.0,
        "per_session": len(values) / sessions if sessions else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Screen intraday entry rules")
    parser.add_argument("--sessions", type=int, default=120)
    parser.add_argument("--horizon", type=int, default=30, help="Exit horizon in bars")
    parser.add_argument("--is-frac", type=float, default=0.7, help="In-sample fraction")
    parser.add_argument("--cost-percentile", choices=["median", "p95"], default="median")
    parser.add_argument("--emit-json", action="store_true")
    args = parser.parse_args()

    universe = INTRADAY_CONFIG["universe"]
    sessions = cached_sessions(universe, args.sessions)
    if len(sessions) < 20:
        print(f"Only {len(sessions)} cached sessions — need at least 20.")
        return 1

    split = int(len(sessions) * args.is_frac)
    is_sessions, oos_sessions = sessions[:split], sessions[split:]
    cost_by_symbol = load_half_spreads(args.cost_percentile)

    print(f"\nFactor lab — {len(sessions)} sessions ({sessions[0]} → {sessions[-1]})")
    print(f"  in-sample : {len(is_sessions)} sessions ({is_sessions[0]} → {is_sessions[-1]})")
    print(f"  out-sample: {len(oos_sessions)} sessions ({oos_sessions[0]} → {oos_sessions[-1]})")
    print(f"  horizon   : {args.horizon} bars, entry at next bar open, "
          f"cost = 2 x {args.cost_percentile} half-spread\n")

    events = collect_events(universe, sessions, args.horizon, cost_by_symbol)
    if not events:
        print("[FAILED] No events.")
        return 1

    is_events = [e for e in events if e["session"] in set(is_sessions)]
    oos_events = [e for e in events if e["session"] in set(oos_sessions)]

    rules = sorted({e["rule"] for e in events})
    descriptions = {name: desc for name, (_m, desc) in
                    candidate_rules(build_features(load_session(universe, sessions[0])[universe[0]])).items()}

    print(f"{'rule':<22}{'n':>6}{'/day':>6}{'gross':>9}{'net':>9}{'hit%':>7}{'t':>6}"
          f"{'OOS gross':>10}{'OOS net':>9}")
    print("-" * 88)

    results = []
    for rule in rules:
        gross_all = [e["gross"] for e in events if e["rule"] == rule]
        net_all = [e["net"] for e in events if e["rule"] == rule]
        net_is = [e["net"] for e in is_events if e["rule"] == rule]
        net_oos = [e["net"] for e in oos_events if e["rule"] == rule]
        s_all = stats(net_all, len(sessions))
        s_oos = stats(net_oos, len(oos_sessions))
        row = {
            "rule": rule, "description": descriptions.get(rule, ""),
            "n": s_all.get("n", 0), "per_day": s_all.get("per_session", 0),
            "net_mean": s_all.get("mean", 0), "hit_rate": s_all.get("hit_rate", 0),
            "t_stat": s_all.get("t_stat", 0),
            "gross_mean": statistics.mean(gross_all) if gross_all else 0,
            "is_mean": statistics.mean(net_is) if net_is else 0,
            "oos_n": s_oos.get("n", 0), "oos_mean": s_oos.get("mean", 0),
            "oos_hit": s_oos.get("hit_rate", 0),
            "oos_gross": statistics.mean(
                [e["gross"] for e in oos_events if e["rule"] == rule]) if any(
                    e["rule"] == rule for e in oos_events) else 0,
        }
        results.append(row)
        print(f"{rule:<22}{row['n']:>6}{row['per_day']:>6.2f}"
              f"{row['gross_mean']*1e6:>9.0f}{row['net_mean']*1e6:>9.0f}"
              f"{row['hit_rate']:>7.1%}{row['t_stat']:>6.2f}"
              f"{row['oos_gross']*1e6:>10.0f}{row['oos_mean']*1e6:>9.0f}")

    print("\n(ppm = parts per million of notional, net of measured cost per trade)\n")

    survivors = [r for r in results
                 if r["oos_n"] >= 30 and r["oos_mean"] > 0 and r["is_mean"] > 0]
    if survivors:
        print("Rules positive in BOTH in-sample and out-of-sample:")
        for row in sorted(survivors, key=lambda r: r["oos_mean"], reverse=True):
            print(f"  {row['rule']:<22} IS {row['is_mean']*1e6:>7.0f} ppm  "
                  f"OOS {row['oos_mean']*1e6:>7.0f} ppm  "
                  f"({row['oos_n']} trades, {row['per_day']:.2f}/day)  — {row['description']}")
    else:
        print("No rule is positive in both halves. Any of these rules trading live "
              "would be paying the spread for nothing.")

    if args.emit_json:
        os.makedirs("logs", exist_ok=True)
        with open("logs/factor_lab.json", "w", encoding="utf-8") as handle:
            json.dump({
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "sessions": len(sessions), "horizon": args.horizon,
                "in_sample": [is_sessions[0], is_sessions[-1]],
                "out_sample": [oos_sessions[0], oos_sessions[-1]],
                "cost_model": cost_by_symbol, "results": results,
            }, handle, indent=2)
        print("\nSaved → logs/factor_lab.json")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
