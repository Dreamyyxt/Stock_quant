"""
Order Manager
=============
Execution layer for Tiger Securities.

What changed vs. the old version, and why:

  * A submitted order is no longer treated as a filled order. Every order is
    polled to a terminal state before the next step spends the proceeds.
    The old code slept 5 seconds, then bought $76k of AAPL against $12k of cash.
  * Spending is bounded by an explicit budget (target exposure minus what is
    already invested, floored by broker buying power). No more accidental
    margin borrowing.
  * Duplicate protection: an existing open order, or an already-submitted
    symbol/side pair today, blocks a second submission. Previously a restart or
    a manual run could double every trade.
  * Daily counters and the pending-order book are persisted, so they survive the
    restarts that launchd performs routinely.
  * Exchange resolution moved to the broker contract lookup (see TigerClient).
  * Orders outside the configured execution window are refused instead of being
    fired into the opening print.
"""

import logging
import os
import time
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional

from config.settings import EXECUTION_CONFIG, STATE_DIR, is_live, live_trading_allowed
from core.event_logger import EventLogger
from core.notifier import Notifier
from core.state_store import StateStore
from core.tiger_client import TigerClient

logger = logging.getLogger(__name__)

_DEAD_STATUSES = ("cancelled", "canceled", "rejected", "expired", "failed")
_FILLED_STATUSES = ("filled", "executed")

# US Eastern trading window is evaluated with zoneinfo when available.
try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    _ET = None


