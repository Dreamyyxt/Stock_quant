"""
Backtesting Engine
==================
Simulates the combined strategy on historical data.
Uses yfinance for free historical data in backtesting mode
(no Tiger API needed for backtesting).

Run: python -m backtest.backtester
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

# Sector mapping for backtesting (simplified)
SECTOR_MAP = {
    "AAPL": "Tech", "MSFT": "Tech", "GOOGL": "Tech", "META": "Tech",
    "NVDA": "Tech", "AMD": "Tech", "AVGO": "Tech", "QCOM": "Tech",
    "ORCL": "Tech", "CRM": "Tech", "ADBE": "Tech",
    "UNH": "Healthcare", "LLY": "Healthcare", "JNJ": "Healthcare",
    "ABBV": "Healthcare", "MRK": "Healthcare", "TMO": "Healthcare",
    "JPM": "Finance", "BAC": "Finance", "GS": "Finance", "V": "Finance",
    "MA": "Finance", "PYPL": "Finance",
    "AMZN": "Consumer", "TSLA": "Consumer", "HD": "Consumer", "COST": "Consumer",
    "HON": "Industrial", "GE": "Industrial", "CAT": "Industrial",
    "XOM": "Energy", "CVX": "Energy",
    "NFLX": "Communication", "DIS": "Communication",
}


class Backtester:
    """
    Event-driven backtester for the multi-factor strategy.
    Uses monthly rebalancing with daily PnL tracking.
    """

    def __init__(self, initial_capital: float = 100_000,
                 commission_per_trade: float = 0.001,
                 slippage_per_trade: float = 0.001):
        self.initial_capital = initial_capital
        self.commission = commission_per_trade   # 0.1% per trade
        self.slippage = slippage_per_trade        # 0.1% slippage

    def run(self, prices: pd.DataFrame, start_date: str = None,
            end_date: str = None, rebalance_freq: str = "ME") -> dict:
        """
        Run backtest.

        Args:
            prices: DataFrame of adjusted close prices (index=date, cols=symbols)
            start_date: "YYYY-MM-DD" (default: 2 years ago)
            end_date: "YYYY-MM-DD" (default: today)
            rebalance_freq: pandas offset alias for rebalancing frequency
                            "ME" = month end, "W" = weekly, "QE" = quarterly

        Returns:
            Dict with equity curve, trades, performance metrics
        """
        from strategies.momentum import MomentumStrategy
        from strategies.quality_value import QualityValueStrategy
        from config.settings import STRATEGY_CONFIG, CORE_UNIVERSE, GROWTH_UNIVERSE

        if start_date:
            prices = prices[prices.index >= pd.Timestamp(start_date)]
        if end_date:
            prices = prices[prices.index <= pd.Timestamp(end_date)]

        if prices.empty or len(prices) < 60:
            raise ValueError("Insufficient data for backtesting")

        momentum_strat = MomentumStrategy(STRATEGY_CONFIG)
        quality_strat = QualityValueStrategy(STRATEGY_CONFIG)

        # Rebalancing dates
        rebal_dates = prices.resample(rebalance_freq).last().index.tolist()

        capital = self.initial_capital
        holdings: Dict[str, int] = {}        # {symbol: shares}
        entry_prices: Dict[str, float] = {}  # {symbol: entry_price}
        cash = capital
        equity_curve = []
        all_trades = []

        logger.info("Backtest: %s to %s | capital=$%.0f | rebalance=%s",
                    prices.index[0].date(), prices.index[-1].date(),
                    capital, rebalance_freq)

        for date in prices.index:
            day_prices = prices.loc[date].dropna()

            # Mark-to-market
            portfolio_value = cash
            for sym, shares in holdings.items():
                if sym in day_prices:
                    portfolio_value += shares * day_prices[sym]

            equity_curve.append({"date": date, "value": portfolio_value})

            # --- Stop Loss Check ---
            for sym in list(holdings.keys()):
                if sym not in day_prices:
                    continue
                entry = entry_prices.get(sym, day_prices[sym])
                loss = (day_prices[sym] - entry) / entry
                if loss <= -0.15:  # 15% hard stop
                    qty = holdings[sym]
                    sell_price = day_prices[sym] * (1 - self.slippage)
                    proceeds = qty * sell_price * (1 - self.commission)
                    cash += proceeds
                    del holdings[sym]
                    del entry_prices[sym]
                    all_trades.append({
                        "date": date, "symbol": sym, "action": "SELL_STOP",
                        "quantity": qty, "price": sell_price,
                        "value": proceeds
                    })

            # --- Rebalancing ---
            if date in rebal_dates:
                available_history = prices.loc[:date]
                if len(available_history) < 60:
                    continue

                # Generate signals on past data only (no lookahead)
                core_syms = [s for s in CORE_UNIVERSE if s in prices.columns]
                growth_syms = [s for s in GROWTH_UNIVERSE if s in prices.columns]
                all_syms = core_syms + growth_syms

                mom_signals = momentum_strat.generate_signals(
                    all_syms, available_history, {}
                )
                qv_signals = quality_strat.generate_signals(
                    all_syms, available_history, {}
                )

                # Combine scores
                mom_lookup = {s.symbol: s.score for s in mom_signals}
                qv_lookup = {s.symbol: s.score for s in qv_signals}

                scored = {}
                for s in all_syms:
                    if s not in day_prices:
                        continue
                    m = mom_lookup.get(s, 0.3)
                    qv = qv_lookup.get(s, 0.3)
                    # Apply absolute momentum filter
                    mom_sig = next((x for x in mom_signals if x.symbol == s), None)
                    if mom_sig and mom_sig.direction == 0:
                        m *= 0.3
                    scored[s] = 0.40 * m + 0.60 * qv

                # Select top 20 stocks
                n_positions = STRATEGY_CONFIG.get("max_positions", 20)
                top_stocks = sorted(scored, key=scored.get, reverse=True)[:n_positions]
                top_stocks = [s for s in top_stocks if scored.get(s, 0) > 0.30]

                if not top_stocks:
                    continue

                # Equal weight for simplicity in backtest
                target_weight = 1.0 / len(top_stocks)

                # Close positions not in new portfolio
                for sym in list(holdings.keys()):
                    if sym not in top_stocks and sym in day_prices:
                        qty = holdings[sym]
                        sell_price = day_prices[sym] * (1 - self.slippage)
                        proceeds = qty * sell_price * (1 - self.commission)
                        cash += proceeds
                        del holdings[sym]
                        entry_prices.pop(sym, None)
                        all_trades.append({
                            "date": date, "symbol": sym, "action": "SELL_REBAL",
                            "quantity": qty, "price": sell_price, "value": proceeds
                        })

                # Recalculate portfolio value after sells
                portfolio_value = cash
                for sym, shares in holdings.items():
                    if sym in day_prices:
                        portfolio_value += shares * day_prices[sym]

                # Buy new / adjust existing positions
                for sym in top_stocks:
                    target_value = portfolio_value * target_weight
                    current_value = holdings.get(sym, 0) * day_prices.get(sym, 0)
                    delta = target_value - current_value

                    price = day_prices[sym]
                    if delta > price and cash >= delta:
                        # Buy
                        buy_price = price * (1 + self.slippage)
                        qty = int(delta / buy_price)
                        if qty > 0:
                            cost = qty * buy_price * (1 + self.commission)
                            if cost <= cash:
                                cash -= cost
                                holdings[sym] = holdings.get(sym, 0) + qty
                                entry_prices[sym] = buy_price
                                all_trades.append({
                                    "date": date, "symbol": sym, "action": "BUY",
                                    "quantity": qty, "price": buy_price, "value": cost
                                })

        # Final equity curve
        equity_df = pd.DataFrame(equity_curve).set_index("date")
        metrics = self._compute_metrics(equity_df["value"])

        trades_df = pd.DataFrame(all_trades) if all_trades else pd.DataFrame()

        result = {
            "equity_curve": equity_df,
            "trades": trades_df,
            "metrics": metrics,
            "final_value": equity_df["value"].iloc[-1] if not equity_df.empty else capital,
            "total_return": (equity_df["value"].iloc[-1] / capital - 1) if not equity_df.empty else 0,
        }

        self._print_summary(metrics, capital, result["final_value"])
        return result

    def _compute_metrics(self, equity: pd.Series) -> dict:
        """Compute comprehensive performance metrics."""
        if equity.empty or len(equity) < 2:
            return {}

        returns = equity.pct_change().dropna()
        total_return = (equity.iloc[-1] / equity.iloc[0]) - 1
        n_years = len(equity) / 252
        cagr = (1 + total_return) ** (1 / n_years) - 1 if n_years > 0 else 0

        annual_vol = returns.std() * np.sqrt(252)
        risk_free = 0.045 / 252
        excess_returns = returns - risk_free
        sharpe = (excess_returns.mean() / returns.std() * np.sqrt(252)) if returns.std() > 0 else 0

        # Sortino ratio (downside deviation)
        negative_returns = returns[returns < 0]
        downside_vol = negative_returns.std() * np.sqrt(252) if len(negative_returns) > 0 else annual_vol
        sortino = (returns.mean() * 252 - 0.045) / downside_vol if downside_vol > 0 else 0

        # Max drawdown
        rolling_max = equity.expanding().max()
        drawdown = (equity - rolling_max) / rolling_max
        max_dd = drawdown.min()

        # Calmar ratio (CAGR / max drawdown)
        calmar = cagr / abs(max_dd) if max_dd != 0 else 0

        # Win rate (monthly)
        monthly_ret = equity.resample("ME").last().pct_change().dropna()
        win_rate = (monthly_ret > 0).sum() / len(monthly_ret) if len(monthly_ret) > 0 else 0

        # Best / worst months
        best_month = monthly_ret.max() if len(monthly_ret) > 0 else 0
        worst_month = monthly_ret.min() if len(monthly_ret) > 0 else 0

        return {
            "total_return": total_return,
            "cagr": cagr,
            "annual_volatility": annual_vol,
            "sharpe_ratio": sharpe,
            "sortino_ratio": sortino,
            "max_drawdown": max_dd,
            "calmar_ratio": calmar,
            "win_rate_monthly": win_rate,
            "best_month": best_month,
            "worst_month": worst_month,
            "n_years": n_years,
        }

    def _print_summary(self, metrics: dict, initial: float, final: float):
        print("\n" + "=" * 60)
        print("  BACKTEST RESULTS")
        print("=" * 60)
        print(f"  Initial Capital:     ${initial:>12,.0f}")
        print(f"  Final Value:         ${final:>12,.0f}")
        print(f"  Total Return:        {metrics.get('total_return', 0):>11.1%}")
        print(f"  CAGR:                {metrics.get('cagr', 0):>11.1%}")
        print(f"  Annual Volatility:   {metrics.get('annual_volatility', 0):>11.1%}")
        print(f"  Sharpe Ratio:        {metrics.get('sharpe_ratio', 0):>11.2f}")
        print(f"  Sortino Ratio:       {metrics.get('sortino_ratio', 0):>11.2f}")
        print(f"  Max Drawdown:        {metrics.get('max_drawdown', 0):>11.1%}")
        print(f"  Calmar Ratio:        {metrics.get('calmar_ratio', 0):>11.2f}")
        print(f"  Monthly Win Rate:    {metrics.get('win_rate_monthly', 0):>11.1%}")
        print(f"  Best Month:          {metrics.get('best_month', 0):>11.1%}")
        print(f"  Worst Month:         {metrics.get('worst_month', 0):>11.1%}")
        print("=" * 60 + "\n")
