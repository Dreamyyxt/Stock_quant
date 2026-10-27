#!/usr/bin/env python3
"""
Refine the opening-range breakout: which variant survives both halves of the sample?

The broad screen in tsla_lab.py showed the family is mildly positive but the
afternoon breakouts dilute it. This script isolates the morning window and walks
the parameter grid, reporting in-sample and out-of-sample side by side. A variant
only earns trust if both halves agree.

Usage:
    python scripts/tsla_refine.py
"""
from __future__ import annotations

import itertools
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.tsla_lab import (OPEN_MIN, attach_context, attach_vol, evaluate,
                              last_index, load_cost, load_sessions, simulate,
                              summarize)


def scan_breaks(session: dict, or_minutes: int) -> dict:
    """First close beyond each side of the opening range, per direction."""
    n = min(or_minutes, last_index(session) - 1)
    high, low, close = session["high"], session["low"], session["close"]
    or_high, or_low = float(high[:n].max()), float(low[:n].min())
    out = {}
    for idx in range(n - 1, last_index(session)):
        if 1 not in out and close[idx] > or_high:
            out[1] = (idx, or_low)
        if -1 not in out and close[idx] < or_low:
            out[-1] = (idx, or_high)
        if len(out) == 2:
            break
    return out


def main() -> int:
    sessions = load_sessions()
    attach_context(sessions)
    attach_vol(sessions)
    cost = load_cost()
    cost_bp = cost["half_spread_bp"] * 2 + cost["slippage_bp"] * 2
    dates = sorted(sessions)
    split_at = int(len(dates) * 0.6)
    sample, oos = dates[1:split_at], dates[split_at:]
    print(f"cost {cost_bp:.2f} bp round trip | IS {len(sample)} sessions | "
          f"OOS {len(oos)} sessions\n")

    # Pre-scan once per range length; every variant below is a cheap lookup.
    for or_minutes in (5, 15, 30):
        for date in dates:
            sessions[date].setdefault("breaks", {})[or_minutes] = \
                scan_breaks(sessions[date], or_minutes)

    def make_rule(or_minutes, cutoff, direction_mode, vol_filter):
        def rule(session):
            breaks = session["breaks"][or_minutes]
            candidates = []
            if direction_mode in ("long", "both") and 1 in breaks:
                candidates.append((1, breaks[1]))
            if direction_mode in ("short", "both") and -1 in breaks:
                candidates.append((-1, breaks[-1]))
            if not candidates:
                return None
            if direction_mode == "both":
                # Whichever side broke first is the one that set the day's tone.
                candidates.sort(key=lambda item: item[1][0])
            direction, (idx, stop_level) = candidates[0]
            if OPEN_MIN + idx > cutoff:
                return None
            if vol_filter:
                realized, median = session.get("realized_vol"), session.get("vol_median")
                if realized is None or median is None or realized < median:
                    return None
            return idx, direction, {"stop_price": stop_level}
        return rule

    rows = []
    grid = itertools.product((5, 15, 30), (10 * 60 + 30, 11 * 60, 11 * 60 + 30),
                             ("long", "short", "both"), (False, True),
                             (0, 120))
    for or_minutes, cutoff, direction_mode, vol_filter, max_hold in grid:
        rule = make_rule(or_minutes, cutoff, direction_mode, vol_filter)
        ins = summarize(evaluate(sample, sessions, rule, cost_bp, max_hold=max_hold),
                        len(sample))
        oo = summarize(evaluate(oos, sessions, rule, cost_bp, max_hold=max_hold),
                       len(oos))
        if ins["n"] < 80 or oo["n"] < 40:
            continue
        rows.append({
            "or": or_minutes, "cutoff": f"{cutoff // 60:02d}:{cutoff % 60:02d}",
            "dir": direction_mode, "vol": "Y" if vol_filter else "-",
            "hold": max_hold or "close",
            "is_n": ins["n"], "is_bp": ins["net_bp"], "is_t": ins["t"],
            "oos_n": oo["n"], "oos_bp": oo["net_bp"], "oos_t": oo["t"],
            "both": ins["net_bp"] > 0 and oo["net_bp"] > 0,
        })

    rows.sort(key=lambda r: r["is_bp"])
    header = (f"{'OR':>3}{'cutoff':>8}{'dir':>7}{'vol':>5}{'hold':>7}"
              f"{'IS n':>6}{'IS bp':>8}{'IS t':>6}{'OOS n':>7}{'OOS bp':>8}{'OOS t':>7}"
              f"{'both':>6}")
    print(header)
    print("-" * len(header))
    for row in rows:
        print(f"{row['or']:>3}{row['cutoff']:>8}{row['dir']:>7}{row['vol']:>5}"
              f"{str(row['hold']):>7}{row['is_n']:>6}{row['is_bp']:>8.1f}{row['is_t']:>6.2f}"
              f"{row['oos_n']:>7}{row['oos_bp']:>8.1f}{row['oos_t']:>7.2f}"
              f"{'YES' if row['both'] else '':>6}")

    passed = [r for r in rows if r["both"]]
    print(f"\n{len(passed)}/{len(rows)} variants are positive in both halves")
    if passed:
        passed.sort(key=lambda r: min(r["is_t"], r["oos_t"]), reverse=True)
        best = passed[0]
        print("Most consistent:", best)
    return 0




