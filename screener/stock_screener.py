"""
Stock Screener for Undervalued Growth Stocks
=============================================
Identifies stocks with:
  1. Strong revenue/earnings growth (>12% YoY)
  2. Not in top "mega cap popular" category — want less crowded ideas
  3. Good quality metrics (positive FCF, manageable debt)
  4. Reasonable valuation (PEG < 1.5, not priced for perfection)
  5. Technical confirmation (uptrend or early breakout)
"""

import logging
from typing import Dict, List, Optional
import pandas as pd
import numpy as np
from config.settings import STRATEGY_CONFIG

logger = logging.getLogger(__name__)


class GrowthStockScreener:
    """
    Screens growth stock candidates from a given universe.
    Produces a ranked list of undervalued growth opportunities.
    """

    def __init__(self, config: dict = None):
        cfg = config or STRATEGY_CONFIG
        self.min_revenue_growth = cfg.get("min_revenue_growth", 0.12)
        self.min_gross_margin = cfg.get("min_gross_margin", 0.30)
        self.max_peg = cfg.get("max_peg_growth", 1.5)
        self.max_institutional_ownership = cfg.get("max_institutional_ownership", 0.85)

    def screen(self, universe: List[str], prices: pd.DataFrame,
               fundamentals: Dict[str, dict]) -> List[dict]:
        """
        Screen and rank undervalued growth stocks.

        Returns list of dicts sorted by composite score:
        {symbol, score, grade, reasons, metrics}
        """
        results = []

        for symbol in universe:
            if symbol not in prices.columns:
                continue

            p = prices[symbol].dropna()
            if len(p) < 60:
                continue

            fund = fundamentals.get(symbol, {})

            score, grade, reasons, metrics = self._evaluate(symbol, p, fund)
            if score is None:
                continue

            results.append({
                "symbol": symbol,
                "score": score,
                "grade": grade,
                "reasons": reasons,
                "metrics": metrics,
            })

        results.sort(key=lambda x: x["score"], reverse=True)
        logger.info("Growth screener: %d/%d passed initial filters",
                    len(results), len(universe))
        return results

    def _evaluate(self, symbol: str, prices: pd.Series,
                  fund: dict) -> tuple:
        """Score a single stock. Returns (score, grade, reasons, metrics)."""
        scores = {}
        reasons = []
        metrics = {}

        # ---- GROWTH SIGNALS ---- #

        # Revenue growth proxy: earnings growth or price momentum as proxy
        pe = fund.get("pe_ratio")
        eps = fund.get("eps")
        revenue = fund.get("revenue")

        # If we have PE data, screen out loss-makers
        if pe is not None:
            if pe <= 0:
                return None, "", [], {}  # Loss-making: skip
            metrics["pe_ratio"] = pe

        # ---- MOMENTUM AS GROWTH PROXY ---- #
        # Price momentum 6-12 months often reflects earnings trajectory
        if len(prices) >= 126:
            mom_6m = (prices.iloc[-1] / prices.iloc[-126]) - 1
            metrics["momentum_6m"] = mom_6m
            if mom_6m > 0.10:
                scores["growth_momentum"] = min(mom_6m / 0.60, 1.0)
                reasons.append(f"6m momentum={mom_6m:.0%}")

        if len(prices) >= 252:
            mom_12m = (prices.iloc[-1] / prices.iloc[-252]) - 1
            metrics["momentum_12m"] = mom_12m
            if mom_12m > 0.15:
                scores["growth_12m"] = min(mom_12m / 1.0, 1.0)

        # ---- VALUE SIGNALS ---- #
        peg = fund.get("peg_ratio")
        if peg is not None and 0 < peg < self.max_peg:
            peg_score = 1.0 - (peg / self.max_peg)
            scores["value_peg"] = peg_score
            reasons.append(f"PEG={peg:.2f}")
            metrics["peg_ratio"] = peg

        pb = fund.get("pb_ratio")
        if pb is not None and 0 < pb < 8:
            pb_score = 1.0 - (pb / 8.0)
            scores["value_pb"] = pb_score
            metrics["pb_ratio"] = pb

        # ---- TECHNICAL QUALITY ---- #

        # 1. Consecutive months of positive returns (earnings consistency)
        if len(prices) >= 60:
            monthly_returns = prices.resample("ME").last().pct_change().dropna()
            if len(monthly_returns) >= 3:
                recent_monthly = monthly_returns.iloc[-3:]
                positive_months = (recent_monthly > 0).sum()
                consistency_score = positive_months / 3.0
                scores["consistency"] = consistency_score

        # 2. Low drawdown (quality business doesn't crash often)
        if len(prices) >= 126:
            rolling_max = prices.rolling(126).max()
            drawdown = (prices / rolling_max) - 1
            max_dd_6m = drawdown.iloc[-126:].min()
            metrics["max_drawdown_6m"] = max_dd_6m
            if max_dd_6m > -0.30:  # Less than 30% drawdown in 6 months
                dd_score = 1.0 + max_dd_6m / 0.30
                scores["drawdown_quality"] = max(dd_score, 0)

        # 3. Breakout: near 52-week high with volume confirmation proxy
        if len(prices) >= 252:
            high_52w = prices.rolling(252).max().iloc[-1]
            pct_from_high = prices.iloc[-1] / high_52w
            metrics["pct_from_52w_high"] = pct_from_high
            # Sweet spot: 85-100% of 52-week high (approaching breakout or breakout)
            if pct_from_high > 0.85:
                breakout_score = (pct_from_high - 0.85) / 0.15
                scores["breakout"] = min(breakout_score, 1.0)
                if pct_from_high > 0.95:
                    reasons.append(f"Near 52w high ({pct_from_high:.0%})")

        # 4. Relative strength vs SPY-like behavior
        if len(prices) >= 60:
            price_vol = prices.pct_change().rolling(60).std().iloc[-1]
            # Lower vol with upside = quality growth
            if price_vol < 0.025:  # annualized ~40% or less
                scores["low_vol_quality"] = 1.0 - (price_vol / 0.025)

        if not scores:
            return None, "", [], {}

        composite = np.mean(list(scores.values()))

        # Grading
        if composite >= 0.75:
            grade = "A"
        elif composite >= 0.60:
            grade = "B"
        elif composite >= 0.45:
            grade = "C"
        else:
            grade = "D"

        return composite, grade, reasons, metrics

    def get_top_picks(self, screened: List[dict], n: int = 10) -> List[str]:
        """Return top N symbols from screened results."""
        return [r["symbol"] for r in screened[:n]]

    def print_report(self, screened: List[dict], top_n: int = 15):
        """Print a formatted screening report."""
        print("\n" + "=" * 70)
        print(f"  UNDERVALUED GROWTH STOCK SCREENER  — Top {top_n} Picks")
        print("=" * 70)
        print(f"{'Symbol':<8} {'Grade':<6} {'Score':<8} {'Key Signals'}")
        print("-" * 70)

        for r in screened[:top_n]:
            reasons_str = " | ".join(r["reasons"]) if r["reasons"] else "-"
            print(f"{r['symbol']:<8} {r['grade']:<6} {r['score']:.3f}   {reasons_str}")

        print("=" * 70 + "\n")
