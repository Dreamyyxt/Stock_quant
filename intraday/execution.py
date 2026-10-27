"""
Execution adapters for the intraday engine
==========================================
Two implementations behind one interface, so the same strategy code runs in
replay, in shadow mode, and against the broker:

  SimExecution     instant fill at the reference price ± slippage, local ledger
  BrokerExecution  real orders through OrderManager, with fill confirmation

Keeping this behind an interface is what makes "validate on historical bars,
then run the identical logic on the paper account" an actual guarantee instead
of a hope.
"""

import json
import logging
import os
from abc import ABC, abstractmethod
from typing import Dict, Optional

from config.settings import INTRADAY_CONFIG, STATE_DIR

logger = logging.getLogger(__name__)

COST_MODEL_PATH = os.path.join(STATE_DIR, "cost_model.json")
_cost_model_cache: Optional[dict] = None


def load_cost_model() -> dict:
    """Per-symbol measured spread cost, produced by scripts/measure_spread.py."""
    global _cost_model_cache
    if _cost_model_cache is not None:
        return _cost_model_cache
    try:
        with open(COST_MODEL_PATH, "r", encoding="utf-8") as handle:
            _cost_model_cache = json.load(handle)
    except Exception:
        _cost_model_cache = {}
    return _cost_model_cache


def slippage_for(symbol: str, urgency: str = "normal", config: dict = None) -> float:
    """Cost of crossing the spread once, as a fraction of price.

    Prefers the measured per-symbol spread; falls back to config. Using one flat
    number across symbols is what made the old replay meaningless: AMD's median
    spread is 6x AAPL's, so a flat cost either flatters AMD or buries AAPL.

    Two extra knobs exist so a replay can be costed *identically* to the
    standalone backtest it is supposed to verify:
      cost_override_half_spread_bp  {"TSLA": 1.51} — pin an exact figure
      slippage_extra_bp             0.5           — on top of the measured half
                                                    spread, for impact
    """
    cfg = config or INTRADAY_CONFIG
    percentile = str(cfg.get("cost_model_percentile", "p95")).lower()
    key = "half_spread_p95" if percentile == "p95" else "half_spread_median"
    extra = float(cfg.get("slippage_extra_bp", 0.0) or 0.0) / 1e4

    override = (cfg.get("cost_override_half_spread_bp") or {}).get(symbol.upper())
    if override is not None:
        return float(override) / 1e4 + extra

    entry = (load_cost_model().get("symbols") or {}).get(symbol.upper())
    if entry and entry.get(key):
        return float(entry[key]) + extra

    fallback = (cfg.get("flatten_slippage_pct", 0.0006) if urgency == "flatten"
                else cfg.get("limit_slippage_pct", 0.0003))
    return float(fallback) + extra


class ExecutionAdapter(ABC):
    name = "base"

    @abstractmethod
    def equity(self, prices: Dict[str, float]) -> float: ...

    @abstractmethod
    def position_qty(self, symbol: str) -> int: ...

    @abstractmethod
    def fill(self, symbol: str, side: str, quantity: int,
             ref_price: float, urgency: str = "normal") -> dict: ...

    @abstractmethod
    def all_positions(self) -> Dict[str, int]: ...

    def cash(self) -> Optional[float]:
        return None