def stage2() -> int:
    """Stop-width sensitivity for the morning breakout family."""
    sessions = load_sessions()
    attach_context(sessions)
    dates = sorted(sessions)
    split_at = int(len(dates) * 0.6)
    sample, oos = dates[1:split_at], dates[split_at:]
    cost = load_cost()
    cost_bp = cost["half_spread_bp"] * 2 + cost["slippage_bp"] * 2

    for or_minutes in (5, 15, 30):
        for date in dates:
            sessions[date].setdefault("breaks", {})[or_minutes] = \
                scan_breaks(sessions[date], or_minutes)

    def make_rule(or_minutes, cutoff, stop_spec):
        def rule(session):
            breaks = session["breaks"][or_minutes]
            if not breaks:
                return None
            # Earliest break wins; direction is whatever side broke first.
            direction = min(breaks, key=lambda d: breaks[d][0])
            idx, stop_level = breaks[direction]
            if OPEN_MIN + idx > cutoff:
                return None
            n = min(or_minutes, last_index(session) - 1)
            range_size = float(session["high"][:n].max() - session["low"][:n].min())
            price = float(session["close"][idx])
            extra = {}
            if stop_spec[0] == "range":
                extra["stop_pct"] = range_size * stop_spec[1] / price
            elif stop_spec[0] == "fixed":
                extra["stop_pct"] = stop_spec[1]
            return idx, direction, extra
        return rule

    specs = [("range", m) for m in (0.75, 1.0, 1.5, 2.0, 3.0)]
    specs += [("fixed", 0.01), ("fixed", 0.015), ("fixed", 0.02), ("none", 0.0)]

    header = (f"{'OR':>3}{'cutoff':>8}{'stop':>10}{'IS n':>6}{'IS bp':>8}{'IS t':>6}"
              f"{'OOS n':>7}{'OOS bp':>8}{'OOS t':>7}{'both':>6}")
    print(header)
    print("-" * len(header))
    rows = []
    for or_minutes, cutoff in itertools.product((5, 15, 30), (10 * 60, 10 * 60 + 30, 11 * 60)):
        for spec in specs:
            rule = make_rule(or_minutes, cutoff, spec)
            ins = summarize(evaluate(sample, sessions, rule, cost_bp), len(sample))
            oo = summarize(evaluate(oos, sessions, rule, cost_bp), len(oos))
            if ins["n"] < 80 or oo["n"] < 40:
                continue
            label = f"{spec[0]}{'' if spec[0] == 'none' else f'x{spec[1]:g}'}"
            rows.append((ins["net_bp"], oo["net_bp"]))
            print(f"{or_minutes:>3}{cutoff // 60:>5}:{cutoff % 60:02d}{label:>10}"
                  f"{ins['n']:>6}{ins['net_bp']:>8.1f}{ins['t']:>6.2f}"
                  f"{oo['n']:>7}{oo['net_bp']:>8.1f}{oo['t']:>7.2f}"
                  f"{'YES' if ins['net_bp'] > 0 and oo['net_bp'] > 0 else '':>6}")
    print(f"\n{sum(1 for a, b in rows if a > 0 and b > 0)}/{len(rows)} positive in both halves")
    return 0


