"""
Intraday trading engine
=======================
One session, one loop:

    read bars → score signals → manage open risk → open new risk → flatten

Design constraints that shaped this:

  * **Day trades only.** Everything is flat by `flatten_time`; nothing is held
    overnight. Overnight gap risk is not modelled, so it must not exist.
  * **The exit path is never blocked.** Entries are gated by windows, cooldowns,
    exposure and loss limits. Exits are only gated by the order actually filling.
  * **State survives a restart.** Open positions are persisted; a restart mid
    session finds them, and a restart on a new day force-flattens them instead
    of silently pretending the account is clean.
  * **Same code for replay and for the broker.** Only the ExecutionAdapter
    differs, so a replay result is evidence about this engine, not about a
    different one.
"""

import json
import logging
import os
import time as time_module
from dataclasses import asdict, dataclass, field
from datetime import datetime, time
from typing import Dict, List, Optional

from config.settings import INTRADAY_CONFIG, STATE_DIR
from core.event_logger import EventLogger
from core.notifier import Notifier
from core.state_store import StateStore
from intraday.execution import BrokerExecution, ExecutionAdapter, SimExecution
from intraday.signals import IntradaySignal, make_signals

logger = logging.getLogger(__name__)


@dataclass
class Position:
    symbol: str
    quantity: int
    entry_price: float
    entry_ts: str
    stop_price: float
    target_price: float
    reason: str = ""
    # +1 long, -1 short. Defaults to long so state files written by the
    # long-only version of the engine still load.
    side: int = 1

    def as_dict(self) -> dict:
        return asdict(self)


