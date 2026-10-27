"""
Batch replay: run the intraday engine over N past sessions and aggregate.

One day of replay is an anecdote. Ten days is a distribution. This script exists
so that any parameter change is judged on the aggregate, not on a single lucky
session.

    .venv/bin/python scripts/batch_replay.py --days 10
    .venv/bin/python scripts/batch_replay.py --days 20 --end 2026-10-23
    .venv/bin/python scripts/batch_replay.py --days 10 --set entry_score=0.80 \
                                                   --set min_hold_minutes=15 --tag tuned

Data comes from Alpaca historical SIP (full consolidated tape) and is cached in
state/intraday_cache, so re-running the same dates is offline and free.
"""

import argparse
import json
import os
import statistics
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import INTRADAY_CONFIG, STATE_DIR  # noqa: E402
from intraday.engine import build_engine  # noqa: E402
from intraday.providers import (  # noqa: E402
    CACHE_DIR, AlpacaBarProvider, cache_path,
)


def trading_days(end: date, count: int) -> list:
    """The `count` most recent weekdays up to and including `end`, ascending."""
    days, cursor = [], end
    while len(days) < count:
        if cursor.weekday() < 5:          # Mon-Fri; US holidays are skipped later
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


def ensure_data(symbols, session_date: str) -> int:
    """Download missing 1-minute bars for a session. Returns symbols cached."""
    missing = [s for s in symbols if not os.path.exists(cache_path(s, session_date))]
    if not missing:
        return len(symbols)

    provider = AlpacaBarProvider(missing, lookback=500,
                                 session_date=session_date, feed="sip")
    if not provider.configured:
        raise RuntimeError("Alpaca keys missing — set ALPACA_API_KEY / ALPACA_API_SECRET")

    bars = provider.get_recent(missing)
    os.makedirs(CACHE_DIR, exist_ok=True)
    for symbol, frame in bars.items():
        frame.to_csv(cache_path(symbol, session_date))
    return (len(symbols) - len(missing)) + len(bars)


def apply_overrides(config: dict, pairs) -> dict:
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"bad --set '{pair}', expected key=value")
        key, raw = pair.split("=", 1)
        key, raw = key.strip(), raw.strip()
        current = config.get(key)
        try:
            if isinstance(current, bool):
                value = raw.lower() in ("1", "true", "yes", "on")
            elif isinstance(current, int):
                value = int(raw)
            elif isinstance(current, float):
                value = float(raw)
            else:
                value = json.loads(raw) if raw[:1] in "[{" else raw
        except ValueError:
            value = raw
        config[key] = value
    return config


def _cumulative(returns: list):
    """(index, compounded value) starting from 1.0 — index 0 is the start."""
    value = 1.0
    for index, r in enumerate(returns, start=1):
        value *= (1 + r)
        yield index, value


def equity_drawdown(values: list) -> float:
    """Peak-to-trough drawdown of the compounded equity curve.

    The per-session drawdown is not this number: a strategy can lose 1.5% over a
    month while every single day stays inside 0.4%.
    """
    peak, worst = values[0], 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1)
    return worst


def aggregate(rows: list) -> dict:
    returns = [r["return_pct"] for r in rows]
    trades = [r["trades"] for r in rows]
    equity = 1.0
    for value in returns:
        equity *= (1 + value)
    return {
        "sessions": len(rows),
        "total_return_pct": equity - 1,
        "avg_daily_return_pct": statistics.mean(returns) if returns else 0.0,
        "median_daily_return_pct": statistics.median(returns) if returns else 0.0,
        "stdev_daily_return_pct": statistics.stdev(returns) if len(returns) > 1 else 0.0,
        "best_day_pct": max(returns) if returns else 0.0,
        "worst_day_pct": min(returns) if returns else 0.0,
        "positive_days": sum(1 for v in returns if v > 0),
        "total_trades": sum(trades),
        "avg_trades_per_day": statistics.mean(trades) if trades else 0.0,
        "avg_win_rate": statistics.mean(r["win_rate"] for r in rows) if rows else 0.0,
        "halted_days": sum(1 for r in rows if r["halted"]),
        "worst_intraday_drawdown": min((r["max_intraday_drawdown"] for r in rows),
                                       default=0.0),
        "equity_max_drawdown": equity_drawdown(
            [1.0] + [v for _, v in _cumulative(returns)]),
    }


