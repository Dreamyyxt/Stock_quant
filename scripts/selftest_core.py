"""
Regression self-test for the execution/risk fixes.

Runs fully offline (no broker, no network). Intended to be run after any change
to risk or execution code:

    .venv/bin/python scripts/selftest_core.py

Covers the four defects that mattered:
  1. risk state survives a process restart
  2. a stopped-out symbol cannot be re-bought in the same cycle
  3. validate_weights refuses to run without a sector map
  4. state store never loses the document on a corrupt read
"""

import os
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from config.settings import SECTOR_MAP  # noqa: E402
from core.state_store import StateStore  # noqa: E402
from execution.fixed_rebalancer import FixedPortfolioRebalancer  # noqa: E402
from risk.risk_manager import RiskManager  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name: str, condition: bool, detail: str = ""):
    results.append((PASS if condition else FAIL, name, detail))
    print(f"[{PASS if condition else FAIL}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="sq_selftest_")
    store_path = os.path.join(tmp, "risk_state.json")

    try:
        # ---------- 1. persistence across restart ---------- #
        store = StateStore(store_path)
        risk = RiskManager({}, state_store=store)
        risk.record_entry("AAPL", 200.0)
        risk.update_peak("AAPL", 220.0)
        risk.check_portfolio_drawdown(1_000_000)

        reloaded = RiskManager({}, state_store=store)
        check("entry price persists", reloaded._entry_prices.get("AAPL") == 200.0,
              str(reloaded._entry_prices))
        check("peak persists", reloaded._peak_prices.get("AAPL") == 220.0,
              str(reloaded._peak_prices))
        check("portfolio peak persists",
              reloaded._portfolio_peak_value == 1_000_000,
              str(reloaded._portfolio_peak_value))

        # ---------- 2. stop-out cooldown blocks re-entry ---------- #
        reloaded.record_exit("MSFT", 400.0, "hard stop")
        check("cooldown registered", "MSFT" in reloaded.cooldown_symbols(),
              str(reloaded.cooldown_report()))
        check("entry tracking cleared on exit", "MSFT" not in reloaded._entry_prices)

        rebalancer = FixedPortfolioRebalancer.__new__(FixedPortfolioRebalancer)
        rebalancer.risk = reloaded
        rebalancer.threshold = 0.05

        trades = rebalancer._compute_trades(
            current_weights={},
            current_positions={},
            target={"MSFT": 0.20, "AAPL": 0.20},
            portfolio_value=1_000_000,
            prices={"MSFT": 400.0, "AAPL": 200.0},
        )
        symbols = {t["symbol"] for t in trades}
        check("cooldown symbol is not re-bought", "MSFT" not in symbols, str(symbols))
        check("non-cooldown symbol still trades", "AAPL" in symbols, str(symbols))

        # ---------- 3. sector cap requires a sector map ---------- #
        weights = {"MSFT": 0.20, "GOOGL": 0.20, "AAPL": 0.20, "NVDA": 0.10,
                   "AMD": 0.10, "BABA": 0.08, "INTC": 0.06, "PFE": 0.06}
        raised = False
        try:
            reloaded.validate_weights(weights, max_position=0.12, max_sector=0.35)
        except ValueError:
            raised = True
        check("validate_weights rejects missing sector_map", raised)

        validated = reloaded.validate_weights(
            weights, sector_map=SECTOR_MAP, max_position=0.20, max_sector=0.35
        )
        tech = sum(w for s, w in validated.items() if SECTOR_MAP.get(s) == "Technology")
        check("sector cap respected", tech <= 0.3501, f"tech={tech:.3f}")

        # The validator clamps rather than renormalises, so the overflow is
        # dropped and total exposure falls to ~49%. That is the correct
        # behaviour for a *cap*; it also exposes how concentrated the fixed
        # book really is.
        raw_tech = sum(w for s, w in weights.items() if SECTOR_MAP.get(s) == "Technology")
        check("fixed portfolio tech weight is 86% (cap would cut it to 35%)",
              abs(raw_tech - 0.86) < 1e-9, f"raw tech={raw_tech:.2f}")
        check("capped book keeps non-tech names", abs(sum(validated.values()) - 0.49) < 0.01,
              f"total={sum(validated.values()):.3f}")

        # ---------- 4. corrupt state is quarantined, not zeroed ---------- #
        with open(store_path, "w", encoding="utf-8") as handle:
            handle.write("{ this is not json")
        corrupt_store = StateStore(store_path)
        doc = corrupt_store.load()
        check("corrupt state returns empty", doc == {})
        check("corrupt state file kept for forensics",
              os.path.exists(store_path + ".corrupt"))

        # ---------- 5. duplicate-order guard ---------- #
        from datetime import date
        from execution.order_manager import OrderManager

        class StubClient:
            def has_open_order(self, symbol, action):
                return False

        orders = OrderManager.__new__(OrderManager)
        orders.store = StateStore(os.path.join(tmp, "order_state.json"))
        orders._state = orders.store.load()
        orders.block_repeat_days = 1
        orders._pending_orders = {}
        orders.max_daily_trades = 10
        orders._state["daily"] = {"date": date.today().isoformat(), "count": 1,
                                  "submitted": {"AAPL:BUY": date.today().isoformat()}}
        check("same-day repeat blocked", orders._already_submitted("AAPL", "BUY"))
        check("opposite side allowed", not orders._already_submitted("AAPL", "SELL"))

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failures = [r for r in results if r[0] == FAIL]
    print("\n" + "=" * 60)
    print(f"  {len(results) - len(failures)}/{len(results)} checks passed")
    print("=" * 60)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
