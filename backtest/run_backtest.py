"""
Run a backtest using yfinance for historical data (no Tiger API needed).
Usage: python backtest/run_backtest.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
import pandas as pd
import yfinance as yf
from backtest.backtester import Backtester
from config.settings import CORE_UNIVERSE, GROWTH_UNIVERSE

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def fetch_backtest_data(symbols: list, start: str = "2019-01-01",
                         end: str = None) -> pd.DataFrame:
    """Download adjusted close prices from Yahoo Finance."""
    end = end or pd.Timestamp.today().strftime("%Y-%m-%d")
    logger.info("Downloading price data for %d symbols (%s to %s)...",
                len(symbols), start, end)
    raw = yf.download(symbols, start=start, end=end, auto_adjust=True, progress=False)
    if "Close" in raw.columns:
        prices = raw["Close"]
    else:
        prices = raw
    prices = prices.dropna(how="all", axis=0).dropna(how="all", axis=1)
    logger.info("Downloaded: %d symbols, %d trading days", len(prices.columns), len(prices))
    return prices


def main():
    # Use a combined universe for backtesting
    universe = list(set(CORE_UNIVERSE + GROWTH_UNIVERSE))

    # Download 5 years of data
    prices = fetch_backtest_data(universe, start="2020-01-01")

    if prices.empty:
        logger.error("No price data downloaded. Check your internet connection.")
        return

    # Run backtest
    bt = Backtester(
        initial_capital=100_000,
        commission_per_trade=0.001,
        slippage_per_trade=0.001,
    )
    result = bt.run(
        prices=prices,
        start_date="2020-06-01",
        end_date=None,
        rebalance_freq="ME",
    )

    # Save equity curve
    output_path = "data/backtest_equity.csv"
    os.makedirs("data", exist_ok=True)
    result["equity_curve"].to_csv(output_path)
    logger.info("Equity curve saved to %s", output_path)

    # Save trade log
    if not result["trades"].empty:
        result["trades"].to_csv("data/backtest_trades.csv", index=False)
        logger.info("Trade log saved to data/backtest_trades.csv (%d trades)",
                    len(result["trades"]))

    # Compare to SPY benchmark
    try:
        spy = yf.download("SPY", start="2020-06-01", auto_adjust=True, progress=False)
        spy_ret = (spy["Close"].iloc[-1] / spy["Close"].iloc[0]) - 1
        strat_ret = result["total_return"]
        logger.info("Strategy vs SPY: %.1f%% vs %.1f%% total return",
                    strat_ret * 100, spy_ret * 100)
    except Exception:
        pass

    return result


if __name__ == "__main__":
    main()
