"""
Risk Manager
============
Monitors portfolio risk and enforces:
  1. Position-level stop losses (hard & trailing)
  2. Post-stop-out cooldown (blocks the rebalance from buying the name back)
  3. Portfolio drawdown circuit breaker
  4. Volatility targeting (scale down in high-vol regimes)
  5. Concentration limits (position & sector)

All mutating state is persisted through StateStore. Previously these were plain
dicts, so a restart wiped every stop-loss baseline and reset the drawdown
breaker — on a launchd-managed service that happened routinely.
"""

import logging
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from config.settings import RISK_CONFIG, STATE_DIR
from core.state_store import StateStore

logger = logging.getLogger(__name__)


class RiskManager:
    """Enforces risk rules at both position and portfolio level."""

    def __init__(self, config: dict = None, state_store: StateStore = None):
        cfg = config or RISK_CONFIG
        self.stop_loss_pct = cfg.get("position_stop_loss", -0.15)
        self.trailing_stop_pct = cfg.get("trailing_stop_pct", 0.12)
        self.max_drawdown = cfg.get("max_portfolio_drawdown", -0.20)
        self.drawdown_reduce_to = cfg.get("drawdown_reduce_to", 0.50)
        self.target_vol = cfg.get("target_volatility", 0.18)
        self.vol_lookback = cfg.get("vol_lookback_days", 60)
        self.cooldown_days = int(cfg.get("stop_cooldown_days", 10))

        self.store = state_store or StateStore(f"{STATE_DIR}/risk_state.json")
        self._entry_prices: Dict[str, float] = {}
        self._peak_prices: Dict[str, float] = {}
        self._portfolio_peak_value: float = 0.0
        self._cooldowns: Dict[str, str] = {}
        self._load()

    # ------------------------------------------------------------------ #
    #  Persistence
    # ------------------------------------------------------------------ #

    def _load(self) -> None:
        doc = self.store.load()
        self._entry_prices = {
            k: float(v) for k, v in (doc.get("entry_prices") or {}).items()
            if _is_number(v)
        }
        self._peak_prices = {
            k: float(v) for k, v in (doc.get("peak_prices") or {}).items()
            if _is_number(v)
        }
        self._cooldowns = dict(doc.get("cooldowns") or {})
        peak = doc.get("portfolio_peak", 0.0)
        self._portfolio_peak_value = float(peak) if _is_number(peak) else 0.0
        if self._entry_prices or self._peak_prices or self._cooldowns:
            logger.info(
                "Risk state restored: %d entries, %d peaks, %d cooldowns, peak=$%.2f",
                len(self._entry_prices), len(self._peak_prices),
                len(self._cooldowns), self._portfolio_peak_value,
            )

    def _save(self) -> None:
        self.store.save({
            "entry_prices": self._entry_prices,
            "peak_prices": self._peak_prices,
            "portfolio_peak": self._portfolio_peak_value,
            "cooldowns": self._cooldowns,
            "updated_at": datetime.now().astimezone().isoformat(),
        })

    # ------------------------------------------------------------------ #
    #  Entry / peak tracking
    # ------------------------------------------------------------------ #

    def record_entry(self, symbol: str, price: float):
        """Register a new position. Existing baselines are never overwritten."""
        if not _is_number(price) or price <= 0:
            return
        if symbol not in self._entry_prices:
            self._entry_prices[symbol] = float(price)
            logger.info("Entry recorded: %s @ $%.2f", symbol, price)
        if symbol not in self._peak_prices or price > self._peak_prices[symbol]:
            self._peak_prices[symbol] = float(price)
        self._cooldowns.pop(symbol, None)
        self._save()

    def update_peak(self, symbol: str, current_price: float):
        if not _is_number(current_price):
            return
        if symbol not in self._peak_prices or current_price > self._peak_prices[symbol]:
            self._peak_prices[symbol] = float(current_price)
            self._save()

    def seed_peaks(self, histories: Dict[str, pd.Series]) -> int:
        """
        Initialise trailing-stop peaks from recent highs.

        Without this, a position first observed today starts its peak at today's
        price, so a name that already fell 20% from its high never trips the
        trailing stop.
        """
        seeded = 0
        for symbol, series in (histories or {}).items():
            if series is None or len(series.dropna()) == 0:
                continue
            high = float(series.dropna().max())
            if high > 0 and high > self._peak_prices.get(symbol, 0.0):
                self._peak_prices[symbol] = high
                seeded += 1
        if seeded:
            self._save()
        return seeded

    def remove_position(self, symbol: str):
        changed = False
        for book in (self._entry_prices, self._peak_prices):
            if symbol in book:
                book.pop(symbol)
                changed = True
        if changed:
            self._save()

    # ------------------------------------------------------------------ #
    #  Stop-out cooldown
    # ------------------------------------------------------------------ #

    def record_exit(self, symbol: str, price: Optional[float] = None,
                    reason: str = "stop") -> None:
        """Mark a forced exit and start the re-entry cooldown clock."""
        self._cooldowns[symbol] = date.today().isoformat()
        self._entry_prices.pop(symbol, None)
        self._peak_prices.pop(symbol, None)
        self._save()
        logger.warning(
            "Stop-out recorded: %s @ %s (%s) — no re-entry for %d days",
            symbol, f"${price:.2f}" if _is_number(price) else "n/a", reason, self.cooldown_days,
        )

    def cooldown_days_left(self, symbol: str) -> int:
        started = self._cooldowns.get(symbol)
        if not started:
            return 0
        try:
            started_date = date.fromisoformat(started)
        except ValueError:
            return 0
        elapsed = (date.today() - started_date).days
        remaining = self.cooldown_days - elapsed
        if remaining <= 0:
            self._cooldowns.pop(symbol, None)
            self._save()
            logger.info("Cooldown expired for %s", symbol)
            return 0
        return remaining

    def cooldown_symbols(self) -> set:
        return {sym for sym in list(self._cooldowns) if self.cooldown_days_left(sym) > 0}

    def cooldown_report(self) -> Dict[str, int]:
        return {sym: self.cooldown_days_left(sym) for sym in list(self._cooldowns)}

    # ------------------------------------------------------------------ #
    #  Stop Loss Checks
    # ------------------------------------------------------------------ #

    def check_stop_losses(self, positions: Dict[str, dict],
                          current_prices: Dict[str, float]) -> List[dict]:
        """
        Check all positions for stop triggers.

        Hard stops reference the broker's average cost (authoritative, survives
        restarts) and fall back to the locally tracked entry price.
        """
        to_close = []

        for symbol, pos in positions.items():
            price = current_prices.get(symbol)
            if not _is_number(price) or price <= 0:
                continue

            self.update_peak(symbol, price)

            broker_cost = pos.get("avg_cost") or 0
            entry = broker_cost if _is_number(broker_cost) and broker_cost > 0 \
                else self._entry_prices.get(symbol, price)
            peak = max(self._peak_prices.get(symbol, price), entry, price)

            loss_from_entry = (price - entry) / entry if entry > 0 else 0.0
            if loss_from_entry <= self.stop_loss_pct:
                to_close.append({
                    "symbol": symbol,
                    "reason": f"Hard stop: {loss_from_entry:.1%} from entry ${entry:.2f}",
                    "current_price": price,
                    "entry_price": entry,
                    "loss_pct": loss_from_entry,
                    "quantity": int(pos.get("quantity", 0)),
                })
                logger.warning("STOP LOSS triggered: %s @ $%.2f (%.1f%% from entry)",
                               symbol, price, loss_from_entry * 100)
                continue

            loss_from_peak = (price - peak) / peak if peak > 0 else 0.0
            if loss_from_peak <= -self.trailing_stop_pct:
                to_close.append({
                    "symbol": symbol,
                    "reason": f"Trailing stop: {loss_from_peak:.1%} from peak ${peak:.2f}",
                    "current_price": price,
                    "entry_price": entry,
                    "loss_pct": loss_from_peak,
                    "quantity": int(pos.get("quantity", 0)),
                })
                logger.warning("TRAILING STOP triggered: %s @ $%.2f (%.1f%% from peak $%.2f)",
                               symbol, price, loss_from_peak * 100, peak)

        return to_close

    # ------------------------------------------------------------------ #
    #  Portfolio Drawdown Circuit Breaker
    # ------------------------------------------------------------------ #

    def check_portfolio_drawdown(self, current_value: float) -> Tuple[bool, float]:
        """Returns (should_reduce_exposure, target_exposure_pct). Peak is persisted."""
        if not _is_number(current_value) or current_value <= 0:
            return False, 1.0

        if current_value > self._portfolio_peak_value:
            self._portfolio_peak_value = float(current_value)
            self._save()

        if self._portfolio_peak_value <= 0:
            return False, 1.0

        drawdown = (current_value - self._portfolio_peak_value) / self._portfolio_peak_value

        if drawdown <= self.max_drawdown:
            logger.warning(
                "PORTFOLIO DRAWDOWN CIRCUIT BREAKER: %.1f%% drawdown (limit %.1f%%)",
                drawdown * 100, self.max_drawdown * 100,
            )
            return True, self.drawdown_reduce_to

        if drawdown <= self.max_drawdown * 0.75:
            logger.info("Portfolio drawdown warning: %.1f%%", drawdown * 100)

        return False, 1.0

    # ------------------------------------------------------------------ #
    #  Volatility Targeting
    # ------------------------------------------------------------------ #

    def compute_volatility_scalar(self, returns: pd.DataFrame) -> float:
        if returns is None or returns.empty or len(returns) < 20:
            return 1.0
        mean_vol = returns.std().mean() * np.sqrt(252)
        if not _is_number(mean_vol) or mean_vol <= 0:
            return 1.0
        scalar = self.target_vol / mean_vol
        return max(0.3, min(scalar, 1.5))

    def apply_vol_targeting(self, target_weights: Dict[str, float],
                            returns: pd.DataFrame) -> Dict[str, float]:
        scalar = self.compute_volatility_scalar(returns)
        if abs(scalar - 1.0) < 0.05:
            return target_weights
        scaled = {s: w * scalar for s, w in target_weights.items()}
        total = sum(scaled.values())
        if total > 1.0:
            scaled = {s: w / total for s, w in scaled.items()}
        logger.info("Vol targeting: scalar=%.2f, exposure=%.1f%%",
                    scalar, sum(scaled.values()) * 100)
        return scaled

    # ------------------------------------------------------------------ #
    #  Position Size Validation
    # ------------------------------------------------------------------ #

    def validate_weights(self, target_weights: Dict[str, float],
                         sector_map: Dict[str, str] = None,
                         max_position: float = 0.12,
                         max_sector: float = 0.35) -> Dict[str, float]:
        """
        Clamp weights to position and sector limits.

        `sector_map` must be provided. Without it every symbol lands in the same
        bucket and `max_sector` becomes a portfolio-wide cap, which will silently
        liquidate most of the book.
        """
        if not sector_map:
            raise ValueError(
                "validate_weights() requires a sector_map; without it the sector "
                "cap degenerates into a whole-portfolio cap."
            )

        validated: Dict[str, float] = {}
        sector_totals: Dict[str, float] = {}

        for symbol, weight in sorted(target_weights.items(), key=lambda x: x[1], reverse=True):
            weight = min(weight, max_position)
            sector = sector_map.get(symbol, "Unknown")
            used = sector_totals.get(sector, 0.0)
            if used + weight > max_sector:
                weight = max(max_sector - used, 0.0)
            if weight > 0.005:
                validated[symbol] = weight
                sector_totals[sector] = used + weight

        return validated

    # ------------------------------------------------------------------ #
    #  Risk Report
    # ------------------------------------------------------------------ #

    def generate_risk_report(self, positions: Dict[str, dict],
                             current_prices: Dict[str, float],
                             portfolio_value: float) -> dict:
        report = {
            "timestamp": datetime.now().isoformat(),
            "portfolio_value": portfolio_value,
            "portfolio_peak": self._portfolio_peak_value,
            "portfolio_drawdown_pct": 0.0,
            "positions": [],
            "alerts": [],
            "cooldowns": self.cooldown_report(),
        }

        if self._portfolio_peak_value > 0:
            report["portfolio_drawdown_pct"] = (
                portfolio_value - self._portfolio_peak_value
            ) / self._portfolio_peak_value

        for symbol, pos in positions.items():
            price = current_prices.get(symbol, 0)
            broker_cost = pos.get("avg_cost") or 0
            entry = broker_cost if _is_number(broker_cost) and broker_cost > 0 \
                else self._entry_prices.get(symbol, price)
            peak = max(self._peak_prices.get(symbol, price), entry)

            pnl_pct = (price - entry) / entry if entry > 0 else 0
            from_peak = (price - peak) / peak if peak > 0 else 0

            report["positions"].append({
                "symbol": symbol,
                "quantity": pos.get("quantity", 0),
                "current_price": price,
                "entry_price": entry,
                "pnl_pct": pnl_pct,
                "from_peak_pct": from_peak,
                "market_value": pos.get("market_value", 0),
            })

            if pnl_pct <= self.stop_loss_pct * 0.80:
                report["alerts"].append(
                    f"WARNING: {symbol} approaching stop loss ({pnl_pct:.1%})")
            if from_peak <= -self.trailing_stop_pct * 0.80:
                report["alerts"].append(
                    f"WARNING: {symbol} approaching trailing stop ({from_peak:.1%} from peak)")

        return report


def _is_number(value) -> bool:
    try:
        if value is None:
            return False
        float(value)
        return True
    except (TypeError, ValueError):
        return False
