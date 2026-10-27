#!/usr/bin/env python3
"""
Single-symbol intraday strategy lab — which entry rule family actually has an edge?

Research code. The delivered strategy lives in tsla_backtest.py.

Conventions that keep the result honest
---------------------------------------
* A signal is evaluated on the CLOSE of minute i and filled at the OPEN of
  minute i+1. Nothing is traded at a price that only became knowable afterwards.
* One position at a time, at most one entry per session — a single-name retail
  system cannot run 20 overlapping trades.
* Costs are charged on both sides: measured half-spread + a slippage allowance.
* Everything is flat before the close; an intraday rule must not silently become
  an overnight rule.
* Sessions are split by date into in-sample / out-of-sample. Rules are chosen on
  the in-sample half only, then checked on the untouched half.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DATA_PATH = os.path.join(ROOT, "state", "tsla_1min.csv")
COST_PATH = os.path.join(ROOT, "state", "tsla_cost.json")

OPEN_MIN = 9 * 60 + 30
CLOSE_MIN = 15 * 60 + 59
EOD_IDX = 389


# ---------------------------------------------------------------------- #
#  Data
# ---------------------------------------------------------------------- #

def load_sessions(path: str = DATA_PATH, min_bars: int = 385,
                  drop_split_days: bool = True) -> Dict[str, dict]:
    """Return {date: dict of arrays} for complete regular sessions, split-adjusted."""
    frame = pd.read_csv(path)
    # Mixed UTC offsets across DST boundaries make the column object-typed;
    # parse as UTC first, then pin to New York.
    frame["ts"] = pd.to_datetime(frame["timestamp"], utc=True).dt.tz_convert("America/New_York")
    frame["date"] = frame["ts"].dt.strftime("%Y-%m-%d")
    frame["minute"] = frame["ts"].dt.hour * 60 + frame["ts"].dt.minute
    frame = frame[(frame["minute"] >= OPEN_MIN) & (frame["minute"] <= CLOSE_MIN)]
    frame = frame.sort_values("ts")

    # The provider returns raw prices. A stock split would leave a fake -67% gap
    # in the series, which would poison every level-based rule and the daily
    # moving average, so rescale everything before the split.
    daily_close = frame.groupby("date")["close"].last()
    factors: Dict[str, float] = {}
    ratio_events = []
    for prev_date, cur_date in zip(daily_close.index, daily_close.index[1:]):
        ratio = daily_close[cur_date] / daily_close[prev_date]
        if ratio < 0.6:
            split = round(1 / ratio)
            ratio_events.append((cur_date, split))
    cumulative = 1.0
    scale = {d: 1.0 for d in daily_close.index}
    for split_date, split in reversed(ratio_events):
        cumulative *= split
        for d in daily_close.index:
            if d < split_date:
                scale[d] = cumulative
    if ratio_events:
        print(f"Split adjustments applied: {ratio_events}")

    sessions: Dict[str, dict] = {}
    dropped = 0
    for date, group in frame.groupby("date", sort=True):
        if len(group) < min_bars:
            dropped += 1
            continue
        if drop_split_days and any(date == d for d, _ in ratio_events):
            dropped += 1
            continue
        factor = scale.get(date, 1.0)
        sessions[date] = {
            "ts": group["ts"].to_numpy(),
            "open": group["open"].to_numpy(dtype=float) / factor,
            "high": group["high"].to_numpy(dtype=float) / factor,
            "low": group["low"].to_numpy(dtype=float) / factor,
            "close": group["close"].to_numpy(dtype=float) / factor,
            "volume": group["volume"].to_numpy(dtype=float) * factor,
            "minute": group["minute"].to_numpy(),
        }
    print(f"Loaded {len(sessions):,} sessions "
          f"({min(sessions)} -> {max(sessions)}), dropped {dropped} partial/split days")
    return sessions


def load_cost(path: str = COST_PATH) -> dict:
    default = {"half_spread_bp": 0.6, "slippage_bp": 0.5}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            doc = json.load(handle)
        return {
            "half_spread_bp": float(doc.get("half_spread_median_bp", default["half_spread_bp"])),
            "slippage_bp": float(doc.get("slippage_bp", default["slippage_bp"])),
        }
    except Exception:
        return default


def attach_context(sessions: Dict[str, dict], sma_window: int = 200) -> None:
    """Prior-session levels and a daily trend regime, all known before the open."""
    dates = sorted(sessions)
    closes = pd.Series({d: float(sessions[d]["close"][-1]) for d in dates})
    sma = closes.rolling(sma_window).mean().shift(1)     # yesterday's average
    for prev, cur in zip(dates, dates[1:]):
        sessions[cur]["prev_close"] = float(sessions[prev]["close"][-1])
        sessions[cur]["prev_high"] = float(sessions[prev]["high"].max())
        sessions[cur]["prev_low"] = float(sessions[prev]["low"].min())
    for date in dates:
        value = sma.get(date)
        sessions[date]["sma_ref"] = float(value) if value == value and value else None
        sessions[date]["prev_close_level"] = float(closes.get(date, 0))


def regime_of(session: dict) -> int:
    """+1 above the 200-day average, -1 below, 0 unknown."""
    ref = session.get("sma_ref")
    if not ref:
        return 0
    level = session.get("prev_close_level") or session["open"][0]
    return 1 if level >= ref else -1


# ---------------------------------------------------------------------- #
#  Features
# ---------------------------------------------------------------------- #

def vwap_series(high, low, close, volume) -> np.ndarray:
    typical = (high + low + close) / 3.0
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(np.cumsum(volume) > 0,
                        np.cumsum(typical * volume) / np.cumsum(volume), close)


def rel_volume(volume: np.ndarray, window: int = 5, lookback: int = 60) -> np.ndarray:
    series = pd.Series(volume)
    recent = series.rolling(window).mean()
    baseline = series.rolling(window).mean().rolling(lookback, min_periods=10).median()
    with np.errstate(divide="ignore", invalid="ignore"):
        out = (recent / baseline).to_numpy()
    return np.where(np.isfinite(out), out, 1.0)


# ---------------------------------------------------------------------- #
#  Simulator
# ---------------------------------------------------------------------- #

class Trade:
    __slots__ = ("date", "direction", "entry_idx", "exit_idx", "entry_price",
                 "exit_price", "gross_ret", "net_ret", "reason")

    def __init__(self, date, direction, entry_idx, exit_idx, entry_price,
                 exit_price, gross_ret, net_ret, reason):
        self.date = date
        self.direction = direction
        self.entry_idx = entry_idx
        self.exit_idx = exit_idx
        self.entry_price = entry_price
        self.exit_price = exit_price
        self.gross_ret = gross_ret
        self.net_ret = net_ret
        self.reason = reason


def simulate(session: dict, date: str, entry_idx: Optional[int], direction: int,
             cost_bp: float, stop_pct: float = 0.0, target_pct: float = 0.0,
             max_hold: int = 0, stop_price: float = 0.0) -> Optional[Trade]:
    """Fill at the next bar's open, then manage to stop / target / time / close."""
    if entry_idx is None:
        return None
    fill_idx = entry_idx + 1
    if fill_idx > last_index(session) - 1:
        return None

    open_px, high, low, close = (session["open"], session["high"],
                                 session["low"], session["close"])
    entry_price = float(open_px[fill_idx])
    if not np.isfinite(entry_price) or entry_price <= 0:
        return None

    if stop_price:
        stop_pct = max(stop_pct, abs(entry_price - stop_price) / entry_price)

    last_idx = min(EOD_IDX, len(close) - 1)
    exit_idx, exit_price, reason = last_idx, float(close[last_idx]), "close"

    for idx in range(fill_idx, last_idx + 1):
        held = idx - fill_idx
        if stop_pct > 0:
            if direction > 0 and low[idx] <= entry_price * (1 - stop_pct):
                exit_idx, exit_price, reason = idx, entry_price * (1 - stop_pct), "stop"
                break
            if direction < 0 and high[idx] >= entry_price * (1 + stop_pct):
                exit_idx, exit_price, reason = idx, entry_price * (1 + stop_pct), "stop"
                break
        if target_pct > 0:
            if direction > 0 and high[idx] >= entry_price * (1 + target_pct):
                exit_idx, exit_price, reason = idx, entry_price * (1 + target_pct), "target"
                break
            if direction < 0 and low[idx] <= entry_price * (1 - target_pct):
                exit_idx, exit_price, reason = idx, entry_price * (1 - target_pct), "target"
                break
        if max_hold > 0 and held >= max_hold:
            exit_idx, exit_price, reason = idx, float(close[idx]), "time"
            break

    gross = direction * (exit_price / entry_price - 1)
    return Trade(date, direction, fill_idx, exit_idx, entry_price, exit_price,
                 gross, gross - cost_bp / 10000.0, reason)



