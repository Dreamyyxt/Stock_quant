"""
Dual Momentum Strategy
======================
Based on Gary Antonacci's Dual Momentum framework, adapted for individual stock selection.

- Cross-sectional (relative) momentum: Rank stocks by 12-1 month return
- Time-series (absolute) momentum: Only go long if stock outperforms risk-free rate
- Skip last month to avoid short-term mean reversion

Expected contribution: 12-18% per year when stocks are in uptrend.
Combined with quality and value factors, targets 30-50% annual return.
"""

import logging
from typing import Dict, List
import pandas as pd
import numpy as np
from strategies.base_strategy import BaseStrategy, Signal

logger = logging.getLogger(__name__)

# US 3-month T-bill annualized (approximate risk-free rate; update periodically)
RISK_FREE_ANNUAL = 0.045   # 4.5%


class MomentumStrategy(BaseStrategy):
    """
    Cross-sectional + absolute momentum factor scoring.

    Signals:
    - momentum_12_1: 12-month return excluding last month (main signal)
    - momentum_6_1:  6-month return excluding last month (secondary)
    - short_reversal: 1-month return inverted (avoid recent losers)
    - trend_filter: price vs 200-day MA
    """

    def __init__(self, config: dict):
        super().__init__("MomentumStrategy", config)
        self.lookback_months = config.get("momentum_lookback_months", 12)
        self.skip_months = config.get("momentum_skip_months", 1)

    def generate_signals(self, universe: List[str], prices: pd.DataFrame,
                         fundamentals: Dict[str, dict]) -> List[Signal]:
        if prices.empty:
            return []

        # Filter to symbols with enough data
        available = [s for s in universe if s in prices.columns]
        prices = prices[available].copy()

        # Minimum data requirement: lookback_months * 22 trading days
        min_days = self.lookback_months * 22
        valid_cols = prices.columns[prices.count() >= min_days].tolist()
        prices = prices[valid_cols]

        if prices.empty:
            return []

        signals = []
        scores = self._compute_scores(prices)

        for symbol in valid_cols:
            if symbol not in scores.index:
                continue

            row = scores.loc[symbol]
            composite = row["composite"]
            abs_momentum = row.get("abs_momentum", 0)

            # Absolute momentum filter: skip if stock return < risk-free rate
            if abs_momentum < 0:
                direction = 0
            else:
                direction = 1

            signals.append(Signal(
                symbol=symbol,
                score=composite,
                direction=direction,
                suggested_weight=0.0,  # Set by portfolio constructor
                reason=f"mom12_1={row['mom_12_1']:.2%}, trend={row['trend_score']:.2f}",
                metadata={
                    "momentum_12_1": row["mom_12_1"],
                    "momentum_6_1": row["mom_6_1"],
                    "trend_score": row["trend_score"],
                    "abs_momentum": abs_momentum,
                },
            ))

        signals.sort(key=lambda s: s.score, reverse=True)
        return signals

    def _compute_scores(self, prices: pd.DataFrame) -> pd.DataFrame:
        """Compute all momentum scores, return DataFrame indexed by symbol."""
        result = {}

        for symbol in prices.columns:
            p = prices[symbol].dropna()
            if len(p) < 60:
                continue

            current = p.iloc[-1]

            # 12-1 month momentum (skip last 21 trading days)
            skip = 21
            lookback_12 = self.lookback_months * 22
            if len(p) >= lookback_12 + skip:
                mom_12_1 = (p.iloc[-(skip + 1)] / p.iloc[-(lookback_12)]) - 1
            else:
                mom_12_1 = np.nan

            # 6-1 month momentum
            lookback_6 = 6 * 22
            if len(p) >= lookback_6 + skip:
                mom_6_1 = (p.iloc[-(skip + 1)] / p.iloc[-(lookback_6)]) - 1
            else:
                mom_6_1 = mom_12_1

            # Absolute momentum vs risk-free
            daily_rf = (1 + RISK_FREE_ANNUAL) ** (1 / 252) - 1
            rf_return_12m = (1 + daily_rf) ** min(lookback_12, len(p)) - 1
            abs_momentum = mom_12_1 - rf_return_12m if not np.isnan(mom_12_1) else -1

            # Trend: price above 200-day SMA
            ma200 = p.rolling(200).mean().iloc[-1] if len(p) >= 200 else p.mean()
            trend_score = 1.0 if current > ma200 else 0.0

            # 52-week high proximity (stocks near highs tend to continue)
            high_52w = p.rolling(252).max().iloc[-1] if len(p) >= 252 else p.max()
            pct_from_high = current / high_52w if high_52w > 0 else 0

            result[symbol] = {
                "mom_12_1": mom_12_1 if not np.isnan(mom_12_1) else 0,
                "mom_6_1": mom_6_1 if not np.isnan(mom_6_1) else 0,
                "abs_momentum": abs_momentum,
                "trend_score": trend_score,
                "pct_from_52w_high": pct_from_high,
            }

        if not result:
            return pd.DataFrame()

        df = pd.DataFrame(result).T

        # Cross-sectional rank normalization
        df["rank_12_1"] = self._rank_normalize(df["mom_12_1"])
        df["rank_6_1"] = self._rank_normalize(df["mom_6_1"])
        df["rank_high"] = self._rank_normalize(df["pct_from_52w_high"])

        # Composite momentum score
        df["composite"] = (
            0.50 * df["rank_12_1"] +
            0.30 * df["rank_6_1"] +
            0.10 * df["trend_score"] +
            0.10 * df["rank_high"]
        )

        return df