class SimExecution(ExecutionAdapter):
    """Local ledger with slippage. Used for replay and shadow runs."""

    name = "sim"

    def __init__(self, initial_cash: float, config: dict = None,
                 commission_pct: float = None):
        cfg = config or INTRADAY_CONFIG
        self.cfg = cfg
        self._cash = float(initial_cash)
        self._positions: Dict[str, int] = {}
        self.slippage = float(cfg.get("limit_slippage_pct", 0.0003))
        self.flatten_slippage = float(cfg.get("flatten_slippage_pct", 0.0006))
        self.commission = (float(cfg.get("commission_pct", 0.0))
                           if commission_pct is None else float(commission_pct))
        # Shorting is opt-in per strategy: a long-only strategy that sells more
        # than it holds is a bug, and silently opening a short would hide it.
        self.allow_short = bool(cfg.get("allow_short", False))

    def equity(self, prices: Dict[str, float]) -> float:
        value = self._cash
        for symbol, qty in self._positions.items():
            price = prices.get(symbol)
            if price:
                value += qty * price
        return value

    def position_qty(self, symbol: str) -> int:
        return int(self._positions.get(symbol, 0))

    def all_positions(self) -> Dict[str, int]:
        return dict(self._positions)

    def cash(self) -> float:
        return self._cash

    def fill(self, symbol: str, side: str, quantity: int,
             ref_price: float, urgency: str = "normal") -> dict:
        if quantity <= 0 or not ref_price or ref_price <= 0:
            return {"ok": False, "reason": "invalid order"}

        slip = slippage_for(symbol, urgency, self.cfg)
        if side.upper() == "BUY":
            price = ref_price * (1 + slip)
            # Covering a short reduces the negative position; opening a long
            # adds to it. Signed quantity handles both without a special case.
            if quantity > 0 and self._positions.get(symbol, 0) < 0:
                cost = price * quantity * (1 + self.commission)
                if cost > self._cash:
                    affordable = int(self._cash / (price * (1 + self.commission)))
                    if affordable <= 0:
                        return {"ok": False, "reason": "insufficient cash to cover"}
                    quantity = affordable
                    cost = price * quantity * (1 + self.commission)
                self._cash -= cost
                remaining = self._positions.get(symbol, 0) + quantity
                if remaining:
                    self._positions[symbol] = remaining
                else:
                    self._positions.pop(symbol, None)
                return {"ok": True, "fill_price": round(price, 4),
                        "filled_qty": quantity, "status": "filled"}
            cost = price * quantity * (1 + self.commission)
            if cost > self._cash:
                affordable = int(self._cash / (price * (1 + self.commission)))
                if affordable <= 0:
                    return {"ok": False, "reason": "insufficient cash"}
                quantity = affordable
                cost = price * quantity * (1 + self.commission)
            self._cash -= cost
            self._positions[symbol] = self._positions.get(symbol, 0) + quantity
        else:
            held = self._positions.get(symbol, 0)
            if quantity > held and not self.allow_short:
                quantity = held
            if quantity <= 0:
                return {"ok": False, "reason": "no position"}
            price = ref_price * (1 - slip)
            proceeds = price * quantity * (1 - self.commission)
            self._cash += proceeds
            remaining = held - quantity
            if remaining:
                self._positions[symbol] = remaining
            else:
                self._positions.pop(symbol, None)

        return {"ok": True, "fill_price": round(price, 4),
                "filled_qty": quantity, "status": "filled"}


class BrokerExecution(ExecutionAdapter):
    """Real (paper or live) orders via OrderManager."""

    name = "broker"

    def __init__(self, order_manager, client, config: dict = None):
        self.orders = order_manager
        self.client = client
        self.cfg = config or INTRADAY_CONFIG

    def equity(self, prices: Dict[str, float]) -> float:
        summary = self.client.get_account_summary()
        return float(summary.get("net_liquidation") or 0)

    def cash(self) -> Optional[float]:
        summary = self.client.get_account_summary()
        return float(summary.get("cash") or 0)

    def position_qty(self, symbol: str) -> int:
        positions = self.client.get_positions()
        return int((positions.get(symbol) or {}).get("quantity", 0))

    def all_positions(self) -> Dict[str, int]:
        positions = self.client.get_positions()
        return {s: int(p.get("quantity", 0)) for s, p in positions.items()}

    def fill(self, symbol: str, side: str, quantity: int,
             ref_price: float, urgency: str = "normal") -> dict:
        side = side.upper()
        # Marketable limit: cross the touch by roughly one half-spread. The old
        # flat 0.15% crossed far too deep for liquid names, which in a live
        # account means paying away edge on every entry.
        slippage = slippage_for(symbol, urgency, self.cfg)
        price = ref_price * (1 + slippage) if side == "BUY" else ref_price * (1 - slippage)

        submitted = (self.orders.buy(symbol, quantity, price, exact_price=True) if side == "BUY"
                     else self.orders.sell(symbol, quantity, price, exact_price=True))
        if not submitted:
            return {"ok": False, "reason": "order rejected or blocked"}

        result = self.orders.wait_for_fill(submitted["order_id"])
        status = result.get("status")
        filled = float(result.get("filled_qty") or 0)
        if status == "filled" and filled > 0:
            return {"ok": True, "fill_price": result.get("avg_price"),
                    "filled_qty": int(filled), "status": "filled",
                    "order_id": submitted["order_id"]}

        if filled > 0:
            return {"ok": True, "fill_price": result.get("avg_price"),
                    "filled_qty": int(filled), "status": "partial_fill",
                    "order_id": submitted["order_id"]}

        try:
            self.orders.cancel_order(submitted["order_id"])
        except Exception:
            pass
        return {"ok": False, "reason": f"unfilled ({status})",
                "order_id": submitted["order_id"]}