def last_index(session: dict) -> int:
    """Some sessions lose a bar or two; never index past the actual array."""
    return min(EOD_IDX, len(session["close"]) - 1)


# ---------------------------------------------------------------------- #
#  Rule families (each sees only data up to the decision bar)
# ---------------------------------------------------------------------- #

def rule_orb(session, or_minutes=30, vol_mult=0.0, long_only=True, min_momentum=0.0):
    n = min(or_minutes, len(session["close"]) - 2)
    high, low, close = session["high"], session["low"], session["close"]
    or_high, or_low = high[:n].max(), low[:n].min()
    rvol = rel_volume(session["volume"])
    for idx in range(n - 1, last_index(session)):
        if vol_mult > 0 and rvol[idx] < vol_mult:
            continue
        if min_momentum > 0:
            if idx < 16:
                continue
            if close[idx] / close[idx - 15] - 1 < min_momentum:
                continue
        if close[idx] > or_high:
            return idx, 1, dict(stop_price=or_low)
        if not long_only and close[idx] < or_low:
            return idx, -1, dict(stop_price=or_high)
    return None


def rule_orb_regime(session, or_minutes=30, vol_mult=0.0, trade_with_trend=True):
    """Opening-range breakout, but only in the direction of the daily trend."""
    decision = rule_orb(session, or_minutes=or_minutes, vol_mult=vol_mult,
                        long_only=False)
    if not decision:
        return None
    idx, direction, extra = decision
    regime = regime_of(session)
    if regime == 0:
        return None
    if trade_with_trend and direction != regime:
        return None
    if not trade_with_trend and direction == regime:
        return None
    return idx, direction, extra


