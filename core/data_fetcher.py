"""
Market data fetching and caching layer.

Data source strategy:
  - Historical price data (OHLCV, 1+ years):  yfinance (free, unlimited symbols)
  - Current/delayed quotes:                    Tiger API delayed briefs
  - Account & positions:                       Tiger API trade client
  - Fundamentals (PE, PEG, etc.):             yfinance info (free)

Tiger API historical bars are limited to 20 symbols per account tier.
Using yfinance as the history backend removes this constraint entirely.
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import pandas as pd
import numpy as np
import yfinance as yf
from core.tiger_client import TigerClient

logger = logging.getLogger(__name__)


class DataFetcher:
    """Fetches and caches market data from Tiger Securities."""

    def __init__(self, client: TigerClient, cache_ttl_seconds: int = 300):
        self.client = client
        self.cache_ttl = cache_ttl_seconds
        self._price_cache: Dict[str, tuple] = {}   # symbol → (timestamp, df)
        self._quote_cache: Dict[str, tuple] = {}

    # ------------------------------------------------------------------ #
    #  Price / OHLCV
    # ------------------------------------------------------------------ #

    def get_price_history(self, symbol: str, lookback_days: int = 300,
                          period: str = "day") -> pd.DataFrame:
        """
        Fetch daily OHLCV for a symbol via yfinance.
        Returns DataFrame with columns: open, high, low, close, volume
        """
        cache_key = f"{symbol}_{period}_{lookback_days}"
        if cache_key in self._price_cache:
            ts, df = self._price_cache[cache_key]
            if time.time() - ts < self.cache_ttl:
                return df

        try:
            start = pd.Timestamp.now() - pd.Timedelta(days=lookback_days + 10)
            ticker = yf.Ticker(symbol)
            raw = ticker.history(start=start.strftime("%Y-%m-%d"), auto_adjust=True)
            if raw.empty:
                logger.warning("No yfinance data for %s", symbol)
                return pd.DataFrame()

            df = raw[["Open", "High", "Low", "Close", "Volume"]].copy()
            df.columns = ["open", "high", "low", "close", "volume"]
            df.index = df.index.tz_localize(None) if df.index.tz else df.index
            df.index.name = "date"
            df = df.sort_index()

            self._price_cache[cache_key] = (time.time(), df)
            return df

        except Exception as e:
            logger.error("Failed to fetch history for %s: %s", symbol, e)
            return pd.DataFrame()

    def get_multi_price_history(self, symbols: List[str], lookback_days: int = 300,
                                 field: str = "close") -> pd.DataFrame:
        """
        Fetch closing prices for multiple symbols via yfinance (bulk download).
        Returns DataFrame with symbols as columns.
        """
        cache_key = f"multi_{'_'.join(sorted(symbols)[:10])}_{lookback_days}"
        if cache_key in self._price_cache:
            ts, df = self._price_cache[cache_key]
            if time.time() - ts < self.cache_ttl:
                return df

        try:
            start = pd.Timestamp.now() - pd.Timedelta(days=lookback_days + 10)
            raw = yf.download(
                symbols, start=start.strftime("%Y-%m-%d"),
                auto_adjust=True, progress=False, threads=True
            )
            if raw.empty:
                return pd.DataFrame()

            if "Close" in raw.columns:
                prices = raw["Close"]
            elif isinstance(raw.columns, pd.MultiIndex):
                prices = raw.xs("Close", level=0, axis=1) if "Close" in raw.columns.get_level_values(0) else raw
            else:
                prices = raw

            prices = prices.sort_index()
            prices.index = prices.index.tz_localize(None) if prices.index.tz else prices.index
            prices = prices.dropna(how="all")

            self._price_cache[cache_key] = (time.time(), prices)
            return prices

        except Exception as e:
            logger.error("Failed to bulk download prices: %s", e)
            # Fallback: fetch one by one
            result = {}
            for symbol in symbols:
                df = self.get_price_history(symbol, lookback_days=lookback_days)
                if not df.empty and field in df.columns:
                    result[symbol] = df[field]
            return pd.DataFrame(result).sort_index() if result else pd.DataFrame()

    def get_returns(self, symbols: List[str], lookback_days: int = 300) -> pd.DataFrame:
        """Return daily log returns for a list of symbols."""
        prices = self.get_multi_price_history(symbols, lookback_days=lookback_days)
        if prices.empty:
            return pd.DataFrame()
        returns = np.log(prices / prices.shift(1)).dropna(how="all")
        return returns

    # ------------------------------------------------------------------ #
    #  Real-time Quotes
    # ------------------------------------------------------------------ #

    def get_quotes(self, symbols: List[str]) -> Dict[str, dict]:
        """
        Get latest quote snapshot for symbols.
        Returns dict: {symbol: {bid, ask, last, volume, ...}}
        """
        cache_key = "_".join(sorted(symbols))
        if cache_key in self._quote_cache:
            ts, data = self._quote_cache[cache_key]
            if time.time() - ts < 10:   # 10-second cache for quotes
                return data

        try:
            # get_quote uses get_stock_delay_briefs, returns a DataFrame
            df = self.client.get_quote(symbols)
            result = {}
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    sym = row.get("symbol")
                    if not sym:
                        continue
                    # Delayed briefs use 'close' as latest price
                    result[sym] = {
                        "symbol": sym,
                        "last": row.get("close"),
                        "open": row.get("open"),
                        "prev_close": row.get("pre_close"),
                        "volume": row.get("volume"),
                        "high": row.get("high"),
                        "low": row.get("low"),
                        "bid": None,   # Not available in delayed data
                        "ask": None,
                        "market_cap": None,
                        "pe_ratio": None,
                    }
            self._quote_cache[cache_key] = (time.time(), result)
            return result
        except Exception as e:
            logger.error("Failed to get quotes for %s: %s", symbols, e)
            return {}

    def get_current_price(self, symbol: str) -> Optional[float]:
        """Get the current/last price of a single symbol."""
        quotes = self.get_quotes([symbol])
        return quotes.get(symbol, {}).get("last")

    # ------------------------------------------------------------------ #
    #  Fundamental / Financial Data
    # ------------------------------------------------------------------ #

    def get_fundamentals(self, symbols: List[str]) -> Dict[str, dict]:
        """
        Fetch fundamental data via yfinance (no Tiger API permission required).
        Returns dict: {symbol: {pe_ratio, pb_ratio, peg_ratio, roe, revenue_growth, ...}}
        """
        result = {}
        for symbol in symbols:
            try:
                info = yf.Ticker(symbol).info
                result[symbol] = {
                    "symbol": symbol,
                    "pe_ratio": info.get("trailingPE") or info.get("forwardPE"),
                    "pb_ratio": info.get("priceToBook"),
                    "peg_ratio": info.get("pegRatio"),
                    "eps": info.get("trailingEps"),
                    "roe": info.get("returnOnEquity"),
                    "revenue_growth": info.get("revenueGrowth"),
                    "gross_margins": info.get("grossMargins"),
                    "profit_margins": info.get("profitMargins"),
                    "market_cap": info.get("marketCap"),
                    "debt_to_equity": info.get("debtToEquity"),
                    "free_cashflow": info.get("freeCashflow"),
                    "52w_high": info.get("fiftyTwoWeekHigh"),
                    "52w_low": info.get("fiftyTwoWeekLow"),
                }
                time.sleep(0.05)  # Light rate limiting
            except Exception as e:
                logger.warning("Could not fetch fundamentals for %s: %s", symbol, e)
                result[symbol] = {}
        return result

    # ------------------------------------------------------------------ #
    #  Account Data
    # ------------------------------------------------------------------ #

    def get_account_summary(self) -> dict:
        """Return a normalised account summary (net liq, cash, buying power)."""
        summary = self.client.get_account_summary()
        if not summary:
            logger.error("Account summary unavailable — sizing decisions must not proceed")
        return summary or {}

    def get_current_positions(self) -> Dict[str, dict]:
        """Return current positions as {symbol: {quantity, avg_cost, market_value, ...}}."""
        positions = self.client.get_positions()
        if positions is None:
            logger.error("Positions unavailable — treating as empty is unsafe, check broker")
            return {}
        return positions

    def clear_cache(self):
        """Clear all cached data."""
        self._price_cache.clear()
        self._quote_cache.clear()
        logger.debug("Data cache cleared")
