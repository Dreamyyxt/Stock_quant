"""
Position Sizer
==============
Implements multiple position sizing methods:
  1. Equal weight — simple and robust
  2. Volatility parity — inverse-vol weighting (each position contributes equal vol)
  3. Factor weighted — score-proportional with vol adjustment (default)
"""

import logging
from typing import Dict, List
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


class PositionSizer:
    """Calculates optimal position sizes given a set of selected stocks."""

    def __init__(self, method: str = "factor_weighted",
                 target_volatility: float = 0.18,
                 max_position: float = 0.12,
                 min_position: float = 0.02):
        self.method = method
        self.target_vol = target_volatility
        self.max_pos = max_position
        self.min_pos = min_position

    def compute_weights(self, symbols: List[str],
                        returns: pd.DataFrame,
                        scores: Dict[str, float] = None) -> Dict[str, float]:
        """
        Compute target portfolio weights.

        Args:
            symbols: Stocks to include
            returns: Daily returns DataFrame (index=date, cols=symbols)
            scores: Optional {symbol: factor_score} for factor_weighted method

        Returns:
            {symbol: weight} normalized to sum ≤ 1.0
        """
        if not symbols:
            return {}

        available = [s for s in symbols if s in returns.columns]
        if not available:
            return {s: 1.0 / len(symbols) for s in symbols}

        if self.method == "equal":
            return self._equal_weight(symbols)
        elif self.method == "vol_parity":
            return self._vol_parity(available, returns)
        elif self.method == "factor_weighted":
            return self._factor_weighted(available, returns, scores or {})
        else:
            logger.warning("Unknown sizing method %s, using equal weight", self.method)
            return self._equal_weight(symbols)

    def _equal_weight(self, symbols: List[str]) -> Dict[str, float]:
        w = 1.0 / len(symbols)
        return {s: min(w, self.max_pos) for s in symbols}

    def _vol_parity(self, symbols: List[str], returns: pd.DataFrame) -> Dict[str, float]:
        """Inverse-volatility weighting: each position targets equal volatility contribution."""
        vols = {}
        for s in symbols:
            if s in returns.columns:
                r = returns[s].dropna()
                if len(r) >= 20:
                    vols[s] = r.rolling(60).std().iloc[-1] * np.sqrt(252)
                else:
                    vols[s] = self.target_vol  # Fallback

        if not vols:
            return self._equal_weight(symbols)

        inv_vol = {s: 1.0 / max(v, 0.01) for s, v in vols.items()}
        total_inv_vol = sum(inv_vol.values())

        raw = {s: iv / total_inv_vol for s, iv in inv_vol.items()}

        # Scale to target portfolio vol
        port_vol = self._portfolio_vol(raw, returns)
        scale = self.target_vol / port_vol if port_vol > 0 else 1.0
        scale = max(0.3, min(scale, 1.5))

        weights = {s: w * scale for s, w in raw.items()}
        return self._clamp_and_normalize(weights)

    def _factor_weighted(self, symbols: List[str], returns: pd.DataFrame,
                         scores: Dict[str, float]) -> Dict[str, float]:
        """
        Combine factor scores with volatility parity for final weights.
        Higher-scoring stocks get more weight, but vol-adjusted.
        """
        vol_weights = self._vol_parity(symbols, returns)

        if not scores:
            return vol_weights

        # Blend 50% vol parity + 50% score proportional
        score_total = sum(max(scores.get(s, 0.5), 0.01) for s in symbols)
        score_weights = {s: max(scores.get(s, 0.5), 0.01) / score_total for s in symbols}

        blended = {}
        for s in symbols:
            vw = vol_weights.get(s, 1.0 / len(symbols))
            sw = score_weights.get(s, 1.0 / len(symbols))
            blended[s] = 0.50 * vw + 0.50 * sw

        return self._clamp_and_normalize(blended)

    def _clamp_and_normalize(self, weights: Dict[str, float]) -> Dict[str, float]:
        """Clamp to [min, max] and re-normalize."""
        clamped = {s: max(self.min_pos, min(w, self.max_pos)) for s, w in weights.items()}
        total = sum(clamped.values())
        if total > 0:
            normalized = {s: w / total for s, w in clamped.items()}
        else:
            normalized = clamped
        return normalized

    def _portfolio_vol(self, weights: Dict[str, float], returns: pd.DataFrame) -> float:
        """Approximate portfolio volatility (diagonal covariance for simplicity)."""
        total_var = 0.0
        for s, w in weights.items():
            if s in returns.columns:
                r = returns[s].dropna()
                if len(r) >= 20:
                    vol = r.rolling(60).std().iloc[-1] * np.sqrt(252)
                    total_var += (w * vol) ** 2
        return np.sqrt(total_var) if total_var > 0 else self.target_vol

    def shares_from_weights(self, weights: Dict[str, float],
                             portfolio_value: float,
                             prices: Dict[str, float]) -> Dict[str, int]:
        """Convert target weights to share quantities."""
        shares = {}
        for symbol, weight in weights.items():
            price = prices.get(symbol)
            if price and price > 0:
                dollar_amount = portfolio_value * weight
                qty = int(dollar_amount / price)
                if qty > 0:
                    shares[symbol] = qty
        return shares