def rule_orb_retest(session, or_minutes=30):
    n = min(or_minutes, len(session["close"]) - 2)
    high, close = session["high"], session["close"]
    or_high = high[:n].max()
    broke_at = None
    for idx in range(n - 1, last_index(session)):
        if broke_at is None:
            if close[idx] > or_high:
                broke_at = idx
            continue
        if or_high * 0.997 < close[idx] <= or_high * 1.001 and close[idx] > close[idx - 1]:
            return idx, 1, dict()
        if close[idx] < or_high * 0.99:
            return None
    return None


def rule_gap_momentum(session, gap_min_pct=0.01, direction=1):
    prev_close = session.get("prev_close")
    if not prev_close:
        return None
    gap = session["open"][0] / prev_close - 1
    if direction > 0 and gap < gap_min_pct:
        return None
    if direction < 0 and gap > -gap_min_pct:
        return None
    return 2, direction, dict()


def rule_gap_fade(session, gap_min_pct=0.01):
    prev_close = session.get("prev_close")
    if not prev_close:
        return None
    gap = session["open"][0] / prev_close - 1
    if abs(gap) < gap_min_pct:
        return None
    return 2, (-1 if gap > 0 else 1), dict()


def rule_vwap_reversion(session, dev_pct=0.006):
    close = session["close"]
    vwap = vwap_series(session["high"], session["low"], close, session["volume"])
    dev = close / vwap - 1
    for idx in range(30, last_index(session)):
        if dev[idx] <= -dev_pct and close[idx] > close[idx - 1]:
            return idx, 1, dict()
    return None


