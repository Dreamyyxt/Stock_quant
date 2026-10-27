"""
Minute-bar data providers
=========================
The intraday engine does not care where bars come from, so the source is
pluggable:

  tiger      Tiger 1-minute bars (needs a quote permission; may be delayed)
  yfinance   Yahoo 1-minute bars (free, last ~7 calendar days only)
  replay     Historical bars replayed bar-by-bar at an accelerated clock
  synthetic  Deterministic random walk — for tests, no network needed

`provider="auto"` picks the best available at startup and logs what it chose,
so a silently degraded feed cannot masquerade as real-time data.

Every provider returns the same shape: {symbol: DataFrame(index=ts, open, high,
low, close, volume)} truncated to the provider's current clock.
"""

import logging
import os
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from config.settings import STATE_DIR

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    ET = None

CACHE_DIR = os.path.join(STATE_DIR, "intraday_cache")
_OHLC = ["open", "high", "low", "close", "volume"]


# ---------------------------------------------------------------------- #
#  Normalisation helpers
# ---------------------------------------------------------------------- #

def _epoch_unit(values: pd.Series) -> str:
    """Infer the epoch unit from magnitude instead of trusting the column name.

    Tiger's quote API returns a column called `time` holding epoch
    *milliseconds*. Parsing that with pandas' default `unit='ns'` silently
    lands every bar in 1970-01-01, which quietly destroys any time-of-day rule
    (an opening-range strategy sees a 30-minute window that never exists).
    Magnitude decides, not the header.
    """
    peak = float(np.nanmax(np.abs(values.to_numpy(dtype="float64"))))
    if peak >= 1e17:
        return "ns"
    if peak >= 1e14:
        return "us"
    if peak >= 1e11:
        return "ms"
    return "s"


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce any vendor frame into (DatetimeIndex, open/high/low/close/volume)."""
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=_OHLC)

    out = df.copy()
    out.columns = [str(c).lower() for c in out.columns]

    if not isinstance(out.index, pd.DatetimeIndex):
        for candidate in ("time", "datetime", "date", "timestamp"):
            if candidate in out.columns:
                values = out[candidate]
                try:
                    numeric = np.issubdtype(values.dtype, np.number)
                except TypeError:
                    # pandas 3.x StringDtype breaks np.issubdtype — treat as non-numeric
                    numeric = False
                if numeric:
                    out.index = pd.to_datetime(values, unit=_epoch_unit(values))
                else:
                    out.index = pd.to_datetime(values)
                out = out.drop(columns=[candidate])
                break

    if not isinstance(out.index, pd.DatetimeIndex):
        raise ValueError(f"Could not derive a datetime index from columns {list(out.columns)}")

    if out.index.tz is not None:
        out.index = out.index.tz_convert(ET) if ET else out.index.tz_localize(None)
    elif ET is not None:
        out.index = out.index.tz_localize(ET)

    missing = [c for c in _OHLC if c not in out.columns]
    if missing:
        raise ValueError(f"Bar frame missing columns: {missing}")

    out = out[_OHLC].apply(pd.to_numeric, errors="coerce")
    out = out.dropna(subset=["close"]).sort_index()
    return out[~out.index.duplicated(keep="last")]


def _download_with_retry(fetcher, attempts: int = 3, base_delay: float = 3.0):
    """
    yfinance rate-limits aggressively (HTTP 429). Retry with a widening delay
    and return None instead of propagating, so callers can explain the failure.
    """
    for attempt in range(attempts):
        try:
            result = fetcher()
            if result is not None and len(result) > 0:
                return result
        except Exception as exc:
            logger.warning("Bar download attempt %d/%d failed: %s",
                           attempt + 1, attempts, exc)
        if attempt < attempts - 1:
            time.sleep(base_delay * (attempt + 1))
    return None


def cache_path(symbol: str, session_date: str) -> str:
    return os.path.join(CACHE_DIR, f"{symbol.upper()}_{session_date}.csv")


def _session_window_utc(session_date: str) -> tuple:
    """US regular session for a date, expressed as ISO-8601 UTC range."""
    start = pd.Timestamp(session_date).replace(hour=9, minute=30)
    end = pd.Timestamp(session_date).replace(hour=16, minute=0)
    if ET is not None:
        start = start.tz_localize(ET).tz_convert("UTC")
        end = end.tz_localize(ET).tz_convert("UTC")
    return start.isoformat(), end.isoformat()


def _split_multi(raw: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    """Split a yfinance multi-ticker frame into per-symbol frames."""
    result: Dict[str, pd.DataFrame] = {}
    if raw is None or len(raw) == 0:
        return result

    if not isinstance(raw.columns, pd.MultiIndex):
        result["_single"] = raw
        return result

    level0 = list(raw.columns.get_level_values(0))
    level1 = list(raw.columns.get_level_values(1))
    if "Close" in level0 or "close" in level0:
        symbols = sorted(set(level1))       # (field, ticker)
        for symbol in symbols:
            frame = raw.xs(symbol, level=1, axis=1)
            result[symbol] = frame
    else:
        symbols = sorted(set(level0))       # (ticker, field)
        for symbol in symbols:
            frame = raw.xs(symbol, level=0, axis=1)
            result[symbol] = frame
    return result


# ---------------------------------------------------------------------- #
#  Clocks
# ---------------------------------------------------------------------- #

class SessionClock:
    """Wall clock in US Eastern."""

    def now(self) -> datetime:
        return datetime.now(ET) if ET else datetime.now()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class ReplayClock:
    """Simulated clock that advances one bar per tick."""

    def __init__(self, start: datetime, step_minutes: int = 1):
        self._now = start
        self.step = timedelta(minutes=step_minutes)

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        self._now += self.step


# ---------------------------------------------------------------------- #
#  Providers
# ---------------------------------------------------------------------- #

class BarProvider(ABC):
    name = "base"
    is_delayed = True

    def __init__(self, universe: List[str], lookback: int = 90):
        self.universe = [s.upper() for s in universe]
        self.lookback = lookback

    @abstractmethod
    def get_recent(self, symbols: Optional[List[str]] = None) -> Dict[str, pd.DataFrame]:
        """Return the most recent `lookback` bars for each symbol."""

    def describe(self) -> dict:
        return {"provider": self.name, "delayed": self.is_delayed,
                "lookback": self.lookback, "clock": str(self.now())}

    def now(self) -> datetime:
        return datetime.now(ET) if ET else datetime.now()


class TigerBarProvider(BarProvider):
    """1-minute bars straight from the Tiger quote API."""

    name = "tiger"

    def __init__(self, client, universe, lookback=90):
        super().__init__(universe, lookback)
        self.client = client

    def get_recent(self, symbols=None):
        from tigeropen.common.consts import BarPeriod
        symbols = [s.upper() for s in (symbols or self.universe)]
        try:
            raw = self.client.get_quote_client().get_bars(
                symbols=symbols, period=BarPeriod.ONE_MINUTE, limit=self.lookback
            )
        except Exception as exc:
            logger.error("Tiger minute bars failed: %s", exc)
            return {}

        if raw is None or len(raw) == 0:
            return {}

        frame = raw.copy()
        frame.columns = [str(c).lower() for c in frame.columns]
        result: Dict[str, pd.DataFrame] = {}
        if "symbol" in frame.columns:
            for symbol, group in frame.groupby("symbol"):
                try:
                    result[str(symbol).upper()] = _normalize(group)
                except ValueError as exc:
                    logger.warning("Bad bars for %s: %s", symbol, exc)
        else:
            try:
                result[symbols[0]] = _normalize(frame)
            except ValueError as exc:
                logger.warning("Bad bars: %s", exc)
        return result


class YFinanceBarProvider(BarProvider):
    """Free 1-minute bars. Only the last ~7 calendar days are available."""

    name = "yfinance"

    def __init__(self, universe, lookback=90):
        super().__init__(universe, lookback)

    def get_recent(self, symbols=None):
        import yfinance as yf
        symbols = [s.upper() for s in (symbols or self.universe)]

        def _fetch():
            if len(symbols) == 1:
                return yf.download(symbols[0], period="1d", interval="1m",
                                   progress=False, auto_adjust=False, threads=False)
            return yf.download(symbols, period="1d", interval="1m",
                               progress=False, auto_adjust=False,
                               group_by="ticker", threads=True)

        raw = _download_with_retry(_fetch, attempts=2, base_delay=2.0)
        if raw is None:
            logger.error("yfinance minute bars unavailable (rate limited or offline)")
            return {}

        frames = {"_single": raw} if len(symbols) == 1 else _split_multi(raw)

        result: Dict[str, pd.DataFrame] = {}
        if "_single" in frames and len(symbols) == 1:
            try:
                result[symbols[0]] = _normalize(frames["_single"])
            except ValueError as exc:
                logger.warning("Bad bars for %s: %s", symbols[0], exc)
        else:
            for symbol, frame in frames.items():
                try:
                    result[symbol.upper()] = _normalize(frame)
                except ValueError as exc:
                    logger.warning("Bad bars for %s: %s", symbol, exc)

        return {k: v.tail(self.lookback) for k, v in result.items()}


class ReplayBarProvider(BarProvider):
    """
    Replays a historical session bar by bar.

    Bars are cached as CSV under state/intraday_cache so a replay is repeatable
    and does not depend on the 7-day yfinance window once downloaded.
    """

    name = "replay"

    def __init__(self, universe, session_date: str, lookback=90, speed: int = 1):
        super().__init__(universe, lookback)
        self.session_date = session_date
        self.speed = max(1, int(speed))
        os.makedirs(CACHE_DIR, exist_ok=True)
        self._data = self._load_all()
        self.clock = ReplayClock(self._session_start(), step_minutes=self.speed)

    def _session_start(self) -> datetime:
        base = pd.Timestamp(self.session_date)
        start = base.replace(hour=9, minute=30)
        if ET is not None:
            return start.tz_localize(ET).to_pydatetime()
        return start.to_pydatetime()

    def _load_all(self) -> Dict[str, pd.DataFrame]:
        data = {}
        for symbol in self.universe:
            frame = self._load_symbol(symbol)
            if frame is not None and len(frame):
                data[symbol] = frame
        if not data:
            raise RuntimeError(
                f"No replay bars for {self.session_date}.\n"
                f"  Two ways to fix:\n"
                f"    1) fetch them:  .venv/bin/python scripts/fetch_minute_data.py "
                f"--date {self.session_date}\n"
                f"    2) drop CSV files into {CACHE_DIR}/ named "
                f"<SYMBOL>_{self.session_date}.csv\n"
                f"       with columns: time,open,high,low,close,volume\n"
                f"  Note: free 1-minute history only covers the last ~7 calendar days; "
                f"yfinance is also frequently rate-limited (HTTP 429)."
            )
        logger.info("Replay loaded %d symbols for %s", len(data), self.session_date)
        return data

    def _load_symbol(self, symbol: str) -> Optional[pd.DataFrame]:
        path = cache_path(symbol, self.session_date)
        if os.path.exists(path):
            try:
                cached = pd.read_csv(path, index_col=0, parse_dates=True)
                return _normalize(cached)
            except Exception as exc:
                logger.warning("Cache read failed for %s: %s", symbol, exc)

        # 1) Alpaca historical SIP: full consolidated tape, no 7-day window.
        #    Free plans can read historical SIP but not *recent* SIP, and replay
        #    is by definition historical, so this is the good path.
        alpaca = AlpacaBarProvider([symbol], lookback=500,
                                   session_date=self.session_date, feed="sip")
        if alpaca.configured:
            frame = alpaca.get_recent([symbol]).get(symbol)
            if frame is not None and len(frame):
                frame.to_csv(path)
                return frame
            logger.warning("Alpaca sip had no bars for %s on %s — trying yfinance",
                           symbol, self.session_date)

        # 2) yfinance fallback (last ~7 calendar days, often rate-limited)
        def _fetch():
            import yfinance as yf
            start = pd.Timestamp(self.session_date)
            end = start + pd.Timedelta(days=1)
            return yf.download(symbol, start=start.strftime("%Y-%m-%d"),
                               end=end.strftime("%Y-%m-%d"), interval="1m",
                               progress=False, auto_adjust=False, threads=False)

        raw = _download_with_retry(_fetch, attempts=2, base_delay=2.0)
        if raw is None or len(raw) == 0:
            logger.warning("Could not download replay bars for %s", symbol)
            return None

        try:
            if isinstance(raw.columns, pd.MultiIndex):
                raw.columns = raw.columns.get_level_values(0)
            frame = _normalize(raw)
            frame.to_csv(path)
            return frame
        except Exception as exc:
            logger.warning("Bad downloaded bars for %s: %s", symbol, exc)
            return None

    def now(self) -> datetime:
        return self.clock.now()

    def get_recent(self, symbols=None):
        """Only *completed* bars.

        Bar timestamps are the bar's start (that is how Alpaca and Tiger label
        them), so including the bar at `now` would hand the engine a close that
        is 59 seconds in the future. That was a quiet one-minute look-ahead in
        every replay: the engine filled at the close of the very bar that
        produced the signal. Trading at the open of the *next* bar is both what
        the backtest does and what a real order would get.
        """
        symbols = [s.upper() for s in (symbols or self.universe)]
        current = self.clock.now()
        result = {}
        for symbol in symbols:
            frame = self._data.get(symbol)
            if frame is None or frame.empty:
                continue
            window = frame[frame.index < current]
            if len(window):
                result[symbol] = window.tail(self.lookback)
        return result

    def mark_price(self, symbol: str) -> Optional[float]:
        """Open of the bar that is currently forming = the next trade price."""
        frame = self._data.get(symbol.upper())
        if frame is None or frame.empty:
            return None
        current = self.clock.now()
        upcoming = frame[frame.index >= current]
        if len(upcoming):
            return float(upcoming["open"].iloc[0])
        return float(frame["close"].iloc[-1])


class SyntheticBarProvider(BarProvider):
    """Deterministic random-walk session — lets the whole engine be tested offline."""

    name = "synthetic"
    is_delayed = False

    def __init__(self, universe, session_date: str = None, lookback=90, seed: int = 7):
        super().__init__(universe, lookback)
        self.session_date = session_date or (datetime.now(ET) if ET else datetime.now()).strftime("%Y-%m-%d")
        self._data = self._build(seed)
        self.clock = ReplayClock(self._session_start(), step_minutes=1)

    def _session_start(self) -> datetime:
        base = pd.Timestamp(self.session_date)
        start = base.replace(hour=9, minute=30)
        return (start.tz_localize(ET) if ET else start).to_pydatetime()

    def _build(self, seed: int) -> Dict[str, pd.DataFrame]:
        rng = np.random.default_rng(seed)
        stamps = pd.date_range(
            pd.Timestamp(self.session_date).replace(hour=9, minute=30),
            periods=390, freq="1min",
        )
        if ET is not None:
            stamps = stamps.tz_localize(ET)

        data = {}
        for i, symbol in enumerate(self.universe):
            drift = rng.normal(0, 0.00008)
            returns = rng.normal(drift, 0.0012, len(stamps))
            close = 100 * (1 + i * 0.35) * np.cumprod(1 + returns)
            spread = np.abs(rng.normal(0, 0.0004, len(stamps)))
            high = close * (1 + spread)
            low = close * (1 - spread)
            open_ = np.concatenate([[close[0]], close[:-1]])
            # Lognormal, not uniform: a uniform draw averages out over five bars,
            # so relative volume sits at ~1.0 forever and any strategy with a
            # volume filter silently never trades. That made the offline smoke
            # test a no-op. The burst draws add the occasional real-world spike.
            volume = rng.lognormal(mean=9.0, sigma=0.55, size=len(stamps)).astype(int)
            burst = rng.random(len(stamps)) < 0.02
            volume = np.where(burst, volume * 3, volume).astype(int)
            data[symbol] = pd.DataFrame(
                {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
                index=stamps,
            )
        return data

    def now(self) -> datetime:
        return self.clock.now()

    def get_recent(self, symbols=None):
        symbols = [s.upper() for s in (symbols or self.universe)]
        current = self.clock.now()
        out = {}
        for symbol in symbols:
            frame = self._data.get(symbol)
            if frame is None:
                continue
            window = frame[frame.index <= current]
            if len(window):
                out[symbol] = window.tail(self.lookback)
        return out


class AlpacaBarProvider(BarProvider):
    """
    Alpaca market-data REST: real-time-ish 1-minute bars, free with an API key.

    Free accounts are limited to the IEX feed (`feed=iex`, ~2-3% of consolidated
    volume). Prices track NBBO closely for liquid names but are not the
    consolidated tape — acceptable for paper validation, not for live sizing
    without cross-checking the broker's own quote.

    Env: ALPACA_API_KEY, ALPACA_API_SECRET, ALPACA_FEED (default iex)
    """

    name = "alpaca"
    is_delayed = False

    BASE = "https://data.alpaca.markets/v2/stocks/bars"

    def __init__(self, universe, lookback=90, feed: str = None, session_date: str = None):
        super().__init__(universe, lookback)
        self.key = (os.getenv("ALPACA_API_KEY") or "").strip()
        self.secret = (os.getenv("ALPACA_API_SECRET") or "").strip()
        self.session_date = session_date
        self.feed = self._resolve_feed(feed)

    def _resolve_feed(self, feed: Optional[str]) -> str:
        """Explicit feed > env override > historical default.

        Free subscriptions can read historical SIP (consolidated tape, ~25x the
        IEX volume) but are rejected with HTTP 403 for *recent* SIP. So:
          historical session (session_date set) -> sip
          live/rolling window                  -> iex
        Set ALPACA_FEED in config/.env to pin one explicitly.
        """
        chosen = (feed or os.getenv("ALPACA_FEED") or "").strip().lower()
        if chosen:
            return chosen
        return "sip" if self.session_date else "iex"

    @property
    def configured(self) -> bool:
        return bool(self.key and self.secret)

    def _headers(self) -> dict:
        if not self.configured:
            raise RuntimeError(
                "Alpaca keys missing. Put ALPACA_API_KEY / ALPACA_API_SECRET in config/.env"
            )
        return {
            "APCA-API-KEY-ID": self.key,
            "APCA-API-SECRET-KEY": self.secret,
            "accept": "application/json",
        }

    def get_recent(self, symbols=None):
        if not self.configured:
            logger.warning("Alpaca keys not configured (ALPACA_API_KEY / ALPACA_API_SECRET)")
            return {}

        symbols = [s.upper() for s in (symbols or self.universe)]

        # Always send an explicit window: without start/end the endpoint returns
        # nothing once the session is over, which used to look like "no data".
        if self.session_date:
            start, end = _session_window_utc(self.session_date)
        else:
            now = datetime.now(timezone.utc)
            start = (now - timedelta(minutes=int(self.lookback) + 15)).isoformat()
            end = now.isoformat()

        raw_bars = self._fetch_bars(symbols, start, end)
        result: Dict[str, pd.DataFrame] = {}
        for symbol, rows in raw_bars.items():
            if not rows:
                continue
            frame = pd.DataFrame(rows).rename(columns={
                "t": "time", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume",
            })
            try:
                result[symbol.upper()] = _normalize(frame).tail(self.lookback)
            except ValueError as exc:
                logger.warning("Bad Alpaca bars for %s: %s", symbol, exc)

        if not result:
            logger.warning(
                "Alpaca returned no bars (feed=%s, window %s -> %s). "
                "Outside the session this is expected.", self.feed, start, end,
            )
        return result

    def _fetch_bars(self, symbols: List[str], start: str, end: str,
                    max_pages: int = 10) -> Dict[str, list]:
        """Page through the bars endpoint until the window is exhausted."""
        import json
        import urllib.parse
        import urllib.request

        collected: Dict[str, list] = {}
        page_token = None
        for _ in range(max_pages):
            params = {
                "symbols": ",".join(symbols),
                "timeframe": "1Min",
                "limit": 10000,
                "feed": self.feed,
                "adjustment": "raw",
                "sort": "asc",
                "start": start,
                "end": end,
            }
            if page_token:
                params["page_token"] = page_token
            request = urllib.request.Request(
                f"{self.BASE}?{urllib.parse.urlencode(params)}",
                headers=self._headers(),
            )
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except Exception as exc:
                detail = ""
                if hasattr(exc, "read"):
                    try:
                        detail = exc.read().decode("utf-8", "replace")[:200]
                    except Exception:
                        pass
                logger.error("Alpaca bars request failed (feed=%s): %s %s", self.feed, exc, detail)
                break

            for symbol, rows in (payload.get("bars") or {}).items():
                if rows:
                    collected.setdefault(symbol, []).extend(rows)

            page_token = payload.get("next_page_token")
            if not page_token:
                break
        return collected


# ---------------------------------------------------------------------- #
#  Factory + capability probe
# ---------------------------------------------------------------------- #

def build_provider(name: str, universe: List[str], lookback: int = 90,
                   client=None, session_date: str = None,
                   speed: int = 1) -> BarProvider:
    name = (name or "auto").lower()

    if name == "synthetic":
        return SyntheticBarProvider(universe, session_date, lookback)
    if name == "replay":
        return ReplayBarProvider(universe, session_date, lookback, speed)
    if name == "tiger":
        if client is None:
            raise ValueError("Tiger provider requires a connected client")
        return TigerBarProvider(client, universe, lookback)
    if name == "yfinance":
        return YFinanceBarProvider(universe, lookback)
    if name == "alpaca":
        return AlpacaBarProvider(universe, lookback, session_date=session_date)

    # auto: Tiger -> Alpaca -> yfinance -> synthetic
    if client is not None:
        probe = TigerBarProvider(client, universe, lookback)
        if probe.get_recent(universe[:2]):
            logger.info("Using Tiger 1-minute bars")
            return probe
        logger.warning("Tiger minute bars unavailable — trying Alpaca")

    alpaca = AlpacaBarProvider(universe, lookback)
    if alpaca.configured and alpaca.get_recent(universe[:2]):
        logger.info("Using Alpaca 1-minute bars (feed=%s)", alpaca.feed)
        return alpaca

    yf_provider = YFinanceBarProvider(universe, lookback)
    if yf_provider.get_recent(universe[:2]):
        logger.info("Using yfinance 1-minute bars (delayed, last 7 days only)")
        return yf_provider

    logger.warning("No live minute feed available — using synthetic data (NOT tradeable)")
    return SyntheticBarProvider(universe, session_date, lookback)


def probe_capabilities(client=None) -> dict:
    """Report what data granularity is actually available on this account."""
    report = {}
    symbols = ["AAPL"]

    if client is not None:
        try:
            delays = client.get_quote(symbols)
            row = delays.iloc[0] if hasattr(delays, "iloc") and len(delays) else {}
            report["tiger_delay_brief"] = {
                "ok": bool(row is not None),
                "fields": list(getattr(delays, "columns", [])),
                "sample": {k: row.get(k) for k in ("symbol", "close", "time", "timestamp")
                           if hasattr(row, "get")},
            }
        except Exception as exc:
            report["tiger_delay_brief"] = {"ok": False, "error": str(exc)}

        try:
            provider = TigerBarProvider(client, symbols, lookback=5)
            bars = provider.get_recent(symbols)
            frame = bars.get("AAPL")
            report["tiger_1min"] = {
                "ok": frame is not None and len(frame) > 0,
                "bars": 0 if frame is None else len(frame),
                "last_bar": None if frame is None or frame.empty else str(frame.index[-1]),
            }
        except Exception as exc:
            report["tiger_1min"] = {"ok": False, "error": str(exc)}

    try:
        alpaca = AlpacaBarProvider(symbols, lookback=5)
        if alpaca.configured:
            bars = alpaca.get_recent(symbols)
            frame = bars.get("AAPL")
            report["alpaca_1min"] = {
                "ok": frame is not None and len(frame) > 0,
                "feed": alpaca.feed,
                "bars": 0 if frame is None else len(frame),
                "last_bar": None if frame is None or frame.empty else str(frame.index[-1]),
            }
        else:
            report["alpaca_1min"] = {"ok": False, "reason": "keys not configured"}
    except Exception as exc:
        report["alpaca_1min"] = {"ok": False, "error": str(exc)}

    try:
        provider = YFinanceBarProvider(symbols, lookback=5)
        bars = provider.get_recent(symbols)
        frame = bars.get("AAPL")
        report["yfinance_1min"] = {
            "ok": frame is not None and len(frame) > 0,
            "bars": 0 if frame is None else len(frame),
            "last_bar": None if frame is None or frame.empty else str(frame.index[-1]),
        }
    except Exception as exc:
        report["yfinance_1min"] = {"ok": False, "error": str(exc)}

    report["now_et"] = str(datetime.now(ET) if ET else datetime.now())
    return report