def stage3() -> int:
    """Full-sample detail for the shortlisted variants."""
    import statistics
    sessions = load_sessions()
    attach_context(sessions)
    dates = sorted(sessions)
    cost = load_cost()
    cost_bp = cost["half_spread_bp"] * 2 + cost["slippage_bp"] * 2

    for or_minutes in (5, 15, 30):
        for date in dates:
            sessions[date].setdefault("breaks", {})[or_minutes] = \
                scan_breaks(sessions[date], or_minutes)

    def make_rule(or_minutes, cutoff, stop_mult):
        def rule(session):
            breaks = session["breaks"][or_minutes]
            if not breaks:
                return None
            direction = min(breaks, key=lambda d: breaks[d][0])
            idx, _ = breaks[direction]
            if OPEN_MIN + idx > cutoff:
                return None
            n = min(or_minutes, last_index(session) - 1)
            range_size = float(session["high"][:n].max() - session["low"][:n].min())
            return idx, direction, {"stop_pct": range_size * stop_mult / float(session["close"][idx])}
        return rule

    shortlist = [
        ("OR15 / 10:30 / 1.0x", 15, 10 * 60 + 30, 1.0),
        ("OR15 / 10:30 / 2.0x", 15, 10 * 60 + 30, 2.0),
        ("OR30 / 10:30 / 1.0x", 30, 10 * 60 + 30, 1.0),
        ("OR30 / 10:30 / 2.0x", 30, 10 * 60 + 30, 2.0),
        ("OR30 / 11:00 / 1.5x", 30, 11 * 60, 1.5),
    ]
    for label, or_minutes, cutoff, stop_mult in shortlist:
        trades = evaluate(dates[1:], sessions, make_rule(or_minutes, cutoff, stop_mult), cost_bp)
        net = np.array([t.net_ret for t in trades])
        gross = np.array([t.gross_ret for t in trades])
        wins, losses = net[net > 0], net[net <= 0]
        equity = np.cumprod(1 + net)
        peak = np.maximum.accumulate(equity)
        t_stat = net.mean() / (net.std(ddof=1) / np.sqrt(len(net)))
        print(f"\n=== {label} ===")
        print(f"  trades {len(trades)}  net {net.mean()*1e4:+.1f} bp  "
              f"gross {gross.mean()*1e4:+.1f} bp  t={t_stat:.2f}  hit {len(wins)/len(net):.1%}")
        print(f"  avg win {wins.mean()*100:+.2f}%  avg loss {losses.mean()*100:+.2f}%  "
              f"payoff {abs(wins.mean()/losses.mean()):.2f}  "
              f"best {net.max()*100:+.1f}%  worst {net.min()*100:+.1f}%")
        print(f"  equity {equity[-1]-1:+.1%}  maxDD {(equity/peak-1).min():.1%}")
        by_reason = {}
        for trade in trades:
            by_reason.setdefault(trade.reason, []).append(trade.net_ret)
        print("  exits: " + "  ".join(
            f"{reason} {len(v)} ({np.mean(v)*100:+.2f}%)"
            for reason, v in sorted(by_reason.items(), key=lambda kv: -len(kv[1]))))
        years = {}
        for trade in trades:
            years.setdefault(trade.date[:4], []).append(trade.net_ret)
        print("  by year: " + "  ".join(
            f"{y} {np.mean(v)*1e4:+.0f}bp n={len(v)}" for y, v in sorted(years.items())))
    return 0




