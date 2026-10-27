"""
Adaptive Strategy Overlay  (v2 — Calibrated)
=============================================
Three separated layers with different roles, from the classic quant playbook:

LAYER A — Bear Market Shield  (Macro CTA / Checkmate)
  Triggered only by a CONFIRMED bear market (SPY drawdown from 52w-high).
  Does NOT fire on short-term corrections (<15%) to preserve upside in volatile
  but still bullish markets (e.g. COVID dip March 2020, intra-year pullbacks).
  • SPY drawdown 15-25%  → reduce all positions to 75%  (soft bear)
  • SPY drawdown > 25%   → reduce all positions to 50%  (hard bear)

LAYER B — RSI Mean Reversion Tilt  (Reversal / Ready-Set-Go)
  Tactical tilt around base weight; fires independently of regime.
  • Per-stock RSI < 30 → overweight +30%  (buy the confirmed dip)
  • Per-stock RSI > 75 → underweight -25%  (trim extended)
  Gentle tilts — broad base weight stays intact.

LAYER C — 52-Week Breakout Boost  (Range Break / ORB)
  Trend continuation signal from the page's "Range Break" strategy:
  stocks making or approaching new 52-week highs have momentum tailwind.
  • ≥98% of 52w high → +15% weight boost
  • <65% of 52w high → -15% weight penalty

Individual stock 200MA filter removed — too noisy, causes whipsaws during
recoveries. Per-stock stop-loss handled by the RiskManager at execution time.
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Tuple


class AdaptiveOverlay:
    """
    Computes dynamic weight multipliers for a fixed portfolio.
    Multipliers are applied to base target weights before execution.
    """

    def __init__(self,
                 trend_ma_long: int = 200,
                 trend_ma_short: int = 50,
                 rsi_period: int = 14,
                 rsi_oversold: float = 30,
                 rsi_overbought: float = 72,
                 atr_period: int = 20,
                 atr_vol_target: float = 0.015):   # ~24% annualised
        self.ma_long = trend_ma_long
        self.ma_short = trend_ma_short
        self.rsi_period = rsi_period
        self.rsi_low = rsi_oversold
        self.rsi_high = rsi_overbought
        self.atr_period = atr_period
        self.atr_vol_target = atr_vol_target

    # ------------------------------------------------------------------ #
    #  Main Entry Point
    # ------------------------------------------------------------------ #

    def compute_weights(self, base_weights: Dict[str, float],
                        prices: pd.DataFrame,
                        spy_prices: pd.Series = None) -> Tuple[Dict[str, float], dict]:
        """
        Apply all signal layers to base_weights.

        Returns:
            (adjusted_weights, signals_debug_dict)
        """
        signals = {}
        adjusted = {}

        # ---- Layer A: Bear Market Shield ---- #
        index = spy_prices if spy_prices is not None else prices.mean(axis=1)
        regime_scalar, regime_label = self._market_regime_drawdown(index)
        signals["market_regime"] = regime_scalar
        signals["regime_label"] = regime_label

        for sym, base_w in base_weights.items():
            p = prices[sym].dropna() if sym in prices.columns else pd.Series(dtype=float)

            # ---- Layer B: RSI Mean Reversion Tilt ---- #
            rsi_s = self._rsi_tilt(p) if len(p) >= self.rsi_period + 2 else 1.0

            # ---- Layer C: 52-Week Breakout Boost ---- #
            breakout_s = self._breakout_signal(p) if len(p) >= 60 else 1.0

            # Combine B+C: gentle tilt only, clamped to [0.70, 1.30]
            tilt = max(0.70, min(rsi_s * breakout_s, 1.30))

            # Apply regime as a hard cap, tilt as fine-tuning
            final_w = base_w * regime_scalar * tilt
            adjusted[sym] = final_w

            signals[sym] = {
                "rsi_signal": rsi_s,
                "breakout_signal": breakout_s,
                "tilt": tilt,
                "regime": regime_scalar,
                "base_w": base_w,
                "final_w": final_w,
            }

        # Weights can sum < 1.0 during bear (cash buffer); never exceed 1.0
        total = sum(adjusted.values())
        if total > 1.0:
            adjusted = {k: v / total for k, v in adjusted.items()}

        return adjusted, signals

    # ------------------------------------------------------------------ #
    #  Signal Implementations
    # ------------------------------------------------------------------ #

    def _market_regime_drawdown(self, index_prices: pd.Series):
        """
        Bear Market Shield: only triggers on confirmed bear via SPY drawdown
        from 52-week high. Ignores short-term volatility (<15% dip).

        Returns: (scalar, label)
          1.00  'BULL'         — normal, full exposure
          0.75  'SOFT_BEAR'    — SPY down 15-25% from 52w high
          0.50  'HARD_BEAR'    — SPY down >25% from 52w high
        """
        p = index_prices.dropna()
        if len(p) < 60:
            return 1.0, 'BULL'

        window = min(252, len(p))
        peak_52w = p.rolling(window).max().iloc[-1]
        current = p.iloc[-1]
        drawdown_from_peak = (current - peak_52w) / peak_52w  # negative number

        if drawdown_from_peak > -0.15:
            return 1.00, 'BULL'
        elif drawdown_from_peak > -0.25:
            return 0.75, 'SOFT_BEAR'
        else:
            return 0.50, 'HARD_BEAR'

    def _rsi_tilt(self, prices: pd.Series) -> float:
        """
        RSI mean reversion: buy dips, trim peaks.
        Returns: 1.50 (oversold), 1.0 (neutral), 0.60 (overbought)
        """
        p = prices.dropna()
        if len(p) < self.rsi_period + 2:
            return 1.0

        rsi = self._calc_rsi(p, self.rsi_period)

        if rsi <= self.rsi_low:
            # Oversold — add on weakness (classic mean reversion)
            strength = (self.rsi_low - rsi) / self.rsi_low
            return 1.0 + 0.50 * strength        # Up to +50% more
        elif rsi >= self.rsi_high:
            # Overbought — trim
            strength = (rsi - self.rsi_high) / (100 - self.rsi_high)
            return 1.0 - 0.40 * strength        # Down to -40%
        else:
            return 1.0

    def _atr_scalar(self, prices: pd.Series) -> float:
        """
        ATR-based volatility scaling: reduce when high vol, increase when low vol.
        Targets equal risk contribution across positions.
        """
        p = prices.dropna()
        if len(p) < self.atr_period + 2:
            return 1.0

        # Simple ATR proxy: rolling high-low range as % of price
        daily_range_pct = (p.diff().abs() / p.shift(1)).rolling(self.atr_period).mean().iloc[-1]

        if daily_range_pct <= 0 or np.isnan(daily_range_pct):
            return 1.0

        scalar = self.atr_vol_target / daily_range_pct
        return max(0.50, min(scalar, 1.50))

    def _breakout_signal(self, prices: pd.Series) -> float:
        """
        52-Week Breakout Boost (Range Break strategy).
        Stocks near/at new 52-week highs have momentum continuation tailwind.
        Stocks deep in a hole get a mild penalty.
        """
        p = prices.dropna()
        window = min(252, len(p))
        if window < 60:
            return 1.0

        high_52w = p.rolling(window).max().iloc[-1]
        current = p.iloc[-1]
        pct = current / high_52w if high_52w > 0 else 1.0

        if pct >= 0.98:
            return 1.15   # Near/at 52w high — momentum breakout boost
        elif pct >= 0.90:
            return 1.05   # Within 10% of high
        elif pct < 0.65:
            return 0.85   # Deep in drawdown — cautious
        else:
            return 1.0

    # ------------------------------------------------------------------ #
    #  Helper
    # ------------------------------------------------------------------ #

    @staticmethod
    def _calc_rsi(prices: pd.Series, period: int = 14) -> float:
        delta = prices.diff().dropna()
        gains = delta.clip(lower=0)
        losses = (-delta).clip(lower=0)
        avg_gain = gains.ewm(span=period, adjust=False).mean().iloc[-1]
        avg_loss = losses.ewm(span=period, adjust=False).mean().iloc[-1]
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))
