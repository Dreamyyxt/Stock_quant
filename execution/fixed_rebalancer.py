"""
Fixed Portfolio Rebalancer
==========================
Manages a user-defined fixed-weight portfolio.

Portfolio:
  Core (60%):      MSFT 20%, GOOGL 20%, AAPL 20%
  Satellite (40%): NVDA 10%, AMD 10%, BABA 8%, INTC 6%, PFE 6%

On each rebalancing run:
  1. Fetch account value, cash and positions from the broker
  2. Stop-loss phase — exits are executed and confirmed FIRST
  3. Names stopped out enter a cooldown window and are excluded from re-entry
  4. Compute target weights (adaptive overlay + drawdown scalar)
  5. Compute drift trades against a budget derived from the target exposure
  6. Execute sells, confirm fills, then buy within budget

The stop-loss phase is deliberately separated from the rebalance. The previous
version sold a stopped-out position and then immediately bought it back in the
same cycle, because the refreshed (empty) position still had a non-zero target
weight. See the 2026-06-29 BABA sequence in logs/trade_events.jsonl.
"""

import logging
from datetime import datetime
from typing import Dict, List, Tuple

import pandas as pd

from core.data_fetcher import DataFetcher
from core.event_logger import EventLogger
from core.notifier import Notifier
from execution.order_manager import OrderManager
from risk.risk_manager import RiskManager
from strategies.adaptive_overlay import AdaptiveOverlay
from config.settings import (
    FIXED_PORTFOLIO, RISK_CONFIG, EXECUTION_CONFIG, LOG_CONFIG,
    STATE_DIR, TRADING_ENV, is_live,
)

logger = logging.getLogger(__name__)

# Merge core + satellite into one flat target weights dict
TARGET_WEIGHTS: Dict[str, float] = {
    **FIXED_PORTFOLIO["core"],
    **FIXED_PORTFOLIO["satellite"],
}
ALL_SYMBOLS = list(TARGET_WEIGHTS.keys())

_MIN_NOTIONAL = float(EXECUTION_CONFIG.get("min_order_notional", 200.0))


