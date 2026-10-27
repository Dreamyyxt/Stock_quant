"""
Intraday signals
================
The existing factor family, compressed from months to minutes:

  momentum   short-window rate of change (5 / 15 / 30 minutes)
  rsi        Wilder RSI on 1-minute closes (mean-reversion guard, not entry)
  value      deviation from session VWAP (intraday "fair value")
  quality    relative volume — only trade names that are actually moving

The composite score is cross-sectional: every factor is rank-normalised across
the universe for that bar, then blended 50 / 30 / 20. Raw thresholds are kept
separate from the score so an entry always has an explicit, loggable reason.

Long only. Shorting adds borrow cost, locate risk and hard-to-model slippage;
it is deliberately out of scope for the validation phase.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class IntradaySignal:
    symbol: str
    score: float
    momentum: float          # 15-bar return
    momentum_5: float
    rsi: float
    vwap_dev: float
    rel_volume: float
    atr_pct: float
    last_price: float
    entry_ok: bool
    exit_reason: str = ""
    reasons: List[str] = field(default_factory=list)
    # Direction the engine should trade: +1 long, -1 short. Strategies that are
    # long only leave it at +1, so the engine's behaviour is unchanged for them.
    side: int = 1
    # Absolute stop distance in price units (not a percentage). When > 0 the
    # engine places the stop at fill_price - side * stop_distance; when 0 it
    # falls back to the fixed stop_loss_pct from config.
    stop_distance: float = 0.0

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "score": round(self.score, 4),
            "momentum_15m": round(self.momentum, 5),
            "momentum_5m": round(self.momentum_5, 5),
            "rsi": round(self.rsi, 2),
            "vwap_dev": round(self.vwap_dev, 5),
            "rel_volume": round(self.rel_volume, 3),
            "atr_pct": round(self.atr_pct, 5),
            "last_price": round(self.last_price, 2),
            "entry_ok": self.entry_ok,
            "side": self.side,
            "stop_distance": round(self.stop_distance, 4),
            "reasons": self.reasons,
        }


class IntradaySignals:
    def __init__(self, config: dict):
        self.cfg = config
        self.momentum_window = int(config.get("momentum_window", 15))
        self.rsi_period = int(config.get("rsi_period", 14))
        self.entry_score = float(config.get("entry_score", 0.72))
        self.exit_score = float(config.get("exit_score", 0.35))
        self.rsi_entry_max = float(config.get("rsi_entry_max", 76.0))
        self.rsi_exit = float(config.get("rsi_exit", 80.0))
        self.min_bars = int(config.get("min_bars_for_signal", 35))
        self.min_rel_volume = float(config.get("min_rel_volume", 0.8))
        self.min_momentum_entry = float(config.get("min_momentum_entry", 0.0012))
        self.momentum_exit = float(config.get("momentum_exit", -0.0008))

    # ------------------------------------------------------------------ #
    #  Public
    # ------------------------------------------------------------------ #

    def evaluate(self, bars: Dict[str, pd.DataFrame]) -> List[IntradaySignal]:
        raw = []
        for symbol, frame in bars.items():
            try:
                features = self._features(symbol, frame)
                if features:
                    raw.append(features)
            except Exception as exc:
                logger.debug("Signal failed for %s: %s", symbol, exc)

        if not raw:
            return []

        frame = pd.DataFrame(raw)
        frame["mom_rank"] = frame["momentum"].rank(pct=True)
        frame["vwap_rank"] = frame["vwap_dev"].rank(pct=True)

        signals: List[IntradaySignal] = []
        for _, row in frame.iterrows():
            overbought_penalty = max(0.0, row["rsi"] - 70) / 30.0
            rsi_score = max(0.0, 1.0 - overbought_penalty)
            score = (0.50 * row["mom_rank"]
                     + 0.30 * row["vwap_rank"]
                     + 0.20 * rsi_score)

            reasons = []
            entry_ok = True
            if score < self.entry_score:
                entry_ok = False
                reasons.append(f"score {score:.2f} < {self.entry_score:.2f}")
            if row["momentum"] <= 0:
                entry_ok = False
                reasons.append("momentum not positive")
            elif row["momentum"] < self.min_momentum_entry:
                entry_ok = False
                reasons.append(f"momentum {row['momentum']:.2%} below entry floor "
                               f"{self.min_momentum_entry:.2%}")
            if row["vwap_dev"] <= 0:
                entry_ok = False
                reasons.append("below VWAP")
            if row["rsi"] >= self.rsi_entry_max:
                entry_ok = False
                reasons.append(f"RSI {row['rsi']:.0f} overbought")
            if row["rel_volume"] < self.min_rel_volume:
                entry_ok = False
                reasons.append(f"volume {row['rel_volume']:.2f}x too thin")

            signals.append(IntradaySignal(
                symbol=row["symbol"],
                score=float(score),
                momentum=float(row["momentum"]),
                momentum_5=float(row["momentum_5"]),
                rsi=float(row["rsi"]),
                vwap_dev=float(row["vwap_dev"]),
                rel_volume=float(row["rel_volume"]),
                atr_pct=float(row["atr_pct"]),
                last_price=float(row["last_price"]),
                entry_ok=entry_ok,
                reasons=reasons if reasons else ["all entry conditions met"],
            ))

        signals.sort(key=lambda s: s.score, reverse=True)
        return signals

    def exit_signal(self, signal: IntradaySignal) -> str:
        """Return a non-empty reason when an open long should be closed."""
        if signal.momentum <= self.momentum_exit:
            return f"momentum {signal.momentum:.3%} <= {self.momentum_exit:.3%}"
        if signal.rsi >= self.rsi_exit:
            return f"RSI {signal.rsi:.0f} >= {self.rsi_exit:.0f}"
        if signal.score <= self.exit_score:
            return f"score decayed to {signal.score:.2f}"
        if signal.vwap_dev < -0.002:
            return f"lost VWAP ({signal.vwap_dev:.3%})"
        return ""

    # ------------------------------------------------------------------ #
    #  Features
    # ------------------------------------------------------------------ #

    def _features(self, symbol: str, frame: pd.DataFrame) -> Dict[str, float]:
        if frame is None or len(frame) < self.min_bars:
            return {}

        close = frame["close"].astype(float)
        high = frame["high"].astype(float)
        low = frame["low"].astype(float)
        volume = frame["volume"].astype(float)

        last = float(close.iloc[-1])
        if not np.isfinite(last) or last <= 0:
            return {}

        mom = float(close.iloc[-1] / close.iloc[-1 - self.momentum_window] - 1)
        mom_5 = float(close.iloc[-1] / close.iloc[-6] - 1) if len(close) > 6 else 0.0

        rsi = _rsi(close, self.rsi_period)
        vwap = _vwap(frame)
        vwap_dev = float(last / vwap - 1) if vwap > 0 else 0.0

        recent_vol = volume.tail(5).sum()
        baseline = volume.rolling(5).sum().tail(60).median()
        rel_volume = float(recent_vol / baseline) if baseline and baseline > 0 else 1.0

        atr_pct = float(((high - low).tail(14) / close.tail(14)).mean())

        return {
            "symbol": symbol,
            "momentum": mom,
            "momentum_5": mom_5,
            "rsi": rsi,
            "vwap_dev": vwap_dev,
            "rel_volume": rel_volume,
            "atr_pct": atr_pct,
            "last_price": last,
        }


def _rsi(prices: pd.Series, period: int = 14) -> float:
    delta = prices.diff().dropna()
    if len(delta) < period:
        return 50.0
    gains = delta.clip(lower=0)
    losses = (-delta).clip(lower=0)
    avg_gain = gains.ewm(span=period, adjust=False).mean().iloc[-1]
    avg_loss = losses.ewm(span=period, adjust=False).mean().iloc[-1]
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - (100 / (1 + rs)))


def _vwap(frame: pd.DataFrame) -> float:
    typical = (frame["high"] + frame["low"] + frame["close"]) / 3.0
    volume = frame["volume"].astype(float)
    total = volume.sum()
    if total <= 0:
        return float(frame["close"].iloc[-1])
    return float((typical * volume).sum() / total)


class OrbBreakoutSignals:
    """
    Opening-range breakout, with a momentum and volume confirmation.

    Why this replaced the momentum-rank score
    -----------------------------------------
    Measured on 116 sessions of real 1-minute bars, the ranked-momentum score was
    anti-predictive: higher score meant worse forward returns, and the whole rule
    lost ~0.05% per trade after spread. Breakouts *at the opening range extreme*
    with volume confirmation were the only family that stayed positive in both the
    in-sample and out-of-sample halves once the holding period was long enough to
    clear the spread (30/60-minute holds were still negative; 120 minutes was not).

    Semantics that keep it honest:
      * The opening range is fixed from the first N bars of the session and is
        never recalculated, so the level does not repaint.
      * A breakout is a *state* here (close above the range high), not a crossing
        event, but the engine's cooldown prevents re-entering the same name.
      * Everything is computed from the bar window the provider hands over, which
        only contains bars up to "now".
    """

    def __init__(self, config: dict):
        self.cfg = config
        self.or_minutes = int(config.get("opening_range_minutes", 30))
        self.min_volume = float(config.get("orb_min_volume", 1.5))
        self.min_momentum = float(config.get("orb_min_momentum", 0.005))
        self.min_bars = int(config.get("min_bars_for_signal", 35))
        self.rsi_max = float(config.get("orb_rsi_max", 80.0))
        self.exit_momentum = float(config.get("orb_exit_momentum", -0.004))
        self.exit_vwap = float(config.get("orb_exit_vwap", -0.004))
        self._or_high: Dict[str, float] = {}
        self._session = None

    # ------------------------------------------------------------------ #

    def evaluate(self, bars: Dict[str, pd.DataFrame]) -> List[IntradaySignal]:
        if not bars:
            return []
        session = None
        for frame in bars.values():
            if len(frame):
                session = frame.index[-1].date()
                break
        if session != self._session:
            self._session = session
            self._or_high.clear()

        raw = []
        for symbol, frame in bars.items():
            features = self._features(symbol, frame)
            if features:
                raw.append(features)
        if not raw:
            return []

        frame = pd.DataFrame(raw)
        frame["mom_rank"] = frame["momentum"].rank(pct=True)
        frame["vol_rank"] = frame["rel_volume"].rank(pct=True)

        signals: List[IntradaySignal] = []
        for _, row in frame.iterrows():
            score = 0.60 * row["mom_rank"] + 0.40 * row["vol_rank"]
            reasons = []
            entry_ok = True
            if row["or_high"] is None:
                entry_ok = False
                reasons.append("opening range not formed yet")
            elif not row["broke_range"]:
                entry_ok = False
                reasons.append(f"below opening range high {row['or_high']:.2f}")
            if row["rel_volume"] < self.min_volume:
                entry_ok = False
                reasons.append(f"volume {row['rel_volume']:.2f}x < {self.min_volume:.2f}x")
            if row["momentum"] < self.min_momentum:
                entry_ok = False
                reasons.append(f"momentum {row['momentum']:.2%} < {self.min_momentum:.2%}")
            if row["rsi"] >= self.rsi_max:
                entry_ok = False
                reasons.append(f"RSI {row['rsi']:.0f} too hot")
            if row["vwap_dev"] <= 0:
                entry_ok = False
                reasons.append("below VWAP")

            signals.append(IntradaySignal(
                symbol=row["symbol"],
                score=float(score),
                momentum=float(row["momentum"]),
                momentum_5=float(row["momentum_5"]),
                rsi=float(row["rsi"]),
                vwap_dev=float(row["vwap_dev"]),
                rel_volume=float(row["rel_volume"]),
                atr_pct=float(row["atr_pct"]),
                last_price=float(row["last_price"]),
                entry_ok=entry_ok,
                reasons=reasons if reasons else ["breakout confirmed"],
            ))

        signals.sort(key=lambda s: s.score, reverse=True)
        return signals

    def exit_signal(self, signal: IntradaySignal) -> str:
        if signal.momentum <= self.exit_momentum:
            return f"momentum {signal.momentum:.2%} <= {self.exit_momentum:.2%}"
        if signal.vwap_dev <= self.exit_vwap:
            return f"lost VWAP ({signal.vwap_dev:.2%})"
        return ""

    # ------------------------------------------------------------------ #

    def _features(self, symbol: str, frame: pd.DataFrame) -> dict:
        if frame is None or len(frame) < 16:
            return {}
        close = frame["close"].astype(float)
        high = frame["high"].astype(float)
        volume = frame["volume"].astype(float)
        last = float(close.iloc[-1])
        if not np.isfinite(last) or last <= 0:
            return {}

        if symbol not in self._or_high:
            # Only trust the window if it starts at the session open; otherwise the
            # "opening range" would be a rolling window pretending to be one.
            if len(frame) >= self.or_minutes and frame.index[0].hour == 9 \
                    and frame.index[0].minute <= 35:
                self._or_high[symbol] = float(high.iloc[:self.or_minutes].max())
        or_high = self._or_high.get(symbol)

        mom = float(last / close.iloc[-16] - 1)
        mom_5 = float(last / close.iloc[-6] - 1) if len(close) > 6 else 0.0
        recent_vol = volume.tail(5).mean()
        baseline_vol = volume.rolling(5).mean().tail(60).median()
        rel_volume = float(recent_vol / baseline_vol) if baseline_vol > 0 else 1.0
        vwap = _vwap(frame)
        vwap_dev = float(last / vwap - 1) if vwap > 0 else 0.0
        atr_pct = float(((high - frame["low"].astype(float)).tail(14) / close.tail(14)).mean())

        return {
            "symbol": symbol,
            "or_high": or_high,
            "broke_range": bool(or_high is not None and last > or_high),
            "momentum": mom,
            "momentum_5": mom_5,
            "rsi": _rsi(close, 14),
            "vwap_dev": vwap_dev,
            "rel_volume": rel_volume,
            "atr_pct": atr_pct,
            "last_price": last,
        }


class OrbTslaSignals:
    """
    Single-name opening-range breakout, exactly as validated on TSLA.

    This is a port of ``tsla_breakout_backtest.py`` into the live engine, not a
    re-design: the two must agree bar for bar, otherwise the backtest stops
    being evidence about what the engine will do.

    The rule
    --------
      * range   = high/low of the first 30 bars (09:30–09:59), fixed for the day
      * entry   = the first bar whose *close* is outside the range, filled at
                  the open of the next bar (the engine's mark price)
      * window  = signal bar must be at or before 10:30
      * stop    = range height × 2, measured from the fill price
      * exit    = session close only — no target, no signal exit. The edge is
                  holding the trend day, so an early exit is a bug here, not a
                  safety feature.
      * one trade per day, long or short, whichever side breaks first

    Deliberate choices carried over from the research
    -------------------------------------------------
      * The range is only trusted when the bar window actually starts at 09:30.
        A window that starts later is a rolling window pretending to be an
        opening range, which would repaint the level.
      * Direction comes from the first break. Second-guessing it with a trend
        filter was tested and did not survive both halves of the sample.
    """

    def __init__(self, config: dict):
        self.cfg = config
        self.or_minutes = int(config.get("orb_tsla_range_minutes", 30))
        self.stop_mult = float(config.get("orb_tsla_stop_mult", 2.0))
        self.last_entry_minute = _minute_of(config.get("orb_tsla_last_entry", "10:30"), 630)
        self.session_open_minute = _minute_of(config.get("session_open", "09:30"), 570)
        self._session = None
        self._range: Dict[str, tuple] = {}
        self._fired: set = set()

    # ------------------------------------------------------------------ #

    def evaluate(self, bars: Dict[str, pd.DataFrame]) -> List[IntradaySignal]:
        if not bars:
            return []

        last_index = None
        for frame in bars.values():
            if frame is not None and len(frame):
                last_index = frame.index[-1]
                break
        if last_index is None:
            return []

        session = last_index.date()
        if session != self._session:
            self._session = session
            self._range.clear()
            self._fired.clear()

        signals: List[IntradaySignal] = []
        for symbol, frame in bars.items():
            try:
                signal = self._evaluate_symbol(symbol, frame)
            except Exception as exc:
                logger.debug("orb_tsla signal failed for %s: %s", symbol, exc)
                continue
            if signal is not None:
                signals.append(signal)

        signals.sort(key=lambda s: s.score, reverse=True)
        return signals

    def exit_signal(self, signal: IntradaySignal) -> str:
        """No signal exits. The session-close flatten is the only exit."""
        return ""

    # ------------------------------------------------------------------ #

    def _evaluate_symbol(self, symbol: str, frame: pd.DataFrame):
        if frame is None or len(frame) < 2:
            return None

        close = frame["close"].astype(float)
        last = float(close.iloc[-1])
        if not np.isfinite(last) or last <= 0:
            return None

        stamp = frame.index[-1]
        minute = stamp.hour * 60 + stamp.minute

        rng = self._range.get(symbol)
        if rng is None:
            # Restrict to the current session, starting at the bell. Live
            # providers hand over a rolling window that runs straight through
            # the previous session's after-hours bars, so its first bar is
            # essentially never 09:30 — without this slice the guard below
            # rejects every attempt and the strategy silently never trades.
            session = stamp.date()
            minutes = frame.index.hour * 60 + frame.index.minute
            day = frame[(frame.index.date == session) &
                        (minutes >= self.session_open_minute)]
            if len(day) <= self.or_minutes:
                return self._blocked(symbol, last, "opening range still forming")
            first = day.index[0]
            if first.hour * 60 + first.minute != self.session_open_minute:
                # Something is missing at the open, so the "opening range"
                # would be a lie. Refusing to trade is the correct failure.
                return self._blocked(symbol, last,
                                     "bar window does not start at the session open")
            rng = (float(day["high"].astype(float).iloc[:self.or_minutes].max()),
                   float(day["low"].astype(float).iloc[:self.or_minutes].min()))
            self._range[symbol] = rng
        or_high, or_low = rng
        range_size = or_high - or_low
        if range_size <= 0:
            return self._blocked(symbol, last, "flat opening range")

        if symbol in self._fired:
            return self._blocked(symbol, last, "already traded today")
        if minute > self.last_entry_minute:
            return self._blocked(symbol, last, "entry window closed")

        if last > or_high:
            side = 1
        elif last < or_low:
            side = -1
        else:
            return self._blocked(
                symbol, last,
                f"inside opening range {or_low:.2f}–{or_high:.2f}",
            )

        self._fired.add(symbol)
        return IntradaySignal(
            symbol=symbol,
            score=abs(last / (or_high if side > 0 else or_low) - 1) * 1e4,
            momentum=float(last / or_high - 1) if side > 0 else float(last / or_low - 1),
            momentum_5=float(last / close.iloc[-6] - 1) if len(close) > 6 else 0.0,
            rsi=_rsi(close, 14),
            vwap_dev=float(last / _vwap(frame) - 1) if _vwap(frame) > 0 else 0.0,
            rel_volume=1.0,
            atr_pct=range_size / last,
            last_price=last,
            entry_ok=True,
            reasons=[f"breakout {'up' if side > 0 else 'down'} of "
                     f"{or_low:.2f}–{or_high:.2f} at {stamp.strftime('%H:%M')}"],
            side=side,
            stop_distance=range_size * self.stop_mult,
        )

    @staticmethod
    def _blocked(symbol: str, last: float, reason: str) -> IntradaySignal:
        return IntradaySignal(
            symbol=symbol, score=0.0, momentum=0.0, momentum_5=0.0, rsi=50.0,
            vwap_dev=0.0, rel_volume=1.0, atr_pct=0.0, last_price=last,
            entry_ok=False, reasons=[reason],
        )


def _minute_of(text: str, fallback: int) -> int:
    """'10:30' → 630. Tolerates HH:MM:SS and malformed config."""
    try:
        hour, minute = str(text).split(":")[:2]
        return int(hour) * 60 + int(minute)
    except Exception:
        return fallback


def make_signals(config: dict):
    """Pick the signal engine from config, so switching strategies is one key."""
    mode = str(config.get("signal_mode", "orb_breakout")).lower()
    if mode == "orb_breakout":
        return OrbBreakoutSignals(config)
    if mode == "orb_tsla":
        return OrbTslaSignals(config)
    if mode == "momentum_rank":
        return IntradaySignals(config)
    raise ValueError(f"unknown signal_mode: {mode}")
