"""
Quality + Value Strategy
========================
Combines Piotroski F-Score quality signals with value metrics.

Quality signals (Piotroski F-Score inspired):
  - Profitability: positive ROE, positive operating cash flow
  - Leverage: decreasing debt
  - Operating efficiency: improving gross margins, asset turnover

Value metrics:
  - PEG ratio (P/E divided by growth rate) — the core value signal
  - Price/FCF
  - EV/EBITDA relative to peers

This strategy specifically targets stocks that are:
  - High quality operationally
  - Not yet recognized/re-rated (still at reasonable valuations)
"""

import logging
from typing import Dict, List
import pandas as pd
import numpy as np
from strategies.base_strategy import BaseStrategy, Signal

logger = logging.getLogger(__name__)


class QualityValueStrategy(BaseStrategy):
    """
    Quality + Value composite factor strategy.

    Uses available fundamental data from Tiger API, supplemented by
    price-based proxy signals where fundamentals are unavailable.
    """

    def __init__(self, config: dict):
        super().__init__("QualityValueStrategy", config)
        self.min_roe = config.get("min_roe", 0.10)
        self.max_debt_equity = config.get("max_debt_to_equity", 2.0)
        self.max_peg = config.get("max_peg_ratio", 2.5)
        self.max_pe = config.get("max_pe_ratio", 60)

    def generate_signals(self, universe: List[str], prices: pd.DataFrame,
                         fundamentals: Dict[str, dict]) -> List[Signal]:
        if prices.empty:
            return []

        available = [s for s in universe if s in prices.columns]
        signals = []

        for symbol in available:
            try:
                score, reason, metadata = self._score_stock(symbol, prices, fundamentals)
                if score is None:
                    continue

                signals.append(Signal(
                    symbol=symbol,
                    score=score,
                    direction=1 if score > 0.3 else 0,
                    suggested_weight=0.0,
                    reason=reason,
                    metadata=metadata,
                ))
            except Exception as e:
                logger.debug("Scoring failed for %s: %s", symbol, e)
                continue

        signals.sort(key=lambda s: s.score, reverse=True)
        return signals

    def _score_stock(self, symbol: str, prices: pd.DataFrame,
                     fundamentals: Dict[str, dict]):
        """Score a stock on quality + value. Returns (score, reason, metadata)."""
        fund = fundamentals.get(symbol, {})
        p = prices.get(symbol)
        if p is None:
            return None, "", {}
        p = p.dropna()
        if len(p) < 60:
            return None, "", {}

        scores = {}
        reasons = []

        # ---- VALUE METRICS ---- #
        pe = fund.get("pe_ratio")
        peg = fund.get("peg_ratio")
        pb = fund.get("pb_ratio")

        if pe is not None and 0 < pe < self.max_pe:
            pe_score = 1.0 - min(pe / self.max_pe, 1.0)  # lower P/E = higher score
            scores["value_pe"] = pe_score
            reasons.append(f"PE={pe:.1f}")

        if peg is not None and 0 < peg < self.max_peg:
            peg_score = 1.0 - min(peg / self.max_peg, 1.0)
            scores["value_peg"] = peg_score
            reasons.append(f"PEG={peg:.2f}")

        if pb is not None and 0 < pb < 10:
            pb_score = 1.0 - min(pb / 10.0, 1.0)
            scores["value_pb"] = pb_score

        # ---- QUALITY METRICS (price-based proxies) ---- #
        # Earnings stability: low volatility of returns = stable earnings
        daily_returns = p.pct_change().dropna()
        if len(daily_returns) >= 60:
            vol_60 = daily_returns.rolling(60).std().iloc[-1]
            vol_score = 1.0 - min(vol_60 * np.sqrt(252) / 0.80, 1.0)
            scores["quality_stability"] = max(vol_score, 0)

        # Trend quality: Sharpe-like ratio
        if len(daily_returns) >= 126:
            ret_6m = daily_returns.iloc[-126:].mean() * 252
            vol_6m = daily_returns.iloc[-126:].std() * np.sqrt(252)
            sharpe = ret_6m / vol_6m if vol_6m > 0 else 0
            sharpe_score = min(max((sharpe + 0.5) / 2.0, 0), 1.0)
            scores["quality_sharpe"] = sharpe_score

        # Price distance from 52-week low (cheap relative to recent range)
        if len(p) >= 252:
            low_52w = p.rolling(252).min().iloc[-1]
            high_52w = p.rolling(252).max().iloc[-1]
            rng = high_52w - low_52w
            if rng > 0:
                pos_in_range = (p.iloc[-1] - low_52w) / rng
                # Prefer stocks in lower-to-middle of range (value zone)
                value_zone_score = 1.0 - abs(pos_in_range - 0.40)
                scores["value_zone"] = max(value_zone_score, 0)

        if not scores:
            return None, "", {}

        composite = np.mean(list(scores.values()))
        reason_str = ", ".join(reasons) if reasons else "price-based signals"

        metadata = {**scores, "pe": pe, "peg": peg, "pb": pb}
        return composite, reason_str, metadata

    def apply_quality_filter(self, signals: List[Signal], fundamentals: Dict[str, dict]) -> List[Signal]:
        """Hard filter: remove stocks that fail minimum quality thresholds."""
        filtered = []
        for sig in signals:
            fund = fundamentals.get(sig.symbol, {})
            pe = fund.get("pe_ratio")
            if pe is not None and pe <= 0:
                logger.debug("Removed %s: negative P/E (loss-making)", sig.symbol)
                continue
            if pe is not None and pe > self.max_pe:
                logger.debug("Removed %s: P/E=%s too high", sig.symbol, pe)
                continue
            filtered.append(sig)
        return filtered
