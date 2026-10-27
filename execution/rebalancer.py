"""
Portfolio Rebalancer
====================
Orchestrates the full rebalancing cycle:
  1. Fetch current portfolio state
  2. Run screening + strategy signals
  3. Compute target weights (with risk adjustments)
  4. Calculate delta trades
  5. Execute trades via OrderManager
  6. Log results
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional
import pandas as pd

from core.data_fetcher import DataFetcher
from core.event_logger import EventLogger
from strategies.combined import CombinedStrategy
from screener.stock_screener import GrowthStockScreener
from risk.risk_manager import RiskManager
from risk.position_sizer import PositionSizer
from execution.order_manager import OrderManager
from config.settings import (
    CORE_UNIVERSE, GROWTH_UNIVERSE, SECTOR_MAP,
    STRATEGY_CONFIG, RISK_CONFIG, EXECUTION_CONFIG
)

logger = logging.getLogger(__name__)


class PortfolioRebalancer:
    """
    Full-cycle portfolio rebalancer.
    Call run_rebalance() on the configured rebalancing schedule.
    """

    def __init__(self, data_fetcher: DataFetcher, order_manager: OrderManager):
        self.data = data_fetcher
        self.orders = order_manager
        self.events = EventLogger()
        self.strategy = CombinedStrategy(STRATEGY_CONFIG)
        self.screener = GrowthStockScreener(STRATEGY_CONFIG)
        self.risk = RiskManager(RISK_CONFIG)
        self.sizer = PositionSizer(
            method=RISK_CONFIG.get("sizing_method", "factor_weighted"),
            target_volatility=RISK_CONFIG.get("target_volatility", 0.18),
            max_position=STRATEGY_CONFIG.get("max_position_size", 0.12),
            min_position=STRATEGY_CONFIG.get("min_position_size", 0.02),
        )

    def run_rebalance(self, dry_run: bool = False) -> dict:
        """
        Execute a full rebalancing cycle.

        Args:
            dry_run: If True, compute trades but don't execute them.

        Returns:
            Summary dict with trades, weights, performance metrics.
        """
        logger.info("=" * 60)
        logger.info("REBALANCING CYCLE START — %s (dry_run=%s)",
                    datetime.now().strftime("%Y-%m-%d %H:%M"), dry_run)
        logger.info("=" * 60)
        self.events.log("rebalance_started", mode="dynamic", dry_run=dry_run)

        summary = {
            "timestamp": datetime.now().isoformat(),
            "dry_run": dry_run,
            "status": "started",
        }

        # ---- 1. Account State ---- #
        account = self.data.get_account_summary()
        current_positions = self.data.get_current_positions()
        portfolio_value = account.get("net_liquidation", 0)

        if portfolio_value <= 0:
            logger.error("Invalid portfolio value: %s", portfolio_value)
            summary["status"] = "error: invalid portfolio value"
            self.events.log("rebalance_failed", mode="dynamic", reason="invalid portfolio value")
            return summary

        logger.info("Portfolio value: $%.2f | Positions: %d | Cash: $%.2f",
                    portfolio_value, len(current_positions), account.get("cash", 0))

        # ---- 2. Portfolio Drawdown Check ---- #
        triggered, exposure_limit = self.risk.check_portfolio_drawdown(portfolio_value)
        if triggered:
            logger.warning("Drawdown circuit breaker active! Reducing to %.0f%% exposure.",
                           exposure_limit * 100)

        # ---- 3. Fetch Market Data ---- #
        all_symbols = list(set(CORE_UNIVERSE + GROWTH_UNIVERSE + list(current_positions.keys())))
        logger.info("Fetching price data for %d symbols...", len(all_symbols))

        # 400 calendar days ≈ 285 trading days — enough for 12-month momentum (264 + 21 skip)
        prices = self.data.get_multi_price_history(all_symbols, lookback_days=400)
        returns = self.data.get_returns(all_symbols, lookback_days=400)
        fundamentals = self.data.get_fundamentals(all_symbols[:80])  # Limit API calls

        # ---- 4. Get Current Prices ---- #
        current_prices = {}
        quote_symbols = list(current_positions.keys()) + list(prices.columns[:50])
        quotes = self.data.get_quotes(list(set(quote_symbols)))
        for sym, q in quotes.items():
            if q.get("last"):
                current_prices[sym] = q["last"]

        # ---- 5. Check Stop Losses (executed and confirmed before rebalancing) ---- #
        stops_triggered = self.risk.check_stop_losses(current_positions, current_prices)
        if stops_triggered:
            logger.warning("%d stop losses triggered!", len(stops_triggered))
            stop_trades = [
                {"symbol": s["symbol"], "action": "SELL",
                 "quantity": s["quantity"], "price": current_prices.get(s["symbol"]),
                 "reason": s["reason"]}
                for s in stops_triggered
            ]
            if not dry_run:
                stop_results = self.orders.execute_trade_list(
                    stop_trades, current_prices, budget=0,
                    net_liquidation=portfolio_value,
                )
                for result, stop in zip(stop_results, stops_triggered):
                    if result.get("status") == "filled":
                        self.risk.record_exit(
                            stop["symbol"],
                            result.get("fill_price") or stop["current_price"],
                            stop["reason"],
                        )
                current_positions = self.data.get_current_positions()
            summary["stop_loss_trades"] = stop_trades

        # ---- 6. Generate Strategy Signals ---- #
        logger.info("Running strategy signals...")
        core_signals = self.strategy.generate_signals(CORE_UNIVERSE, prices, fundamentals)
        growth_screened = self.screener.screen(GROWTH_UNIVERSE, prices, fundamentals)

        # Convert screened growth to Signal objects
        from strategies.base_strategy import Signal as Sig
        growth_signals = [
            Sig(symbol=r["symbol"], score=r["score"], direction=1,
                suggested_weight=0.0, reason=", ".join(r["reasons"]))
            for r in growth_screened
        ]

        # ---- 7. Build Target Portfolio ---- #
        target_weights = self.strategy.build_portfolio(core_signals, growth_signals)

        # Apply volatility targeting
        if not returns.empty:
            target_weights = self.risk.apply_vol_targeting(target_weights, returns)

        # Apply drawdown exposure limit
        if triggered:
            target_weights = {s: w * exposure_limit for s, w in target_weights.items()}

        # Validate concentration limits (sector_map is mandatory, otherwise the
        # sector cap silently becomes a whole-portfolio cap)
        target_weights = self.risk.validate_weights(
            target_weights,
            sector_map=SECTOR_MAP,
            max_position=STRATEGY_CONFIG.get("max_position_size", 0.12),
            max_sector=STRATEGY_CONFIG.get("max_sector_weight", 0.35),
        )

        # Never re-enter a name that was stopped out recently
        for symbol in self.risk.cooldown_symbols():
            if target_weights.pop(symbol, None) is not None:
                logger.warning("Blocked re-entry into %s (stop-out cooldown)", symbol)

        # ---- 8. Compute Current Weights ---- #
        current_weights = {}
        for sym, pos in current_positions.items():
            mv = pos.get("market_value", 0)
            current_weights[sym] = mv / portfolio_value if portfolio_value > 0 else 0

        # ---- 9. Calculate Rebalancing Trades ---- #
        threshold = STRATEGY_CONFIG.get("min_rebalance_threshold", 0.05)
        trades = self.strategy.get_rebalance_trades(
            current_positions=current_weights,
            target_weights=target_weights,
            total_value=portfolio_value,
            prices=current_prices,
            threshold=threshold,
        )

        logger.info("Rebalancing: %d trades needed (%d buys, %d sells)",
                    len(trades),
                    sum(1 for t in trades if t["action"] == "BUY"),
                    sum(1 for t in trades if t["action"] == "SELL"))

        # ---- 10. Execute Trades ---- #
        execution_results = []
        if trades and not dry_run:
            planned_sell = sum(t.get("dollar_value", 0) for t in trades if t["action"] == "SELL")
            invested_now = sum(
                p.get("quantity", 0) * current_prices.get(sym, 0)
                for sym, p in current_positions.items()
            )
            target_value = sum(target_weights.values()) * portfolio_value
            buy_budget = max(0.0, target_value - max(0.0, invested_now - planned_sell))

            execution_results = self.orders.execute_trade_list(
                trades, current_prices,
                budget=buy_budget, net_liquidation=portfolio_value,
            )
            for r in execution_results:
                if r.get("status") != "filled":
                    continue
                if r["action"] == "BUY" and r.get("fill_price"):
                    self.risk.record_entry(r["symbol"], r["fill_price"])
                elif r["action"] == "SELL":
                    held = (current_positions.get(r["symbol"]) or {}).get("quantity", 0)
                    if r.get("filled_qty", 0) >= held:
                        self.risk.remove_position(r["symbol"])
        elif dry_run:
            logger.info("[DRY RUN] Trades would be:")
            for t in trades:
                logger.info("  %s %s x%d @ $%.2f — %s",
                            t["action"], t["symbol"], t["quantity"],
                            t.get("price", 0), t["reason"])

        summary.update({
            "status": "completed",
            "portfolio_value": portfolio_value,
            "target_weights": target_weights,
            "trades_planned": len(trades),
            "trades_executed": len(execution_results),
            "trades": trades if dry_run else execution_results,
            "top_core_picks": [s.symbol for s in core_signals[:10]],
            "top_growth_picks": [r["symbol"] for r in growth_screened[:6]],
        })

        logger.info("REBALANCING COMPLETE — %d trades, target exposure=%.1f%%",
                    len(trades), sum(target_weights.values()) * 100)
        self.events.log(
            "rebalance_completed",
            mode="dynamic",
            dry_run=dry_run,
            trades_planned=len(trades),
            trades_executed=len(execution_results),
        )

        return summary

    def run_daily_monitor(self) -> dict:
        """
        Run daily monitoring (not full rebalance):
        - Check stop losses
        - Cancel timed-out orders
        - Log risk metrics
        """
        logger.info("Running daily risk monitor...")
        current_positions = self.data.get_current_positions()
        account = self.data.get_account_summary()
        portfolio_value = account.get("net_liquidation", 0)

        current_prices = {}
        if current_positions:
            quotes = self.data.get_quotes(list(current_positions.keys()))
            current_prices = {s: q["last"] for s, q in quotes.items() if q.get("last")}

        # Cancel timed-out orders
        cancelled_orders = self.orders.cancel_timed_out_orders()

        # Check stops
        stops = self.risk.check_stop_losses(current_positions, current_prices)
        stop_results = []
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
                        stop["symbol"],
                        result.get("fill_price") or stop["current_price"],
                        stop["reason"],
                    )

        # Generate risk report
        risk_report = self.risk.generate_risk_report(
            current_positions, current_prices, portfolio_value
        )
        risk_report["cancelled_orders"] = cancelled_orders
        risk_report["stop_trades"] = stop_results
        risk_report["actionable_events"] = {
            "cancelled_orders": len(cancelled_orders),
            "stop_orders_submitted": sum(
                1 for r in stop_results if isinstance(r, dict) and r.get("status") == "submitted"
            ),
            "alerts": len(risk_report.get("alerts", [])),
        }
        risk_report["should_notify"] = any(risk_report["actionable_events"].values())

        # Log alerts
        for alert in risk_report.get("alerts", []):
            logger.warning(alert)

        return risk_report
