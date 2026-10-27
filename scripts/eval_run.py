"""
Run one replay configuration and report per-trade statistics.

batch_replay reports daily aggregates, which hides the number that actually
decides whether a strategy works: the mean return of one trade relative to the
cost of putting it on. This wraps batch_replay, then reads only the journal
lines written during that run, so different configurations never contaminate
each other.

    .venv/bin/python scripts/eval_run.py --days 116 --tag stop2 --set stop_loss_pct=0.02
"""

import argparse
import collections
import json
import os
import statistics
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOURNAL = os.path.join(ROOT, "logs", "intraday_trades.jsonl")


def journal_lines() -> int:
    if not os.path.exists(JOURNAL):
        return 0
    with open(JOURNAL, "r", encoding="utf-8") as handle:
        return sum(1 for _ in handle)


def read_new_lines(start: int) -> list:
    with open(JOURNAL, "r", encoding="utf-8") as handle:
        lines = handle.read().splitlines()[start:]
    return [json.loads(line) for line in lines if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate one replay configuration")
    parser.add_argument("--days", type=int, default=116)
    parser.add_argument("--end", default="2026-10-23")
    parser.add_argument("--tag", default="eval")
    parser.add_argument("--set", dest="overrides", action="append", default=[])
    parser.add_argument("--quiet-run", action="store_true", default=True)
    args = parser.parse_args()

    start = journal_lines()
    command = [sys.executable, os.path.join(ROOT, "scripts", "batch_replay.py"),
               "--days", str(args.days), "--end", args.end,
               "--quiet", "--tag", args.tag]
    for pair in args.overrides:
        command.extend(["--set", pair])

    env = dict(os.environ, ALERT_DESKTOP="0")
    result = subprocess.run(command, cwd=ROOT, env=env,
                            capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout[-2000:])
        print(result.stderr[-2000:])
        return 1

    # Daily aggregate from batch_replay's own output.
    for line in result.stdout.splitlines():
        if any(key in line for key in ("Sessions:", "Total return:", "Total trades:",
                                       "Avg win rate:", "Worst intraday DD:",
                                       "Best / worst day:", "Positive days:")):
            print(line)

    events = read_new_lines(start)
    exits = [e for e in events if e.get("type") == "exit"]
    if not exits:
        print("\nNo trades in this configuration.")
        return 0

    returns = []
    by_reason = collections.defaultdict(list)
    for event in exits:
        notional = event["entry_price"] * event["quantity"]
        if notional <= 0:
            continue
        value = event["pnl"] / notional
        returns.append(value)
        by_reason[event["reason"].split("(")[0].strip()].append(value)

    mean = statistics.mean(returns)
    stdev = statistics.stdev(returns) if len(returns) > 1 else 0.0
    t_stat = mean / (stdev / len(returns) ** 0.5) if stdev else 0.0
    print(f"\n  per-trade mean : {mean:>8.4%}   (t = {t_stat:.2f}, n = {len(returns)})")
    print(f"  per-trade median:{statistics.median(returns):>8.4%}")
    print(f"  hit rate       : {sum(1 for v in returns if v > 0)/len(returns):>8.1%}")
    print(f"  stdev          : {stdev:>8.4%}")
    print(f"  {'exit reason':<20}{'n':>5}{'mean':>10}{'hit%':>7}")
    print("  " + "-" * 42)
    for reason, values in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        print(f"  {reason:<20}{len(values):>5}{statistics.mean(values):>9.3%}"
              f"{sum(1 for v in values if v > 0)/len(values):>7.0%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
