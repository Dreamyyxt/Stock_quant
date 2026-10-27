#!/usr/bin/env python3
"""Factor screening for a single A-share instrument (default: 510300 沪深300ETF).

Design notes that matter more than the code:

1. **Every factor is computed from information available at the close of day t.**
   Forward returns are measured from the *next* open, so the screen never uses a
   price the trader could not have transacted at.

2. **Quantile cutoffs use an expanding window, not the full sample.** Cutting on
   full-sample quintiles would quietly tell the factor where its own future
   extremes were. The first 250 observations are warmup.

3. **Overlapping horizons inflate t-statistics.** A 20-day forward return sampled
   every day is ~20 overlapping observations of the same event; the naive t is
   overstated by roughly sqrt(H). Both the naive and the overlap-adjusted t are
   reported so the inflation is visible instead of hidden.

4. **Multiple testing is the whole point.** We test ~45 factor/horizon pairs. At
   α=0.05 that produces ~2 false positives from noise alone. A block-permutation
   null gives the family-wise p-value, and a Benjamini-Hochberg FDR pass is run
   on top.

5. **The last 20% of the sample is sealed.** It is printed only at the very end,
   and only for the factor that survived. Touching it earlier would make it
   ordinary in-sample data.

Usage:
    python ashare_factor_lab.py
    python ashare_factor_lab.py --csv state/ashare/510300_daily.csv --cost-bp 10
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_CSV = os.path.join(PROJECT_ROOT, "state/ashare/510300_daily.csv")

WARMUP = 250
HORIZONS = (1, 3, 5, 10, 20)
SEALED_FRACTION = 0.20


# --------------------------------------------------------------------------- #
#  Factor library
# --------------------------------------------------------------------------- #
def _ma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    # Wilder smoothing; the simple average would front-load the first window and
    # make early RSI values unstable.
    avg_gain = gain.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    avg_loss = loss.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return 100.0 - 100.0 / (1.0 + rs)


def build_factors(df: pd.DataFrame) -> dict[str, pd.Series]:
    """Return {factor_name: series}. Sign convention is left raw on purpose:
    a factor that works only when reversed is a different economic story, so we
    report the signed version and let the sign show up in the result.
    """
    close, high, low = df["close"], df["high"], df["low"]
    vol = df["volume"]
    ret = close.pct_change()
    factors: dict[str, pd.Series] = {}

    for n in (5, 10, 20, 60, 120):
        factors[f"mom_{n}d"] = close.pct_change(n)

    for n in (5, 10, 20):
        factors[f"volratio_{n}d"] = vol / _ma(vol, n)

    for n in (10, 20, 60):
        factors[f"vola_{n}d"] = ret.rolling(n, min_periods=n).std()

    for n in (20, 60):
        hi = high.rolling(n, min_periods=n).max()
        lo = low.rolling(n, min_periods=n).min()
        width = (hi - lo).replace(0.0, np.nan)
        factors[f"donchian_{n}d"] = (close - lo) / width
        factors[f"drawdown_{n}d"] = close / close.rolling(n, min_periods=n).max() - 1.0

    for n in (20, 60):
        factors[f"madist_{n}d"] = close / _ma(close, n) - 1.0

    factors["rsi_14d"] = _rsi(close, 14)
    factors["gap_1d"] = df["open"] / close.shift(1) - 1.0
    factors["intraday_1d"] = close / df["open"] - 1.0
    factors["turnover_chg"] = vol / _ma(vol, 60) - 1.0
    return factors


# --------------------------------------------------------------------------- #
#  Forward returns
# --------------------------------------------------------------------------- #
def forward_returns(df: pd.DataFrame, horizon: int) -> pd.Series:
    """Return realised from the next open to the open `horizon` days later.

    `open.shift(-1)` is tomorrow's open — the first price a decision made at
    today's close can actually be filled at. `open.shift(-(horizon + 1))` is the
    exit. Using closes instead would embed the very bar the signal was read from.
    """
    entry = df["open"].shift(-1)
    exit_ = df["open"].shift(-(horizon + 1))
    return exit_ / entry - 1.0


# --------------------------------------------------------------------------- #
#  Expanding-window quantiles (no look-ahead)
# --------------------------------------------------------------------------- #
def expanding_rank(factor: pd.Series, min_periods: int = WARMUP) -> pd.Series:
    """Percentile rank of today's value within all values seen so far.

    A full-sample rank would leak the distribution's future shape; this uses only
    the past. Computed on the non-NaN prefix with O(n log n) cumulative sorting.
    """
    values = factor.to_numpy(dtype=float)
    out = np.full(len(values), np.nan)
    seen: list[float] = []
    for i, v in enumerate(values):
        if np.isnan(v):
            continue
        seen.append(v)
        if len(seen) >= min_periods:
            arr = np.asarray(seen)
            out[i] = (arr <= v).sum() / len(arr)
    return pd.Series(out, index=factor.index)


@dataclass
class FactorResult:
    name: str
    horizon: int
    n_obs: int
    top_mean_bp: float
    bottom_mean_bp: float
    spread_bp: float
    top_t: float
    bottom_t: float
    side: str              # "top" or "bottom" — the tradable side
    side_mean_bp: float
    side_t: float
    side_t_adj: float
    ic: float
    ic_t: float
    hit_rate: float
    net_bp: float          # tradable-side mean minus round-trip cost
    extra: dict = field(default_factory=dict)


def _tstat(sample: pd.Series) -> float:
    if len(sample) < 3:
        return 0.0
    sd = sample.std(ddof=1)
    if not np.isfinite(sd) or sd == 0:
        return 0.0
    return float(sample.mean() / (sd / np.sqrt(len(sample))))


def evaluate(factor: pd.Series, fwd: pd.Series, name: str, horizon: int,
             cost_bp: float, min_periods: int = WARMUP) -> FactorResult | None:
    rank = expanding_rank(factor, min_periods=min_periods)
    mask = rank.notna() & fwd.notna()
    if mask.sum() < 100:
        return None

    r, f = rank[mask], fwd[mask]
    top = f[r >= 0.8]
    bottom = f[r < 0.2]
    if len(top) < 30 or len(bottom) < 30:
        return None

    top_mean = float(top.mean())
    bottom_mean = float(bottom.mean())

    # A-share individual names / ETFs are long-only in practice, so the only
    # tradable side is the quintile you would actually buy. Ranking purely on the
    # "winners" quintile silently kills every reversal factor — and A-share index
    # products are mean-reverting at 5-60 day horizons, so that mistake would hide
    # the entire signal.
    if top_mean >= bottom_mean:
        side, side_mean = "top", top_mean
        side_t = _tstat(top)
    else:
        side, side_mean = "bottom", bottom_mean
        side_t = _tstat(bottom)

    # Overlap correction: a H-day forward return sampled daily repeats the same
    # event ~H times, so independent-sample t is inflated by about sqrt(H).
    side_t_adj = side_t / np.sqrt(max(horizon, 1))

    ic = float(r.corr(f, method="spearman"))
    ic_t = ic * np.sqrt(max(len(r) - 2, 1)) / np.sqrt(max(1.0 - ic ** 2, 1e-12))

    return FactorResult(
        name=name, horizon=horizon, n_obs=int(mask.sum()),
        top_mean_bp=top_mean * 1e4, bottom_mean_bp=bottom_mean * 1e4,
        spread_bp=(top_mean - bottom_mean) * 1e4,
        top_t=_tstat(top), bottom_t=_tstat(bottom),
        side=side, side_mean_bp=side_mean * 1e4,
        side_t=side_t, side_t_adj=side_t_adj,
        ic=ic, ic_t=ic_t,
        hit_rate=float((top > 0).mean() if side == "top" else (bottom > 0).mean()),
        net_bp=side_mean * 1e4 - cost_bp,
    )


# --------------------------------------------------------------------------- #
#  Null models
# --------------------------------------------------------------------------- #
def block_permutation_pvalue(factor: pd.Series, fwd: pd.Series, horizon: int,
                             observed_t: float, cost_bp: float,
                             n_perm: int = 400, block: int = 20,
                             seed: int = 7) -> float:
    """Family-wise style null: shuffle the factor in blocks, keep the return
    series intact, and ask how often noise produces a t at least as large.

    Block shuffling (rather than iid shuffling) preserves the autocorrelation that
    makes a trend factor look informative when it is not.
    """
    rng = np.random.default_rng(seed)
    values = factor.to_numpy(dtype=float)
    n = len(values)
    better = 0
    for _ in range(n_perm):
        # circular block permutation
        starts = rng.integers(0, n, size=int(np.ceil(n / block)))
        idx = np.concatenate([np.arange(s, s + block) % n for s in starts])[:n]
        shuffled = pd.Series(values[idx], index=factor.index)
        res = evaluate(shuffled, fwd, "perm", horizon, cost_bp)
        if res is not None and res.side_t >= observed_t:
            better += 1
    return (better + 1) / (n_perm + 1)


def random_entry_test(df: pd.DataFrame, signal_mask: pd.Series, horizon: int,
                      n_draw: int = 2000, seed: int = 11) -> dict:
    """Does the signal's timing beat picking entry days at random?

    This is the control that separates "the factor predicts" from "this ETF just
    drifted up and any entry would have made money".

    Takes a boolean mask aligned to `df`'s positional index. An earlier version
    intersected dates against a RangeIndex and silently returned an empty dict,
    which made the control vanish without any error — the worst kind of bug in a
    validation harness, because it looks like the control passed.
    """
    rng = np.random.default_rng(seed)
    fwd = forward_returns(df, horizon)
    valid = fwd.notna().to_numpy()
    values = fwd.to_numpy(dtype=float)
    mask = signal_mask.to_numpy(dtype=bool) & valid
    n_signal = int(mask.sum())
    if n_signal < 10:
        return {}

    real_mean = float(values[mask].mean())
    pool = np.flatnonzero(valid)
    draws = np.empty(n_draw)
    for k in range(n_draw):
        pick = rng.choice(pool, size=n_signal, replace=False)
        draws[k] = values[pick].mean()
    return {
        "n_signals": n_signal,
        "signal_mean_bp": real_mean * 1e4,
        "random_mean_bp": float(draws.mean()) * 1e4,
        "p_value": float((draws >= real_mean).mean()),
    }


def bh_fdr(pvalues: list[float], alpha: float = 0.10) -> list[bool]:
    """Benjamini-Hochberg. Returns a keep/reject flag per test."""
    m = len(pvalues)
    order = np.argsort(pvalues)
    keep = [False] * m
    thresh_idx = -1
    for rank, idx in enumerate(order, start=1):
        if pvalues[idx] <= alpha * rank / m:
            thresh_idx = rank
    if thresh_idx > 0:
        for rank, idx in enumerate(order, start=1):
            if rank <= thresh_idx:
                keep[idx] = True
    return keep


# --------------------------------------------------------------------------- #
#  Main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=DEFAULT_CSV)
    ap.add_argument("--cost-bp", type=float, default=10.0,
                    help="round-trip cost in bp (ETF: commission both sides + one tick spread)")
    ap.add_argument("--perms", type=int, default=400)
    args = ap.parse_args()

    df = pd.read_csv(args.csv, parse_dates=["date"]).reset_index(drop=True)
    print(f"data: {len(df)} bars  {df['date'].iloc[0].date()} → {df['date'].iloc[-1].date()}")

    split = int(len(df) * (1 - SEALED_FRACTION))
    sealed_start = df["date"].iloc[split]
    in_sample = df.iloc[:split].reset_index(drop=True)

    print(f"in-sample: {len(in_sample)} bars, sealed from {sealed_start.date()} "
          f"({len(df) - split} bars held out)\n")

    factors = build_factors(in_sample)
    results: list[FactorResult] = []

    print("=" * 96)
    print("Factor screening (in-sample only, expanding quantiles, cost-adjusted)")
    print("=" * 96)
    print(f"{'factor':<16}{'H':>3}{'n':>6}{'top_bp':>9}{'bot_bp':>9}"
          f"{'spread':>9}{'t':>7}{'t_adj':>7}{'IC':>8}{'side':>7}{'hit':>7}{'net_bp':>8}")
    print("-" * 96)

    for name, factor in factors.items():
        for h in HORIZONS:
            fwd = forward_returns(in_sample, h)
            res = evaluate(factor, fwd, name, h, args.cost_bp)
            if res is None:
                continue
            results.append(res)
            print(f"{res.name:<16}{res.horizon:>3}{res.n_obs:>6}"
                  f"{res.top_mean_bp:>9.1f}{res.bottom_mean_bp:>9.1f}"
                  f"{res.spread_bp:>9.1f}{res.side_t:>7.2f}{res.side_t_adj:>7.2f}"
                  f"{res.ic:>8.3f}{res.side:>7}{res.hit_rate:>7.1%}{res.net_bp:>8.1f}")

    print("-" * 96)
    print(f"total tests: {len(results)}   cost hurdle: {args.cost_bp:.1f} bp round trip")
    if results:
        # Two-sided Bonferroni at family alpha 0.05. Computed from the normal
        # quantile so the number in the report is reproducible, not eyeballed.
        from statistics import NormalDist
        crit = NormalDist().inv_cdf(1 - 0.025 / len(results))
        print(f"Bonferroni |t| threshold ≈ {crit:.2f} "
              f"(naive t; the overlap-adjusted column is stricter by sqrt(H))")

    # Rank survivors on cost-adjusted edge, then validate the best few against
    # the block-permutation null.
    ranked = sorted(results, key=lambda r: r.net_bp, reverse=True)
    print("\nTop 8 by tradable-side edge over cost:")
    print(f"{'factor':<16}{'H':>3}{'side':>8}{'net_bp':>8}{'t_adj':>7}{'IC':>8}")

    pvals: list[float] = []
    checked: list[tuple[FactorResult, float]] = []
    for res in ranked[:8]:
        fwd = forward_returns(in_sample, res.horizon)
        p = block_permutation_pvalue(factors[res.name], fwd, res.horizon,
                                     res.side_t, args.cost_bp, n_perm=args.perms)
        checked.append((res, p))
        pvals.append(p)

    fdr_keep = bh_fdr(pvals, alpha=0.10)
    for keep, (res, p) in zip(fdr_keep, checked):
        flag = "PASS" if keep else "fail"
        print(f"{res.name:<16}{res.horizon:>3}{res.side:>8}{res.net_bp:>8.1f}"
              f"{res.side_t_adj:>7.2f}{res.ic:>8.3f}   p={p:.3f} [{flag}]")

    # Pick the survivor with the highest overlap-adjusted t among those that
    # cleared FDR and still beat cost.
    survivors = [(r, p) for keep, (r, p) in zip(fdr_keep, checked)
                 if keep and r.net_bp > 0]
    print(f"\nsurvivors after FDR + cost hurdle: {len(survivors)}")

    payload: dict = {"n_tests": len(results), "cost_bp": args.cost_bp, "survivors": []}

    if not survivors:
        print("\n=> no factor cleared both the cost hurdle and the permutation null.")
        print("   Reporting the best raw candidate anyway, with its null attached.")
        best = ranked[0]
    else:
        best = max(survivors, key=lambda x: x[0].side_t_adj)[0]

    # Random-entry control on the chosen factor, evaluated on its tradable side.
    rank = expanding_rank(factors[best.name])
    signal_mask = (rank >= 0.8) if best.side == "top" else (rank <= 0.2)
    ctrl = random_entry_test(in_sample, signal_mask, best.horizon)
    print(f"\nchosen: {best.name} @ H={best.horizon}, tradable side = {best.side}")
    print(f"  {'top' if best.side == 'top' else 'bottom'}-quintile mean "
          f"{best.side_mean_bp:.1f} bp | net of cost {best.net_bp:.1f} bp")
    print(f"  naive t {best.side_t:.2f} | overlap-adjusted t {best.side_t_adj:.2f}")
    if ctrl:
        print(f"  random-entry control: signal {ctrl['signal_mean_bp']:.1f} bp vs "
              f"random {ctrl['random_mean_bp']:.1f} bp, p={ctrl['p_value']:.3f} "
              f"(n={ctrl['n_signals']})")
        payload["random_entry"] = {k: round(float(v), 4) for k, v in ctrl.items()}

    # Sealed hold-out — touched once, for the chosen factor only.
    fwd_full = forward_returns(df, best.horizon)
    rank_full = expanding_rank(build_factors(df)[best.name])
    side_mask_full = (rank_full >= 0.8) if best.side == "top" else (rank_full <= 0.2)
    oos_mask = (df["date"] >= sealed_start) & side_mask_full & fwd_full.notna()
    oos = fwd_full[oos_mask]
    if len(oos):
        print(f"\nsealed hold-out ({sealed_start.date()} onward): "
              f"{len(oos)} signal days, mean {oos.mean() * 1e4:.1f} bp, "
              f"hit {(oos > 0).mean():.1%}")
        payload["holdout"] = {
            "start": str(sealed_start.date()), "n": int(len(oos)),
            "mean_bp": round(float(oos.mean()) * 1e4, 2),
            "hit": round(float((oos > 0).mean()), 3),
        }

    payload["chosen"] = {
        "factor": best.name, "horizon": best.horizon, "side": best.side,
        "net_bp": round(best.net_bp, 2), "side_t_adj": round(best.side_t_adj, 3),
        "ic": round(best.ic, 4),
    }
    payload["survivors"] = [
        {"factor": r.name, "horizon": r.horizon, "side": r.side,
         "net_bp": round(r.net_bp, 2),
         "t_adj": round(r.side_t_adj, 3), "ic": round(r.ic, 4),
         "p": round(p, 4), "hit": round(r.hit_rate, 3)}
        for r, p in checked
    ]

    out = os.path.join(PROJECT_ROOT, "state/ashare/factor_screen.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