def rule_first_hour_momentum(session, threshold=0.01):
    close = session["close"]
    idx = 60
    if idx >= last_index(session) - 1:
        return None
    ret = close[idx] / session["open"][0] - 1
    if ret >= threshold:
        return idx, 1, dict()
    if ret <= -threshold:
        return idx, -1, dict()
    return None


def rule_first_hour_fade(session, threshold=0.01):
    close = session["close"]
    idx = 60
    if idx >= last_index(session) - 1:
        return None
    ret = close[idx] / session["open"][0] - 1
    if ret >= threshold:
        return idx, -1, dict()
    if ret <= -threshold:
        return idx, 1, dict()
    return None


def rule_prev_day_level(session, side="high"):
    level = session.get("prev_high" if side == "high" else "prev_low")
    if not level:
        return None
    close = session["close"]
    for idx in range(5, last_index(session)):
        if side == "high" and close[idx] > level:
            return idx, 1, dict()
        if side == "low" and close[idx] < level:
            return idx, -1, dict()
    return None


def rule_late_day_momentum(session, threshold=0.01, at_minute=15 * 60):
    close = session["close"]
    idx = at_minute - OPEN_MIN
    if idx >= last_index(session) - 1:
        return None
    ret = close[idx] / session["open"][0] - 1
    if ret >= threshold:
        return idx, 1, dict()
    if ret <= -threshold:
        return idx, -1, dict()
    return None



def rule_orb_vol(session, or_minutes=30, vol_window=10, long_only=False):
    """Opening-range breakout, only on days when TSLA's own volatility is elevated."""
    realized = session.get("realized_vol")
    median = session.get("vol_median")
    if realized is None or median is None or realized < median:
        return None
    return rule_orb(session, or_minutes=or_minutes, long_only=long_only)


def rule_orb_gap(session, or_minutes=30, min_gap=0.005):
    """Breakout that agrees with the opening gap direction."""
    decision = rule_orb(session, or_minutes=or_minutes, long_only=False)
    if not decision:
        return None
    prev = session.get("prev_close")
    if not prev:
        return None
    gap = session["open"][0] / prev - 1
    if gap == 0 or decision[1] != (1 if gap > 0 else -1) or abs(gap) < min_gap:
        return None
    return decision


def rule_orb_morning(session, or_minutes=15, cutoff_minute=11 * 60):
    """First-hour breakouts only — later breakouts are mostly noise on a single name."""
    decision = rule_orb(session, or_minutes=or_minutes, long_only=False)
    if not decision:
        return None
    if OPEN_MIN + decision[0] > cutoff_minute:
        return None
    return decision


def attach_vol(sessions, window=10, lookback=252):
    """Realized volatility from the last N daily closes, known before the open."""
    dates = sorted(sessions)
    closes = pd.Series({d: float(sessions[d]["close"][-1]) for d in dates})
    rets = closes.pct_change()
    realized = rets.rolling(window).std().shift(1)
    median = realized.rolling(lookback, min_periods=60).median().shift(1)
    for date in dates:
        r, m = realized.get(date), median.get(date)
        sessions[date]["realized_vol"] = None if r is None or r != r else float(r)
        sessions[date]["vol_median"] = None if m is None or m != m else float(m)

# ---------------------------------------------------------------------- #
#  Evaluation
# ---------------------------------------------------------------------- #

def evaluate(sessions: List[str], data: Dict[str, dict], rule: Callable,
             cost_bp: float, **sim_kwargs) -> List[Trade]:
    trades = []
    for date in sessions:
        decision = rule(data[date])
        if not decision:
            continue
        entry_idx, direction, extra = decision
        merged = dict(sim_kwargs)
        merged.update(extra)
        trade = simulate(data[date], date, entry_idx, direction, cost_bp, **merged)
        if trade:
            trades.append(trade)
    return trades