def stage4() -> int:
    """Adversarial checks on the chosen variant."""
    import random
    sessions = load_sessions()
    attach_context(sessions)
    dates = sorted(sessions)
    cost = load_cost()
    cost_bp = cost["half_spread_bp"] * 2 + cost["slippage_bp"] * 2
    for or_minutes in (5, 15, 30):
        for date in dates:
            sessions[date].setdefault("breaks", {})[or_minutes] = \
                scan_breaks(sessions[date], or_minutes)

    def make_rule(or_minutes, cutoff, stop_mult, direction_override=None):
        def rule(session):
            breaks = session["breaks"][or_minutes]
            if not breaks:
                return None
            direction = min(breaks, key=lambda d: breaks[d][0])
            idx, _ = breaks[direction]
            if OPEN_MIN + idx > cutoff:
                return None
            n = min(or_minutes, last_index(session) - 1)
            range_size = float(session["high"][:n].max() - session["low"][:n].min())
            if direction_override is not None:
                direction = direction_override(session, idx)
            return idx, direction, {"stop_pct": range_size * stop_mult / float(session["close"][idx])}
        return rule

    chosen = ("OR30/10:30/2.0x", 30, 10 * 60 + 30, 2.0)
    label, or_minutes, cutoff, stop_mult = chosen
    rule = make_rule(or_minutes, cutoff, stop_mult)
    trades = evaluate(dates[1:], sessions, rule, cost_bp)
    net = np.array([t.net_ret for t in trades])

    print(f"=== {label} ===")
    for direction, name in ((1, "long"), (-1, "short")):
        subset = np.array([t.net_ret for t in trades if t.direction == direction])
        print(f"  {name:<6} n={len(subset):>4}  {subset.mean()*1e4:+6.1f} bp  "
              f"t={subset.mean()/(subset.std(ddof=1)/np.sqrt(len(subset))):5.2f}  "
              f"hit {(subset > 0).mean():.1%}")

    # Regime dependence: drop the 2022 crash year and see what survives.
    for excluded in (["2022"], ["2024"], []):
        subset = np.array([t.net_ret for t in trades if t.date[:4] not in excluded])
        tag = f"excl {','.join(excluded)}" if excluded else "all years"
        print(f"  {tag:<14} n={len(subset):>4}  {subset.mean()*1e4:+6.1f} bp  "
              f"t={subset.mean()/(subset.std(ddof=1)/np.sqrt(len(subset))):5.2f}")

    # Null: same entry bars and exit rule, but the side is a coin flip.
    random.seed(7)
    null_means = []
    for _ in range(200):
        null_rule = make_rule(or_minutes, cutoff, stop_mult,
                              direction_override=lambda s, i: 1 if random.random() < 0.5 else -1)
        null = np.array([t.net_ret for t in evaluate(dates[1:], sessions, null_rule, cost_bp)])
        null_means.append(null.mean() * 1e4)
    null_means = np.array(null_means)
    observed = net.mean() * 1e4
    p_value = float((null_means >= observed).mean())
    print(f"\n  random-direction null (200 draws): mean {null_means.mean():+.1f} bp, "
          f"sd {null_means.std():.1f}, 95th pct {np.percentile(null_means, 95):+.1f} bp")
    print(f"  observed {observed:+.1f} bp  →  p = {p_value:.3f}")
    return 0


if __name__ == "__main__":
    if "--stage4" in sys.argv:
        sys.exit(stage4())
    if "--stage3" in sys.argv:
        sys.exit(stage3())
    sys.exit(stage2() if "--stage2" in sys.argv else main())