class OrderManager:
    """Executes and manages orders through the Tiger Securities API."""

    def __init__(self,
                 client: TigerClient,
                 config: dict = None,
                 state_store: StateStore = None,
                 notifier: Notifier = None,
                 price_provider: Optional[Callable[[str], Optional[float]]] = None):
        cfg = config or EXECUTION_CONFIG
        self.client = client
        self.order_type = self._normalize_order_type(cfg.get("order_type", "LMT"))
        self.slippage_pct = cfg.get("limit_slippage_pct", 0.002)
        self.timeout_minutes = cfg.get("order_timeout_minutes", 30)
        self.max_daily_trades = cfg.get("max_daily_trades", 30)

        self.require_fill_before_buy = bool(cfg.get("require_fill_before_buy", True))
        self.fill_timeout = int(cfg.get("fill_timeout_seconds", 120))
        self.fill_interval = float(cfg.get("fill_poll_interval_seconds", 3))
        self.max_retries = int(cfg.get("max_order_retries", 1))

        self.cash_buffer_pct = float(cfg.get("cash_buffer_pct", 0.02))
        self.min_order_notional = float(cfg.get("min_order_notional", 200.0))
        self.allow_proceeds_reuse = bool(cfg.get("allow_sell_proceeds_reuse", True))

        self.block_repeat_days = int(cfg.get("block_repeat_symbol_action_days", 1))
        self.enforce_window = bool(cfg.get("enforce_trading_window", True))
        self.window_start = cfg.get("trade_start_time", "09:40")
        self.window_end = cfg.get("trade_end_time", "15:45")

        self.events = EventLogger(log_dir=cfg.get("event_log_dir", "logs"))
        self.notifier = notifier or Notifier(state_dir=STATE_DIR)
        self.store = state_store or StateStore(f"{STATE_DIR}/order_state.json")
        self.price_provider = price_provider

        self._pending_orders: Dict[int, dict] = {}
        self._state = self.store.load()
        self._restore_pending()

    # ------------------------------------------------------------------ #
    #  Guard rails
    # ------------------------------------------------------------------ #

    def _assert_can_trade(self) -> None:
        if not is_live():
            return
        if not live_trading_allowed():
            raise PermissionError(
                "TRADING_ENV=live but LIVE_TRADING_CONFIRM is missing or wrong. "
                "Set LIVE_TRADING_CONFIRM to the documented token to trade real money."
            )

    def trading_window_ok(self) -> tuple:
        """Return (allowed, reason). Only enforced for real order submission."""
        if not self.enforce_window:
            return True, "window check disabled"
        if _ET is None:
            return True, "zoneinfo unavailable"

        now_et = datetime.now(_ET)
        if now_et.weekday() >= 5:
            return False, f"weekend in US Eastern ({now_et:%a %H:%M})"

        def _parse(text: str, fallback: tuple) -> tuple:
            try:
                h, m = text.split(":")[:2]
                return int(h), int(m)
            except Exception:
                return fallback

        start_h, start_m = _parse(self.window_start, (9, 40))
        end_h, end_m = _parse(self.window_end, (15, 45))
        start = now_et.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
        end = now_et.replace(hour=end_h, minute=end_m, second=0, microsecond=0)

        if now_et < start:
            return False, f"before {self.window_start} ET (now {now_et:%H:%M} ET)"
        if now_et > end:
            return False, f"after {self.window_end} ET (now {now_et:%H:%M} ET)"
        return True, "inside window"

    def _check_daily_limit(self) -> bool:
        daily = self._daily()
        if daily["count"] >= self.max_daily_trades:
            logger.error("Daily trade limit reached: %d/%d", daily["count"], self.max_daily_trades)
            self.notifier.warning(
                "Daily trade limit reached",
                f"{daily['count']}/{self.max_daily_trades} orders submitted today",
            )
            return False
        return True

    def _daily(self) -> dict:
        today = date.today().isoformat()
        daily = self._state.get("daily") or {}
        if daily.get("date") != today:
            daily = {"date": today, "count": 0, "submitted": {}}
            self._state["daily"] = daily
            self._persist()
        daily.setdefault("submitted", {})
        return daily

    def _already_submitted(self, symbol: str, action: str) -> bool:
        if self.block_repeat_days <= 0:
            return False
        key = f"{symbol.upper()}:{action.upper()}"
        stamp = (self._daily().get("submitted") or {}).get(key)
        if not stamp:
            return False
        try:
            submitted_day = date.fromisoformat(str(stamp)[:10])
        except ValueError:
            return False
        if (date.today() - submitted_day).days < self.block_repeat_days:
            logger.warning("Duplicate %s %s blocked (already submitted %s)", action, symbol, stamp)
            return True
        return False

    def _mark_submitted(self, symbol: str, action: str, order_id) -> None:
        daily = self._daily()
        daily["submitted"][f"{symbol.upper()}:{action.upper()}"] = date.today().isoformat()
        daily["count"] = int(daily.get("count", 0)) + 1
        self._state["daily"] = daily
        self._persist()

    # ------------------------------------------------------------------ #
    #  Persistence
    # ------------------------------------------------------------------ #

    def _persist(self) -> None:
        self._state["pending"] = {
            str(k): {**v, "submitted_at": v.get("submitted_at").isoformat()
                     if isinstance(v.get("submitted_at"), datetime) else v.get("submitted_at")}
            for k, v in self._pending_orders.items()
        }
        self._state["updated_at"] = datetime.now().astimezone().isoformat()
        try:
            self.store.save(self._state)
        except Exception as exc:
            logger.error("Failed to persist order state: %s", exc)

    def _restore_pending(self) -> None:
        for key, info in (self._state.get("pending") or {}).items():
            try:
                submitted = info.get("submitted_at")
                if isinstance(submitted, str):
                    info["submitted_at"] = datetime.fromisoformat(submitted)
                self._pending_orders[int(key)] = info
            except (TypeError, ValueError):
                continue
        if self._pending_orders:
            logger.info("Restored %d pending order(s) from state", len(self._pending_orders))

    # ------------------------------------------------------------------ #
    #  Core Order Placement
    # ------------------------------------------------------------------ #

    def buy(self, symbol: str, quantity: int, price: float = None,
            exact_price: bool = False) -> Optional[dict]:
        return self._submit(symbol, "BUY", quantity, price, exact_price)

    def sell(self, symbol: str, quantity: int, price: float = None,
             exact_price: bool = False) -> Optional[dict]:
        return self._submit(symbol, "SELL", quantity, price, exact_price)

    def _submit(self, symbol: str, action: str, quantity: int,
                price: float = None, exact_price: bool = False) -> Optional[dict]:
        symbol = symbol.upper()
        if quantity is None or int(quantity) <= 0:
            logger.warning("Refusing %s %s: invalid quantity %s", action, symbol, quantity)
            return None

        self._assert_can_trade()
        if not self._check_daily_limit():
            return None
        if self._already_submitted(symbol, action):
            return None
        if hasattr(self.client, "has_open_order") and self.client.has_open_order(symbol, action):
            return None

        if not price or self.order_type != "LMT":
            limit_price = None
        elif exact_price:
            limit_price = round(float(price), 2)
        else:
            limit_price = self._compute_limit_price(price, action)

        try:
            result = self.client.place_order(
                symbol=symbol,
                action=action,
                quantity=int(quantity),
                order_type=self.order_type,
                limit_price=limit_price,
            )
            order_id = getattr(result, "id", None) or result
            self._track_order(order_id, symbol, action, int(quantity), limit_price)
            self._mark_submitted(symbol, action, order_id)
            self.events.log(
                "order_submitted", order_id=order_id, symbol=symbol, action=action,
                quantity=int(quantity), limit_price=limit_price, order_type=self.order_type,
            )
            return {
                "order_id": order_id,
                "symbol": symbol,
                "action": action,
                "quantity": int(quantity),
                "limit_price": limit_price,
                "status": "submitted",
            }
        except Exception as exc:
            logger.error("%s order failed for %s: %s", action, symbol, exc)
            self.events.log(
                "order_failed", symbol=symbol, action=action,
                quantity=int(quantity), limit_price=limit_price, reason=str(exc),
            )
            self.notifier.warning(
                f"{action} order rejected — {symbol}",
                f"{quantity} shares @ {limit_price}: {exc}",
                symbol=symbol, action=action,
            )
            return None

    # ------------------------------------------------------------------ #
    #  Fill confirmation
    # ------------------------------------------------------------------ #

    def wait_for_fill(self, order_id: int, timeout: Optional[int] = None) -> dict:
        """Poll until the order reaches a terminal state or the timeout expires."""
        timeout = self.fill_timeout if timeout is None else timeout
        deadline = time.time() + timeout
        last = {"status": "unknown", "filled_qty": 0.0, "avg_price": None, "raw_status": ""}

        while time.time() < deadline:
            snapshot = self._order_snapshot(order_id)
            if snapshot:
                last = snapshot
                if snapshot["status"] in ("filled",) or snapshot["status"] in _DEAD_STATUSES:
                    return snapshot
            time.sleep(self.fill_interval)

        logger.warning("Order %s not filled within %ss (last status=%s)",
                       order_id, timeout, last.get("raw_status"))
        return last

    def _order_snapshot(self, order_id: int) -> Optional[dict]:
        order = self.client.get_order_by_id(order_id)
        if order is None:
            return None
        raw_status = str(getattr(order, "status", "") or "")
        status = _normalize_status(raw_status)
        filled = getattr(order, "filled", None) or getattr(order, "filled_quantity", None) or 0
        avg_price = (getattr(order, "avg_fill_price", None)
                     or getattr(order, "average_fill_price", None)
                     or getattr(order, "limit_price", None))
        try:
            filled_qty = float(filled or 0)
        except (TypeError, ValueError):
            filled_qty = 0.0
        try:
            avg = float(avg_price) if avg_price is not None else None
        except (TypeError, ValueError):
            avg = None
        return {"status": status, "filled_qty": filled_qty, "avg_price": avg,
                "raw_status": raw_status}

    def _cancel_quietly(self, order_id: int) -> None:
        try:
            self.client.cancel_order(order_id)
        except Exception as exc:
            logger.warning("Cancel failed for order %s: %s", order_id, exc)

    # ------------------------------------------------------------------ #
    #  Batch execution
    # ------------------------------------------------------------------ #

    def execute_trade_list(self, trades: List[dict], prices: Dict[str, float],
                           dry_run: bool = False,
                           budget: Optional[float] = None,
                           net_liquidation: Optional[float] = None) -> List[dict]:
        """
        Execute sells first, confirm fills, then buy within the remaining budget.

        trades: [{symbol, action, quantity, price, reason}]
        budget: hard cap on BUY notional for this batch. When omitted it is
                derived from the broker account minus a safety buffer.
        """
        sells = [t for t in trades if str(t.get("action")).upper() == "SELL"]
        buys = [t for t in trades if str(t.get("action")).upper() == "BUY"]

        if dry_run:
            return [{**t, "status": "dry_run", "order_id": None} for t in trades]

        if not trades:
            return []

        self._assert_can_trade()
        allowed, reason = self.trading_window_ok()
        if not allowed:
            logger.error("Execution refused: %s", reason)
            self.notifier.warning("Rebalance skipped", f"outside execution window: {reason}")
            self.events.log("execution_blocked", reason=reason, trades_count=len(trades))
            return [{**t, "status": "blocked", "reason": reason, "order_id": None} for t in trades]

        self.events.log(
            "execution_batch_started", trades_count=len(trades),
            sells_count=len(sells), buys_count=len(buys),
            budget=budget, net_liquidation=net_liquidation,
        )

        results: List[dict] = []
        sell_proceeds = 0.0

        # ---------------- Sells first ---------------- #
        for trade in sells:
            symbol = trade["symbol"].upper()
            qty = int(trade["quantity"])
            ref_price = prices.get(symbol, trade.get("price"))
            res = self._submit(symbol, "SELL", qty, ref_price)
            entry = {
                "symbol": symbol, "action": "SELL", "quantity": qty,
                "ref_price": ref_price, "order_id": None,
                "status": "failed", "filled_qty": 0.0, "fill_price": None,
                "reason": trade.get("reason", ""),
            }
            if res:
                entry.update(order_id=res["order_id"], status="submitted",
                             limit_price=res["limit_price"])
                fill = self.wait_for_fill(res["order_id"])
                entry["status"] = fill["status"]
                entry["filled_qty"] = fill["filled_qty"]
                entry["fill_price"] = fill["avg_price"]
                if fill["status"] in _DEAD_STATUSES and self.max_retries > 0:
                    fill = self._retry_order(res["order_id"], symbol, "SELL",
                                             qty, ref_price)
                    entry.update(status=fill["status"], filled_qty=fill["filled_qty"],
                                 fill_price=fill["avg_price"])
                if entry["filled_qty"] > 0 and entry["fill_price"]:
                    sell_proceeds += entry["filled_qty"] * entry["fill_price"]
                    self._pending_orders.pop(res["order_id"], None)
            results.append(entry)
            self.events.log("order_result", **{k: v for k, v in entry.items() if k != "reason"})

        # ---------------- Budget ---------------- #
        if budget is None:
            budget = self._derive_budget(sell_proceeds, net_liquidation)
        remaining = max(0.0, float(budget)) * (1 - self.cash_buffer_pct)
        logger.info("Buy budget: $%.2f (sells filled $%.2f)", remaining, sell_proceeds)

        # ---------------- Buys ---------------- #
        for trade in buys:
            symbol = trade["symbol"].upper()
            qty = int(trade["quantity"])
            ref_price = prices.get(symbol, trade.get("price"))
            entry = {
                "symbol": symbol, "action": "BUY", "quantity": qty,
                "ref_price": ref_price, "order_id": None,
                "status": "skipped", "filled_qty": 0.0, "fill_price": None,
                "reason": trade.get("reason", ""),
            }

            if not ref_price or ref_price <= 0:
                entry["reason"] = "no reference price"
                results.append(entry)
                continue

            affordable = int(remaining / ref_price)
            if affordable <= 0:
                entry["reason"] = f"budget exhausted (remaining ${remaining:,.0f})"
                logger.warning("Skipping BUY %s: %s", symbol, entry["reason"])
                self.notifier.warning("Buy skipped — no cash", f"{symbol}: {entry['reason']}")
                results.append(entry)
                continue
            if affordable < qty:
                logger.warning("Downsizing BUY %s from %d to %d shares (budget)",
                               symbol, qty, affordable)
                qty = affordable
                entry["quantity"] = qty

            if qty * ref_price < self.min_order_notional:
                entry["reason"] = f"notional below minimum (${qty * ref_price:,.0f})"
                results.append(entry)
                continue

            res = self._submit(symbol, "BUY", qty, ref_price)
            if res:
                entry.update(order_id=res["order_id"], status="submitted",
                             limit_price=res["limit_price"])
                fill = self.wait_for_fill(res["order_id"])
                entry["status"] = fill["status"]
                entry["filled_qty"] = fill["filled_qty"]
                entry["fill_price"] = fill["avg_price"]
                if fill["status"] in _DEAD_STATUSES and self.max_retries > 0:
                    fill = self._retry_order(res["order_id"], symbol, "BUY", qty, ref_price)
                    entry.update(status=fill["status"], filled_qty=fill["filled_qty"],
                                 fill_price=fill["avg_price"])
                if entry["filled_qty"] > 0 and entry["fill_price"]:
                    remaining -= entry["filled_qty"] * entry["fill_price"]
                    self._pending_orders.pop(res["order_id"], None)
            results.append(entry)
            self.events.log("order_result", **{k: v for k, v in entry.items() if k != "reason"})

        submitted = sum(1 for r in results if r.get("status") == "submitted")
        filled = sum(1 for r in results if r.get("status") == "filled")
        failed = sum(1 for r in results if r.get("status") in ("failed", *DEAD_STATUSES))
        self.events.log(
            "execution_batch_finished", results_count=len(results),
            submitted_count=submitted, filled_count=filled, failed_count=failed,
        )
        if failed or any(r.get("status") in ("partial_fill", "skipped") for r in results):
            self.notifier.warning(
                "Execution incomplete",
                f"filled={filled} submitted={submitted} failed={failed} "
                f"of {len(results)} orders",
            )
        return results

    def _derive_budget(self, sell_proceeds: float,
                       net_liquidation: Optional[float]) -> float:
        """Fallback budget when the caller does not supply one."""
        acct = {}
        try:
            acct = self.client.get_account_summary()
        except Exception as exc:
            logger.warning("Could not read account for budget: %s", exc)

        cash = float(acct.get("cash") or 0)
        power = float(acct.get("buying_power") or 0)
        usable = min(cash, power) if (cash > 0 and power > 0) else max(cash, power)
        if self.allow_proceeds_reuse:
            usable += max(0.0, sell_proceeds)
        if net_liquidation and net_liquidation > 0:
            usable = min(usable, net_liquidation * 0.98)
        logger.info("Derived buy budget: cash=%.2f power=%.2f proceeds=%.2f → %.2f",
                    cash, power, sell_proceeds, usable)
        return usable

    def _retry_order(self, order_id: int, symbol: str, action: str,
                     quantity: int, ref_price: float) -> dict:
        """Cancel a dead/unfilled order and re-place once at a refreshed price."""
        self._cancel_quietly(order_id)
        self._pending_orders.pop(order_id, None)

        fresh = None
        if self.price_provider:
            try:
                fresh = self.price_provider(symbol)
            except Exception as exc:
                logger.warning("Refresh price failed for %s: %s", symbol, exc)
        price = fresh or ref_price

        logger.info("Retrying %s %s x%d @ %.2f (previous order %s)",
                    action, symbol, quantity, price or 0, order_id)
        snapshot = self._order_snapshot(order_id) or {}
        already = float(snapshot.get("filled_qty") or 0)
        remaining_qty = max(0, int(quantity - already))
        if remaining_qty <= 0:
            return {"status": "filled", "filled_qty": already,
                    "avg_price": snapshot.get("avg_price"), "raw_status": "filled"}

        res = self._submit(symbol, action, remaining_qty, price)
        if not res:
            return {"status": "failed", "filled_qty": already,
                    "avg_price": None, "raw_status": "retry_failed"}
        fill = self.wait_for_fill(res["order_id"])
        fill["filled_qty"] = float(fill.get("filled_qty") or 0) + already
        return fill

    # ------------------------------------------------------------------ #
    #  Order housekeeping
    # ------------------------------------------------------------------ #

    def _track_order(self, order_id, symbol: str, action: str,
                     quantity: int, price: float):
        if order_id is None:
            return
        self._pending_orders[int(order_id)] = {
            "order_id": int(order_id),
            "symbol": symbol,
            "action": action,
            "quantity": int(quantity),
            "price": price,
            "submitted_at": datetime.now(),
        }
        self._persist()

    def cancel_timed_out_orders(self) -> List[dict]:
        now = datetime.now()
        cancelled = []
        for order_id, info in list(self._pending_orders.items()):
            submitted_at = info.get("submitted_at")
            if not isinstance(submitted_at, datetime):
                continue
            age_minutes = (now - submitted_at).total_seconds() / 60
            if age_minutes <= self.timeout_minutes:
                continue
            try:
                self.client.cancel_order(order_id)
                self._pending_orders.pop(order_id, None)
                logger.info("Cancelled timed-out order %s (%s %s x%d, %.0f min old)",
                            order_id, info["action"], info["symbol"],
                            info["quantity"], age_minutes)
                self.events.log(
                    "order_cancelled", order_id=order_id, symbol=info["symbol"],
                    action=info["action"], quantity=info["quantity"], reason="timeout",
                )
                cancelled.append({"order_id": order_id, **info, "reason": "timeout"})
            except Exception as exc:
                logger.error("Failed to cancel order %s: %s", order_id, exc)
                self.events.log("order_cancel_failed", order_id=order_id, reason=str(exc))
        self._persist()
        return cancelled

    def get_open_orders(self) -> List[dict]:
        return list(self._pending_orders.values())

    def sync_order_statuses(self) -> List[dict]:
        """Fetch latest broker statuses and emit structured change events."""
        previous = self._state.get("order_snapshots") or {}
        current: Dict[str, dict] = {}
        emitted: List[dict] = []

        try:
            orders = self.client.get_orders()
        except Exception as exc:
            logger.error("Failed to sync order statuses: %s", exc)
            self.events.log("order_sync_failed", reason=str(exc))
            return emitted

        for order in orders or []:
            snap = self._normalize_order_snapshot(order)
            if not snap.get("order_id"):
                continue
            key = str(snap["order_id"])
            current[key] = snap
            prev = previous.get(key, {})
            if (prev.get("status") != snap.get("status")
                    or prev.get("filled") != snap.get("filled")
                    or prev.get("avg_fill_price") != snap.get("avg_fill_price")):
                emitted.append(self.events.log("order_status_changed", **snap))
                if snap.get("status_normalized") in ("filled", *[s.lower() for s in _DEAD_STATUSES]):
                    self._pending_orders.pop(snap["order_id"], None)

        self._state["order_snapshots"] = current
        self._persist()
        return emitted

    # ------------------------------------------------------------------ #
    #  Helpers
    # ------------------------------------------------------------------ #

    def _compute_limit_price(self, market_price: float, action: str) -> float:
        if action.upper() == "BUY":
            return round(market_price * (1 + self.slippage_pct), 2)
        return round(market_price * (1 - self.slippage_pct), 2)

    def _normalize_order_type(self, order_type: str) -> str:
        normalized = (order_type or "LMT").upper()
        if normalized in {"LMT", "LIMIT"}:
            return "LMT"
        if normalized in {"MKT", "MARKET"}:
            return "MKT"
        logger.warning("Unknown order type '%s', defaulting to LMT", order_type)
        return "LMT"

    def _normalize_order_snapshot(self, order) -> dict:
        symbol = ""
        contract = getattr(order, "contract", None)
        if contract is not None:
            symbol = getattr(contract, "symbol", "") or ""
        symbol = (symbol or getattr(order, "symbol", "") or "").upper()

        raw_status = (
            getattr(order, "status", None)
            or getattr(order, "order_status", None)
            or getattr(order, "state", None)
            or ""
        )
        status_text = str(raw_status)
        status_normalized = _normalize_status(status_text)

        order_id = getattr(order, "id", None) or getattr(order, "order_id", None)
        quantity = getattr(order, "quantity", None) or getattr(order, "total_quantity", None)
        filled = getattr(order, "filled", None) or getattr(order, "filled_quantity", None) or 0
        avg_fill_price = (getattr(order, "avg_fill_price", None)
                          or getattr(order, "average_fill_price", None))
        limit_price = getattr(order, "limit_price", None)
        action = getattr(order, "action", None) or getattr(order, "side", None)

        return {
            "order_id": int(order_id) if order_id is not None else None,
            "symbol": symbol,
            "action": str(action) if action is not None else "",
            "quantity": int(quantity) if quantity is not None else None,
            "filled": float(filled) if filled is not None else 0,
            "avg_fill_price": float(avg_fill_price) if avg_fill_price is not None else None,
            "limit_price": float(limit_price) if limit_price is not None else None,
            "status": status_text,
            "status_normalized": status_normalized,
        }


def _normalize_status(raw: str) -> str:
    text = str(raw or "").lower()
    if not text:
        return "unknown"
    if "partial" in text:
        return "partial_fill"
    if any(word in text for word in _FILLED_STATUSES):
        return "filled"
    for word in _DEAD_STATUSES:
        if word in text:
            return "cancelled" if word in ("cancelled", "canceled") else word
    if "submit" in text or "new" in text or "held" in text or "pending" in text:
        return "submitted"
    return text