def summarize(trades: List[Trade], sessions: int) -> dict:
    if not trades:
        return {"n": 0, "per_day": 0.0, "gross_bp": 0.0, "net_bp": 0.0,
                "hit": 0.0, "t": 0.0, "total_pct": 0.0, "worst_dd_pct": 0.0}
    net = np.array([t.net_ret for t in trades])
    gross = np.array([t.gross_ret for t in trades])
    equity = np.cumprod(1 + net)
    peak = np.maximum.accumulate(equity)
    std = net.std(ddof=1) if len(net) > 1 else 0.0
    return {
        "n": len(trades),
        "per_day": len(trades) / max(sessions, 1),
        "gross_bp": float(gross.mean() * 1e4),
        "net_bp": float(net.mean() * 1e4),
        "hit": float((net > 0).mean()),
        "t": float(net.mean() / (std / np.sqrt(len(net)))) if std > 0 else 0.0,
        "total_pct": float(equity[-1] - 1),
        "worst_dd_pct": float((equity / peak - 1).min()),
    }


def yearly_breakdown(trades: List[Trade], sessions: List[str]) -> dict:
    """Per-calendar-year stats: a single-name rule usually only works in one regime."""
    out = {}
    for year in sorted({t.date[:4] for t in trades}):
        subset = [t for t in trades if t.date[:4] == year]
        net = np.array([t.net_ret for t in subset])
        out[year] = {
            "n": len(subset),
            "net_bp": float(net.mean() * 1e4),
            "hit": float((net > 0).mean()),
            "total_pct": float(np.cumprod(1 + net)[-1] - 1),
            "sessions": sum(1 for s in sessions if s[:4] == year),
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default=DATA_PATH)
    parser.add_argument("--split", type=float, default=0.6)
    args = parser.parse_args()

    sessions_map = load_sessions(args.data)
    attach_context(sessions_map)
    attach_vol(sessions_map)
    cost = load_cost()
    cost_bp = cost["half_spread_bp"] * 2 + cost["slippage_bp"] * 2
    print(f"Cost per round trip: {cost_bp:.2f} bp "
          f"(half-spread {cost['half_spread_bp']:.2f} + slippage {cost['slippage_bp']:.2f}, each side)")

    dates = sorted(sessions_map)
    split_at = int(len(dates) * args.split)
    sample, oos = dates[1:split_at], dates[split_at:]
    print(f"In-sample    : {sample[0]} -> {sample[-1]} ({len(sample)} sessions)")
    print(f"Out-of-sample: {oos[0]} -> {oos[-1]} ({len(oos)} sessions)\n")

    rules = [
        ("orb15", rule_orb, dict(or_minutes=15), dict()),
        ("orb30", rule_orb, dict(or_minutes=30), dict()),
        ("orb60", rule_orb, dict(or_minutes=60), dict()),
        ("orb30_hold60", rule_orb, dict(or_minutes=30), dict(max_hold=60)),
        ("orb30_v1.5", rule_orb, dict(or_minutes=30, vol_mult=1.5), dict()),
        ("orb30_mom", rule_orb, dict(or_minutes=30, min_momentum=0.005), dict()),
        ("orb30_ls", rule_orb, dict(or_minutes=30, long_only=False), dict()),
        ("orb30_regime", rule_orb_regime, dict(or_minutes=30), dict()),
        ("orb60_regime", rule_orb_regime, dict(or_minutes=60), dict()),
        ("orb30_counter", rule_orb_regime,
         dict(or_minutes=30, trade_with_trend=False), dict()),
        ("orb30_retest", rule_orb_retest, dict(or_minutes=30), dict()),
        ("gap_mom1", rule_gap_momentum, dict(gap_min_pct=0.01), dict()),
        ("gap_fade1", rule_gap_fade, dict(gap_min_pct=0.01), dict()),
        ("gap_mom3", rule_gap_momentum, dict(gap_min_pct=0.03), dict()),
        ("vwap_rev0.6", rule_vwap_reversion, dict(dev_pct=0.006), dict()),
        ("fw_mom1", rule_first_hour_momentum, dict(threshold=0.01), dict()),
        ("fw_fade1", rule_first_hour_fade, dict(threshold=0.01), dict()),
        ("orb30_vol", rule_orb_vol, dict(or_minutes=30), dict()),
        ("orb15_vol", rule_orb_vol, dict(or_minutes=15), dict()),
        ("orb15_gap", rule_orb_gap, dict(or_minutes=15), dict()),
        ("orb30_gap", rule_orb_gap, dict(or_minutes=30), dict()),
        ("orb15_am", rule_orb_morning, dict(or_minutes=15), dict()),
        ("orb30_am", rule_orb_morning, dict(or_minutes=30), dict()),
        ("prevhigh", rule_prev_day_level, dict(side="high"), dict()),
        ("late_mom1", rule_late_day_momentum, dict(threshold=0.01), dict()),
        ("late_mom2", rule_late_day_momentum, dict(threshold=0.02), dict()),
    ]

    header = (f"{'rule':<16}{'n':>6}{'n/dy':>6}{'gross bp':>10}{'net bp':>9}"
              f"{'hit%':>7}{'t':>7}   |{'OOS n':>7}{'OOS net':>9}{'OOS t':>7}")
    print(header)
    print("-" * len(header))

    results = {}
    for name, func, rule_kw, sim_kw in rules:
        ins = summarize(evaluate(sample, sessions_map, lambda s: func(s, **rule_kw),
                                 cost_bp, **sim_kw), len(sample))
        oo = summarize(evaluate(oos, sessions_map, lambda s: func(s, **rule_kw),
                                cost_bp, **sim_kw), len(oos))
        results[name] = {"in": ins, "oos": oo, "rule": rule_kw, "sim": sim_kw}
        print(f"{name:<16}{ins['n']:>6}{ins['per_day']:>6.2f}"
              f"{ins['gross_bp']:>10.1f}{ins['net_bp']:>9.1f}"
              f"{ins['hit']*100:>6.1f}%{ins['t']:>7.2f}   |"
              f"{oo['n']:>7}{oo['net_bp']:>9.1f}{oo['t']:>7.2f}")

    # Best in-sample rule, for a quick look at how it varies by year.
    if results:
        best = max(results, key=lambda k: results[k]["in"]["net_bp"] * min(results[k]["in"]["n"], 60))
        name, func, rule_kw, sim_kw = next(r for r in rules if r[0] == best)
        trades = evaluate(dates[1:], sessions_map, lambda s: func(s, **rule_kw),
                          cost_bp, **sim_kw)
        print(f"\nBest in-sample rule: {best} — yearly breakdown (full sample)")
        print(f"  {'year':<6}{'n':>5}{'net bp':>9}{'hit%':>7}{'year ret':>10}")
        for year, row in yearly_breakdown(trades, dates[1:]).items():
            print(f"  {year:<6}{row['n']:>5}{row['net_bp']:>9.1f}"
                  f"{row['hit']*100:>6.1f}%{row['total_pct']*100:>9.1f}%")
        for label, direction in (("long", 1), ("short", -1)):
            subset = [t for t in trades if t.direction == direction]
            if not subset:
                continue
            net = np.array([t.net_ret for t in subset])
            print(f"  {label:<6}{len(subset):>5}{net.mean()*1e4:>9.1f}"
                  f"{(net > 0).mean()*100:>6.1f}%{(np.cumprod(1 + net)[-1] - 1)*100:>9.1f}%")

    bh = []
    for date in oos:
        s = sessions_map[date]
        gross = float(s["close"][-1] / s["open"][0] - 1)
        bh.append(Trade(date, 1, 0, len(s["close"]) - 1, s["open"][0],
                        s["close"][-1], gross, gross - cost_bp / 1e4, "hold"))
    bhs = summarize(bh, len(oos))
    print(f"\nReference — long open->close every session (OOS): n={bhs['n']}, "
          f"gross {bhs['gross_bp']:.1f} bp, net {bhs['net_bp']:.1f} bp, "
          f"hit {bhs['hit']*100:.1f}%, t={bhs['t']:.2f}")

    with open(os.path.join(ROOT, "logs", "tsla_lab.json"), "w", encoding="utf-8") as handle:
        json.dump({"cost_bp": cost_bp, "in_sample": [sample[0], sample[-1]],
                   "out_of_sample": [oos[0], oos[-1]], "results": results,
                   "buy_and_hold": bhs}, handle, indent=2)
    print("\nSaved → logs/tsla_lab.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
