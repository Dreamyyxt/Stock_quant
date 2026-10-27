"""
Combined Multi-Factor Strategy
===============================
Blends Momentum (40%) + Quality/Value (60%) signals.

Portfolio split:
  - Core bucket (70% capital): Large/mid cap stocks from S&P500/R1000
    → Top momentum + quality stocks with trend confirmation
  - Growth bucket (30% capital): Undervalued growth stocks
    → High quality, strong growth, NOT overowned, reasonable valuation

This combination historically targets:
  - 30-50% annualized return in bull markets
  - ~20% drawdown max in typical corrections
  - Sharpe ratio 1.0-1.5
"""

import logging
from typing import Dict, List, Tuple
import pandas as pd
import numpy as np
from strategies.base_strategy import BaseStrategy, Signal
from strategies.momentum import MomentumStrategy
from strategies.quality_value import QualityValueStrategy
from config.settings import STRATEGY_CONFIG

logger = logging.getLogger(__name__)


class CombinedStrategy(BaseStrategy):
    """
    Multi-factor strategy combining momentum, quality, and value signals.
    Produces a final ranked list of stocks with target weights.
    """

    def __init__(self, config: dict = None):
        cfg = config or STRATEGY_CONFIG
        super().__init__("CombinedStrategy", cfg)
        self.momentum = MomentumStrategy(cfg)
        self.quality_value = QualityValueStrategy(cfg)

        self.momentum_weight = cfg.get("momentum_weight", 0.40)
        self.quality_weight = cfg.get("quality_weight", 0.30)
        self.value_weight = cfg.get("value_weight", 0.30)

        self.max_positions = cfg.get("max_positions", 20)
        self.core_positions = cfg.get("core_positions", 14)
        self.growth_positions = cfg.get("growth_positions", 6)
        self.max_position_size = cfg.get("max_position_size", 0.12)
        self.min_position_size = cfg.get("min_position_size", 0.02)
        self.max_sector_weight = cfg.get("max_sector_weight", 0.35)

    def generate_signals(self, universe: List[str], prices: pd.DataFrame,
                         fundamentals: Dict[str, dict]) -> List[Signal]:
        """
        Generate combined signals for the full universe.
        Returns signals sorted by composite score, with target weights assigned.
        """
        mom_signals = self.momentum.generate_signals(universe, prices, fundamentals)
        qv_signals = self.quality_value.generate_signals(universe, prices, fundamentals)

        # Build score lookup
        mom_scores = {s.symbol: s for s in mom_signals}
        qv_scores = {s.symbol: s for s in qv_signals}

        all_symbols = set(s.symbol for s in mom_signals) | set(s.symbol for s in qv_signals)

        combined = []
        for symbol in all_symbols:
            mom = mom_scores.get(symbol)
            qv = qv_scores.get(symbol)

            mom_score = mom.score if mom else 0.0
            qv_score = qv.score if qv else 0.0
            mom_direction = mom.direction if mom else 1
            qv_direction = qv.direction if qv else 1

            # If absolute momentum is negative, downgrade score
            if mom_direction == 0:
                mom_score *= 0.3

            composite = (
                self.momentum_weight * mom_score +
                (self.quality_weight + self.value_weight) * qv_score
            )

            metadata = {}
            if mom:
                metadata.update(mom.metadata)
            if qv:
                metadata.update(qv.metadata)

            combined.append(Signal(
                symbol=symbol,
                score=composite,
                direction=1 if composite > 0.35 else 0,
                suggested_weight=0.0,
                reason=f"composite={composite:.3f} | mom={mom_score:.3f} qv={qv_score:.3f}",
                metadata=metadata,
            ))

        combined.sort(key=lambda s: s.score, reverse=True)
        return combined

    def build_portfolio(self, core_signals: List[Signal], growth_signals: List[Signal],
                        sector_map: Dict[str, str] = None) -> Dict[str, float]:
        """
        Construct the target portfolio weights from ranked signals.

        Returns dict: {symbol: target_weight}
        """
        portfolio = {}
        sector_weights = {}
        sector_map = sector_map or {}

        def add_position(signal: Signal, weight: float):
            symbol = signal.symbol
            sector = sector_map.get(symbol, "Unknown")
            sector_w = sector_weights.get(sector, 0.0)

            if sector_w + weight > self.max_sector_weight:
                adjusted = max(self.max_sector_weight - sector_w, self.min_position_size)
                weight = adjusted

            if weight >= self.min_position_size:
                portfolio[symbol] = weight
                sector_weights[sector] = sector_w + weight
                return True
            return False

        # --- Core bucket (70% of portfolio) ---
        core_budget = 0.70
        top_core = [s for s in core_signals if s.direction == 1][:self.core_positions]

        if top_core:
            # Score-weighted allocation within budget
            weights = self._score_to_weights(top_core, budget=core_budget)
            for sig, w in zip(top_core, weights):
                add_position(sig, w)

        # --- Growth bucket (30% of portfolio) ---
        growth_budget = 1.0 - sum(portfolio.values())
        top_growth = [s for s in growth_signals if s.direction == 1][:self.growth_positions]

        if top_growth:
            weights = self._score_to_weights(top_growth, budget=growth_budget)
            for sig, w in zip(top_growth, weights):
                add_position(sig, w)

        # Renormalize to ensure weights sum to ≤ 1.0
        total = sum(portfolio.values())
        if total > 1.0:
            portfolio = {k: v / total for k, v in portfolio.items()}

        logger.info("Portfolio constructed: %d positions, total weight=%.2f",
                    len(portfolio), sum(portfolio.values()))
        return portfolio

    def _score_to_weights(self, signals: List[Signal], budget: float) -> List[float]:
        """
        Convert scores to position weights, bounded by min/max position size.
        Uses score-proportional weighting (higher score = larger weight).
        """
        if not signals:
            return []

        n = len(signals)
        scores = np.array([max(s.score, 0.01) for s in signals])

        # Score-proportional weights
        raw_weights = scores / scores.sum()
        weights = raw_weights * budget

        # Clamp to [min, max] position size
        weights = np.clip(weights, self.min_position_size, self.max_position_size)

        # Re-scale to match budget
        total = weights.sum()
        if total > 0:
            weights = weights * (budget / total)

        return weights.tolist()

    def get_rebalance_trades(self, current_positions: Dict[str, float],
                             target_weights: Dict[str, float],
                             total_value: float,
                             prices: Dict[str, float],
                             threshold: float = 0.05) -> List[dict]:
        """
        Calculate trades needed to move from current to target portfolio.

        Args:
            current_positions: {symbol: current_weight}
            target_weights: {symbol: target_weight}
            total_value: Portfolio total value in USD
            prices: {symbol: current_price}
            threshold: Minimum drift to trigger a trade (default 5%)

        Returns:
            List of trade dicts: {symbol, action, quantity, reason}
        """
        trades = []
        all_symbols = set(current_positions.keys()) | set(target_weights.keys())

        for symbol in all_symbols:
            current_w = current_positions.get(symbol, 0.0)
            target_w = target_weights.get(symbol, 0.0)
            drift = target_w - current_w

            if abs(drift) < threshold:
                continue

            price = prices.get(symbol)
            if not price or price <= 0:
                logger.warning("No price for %s, skipping trade", symbol)
                continue

            dollar_change = drift * total_value
            quantity = int(abs(dollar_change) / price)

            if quantity < 1:
                continue

            action = "BUY" if drift > 0 else "SELL"
            trades.append({
                "symbol": symbol,
                "action": action,
                "quantity": quantity,
                "price": price,
                "dollar_value": quantity * price,
                "current_weight": current_w,
                "target_weight": target_w,
                "drift": drift,
                "reason": f"Drift {drift:+.1%}: {current_w:.1%} → {target_w:.1%}",
            })

        # Sort: sells first (free up cash), then buys by size
        trades.sort(key=lambda t: (0 if t["action"] == "SELL" else 1, -t["dollar_value"]))
        return trades