def print_table(rows: list) -> None:
    print(f"\n{'date':<12}{'return':>10}{'trades':>8}{'win%':>8}{'maxDD':>9}{'halt':>7}")
    print("-" * 54)
    for row in rows:
        print(f"{row['session_date']:<12}"
              f"{row['return_pct']:>9.2%} "
              f"{row['trades']:>7d}"
              f"{row['win_rate']:>7.1%}"
              f"{row['max_intraday_drawdown']:>9.2%}"
              f"{'  yes' if row['halted'] else '   no':>7}")


def print_aggregate(stats: dict) -> None:
    print("\n" + "=" * 54)
    print("  AGGREGATE")
    print("=" * 54)
    print(f"  Sessions:            {stats['sessions']}")
    print(f"  Total return:        {stats['total_return_pct']:>10.2%}")
    print(f"  Avg / median day:    {stats['avg_daily_return_pct']:>10.2%} / "
          f"{stats['median_daily_return_pct']:.2%}")
    print(f"  Stdev (daily):       {stats['stdev_daily_return_pct']:>10.2%}")
    print(f"  Best / worst day:    {stats['best_day_pct']:>10.2%} / "
          f"{stats['worst_day_pct']:.2%}")
    print(f"  Positive days:       {stats['positive_days']}/{stats['sessions']}")
    print(f"  Total trades:        {stats['total_trades']:>10d}  "
          f"({stats['avg_trades_per_day']:.1f}/day)")
    print(f"  Avg win rate:        {stats['avg_win_rate']:>10.1%}")
    print(f"  Halted days:         {stats['halted_days']:>10d}")
    print(f"  Worst intraday DD:   {stats['worst_intraday_drawdown']:>10.2%}")
    print(f"  Equity max DD:       {stats['equity_max_drawdown']:>10.2%}")

    # Per-trade expectancy is the number that decides viability: a day-trading
    # strategy must clear spread + commission per round trip, nothing else.
    if stats["total_trades"]:
        expectancy = stats["total_return_pct"] / stats["total_trades"]
        print(f"  P&L per trade:       {expectancy:>10.3%}")
    print("=" * 54 + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch replay over N sessions")
    parser.add_argument("--days", type=int, default=10, help="Sessions to replay")
    parser.add_argument("--end", default=None, help="Last session date (YYYY-MM-DD)")
    parser.add_argument("--cash", type=float, default=100_000.0)
    parser.add_argument("--tag", default="baseline", help="Label for the output file")
    parser.add_argument("--set", dest="overrides", action="append",
                        help="Override an INTRADAY_CONFIG key, e.g. --set entry_score=0.8")
    parser.add_argument("--quiet", action="store_true", help="Hide per-session output")
    parser.add_argument("--export-prefix", default=None,
                        help="Also write <prefix>_equity.csv / _trades.csv / _summary.json")
    args = parser.parse_args()

    end = (datetime.strptime(args.end, "%Y-%m-%d").date() if args.end
           else datetime.now().date())
    dates = trading_days(end, args.days)
    config = apply_overrides(dict(INTRADAY_CONFIG), args.overrides)
    universe = config["universe"]

    print(f"\nBatch replay: {len(dates)} sessions  "
          f"({dates[0]} → {dates[-1]})")
    print(f"Universe: {', '.join(universe)}   cash: ${args.cash:,.0f}")
    if args.overrides:
        print(f"Overrides: {', '.join(args.overrides)}")

    journal = os.path.join("logs", "intraday_trades.jsonl")
    journal_start = 0
    if os.path.exists(journal):
        with open(journal, "r", encoding="utf-8") as handle:
            journal_start = sum(1 for _ in handle)

    rows = []
    for session in dates:
        session_date = session.strftime("%Y-%m-%d")
        got = ensure_data(universe, session_date)
        if got < len(universe):
            print(f"  {session_date}: only {got}/{len(universe)} symbols — "
                  f"likely a market holiday, skipping")
            continue

        if args.quiet:
            import contextlib
            import io
            sink = io.StringIO()
            with contextlib.redirect_stdout(sink):
                summary = build_engine("replay", dict(config),
                                       session_date=session_date, speed=1,
                                       initial_cash=args.cash).run()
        else:
            print(f"\n--- {session_date} ---")
            summary = build_engine("replay", dict(config),
                                   session_date=session_date, speed=1,
                                   initial_cash=args.cash).run()
        rows.append(summary)

    if not rows:
        print("\n[FAILED] No sessions replayed.\n")
        return 1

    stats = aggregate(rows)
    print_table(rows)
    print_aggregate(stats)

    out = os.path.join("logs", f"intraday_batch_{args.tag}.json")
    with open(out, "w", encoding="utf-8") as handle:
        json.dump({"tag": args.tag, "overrides": args.overrides,
                   "config": {k: v for k, v in config.items() if k != "universe"},
                   "sessions": rows, "aggregate": stats}, handle, indent=2)
    print(f"Saved → {out}\n")

    if args.export_prefix:
        export(args.export_prefix, rows, stats, journal, journal_start, args.cash)
    return 0


def export(prefix, rows, stats, journal, journal_start, initial_cash) -> None:
    """Standard export shape: equity curve, paired trades, summary metrics."""
    import csv

    equity, value = [], float(initial_cash)
    for row in rows:
        value *= (1 + row["return_pct"])
        equity.append({"date": row["session_date"], "value": round(value, 2)})
    with open(f"{prefix}_equity.csv", "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["date", "value"])
        writer.writeheader()
        writer.writerows(equity)

    entries, exits = {}, []
    with open(journal, "r", encoding="utf-8") as handle:
        lines = handle.read().splitlines()[journal_start:]
    for line in lines:
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("type") == "entry":
            entries[event["symbol"]] = event
        elif event.get("type") == "exit":
            entry = entries.pop(event["symbol"], None)
            if not entry:
                continue
            exits.append({
                "entry_date": entry.get("session_date", entry["ts"][:10]),
                "exit_date": event.get("session_date", event["ts"][:10]),
                "entry_time": entry.get("signal_time", entry["ts"][11:16]),
                "exit_time": event.get("exit_time", event["ts"][11:16]),
                "side": "long", "size": event["quantity"], "symbol": event["symbol"],
                "entry_price": round(entry["price"], 4),
                "exit_price": round(event["price"], 4),
                "pnl": round(event["pnl"], 2),
                "pnl_pct": round(event["pnl"] / (entry["price"] * event["quantity"]) * 100, 4),
                "holding_bars": event.get("held_minutes"), "exit_reason": event["reason"],
            })
    if exits:
        with open(f"{prefix}_trades.csv", "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(exits[0].keys()))
            writer.writeheader()
            writer.writerows(exits)

    payload = {
        "meta": {
            "strategy_name": "opening-range breakout, 2h time exit",
            "symbol": ",".join(INTRADAY_CONFIG["universe"]),
            "start": rows[0]["session_date"], "end": rows[-1]["session_date"],
            "initial_cash": initial_cash, "final_value": equity[-1]["value"],
            "market": "us", "generated_at": datetime.now().isoformat(timespec="seconds"),
        },
        "summary": {
            "total_return_pct": round(stats["total_return_pct"] * 100, 3),
            "max_drawdown_pct": round(stats["equity_max_drawdown"] * 100, 3),
            "worst_intraday_drawdown_pct": round(stats["worst_intraday_drawdown"] * 100, 3),
            "win_rate_pct": round(stats["avg_win_rate"] * 100, 2),
            "total_trades": stats["total_trades"],
            "sessions": stats["sessions"],
            "avg_daily_return_pct": round(stats["avg_daily_return_pct"] * 100, 4),
            "stdev_daily_pct": round(stats["stdev_daily_return_pct"] * 100, 4),
            "positive_days": stats["positive_days"],
        },
        "per_trade": {
            "mean_pct": round(sum(t["pnl_pct"] for t in exits) / len(exits), 4) if exits else 0,
            "hit_rate_pct": round(sum(1 for t in exits if t["pnl"] > 0) / len(exits) * 100, 2) if exits else 0,
        },
        "config": {k: v for k, v in INTRADAY_CONFIG.items() if k != "universe"},
    }
    with open(f"{prefix}_summary.json", "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    print(f"Exported → {prefix}_equity.csv / _trades.csv / _summary.json\n")


if __name__ == "__main__":
    sys.exit(main())