class FixedPortfolioRebalancer:
    """Rebalances a fixed-weight portfolio using Tiger Securities API."""

    def __init__(self, data_fetcher: DataFetcher, order_manager: OrderManager):
        self.data = data_fetcher
        self.orders = order_manager
        self.events = EventLogger(log_dir=LOG_CONFIG.get("log_dir", "logs"))
        self.notifier = getattr(order_manager, "notifier", None) or Notifier(state_dir=STATE_DIR)
        self.risk = RiskManager(RISK_CONFIG)
        self.overlay = AdaptiveOverlay()
        self.threshold = FIXED_PORTFOLIO.get("rebalance_threshold", 0.05)

    # ------------------------------------------------------------------ #
    #  Main Rebalancing Cycle
    # ------------------------------------------------------------------ #

    def run_rebalance(self, dry_run: bool = True) -> dict:
        logger.info("=" * 60)
        logger.info("FIXED PORTFOLIO REBALANCE — %s [%s / %s]",
                    datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "DRY RUN" if dry_run else "LIVE",
                    TRADING_ENV.upper())
        logger.info("=" * 60)
        self.events.log("rebalance_started", mode="fixed", dry_run=dry_run, env=TRADING_ENV)

        # ---- 1. Account state ---- #
        account = self.data.get_account_summary()
        portfolio_value = account.get("net_liquidation", 0)
        cash = account.get("cash", 0)
        buying_power = account.get("buying_power", 0)

        if portfolio_value <= 0:
            logger.error("Invalid portfolio value: %s", portfolio_value)
            self.events.log("rebalance_failed", mode="fixed", reason="invalid portfolio value")
            self.notifier.critical("Rebalance aborted", "Broker returned an invalid portfolio value")
            return {"status": "error"}

        logger.info("Portfolio value: $%.2f | Cash: $%.2f | Buying power: $%.2f",
                    portfolio_value, cash, buying_power)

        # ---- 2. Drawdown circuit breaker (persisted peak) ---- #
        triggered, exposure_limit = self.risk.check_portfolio_drawdown(portfolio_value)
        if triggered:
            logger.warning("DRAWDOWN CIRCUIT BREAKER: reducing exposure to %.0f%%",
                           exposure_limit * 100)
            self.notifier.warning(
                "Drawdown circuit breaker",
                f"Portfolio ${portfolio_value:,.0f} — exposure cut to {exposure_limit:.0%}",
            )

        # ---- 3. Prices & positions ---- #
        current_prices = self._get_prices()
        if not current_prices:
            logger.error("Could not fetch prices — aborting rebalance")
            self.notifier.critical("Rebalance aborted", "No usable prices for the portfolio")
            return {"status": "error: no prices"}

        current_positions = self.data.get_current_positions()
        invested_value = self._position_value(current_positions, current_prices)
        logger.info("Invested: $%.2f (%d positions)", invested_value, len(current_positions))

        # Seed trailing-stop peaks from recent highs so a name that already fell
        # from its high can actually trip the trailing stop on day one.
        self._seed_peaks(current_prices)

        # ---- 4. Stop-loss phase (separate, and it wins) ---- #
        stops = self.risk.check_stop_losses(current_positions, current_prices)
        stop_results: List[dict] = []
        if stops:
            logger.warning("%d stop loss(es) triggered", len(stops))
            stop_trades = [
                {"symbol": s["symbol"], "action": "SELL",
                 "quantity": s["quantity"], "price": current_prices.get(s["symbol"]),
                 "reason": s["reason"]}
                for s in stops
            ]
            if dry_run:
                for t in stop_trades:
                    logger.info("[DRY RUN] Would SELL %s x%d — %s",
                                t["symbol"], t["quantity"], t["reason"])
            else:
                stop_results = self.orders.execute_trade_list(
                    stop_trades, current_prices, budget=0, net_liquidation=portfolio_value,
                )
                for result, stop in zip(stop_results, stops):
                    if result.get("status") == "filled":
                        self.risk.record_exit(
                            stop["symbol"], result.get("fill_price") or stop["current_price"],
                            stop["reason"],
                        )
                    elif result.get("status") in ("failed", "cancelled", "rejected"):
                        self.notifier.critical(
                            "Stop loss NOT executed",
                            f"{stop['symbol']} — {stop['reason']} (status {result.get('status')})",
                        )
                # Refresh only after fills are confirmed by the executor.
                current_positions = self.data.get_current_positions()
                invested_value = self._position_value(current_positions, current_prices)

        cooldowns = self.risk.cooldown_symbols()
        if cooldowns:
            logger.info("Re-entry blocked (stop-out cooldown): %s", ", ".join(sorted(cooldowns)))

        # ---- 5. Current weights ---- #
        current_weights = {
            sym: (pos.get("quantity", 0) * current_prices.get(sym, 0)) / portfolio_value
            for sym, pos in current_positions.items()
            if portfolio_value > 0
        }

        # ---- 6. Target weights (adaptive overlay + drawdown scalar) ---- #
        prices_hist = self.data.get_multi_price_history(
            ALL_SYMBOLS + ["SPY"], lookback_days=280
        )
        spy_series = prices_hist["SPY"] if "SPY" in getattr(prices_hist, "columns", []) else None
        port_prices = prices_hist[[s for s in ALL_SYMBOLS if s in prices_hist.columns]] \
            if not prices_hist.empty else prices_hist

        if prices_hist is None or prices_hist.empty:
            logger.error("Price history unavailable — overlay falls back to base weights")
            self.notifier.warning(
                "Overlay disabled",
                "No historical prices (yfinance may be rate-limited); using static weights",
            )

        target, overlay_signals = self.overlay.compute_weights(
            TARGET_WEIGHTS, port_prices, spy_series
        )
        regime = overlay_signals.get("regime_label", "BULL")
        if regime != "BULL":
            logger.warning("Market regime: %s — exposure reduced to %.0f%%",
                           regime, overlay_signals.get("market_regime", 1.0) * 100)

        target = {s: w * exposure_limit for s, w in target.items()}

        # Hard block: a stopped-out name cannot be re-bought during cooldown.
        blocked = []
        for sym in ALL_SYMBOLS:
            if sym in cooldowns and target.get(sym, 0) > 0:
                blocked.append(sym)
                target[sym] = 0.0
        if blocked:
            logger.warning("Target weight zeroed for cooldown names: %s", ", ".join(blocked))

        # ---- 7. Trades & budget ---- #
        trades = self._compute_trades(
            current_weights, current_positions, target, portfolio_value, current_prices
        )
        planned_sell = sum(t["dollar_value"] for t in trades if t["action"] == "SELL")
        invested_after_sells = max(0.0, invested_value - planned_sell)
        target_value = sum(target.values()) * portfolio_value
        buy_budget = max(0.0, target_value - invested_after_sells)

        logger.info("Target exposure: %.1f%% ($%.0f) | planned sells $%.0f | buy budget $%.0f",
                    sum(target.values()) * 100, target_value, planned_sell, buy_budget)

        # ---- 8. Execute ---- #
        if trades:
            results = self.orders.execute_trade_list(
                trades, current_prices,
                dry_run=dry_run,
                budget=buy_budget,
                net_liquidation=portfolio_value,
            )
        else:
            results = []

        if not dry_run:
            self._update_risk_tracking(results, current_positions)

        # ---- 9. Summary ---- #
        summary = self._build_summary(
            portfolio_value, cash, current_prices,
            current_weights, target, trades, dry_run,
        )
        summary["overlay_signals"] = overlay_signals
        summary["regime"] = regime
        summary["env"] = TRADING_ENV
        summary["cooldowns"] = self.risk.cooldown_report()
        summary["buy_budget"] = buy_budget
        summary["stop_results"] = stop_results
        summary["execution_results"] = results if not dry_run else []
        self._print_summary(summary)

        filled = sum(1 for r in results if r.get("status") == "filled")
        self.events.log(
            "rebalance_completed", mode="fixed", dry_run=dry_run,
            trades_planned=len(trades), trades_filled=filled,
            regime=regime, buy_budget=buy_budget, env=TRADING_ENV,
        )
        return summary

    # ------------------------------------------------------------------ #
    #  Risk tracking after execution
    # ------------------------------------------------------------------ #

    def _update_risk_tracking(self, results: List[dict],
                              positions_before: Dict[str, dict]) -> None:
        for result in results:
            symbol = result.get("symbol")
            status = result.get("status")
            if status != "filled" or not symbol:
                continue
            filled_qty = float(result.get("filled_qty") or 0)
            fill_price = result.get("fill_price")
            if filled_qty <= 0:
                continue

            if result.get("action") == "BUY":
                if fill_price:
                    self.risk.record_entry(symbol, float(fill_price))
            else:
                held = float((positions_before.get(symbol) or {}).get("quantity") or 0)
                if filled_qty >= held > 0:
                    self.risk.remove_position(symbol)

    # ------------------------------------------------------------------ #
    #  Helpers
    # ------------------------------------------------------------------ #

    def _get_prices(self) -> Dict[str, float]:
        quotes = self.data.get_quotes(ALL_SYMBOLS)
        prices = {}
        for sym in ALL_SYMBOLS:
            price = (quotes.get(sym) or {}).get("last")
            if price and price > 0:
                prices[sym] = float(price)
            else:
                logger.warning("No price for %s", sym)
        return prices

    @staticmethod
    def _position_value(positions: Dict[str, dict],
                        prices: Dict[str, float]) -> float:
        """Value positions from live prices, falling back to broker market value."""
        total = 0.0
        for symbol, pos in positions.items():
            qty = float(pos.get("quantity") or 0)
            price = prices.get(symbol)
            if price and price > 0:
                total += qty * price
            else:
                total += float(pos.get("market_value") or 0)
        return total

    def _seed_peaks(self, current_prices: Dict[str, float]) -> None:
        try:
            history = self.data.get_multi_price_history(
                list(current_prices.keys()), lookback_days=280
            )
        except Exception as exc:
            logger.warning("Could not load history to seed trailing peaks: %s", exc)
            return
        if history is None or history.empty:
            return
        series_map = {
            sym: history[sym] for sym in history.columns
            if sym in current_prices
        }
        seeded = self.risk.seed_peaks(series_map)
        if seeded:
            logger.info("Seeded trailing-stop peaks for %d symbol(s)", seeded)

    def _compute_trades(self,
                        current_weights: Dict[str, float],
                        current_positions: Dict[str, dict],
                        target: Dict[str, float],
                        portfolio_value: float,
                        prices: Dict[str, float]) -> List[dict]:
        """Compute the trades needed to reach target weights."""
        trades = []
        cooldowns = self.risk.cooldown_symbols()
        all_syms = set(current_weights.keys()) | set(target.keys())

        for sym in sorted(all_syms):
            cur_w = current_weights.get(sym, 0.0)
            tgt_w = target.get(sym, 0.0)
            drift = tgt_w - cur_w

            if abs(drift) < self.threshold:
                continue

            price = prices.get(sym)
            if not price or price <= 0:
                logger.warning("Skipping %s — no price", sym)
                continue

            dollar_delta = drift * portfolio_value
            quantity = int(abs(dollar_delta) / price)
            if quantity < 1:
                continue

            action = "BUY" if drift > 0 else "SELL"

            if action == "BUY" and sym in cooldowns:
                logger.warning("Blocked re-entry into %s (stop-out cooldown)", sym)
                continue

            if action == "SELL":
                held = int((current_positions.get(sym) or {}).get("quantity", 0))
                quantity = min(quantity, held)
                if quantity < 1:
                    continue

            notional = quantity * price
            if notional < _MIN_NOTIONAL:
                logger.info("Skipping %s %s: notional $%.0f below minimum",
                            action, sym, notional)
                continue

            trades.append({
                "symbol": sym,
                "action": action,
                "quantity": quantity,
                "price": price,
                "dollar_value": notional,
                "current_weight": cur_w,
                "target_weight": tgt_w,
                "drift": drift,
                "reason": f"{cur_w:.1%} → {tgt_w:.1%} (drift {drift:+.1%})",
            })

        trades.sort(key=lambda t: (0 if t["action"] == "SELL" else 1, -t["dollar_value"]))
        return trades

    def _build_summary(self, portfolio_value: float, cash: float,
                       prices: Dict[str, float], current_weights: Dict[str, float],
                       target: Dict[str, float], trades: List[dict],
                       dry_run: bool) -> dict:
        return {
            "timestamp": datetime.now().isoformat(),
            "dry_run": dry_run,
            "status": "completed",
            "portfolio_value": portfolio_value,
            "cash": cash,
            "positions": {
                sym: {
                    "price": prices.get(sym, 0),
                    "current_weight": current_weights.get(sym, 0),
                    "target_weight": target.get(sym, 0),
                    "drift": target.get(sym, 0) - current_weights.get(sym, 0),
                }
                for sym in ALL_SYMBOLS
            },
            "trades": trades,
            "trades_count": len(trades),
        }

    def _print_summary(self, summary: dict):
        pv = summary["portfolio_value"]
        mode = "[DRY RUN]" if summary["dry_run"] else "[LIVE]"
        print(f"\n{'='*70}")
        print(f"  FIXED PORTFOLIO STATUS  {mode}  env={summary.get('env','paper').upper()}"
              f"  —  {datetime.now():%Y-%m-%d %H:%M}")
        print(f"{'='*70}")
        print(f"  Portfolio Value: ${pv:>12,.2f}")
        print(f"  Cash:            ${summary['cash']:>12,.2f}")
        print(f"  Buy budget:      ${summary.get('buy_budget', 0):>12,.2f}")
        regime = summary.get("regime", "BULL")
        icons = {"BULL": "BULL", "SOFT_BEAR": "SOFT BEAR (-15~25%)",
                 "HARD_BEAR": "HARD BEAR (>-25%)"}
        print(f"  Market regime:   {icons.get(regime, regime)}")
        cooldowns = summary.get("cooldowns") or {}
        if cooldowns:
            print(f"  Cooldown:        {', '.join(f'{k}({v}d)' for k, v in cooldowns.items())}")
        print(f"\n  {'Symbol':<8} {'Price':>8} {'Current':>9} {'Target':>9} {'Drift':>8} {'RSI':>6}  Action")
        print(f"  {'-'*68}")

        positions = summary["positions"]
        trades_by_sym = {t["symbol"]: t for t in summary["trades"]}
        overlay_sigs = summary.get("overlay_signals", {})

        for sym in ALL_SYMBOLS:
            p = positions[sym]
            trade = trades_by_sym.get(sym)
            rsi_s = (overlay_sigs.get(sym) or {}).get("rsi_signal", 1.0)
            rsi_str = f"{'↑' if rsi_s > 1.05 else ('↓' if rsi_s < 0.95 else '—')}{rsi_s:.2f}"
            action_str = ""
            if trade:
                action_str = f"{trade['action']} {trade['quantity']}sh @ ${trade['price']:.2f}"
            drift_str = f"{p['drift']:+.1%}" if abs(p['drift']) >= 0.005 else "  —"
            print(f"  {sym:<8} ${p['price']:>7.2f} {p['current_weight']:>8.1%} "
                  f"{p['target_weight']:>8.1%} {drift_str:>8} {rsi_str:>6}  {action_str}")

        print(f"  {'='*60}")
        if summary["trades"]:
            total_buy = sum(t["dollar_value"] for t in summary["trades"] if t["action"] == "BUY")
            total_sell = sum(t["dollar_value"] for t in summary["trades"] if t["action"] == "SELL")
            print(f"  Trades: {summary['trades_count']}  |  Buy: ${total_buy:,.0f}  |  Sell: ${total_sell:,.0f}")
        else:
            print("  Portfolio is on target — no trades needed.")
        stops = summary.get("stop_results") or []
        if stops:
            print(f"  Stop-outs: {len(stops)}")
            for s in stops:
                print(f"    {s.get('symbol')}: {s.get('status')} "
                      f"filled={s.get('filled_qty')} @ {s.get('fill_price')}")
        print(f"{'='*70}\n")

    # ------------------------------------------------------------------ #
    #  Daily Monitor
    # ------------------------------------------------------------------ #

    def run_daily_monitor(self) -> dict:
        """Daily risk check: stop losses, drawdown, order cleanup."""
        logger.info("--- Daily Monitor ---")
        current_positions = self.data.get_current_positions()
        account = self.data.get_account_summary()
        portfolio_value = account.get("net_liquidation", 0)

        if portfolio_value <= 0:
            self.notifier.critical("Monitor aborted", "Broker returned an invalid portfolio value")
            return {"status": "error", "alerts": ["invalid portfolio value"]}

        cancelled_orders = self.orders.cancel_timed_out_orders()
        current_prices = self._get_prices()
        self._seed_peaks(current_prices)

        stops = self.risk.check_stop_losses(current_positions, current_prices)
        stop_results: List[dict] = []
        if stops:
            stop_trades = [
                {"symbol": s["symbol"], "action": "SELL",
                 "quantity": s["quantity"], "price": current_prices.get(s["symbol"]),
                 "reason": s["reason"]}
                for s in stops
            ]
            stop_results = self.orders.execute_trade_list(
                stop_trades, current_prices, budget=0, net_liquidation=portfolio_value,
            )
            for result, stop in zip(stop_results, stops):
                if result.get("status") == "filled":
                    self.risk.record_exit(
                        stop["symbol"], result.get("fill_price") or stop["current_price"],
                        stop["reason"],
                    )
                else:
                    self.notifier.critical(
                        "Stop loss NOT executed",
                        f"{stop['symbol']} — {stop['reason']} (status {result.get('status')})",
                    )

        report = self.risk.generate_risk_report(current_positions, current_prices, portfolio_value)
        report["cancelled_orders"] = cancelled_orders
        report["stop_trades"] = stop_results
        report["actionable_events"] = {
            "cancelled_orders": len(cancelled_orders),
            "stop_orders_submitted": sum(
                1 for r in stop_results if isinstance(r, dict)
                and r.get("status") in ("submitted", "filled")
            ),
            "alerts": len(report.get("alerts", [])),
        }
        report["should_notify"] = any(report["actionable_events"].values())
        for alert in report.get("alerts", []):
            logger.warning(alert)
        return report