class IntradayEngine:
    def __init__(self,
                 provider,
                 execution: ExecutionAdapter,
                 config: dict = None,
                 notifier: Notifier = None,
                 dry_run: bool = False):
        self.cfg = config or INTRADAY_CONFIG
        self.provider = provider
        self.exec = execution
        # Desktop pop-ups are for a human watching a real session. A 120-session
        # replay would otherwise fire one notification per session per alert.
        desktop_ok = (getattr(execution, "name", "") == "broker" and not dry_run)
        self.notifier = notifier or Notifier(state_dir=STATE_DIR, desktop=desktop_ok)
        self.dry_run = dry_run

        self.signals = make_signals(self.cfg)
        self.journal = EventLogger(
            log_dir=str(self.cfg.get("journal_dir", "logs")),
            filename=str(self.cfg.get("journal_filename", "intraday_trades.jsonl")),
        )
        self.summary_dir = str(self.cfg.get("summary_dir", "logs"))
        self.store = StateStore(f"{STATE_DIR}/intraday_state.json")

        self.max_positions = int(self.cfg.get("max_positions", 3))
        self.position_pct = float(self.cfg.get("position_pct", 0.12))
        self.max_gross = float(self.cfg.get("max_gross_exposure", 0.45))
        self.min_notional = float(self.cfg.get("min_order_notional", 200.0))
        self.allow_short = bool(self.cfg.get("allow_short", False))

        self.stop_loss_pct = float(self.cfg.get("stop_loss_pct", 0.008))
        self.take_profit_pct = float(self.cfg.get("take_profit_pct", 0.018))
        self.max_hold_minutes = int(self.cfg.get("max_hold_minutes", 90))
        self.min_hold_minutes = int(self.cfg.get("min_hold_minutes", 5))
        self.cooldown_minutes = int(self.cfg.get("symbol_cooldown_minutes", 30))

        self.max_daily_loss_pct = float(self.cfg.get("max_daily_loss_pct", 0.015))
        self.max_trades = int(self.cfg.get("max_trades_per_day", 30))
        self.max_round_trips = int(self.cfg.get("max_round_trips_per_symbol", 3))
        # A one-trade-per-day strategy hits its cap on every single day, so this
        # alert would be pure noise. Off for such strategies, on by default.
        self.alert_on_trade_cap = bool(self.cfg.get("alert_on_trade_cap", True))

        entry_window = self.cfg.get("entry_window", ["09:40", "15:20"])
        self.entry_start = _to_time(entry_window[0], time(9, 40))
        self.entry_end = _to_time(self.cfg.get("no_new_entry_after", entry_window[-1]),
                                  time(15, 20))
        self.flatten_time = _to_time(self.cfg.get("flatten_time", "15:50"), time(15, 50))
        self.poll_seconds = float(self.cfg.get("poll_seconds", 20))

        self.session_date = self._session_date()
        self.positions: Dict[str, Position] = {}
        self.start_equity: float = 0.0
        self.realized: float = 0.0
        self.trades: int = 0        # closed round trips (win-rate denominator)
        self.entries: int = 0       # order entries (daily-cap denominator)
        self.wins: int = 0
        self.round_trips: Dict[str, int] = {}
        self.last_exit: Dict[str, datetime] = {}
        self.halted = False          # no new entries (daily loss or manual)
        self.abort_session = False   # flatten now and stop the loop
        self.capped = False          # trade-cap throttle hit (entries paused only)
        self.equity_curve: List[tuple] = []
        # Only a real broker session owns persisted state. Replay/shadow runs are
        # throwaway simulations and must never inherit positions from each other.
        self.persist_state = getattr(execution, "name", "") == "broker"
        self._restore()

    # ------------------------------------------------------------------ #
    #  Public entry point
    # ------------------------------------------------------------------ #

    def run(self) -> dict:
        self._banner()
        self._recover_session()

        if self.start_equity <= 0:
            prices = self._prices(self.provider.get_recent())
            self.start_equity = self.exec.equity(prices)
        logger.info("Session start equity: $%.2f (provider=%s, dry_run=%s)",
                    self.start_equity, getattr(self.provider, "name", "?"), self.dry_run)

        last_prices: Dict[str, float] = {}
        while True:
            now = self.provider.now()
            if now.time() >= self.flatten_time:
                logger.info("Flatten time reached (%s)", now.strftime("%H:%M"))
                break

            bars = self.provider.get_recent()
            if not bars:
                logger.debug("No bars yet at %s", now.strftime("%H:%M"))
                self._wait()
                continue

            prices = self._prices(bars)
            last_prices = prices or last_prices
            equity = self.exec.equity(prices)
            self.equity_curve.append((now.isoformat(), equity))

            if self._check_daily_loss(equity, now, prices):
                break

            ranked = self.signals.evaluate(bars)
            self._manage_exits(ranked, prices, now, bars)
            if self._can_open_new(now):
                self._open_positions(ranked, prices, now, equity)

            self._persist()
            if ranked:
                top = ranked[0]
                logger.debug("%s equity=$%.0f top=%s score=%.2f entry_ok=%s open=%d",
                             now.strftime("%H:%M"), equity, top.symbol, top.score,
                             top.entry_ok, len(self.positions))
            self._wait()

        final_prices = self._final_prices(last_prices)
        self._flatten_all(final_prices, "session end")
        # Record equity *after* flattening: the pre-flatten mark would hide the
        # exit slippage/cost, making return_pct disagree with realized P&L.
        self.equity_curve.append(
            (self.provider.now().isoformat(), self.exec.equity(final_prices))
        )
        return self._summary()

    # ------------------------------------------------------------------ #
    #  Entries
    # ------------------------------------------------------------------ #

    def _can_open_new(self, now: datetime) -> bool:
        if self.halted:
            return False
        if not (self.entry_start <= now.time() <= self.entry_end):
            return False
        if len(self.positions) >= self.max_positions:
            return False
        if self.entries >= self.max_trades:
            # This is a *throttle*, not an exit condition: stop opening new risk
            # but keep managing what is open until flatten_time. It used to set
            # self.halted, which the daily-loss check read as "abort the session"
            # and flattened everything on the spot.
            if not self.capped:
                self.capped = True
                if self.alert_on_trade_cap:
                    logger.warning(
                        "Daily trade cap reached (%d/%d) — no new entries, "
                        "open positions still managed until %s",
                        self.entries, self.max_trades, self.flatten_time,
                    )
                    self.notifier.warning(
                        "Intraday trade cap",
                        f"max trades per day reached ({self.entries}) — entries paused",
                    )
            return False
        return True

    def _open_positions(self, ranked: List[IntradaySignal], prices: Dict[str, float],
                        now: datetime, equity: float) -> None:
        slots = self.max_positions - len(self.positions)
        if slots <= 0:
            return

        gross = sum(abs(p.quantity) * prices.get(s, p.entry_price)
                    for s, p in self.positions.items())
        capacity = max(0.0, equity * self.max_gross - gross)

        for signal in ranked:
            if slots <= 0 or capacity <= 0:
                break
            if not signal.entry_ok or signal.symbol in self.positions:
                continue
            if self._in_cooldown(signal.symbol, now):
                continue
            if self.round_trips.get(signal.symbol, 0) >= self.max_round_trips:
                logger.info("Skipping %s — round-trip cap reached", signal.symbol)
                continue

            side = 1 if int(getattr(signal, "side", 1) or 1) > 0 else -1
            if side < 0 and not self.allow_short:
                logger.warning("Short signal for %s ignored — allow_short is off",
                               signal.symbol)
                continue

            price = self._mark(signal.symbol, prices) or signal.last_price
            if not price or price <= 0:
                continue

            notional = min(equity * self.position_pct, capacity)
            quantity = int(notional / price)
            if quantity <= 0 or quantity * price < self.min_notional:
                continue

            order_side = "BUY" if side > 0 else "SELL"
            fill = self.exec.fill(signal.symbol, order_side, quantity, price)
            if not fill.get("ok"):
                logger.warning("Entry failed for %s: %s", signal.symbol, fill.get("reason"))
                continue

            fill_price = float(fill.get("fill_price") or price)
            filled_qty = int(fill.get("filled_qty") or quantity)
            # Stop distance comes from the strategy when it has one (the opening
            # range height here); the fixed percentage is only a fallback.
            stop_distance = float(getattr(signal, "stop_distance", 0.0) or 0.0)
            if stop_distance <= 0:
                stop_distance = fill_price * self.stop_loss_pct
            self.positions[signal.symbol] = Position(
                symbol=signal.symbol,
                quantity=filled_qty,
                entry_price=fill_price,
                entry_ts=now.isoformat(),
                stop_price=fill_price - side * stop_distance,
                target_price=(fill_price + side * fill_price * self.take_profit_pct
                              if self.take_profit_pct > 0 else 0.0),
                reason=f"{'LONG' if side > 0 else 'SHORT'} score={signal.score:.2f} "
                       f"{signal.reasons[0] if signal.reasons else ''}",
                side=side,
            )
            self.entries += 1
            capacity -= filled_qty * fill_price
            slots -= 1
            logger.info("%s %s x%d @ $%.2f | %s | stop $%.2f target $%.2f",
                        order_side, signal.symbol, filled_qty, fill_price,
                        self.positions[signal.symbol].reason,
                        self.positions[signal.symbol].stop_price,
                        self.positions[signal.symbol].target_price)
            self.journal.log("entry", symbol=signal.symbol, quantity=filled_qty,
                             price=fill_price, score=signal.score,
                             side=side, order_side=order_side,
                             stop_price=self.positions[signal.symbol].stop_price,
                             session_date=self.session_date,
                             signal_time=now.strftime("%H:%M"),
                             signals=signal.as_dict())
            self._persist()

    # ------------------------------------------------------------------ #
    #  Exits
    # ------------------------------------------------------------------ #

    def _manage_exits(self, ranked: List[IntradaySignal], prices: Dict[str, float],
                      now: datetime, bars: Dict = None) -> None:
        by_symbol = {s.symbol: s for s in ranked}

        for symbol, position in list(self.positions.items()):
            price = prices.get(symbol)
            if not price:
                continue

            side = 1 if position.side >= 0 else -1
            held = self._held_minutes(position, now)
            reason = ""
            exit_price = price

            # A stop is an intrabar order. Judging it by the close alone silently
            # widens it: on 2022-09-21 TSLA spiked through a short's stop and
            # closed back below it, so a close-based check would have held a
            # loser to the bell and booked +2% instead of -2%. That is a much
            # bigger distortion than the fill-price assumption, because it
            # changes which trades are losers at all.
            bar_high, bar_low = _bar_extremes(bars, symbol)
            stop = position.stop_price
            target = position.target_price
            if side > 0:
                stop_hit = stop > 0 and (
                    price <= stop or (bar_low is not None and bar_low <= stop))
                target_hit = target > 0 and (
                    price >= target or (bar_high is not None and bar_high >= target))
                if stop_hit:
                    exit_price = min(stop, price)
                elif target_hit:
                    exit_price = target
            else:
                stop_hit = stop > 0 and (
                    price >= stop or (bar_high is not None and bar_high >= stop))
                target_hit = target > 0 and (
                    price <= target or (bar_low is not None and bar_low <= target))
                if stop_hit:
                    exit_price = max(stop, price)
                elif target_hit:
                    exit_price = target

            if stop_hit:
                reason = f"stop hit ({side * (exit_price / position.entry_price - 1):.2%})"
            elif target_hit:
                reason = f"target hit ({side * (exit_price / position.entry_price - 1):.2%})"
            elif held >= self.max_hold_minutes:
                reason = f"max hold {held}m"
            elif held >= self.min_hold_minutes:
                # Signal-based exits are ignored for the first few minutes: a
                # 1-minute momentum wobble right after entry is noise, and
                # reacting to it just donates the spread.
                signal = by_symbol.get(symbol)
                if signal:
                    reason = self.signals.exit_signal(signal)

            if reason:
                self._close(symbol, exit_price, now, reason)

    def _close(self, symbol: str, price: float, now: datetime, reason: str,
               urgency: str = "normal") -> bool:
        position = self.positions.get(symbol)
        if not position:
            return False

        side = 1 if position.side >= 0 else -1
        order_side = "SELL" if side > 0 else "BUY"
        fill = self.exec.fill(symbol, order_side, position.quantity, price, urgency=urgency)
        if not fill.get("ok"):
            logger.warning("Exit failed for %s (%s): %s", symbol, reason, fill.get("reason"))
            self.notifier.warning("Exit not filled",
                                  f"{symbol} {position.quantity}sh — {reason}")
            return False

        fill_price = float(fill.get("fill_price") or price)
        filled_qty = int(fill.get("filled_qty") or position.quantity)
        pnl = side * (fill_price - position.entry_price) * filled_qty
        self.realized += pnl
        self.trades += 1
        if pnl > 0:
            self.wins += 1
        self.round_trips[symbol] = self.round_trips.get(symbol, 0) + 1
        self.last_exit[symbol] = now

        remaining = position.quantity - filled_qty
        if remaining > 0:
            position.quantity = remaining
        else:
            self.positions.pop(symbol, None)

        logger.info("%s %s x%d @ $%.2f | %s | pnl $%.2f (%+.2f%%)",
                    order_side, symbol, filled_qty, fill_price, reason, pnl,
                    side * (fill_price / position.entry_price - 1) * 100)
        self.journal.log("exit", symbol=symbol, quantity=filled_qty, price=fill_price,
                         pnl=round(pnl, 2), reason=reason,
                         entry_price=position.entry_price,
                         side=side, order_side=order_side,
                         return_pct=round(side * (fill_price / position.entry_price - 1), 6),
                         session_date=self.session_date,
                         exit_time=now.strftime("%H:%M"),
                         held_minutes=self._held_minutes(position, now))
        self._persist()
        return True

    def _flatten_all(self, prices: Dict[str, float], reason: str) -> None:
        for symbol in list(self.positions):
            price = prices.get(symbol) or self.positions[symbol].entry_price
            self._close(symbol, price, self.provider.now(), reason, urgency="flatten")

    # ------------------------------------------------------------------ #
    #  Risk
    # ------------------------------------------------------------------ #

    def _check_daily_loss(self, equity: float, now: datetime,
                          prices: Dict[str, float]) -> bool:
        """True only when the session must be aborted. A trade-cap throttle is
        deliberately *not* an abort: see _can_open_new."""
        if self.start_equity <= 0 or self.abort_session:
            return self.abort_session
        change = (equity - self.start_equity) / self.start_equity
        if change <= -self.max_daily_loss_pct:
            logger.error("DAILY LOSS LIMIT hit: %.2f%% — flattening and halting", change * 100)
            self.notifier.critical(
                "Intraday daily loss limit",
                f"{change:.2%} on ${self.start_equity:,.0f} — flattening, no more entries today",
            )
            self._flatten_all(prices, "daily loss limit")
            self.halted = True
            self.abort_session = True
            self._persist()
            return True
        return False

    def _in_cooldown(self, symbol: str, now: datetime) -> bool:
        last = self.last_exit.get(symbol)
        if not last:
            return False
        elapsed = (now - last).total_seconds() / 60
        if elapsed < self.cooldown_minutes:
            logger.debug("%s in cooldown (%.0f/%d min)", symbol, elapsed, self.cooldown_minutes)
            return True
        return False

    # ------------------------------------------------------------------ #
    #  Session / state
    # ------------------------------------------------------------------ #

    def _banner(self) -> None:
        desc = self.provider.describe() if hasattr(self.provider, "describe") else {}
        logger.info("=" * 66)
        logger.info("INTRADAY SESSION — %s | universe=%d | provider=%s | exec=%s",
                    self.session_date, len(getattr(self.provider, "universe", [])),
                    desc.get("provider"), getattr(self.exec, "name", "?"))
        logger.info("=" * 66)

    def _recover_session(self) -> None:
        if not self.persist_state or not self.positions:
            return
        recovered_day = self.store.get("session_date")
        if recovered_day == self.session_date:
            logger.warning("Resuming %d open position(s) from state", len(self.positions))
            return
        logger.error("Found %d position(s) from session %s — force flattening",
                     len(self.positions), recovered_day)
        self.notifier.critical(
            "Stale intraday positions",
            f"{len(self.positions)} position(s) from {recovered_day} found at startup",
        )
        prices = self._prices(self.provider.get_recent())
        self._flatten_all(prices, "stale session recovery")
        self.positions.clear()
        self._persist()

    def _session_date(self) -> str:
        return self.provider.now().strftime("%Y-%m-%d")

    def _prices(self, bars: Dict) -> Dict[str, float]:
        prices = {}
        for symbol, frame in (bars or {}).items():
            if frame is None or len(frame) == 0:
                continue
            try:
                prices[symbol] = float(frame["close"].iloc[-1])
            except Exception:
                continue
        return prices

    def _mark(self, symbol: str, prices: Dict[str, float]) -> float:
        """Price to trade at right now.

        Replay exposes the open of the bar that just started, which is the
        "next minute open" the backtest fills at. Without it the engine would
        fill at the *close of the signal bar* — up to a minute of hindsight.
        Live providers have no such method and fall through to the last price.
        """
        getter = getattr(self.provider, "mark_price", None)
        if getter is not None:
            try:
                value = getter(symbol)
                if value and value > 0:
                    return float(value)
            except Exception:
                pass
        return float(prices.get(symbol) or 0.0)

    def _final_prices(self, fallback: Dict[str, float]) -> Dict[str, float]:
        """Prices at flatten time, not one bar stale (the loop breaks before a
        final read, so `last_prices` can be a bar behind)."""
        try:
            fresh = self._prices(self.provider.get_recent())
        except Exception:
            fresh = {}
        return fresh or fallback

    def _held_minutes(self, position: Position, now: datetime) -> int:
        try:
            entered = datetime.fromisoformat(position.entry_ts)
        except ValueError:
            return 0
        if entered.tzinfo is None and now.tzinfo is not None:
            entered = entered.replace(tzinfo=now.tzinfo)
        return int((now - entered).total_seconds() / 60)

    def _persist(self) -> None:
        if not self.persist_state:
            return
        try:
            self.store.save({
                "session_date": self.session_date,
                "updated_at": datetime.now().astimezone().isoformat(),
                "positions": {s: p.as_dict() for s, p in self.positions.items()},
                "realized": self.realized,
                "trades": self.trades,
                "entries": self.entries,
                "wins": self.wins,
                "round_trips": self.round_trips,
                "start_equity": self.start_equity,
                "halted": self.halted,
                "capped": self.capped,
                "abort_session": self.abort_session,
            })
        except Exception as exc:
            logger.error("Failed to persist intraday state: %s", exc)

    def _restore(self) -> None:
        if not self.persist_state:
            return
        doc = self.store.load()
        if doc.get("session_date") != self.session_date:
            return
        for symbol, raw in (doc.get("positions") or {}).items():
            try:
                self.positions[symbol] = Position(**raw)
            except TypeError:
                continue
        self.realized = float(doc.get("realized") or 0)
        self.trades = int(doc.get("trades") or 0)
        self.entries = int(doc.get("entries") or 0)
        self.wins = int(doc.get("wins") or 0)
        self.round_trips = {k: int(v) for k, v in (doc.get("round_trips") or {}).items()}
        self.start_equity = float(doc.get("start_equity") or 0)
        self.halted = bool(doc.get("halted"))
        self.capped = bool(doc.get("capped"))
        self.abort_session = bool(doc.get("abort_session"))

    def _wait(self) -> None:
        clock = getattr(self.provider, "clock", None)
        if clock is not None and hasattr(clock, "sleep"):
            clock.sleep(0)          # replay: advance one bar
        else:
            time_module.sleep(self.poll_seconds)

    # ------------------------------------------------------------------ #
    #  Reporting
    # ------------------------------------------------------------------ #

    def _summary(self) -> dict:
        end_equity = self.equity_curve[-1][1] if self.equity_curve else self.start_equity
        total_return = ((end_equity - self.start_equity) / self.start_equity
                        if self.start_equity else 0)
        max_dd = _max_drawdown([e for _, e in self.equity_curve])
        win_rate = (self.wins / self.trades) if self.trades else 0.0

        summary = {
            "session_date": self.session_date,
            "provider": getattr(self.provider, "name", "?"),
            "execution": getattr(self.exec, "name", "?"),
            "dry_run": self.dry_run,
            "start_equity": round(self.start_equity, 2),
            "end_equity": round(end_equity, 2),
            "return_pct": round(total_return, 5),
            "realized_pnl": round(self.realized, 2),
            "trades": self.trades,
            "entries": self.entries,
            "win_rate": round(win_rate, 3),
            "max_intraday_drawdown": round(max_dd, 5),
            "halted": self.halted,
            "trade_cap_hit": self.capped,
            "open_positions": len(self.positions),
        }

        print("\n" + "=" * 66)
        print(f"  INTRADAY SESSION SUMMARY — {summary['session_date']}")
        print("=" * 66)
        print(f"  provider:        {summary['provider']} / exec: {summary['execution']}")
        print(f"  Start equity:    ${summary['start_equity']:>12,.2f}")
        print(f"  End equity:      ${summary['end_equity']:>12,.2f}")
        print(f"  Return:          {summary['return_pct']:>12.2%}")
        print(f"  Realized P&L:    ${summary['realized_pnl']:>12,.2f}")
        print(f"  Trades:          {summary['trades']:>12d}")
        print(f"  Win rate:        {summary['win_rate']:>12.1%}")
        print(f"  Max drawdown:    {summary['max_intraday_drawdown']:>12.2%}")
        print(f"  Halted:          {str(summary['halted']):>12}")
        print(f"  Open positions:  {summary['open_positions']:>12d}")
        print("=" * 66 + "\n")

        out_path = os.path.join(self.summary_dir,
                                f"intraday_summary_{self.session_date}.json")
        os.makedirs(self.summary_dir, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as handle:
            json.dump({**summary,
                       "equity_curve": self.equity_curve[-400:]}, handle, indent=2)
        print(f"  Summary saved → {out_path}\n")

        self.journal.log("session_summary", **summary)
        self._persist()
        return summary


def _bar_extremes(bars: Dict, symbol: str):
    """High/low of the last completed bar, or (None, None) when unavailable."""
    frame = (bars or {}).get(symbol)
    if frame is None or len(frame) == 0:
        return None, None
    try:
        return float(frame["high"].iloc[-1]), float(frame["low"].iloc[-1])
    except Exception:
        return None, None


def _to_time(text: str, fallback: time) -> time:
    try:
        hour, minute = str(text).split(":")[:2]
        return time(int(hour), int(minute))
    except Exception:
        return fallback


def _max_drawdown(values: List[float]) -> float:
    if not values:
        return 0.0
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, (value - peak) / peak)
    return worst


def build_engine(mode: str, config: dict, client=None, order_manager=None,
                 session_date: str = None, speed: int = 60,
                 initial_cash: float = 100_000.0,
                 dry_run: bool = False) -> IntradayEngine:
    """
    Convenience factory used by the CLI.

    mode: live | replay | synthetic | dry
    """
    from intraday.providers import build_provider

    universe = config.get("universe", [])
    provider_name = "replay" if mode == "replay" else (
        "synthetic" if mode == "synthetic" else config.get("provider", "auto")
    )
    provider = build_provider(
        provider_name, universe,
        lookback=int(config.get("lookback_bars", 90)),
        client=client if mode != "replay" and mode != "synthetic" else None,
        session_date=session_date, speed=speed,
    )

    if mode == "live" and not dry_run:
        execution: ExecutionAdapter = BrokerExecution(order_manager, client, config)
    else:
        equity = initial_cash
        if client is not None and mode in ("live", "dry"):
            try:
                equity = float(client.get_account_summary().get("net_liquidation") or initial_cash)
            except Exception:
                pass
        execution = SimExecution(equity, config)

    return IntradayEngine(provider, execution, config, dry_run=dry_run)
