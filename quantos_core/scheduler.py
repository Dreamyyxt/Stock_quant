"""
QuantOS 2.0 — 24/7 调度与股票时段隔离（T-504）
===============================================

股票和加密的作息完全不同，硬塞进一个循环会互相拖累：

    股票   工作日 09:30-16:00（交易所本地时区），周末和节假日休市
    加密   24/7，只在交易所维护窗口停

如果只有一条循环按"股票开市"判断，那美股一收盘，币就不动了；反过来按 24/7
跑，股票那边会在凌晨无事可做地空转，还会在休市日去查一个根本没有价格的标的。

所以这里是**多条独立 lane**，各自带自己的日历和节拍：

    ┌─ lane: equity ── 日历=NYSE  ── 每 60s ── 只有开市才干活
    └─ lane: crypto ── 日历=24/7   ── 每 15s ── 除非维护否则一直干

三条必须守住的规矩：

  1. **异常隔离**：一条 lane 抛异常，绝不能让 `tick()` 中断。实盘里"因为 A 策略
     报错导致 B 策略不再运行"是一个真实发生过的连环事故。
  2. **收盘不补跑**：休市三天后开盘，只跑一次，不是把三天的节拍一次性补出来。
     补跑会让策略在开盘瞬间发出几百个本该分布在三天里的信号。
  3. **结算不遗漏**：进程睡过一个资金费边界，醒来必须知道"漏了几次"，能补账。
     这条和"不补跑"不冲突 —— 交易节拍不补，**账**必须补。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .contracts import utcnow
from .funding import DEFAULT_ANCHOR, boundaries_between

__all__ = [
    "VenueClass", "SessionStatus", "EquitySession", "CryptoSession",
    "nyse_session", "Lane", "LaneResult", "LaneState", "LaneStats",
    "Scheduler", "boundaries_crossed", "load_tz",
]


def load_tz(name: str):
    """
    取时区。拿不到就报错，**绝不静默退化成 UTC**。

    退化成 UTC 的后果是隐蔽的：美股会在 UTC 09:30 开盘，而那是纽约的凌晨
    4:30 —— 系统不会崩，只会在错误的时间交易，而且日志里一切正常。
    """
    try:
        from zoneinfo import ZoneInfo
    except ImportError as exc:                                # pragma: no cover
        raise RuntimeError("需要 Python 3.9+ 的 zoneinfo") from exc
    try:
        return ZoneInfo(name)
    except Exception as exc:                                  # noqa: BLE001
        raise RuntimeError(
            f"取不到时区 {name}。Linux 上装 tzdata（apt install tzdata），"
            f"不要用固定偏移代替 —— 夏令时会让开收盘时间错一小时") from exc


def _aware(ts: datetime, name: str = "ts") -> datetime:
    if ts.tzinfo is None:
        raise ValueError(f"{name} 必须是带时区的时间")
    return ts


class VenueClass(str, Enum):
    EQUITY = "EQUITY"
    CRYPTO = "CRYPTO"


@dataclass(frozen=True)
class SessionStatus:
    """一个日历在某一刻的状态。运维面直接展示它。"""

    name: str
    venue_class: VenueClass
    open: bool
    detail: str
    next_change: Optional[datetime] = None

    def to_dict(self) -> Dict[str, object]:
        return {"name": self.name, "venue_class": self.venue_class.value,
                "open": self.open, "detail": self.detail,
                "next_change": self.next_change.isoformat() if self.next_change else None}


# --------------------------------------------------------------------------- #
#  日历
# --------------------------------------------------------------------------- #

class EquitySession:
    """
    股票交易时段。

    只做时段判断，不做"今天该不该交易"的策略判断 —— 后者属于策略层。
    这里唯一的职责是回答"现在这个场所是不是开着"。
    """

    venue_class = VenueClass.EQUITY

    def __init__(self, name: str, *, tz_name: str = "America/New_York",
                 open_at: time = time(9, 30), close_at: time = time(16, 0),
                 holidays: Sequence[date] = (),
                 early_closes: Optional[Mapping[date, time]] = None) -> None:
        if close_at <= open_at:
            raise ValueError("收盘时间必须晚于开盘时间")
        self.name = name
        self.tz = load_tz(tz_name)
        self.tz_name = tz_name
        self.open_at = open_at
        self.close_at = close_at
        self.holidays = frozenset(holidays)
        self.early_closes = dict(early_closes or {})

    # -- 基本判断 -------------------------------------------------------- #

    def _local(self, ts: datetime) -> datetime:
        return _aware(ts).astimezone(self.tz)

    def is_trading_day(self, day: date) -> bool:
        if day.weekday() >= 5:                 # 周六周日
            return False
        return day not in self.holidays

    def close_on(self, day: date) -> time:
        return self.early_closes.get(day, self.close_at)

    def open_dt(self, day: date) -> datetime:
        return datetime.combine(day, self.open_at, tzinfo=self.tz)

    def close_dt(self, day: date) -> datetime:
        return datetime.combine(day, self.close_on(day), tzinfo=self.tz)

    def is_open(self, now: datetime) -> bool:
        local = self._local(now)
        day = local.date()
        if not self.is_trading_day(day):
            return False
        return self.open_dt(day) <= local < self.close_dt(day)

    # -- 边界 ------------------------------------------------------------ #

    def next_open(self, now: datetime) -> datetime:
        local = self._local(now)
        day = local.date()
        for i in range(0, 40):
            cand = day + timedelta(days=i)
            if not self.is_trading_day(cand):
                continue
            o = self.open_dt(cand)
            if o > local:
                return o
        raise RuntimeError(f"{self.name}: 未来 40 天内没有开盘日，检查节假日配置")

    def next_close(self, now: datetime) -> Optional[datetime]:
        if not self.is_open(now):
            return None
        return self.close_dt(self._local(now).date())

    def describe(self, now: datetime) -> SessionStatus:
        local = self._local(now)
        if self.is_open(now):
            c = self.close_dt(local.date())
            mins = (c - local).total_seconds() / 60
            return SessionStatus(self.name, self.venue_class, True,
                                 f"交易中，{mins:.0f} 分钟后收盘", c)
        nxt = self.next_open(now)
        return SessionStatus(self.name, self.venue_class, False,
                             f"休市，下次开盘 {nxt.strftime('%Y-%m-%d %H:%M')} {self.tz_name}",
                             nxt)


def nyse_session(holidays: Sequence[date] = (),
                 early_closes: Optional[Mapping[date, time]] = None) -> EquitySession:
    """纽交所常规时段。节假日表要自己维护 —— 交易所每年公布的日历不写在代码里。"""
    return EquitySession("NYSE", tz_name="America/New_York",
                         open_at=time(9, 30), close_at=time(16, 0),
                         holidays=holidays, early_closes=early_closes)


class CryptoSession:
    """
    加密场所：永远开，除了维护窗口。

    维护窗口是真实存在且无预警的（交易所公告里写"预计 5 分钟"，实际 40 分钟）。
    所以这里不预测，只登记已知窗口，并在窗口内把 lane 停掉 ——
    在维护期下单只会拿到一堆 5xx，然后被识别为"不确定"，白占对账预算。
    """

    venue_class = VenueClass.CRYPTO

    def __init__(self, name: str = "CRYPTO",
                 maintenance: Sequence[Tuple[datetime, datetime]] = ()) -> None:
        self.name = name
        self._windows: List[Tuple[datetime, datetime]] = []
        for start, end in maintenance:
            self.add_maintenance(start, end)

    def add_maintenance(self, start: datetime, end: datetime) -> None:
        if end <= start:
            raise ValueError("维护窗口结束必须晚于开始")
        self._windows.append((_aware(start, "maintenance.start"), _aware(end, "maintenance.end")))
        self._windows.sort()

    @property
    def maintenance_windows(self) -> Tuple[Tuple[datetime, datetime], ...]:
        return tuple(self._windows)

    def _window(self, now: datetime) -> Optional[Tuple[datetime, datetime]]:
        for start, end in self._windows:
            if start <= now < end:
                return start, end
        return None

    def is_open(self, now: datetime) -> bool:
        return self._window(_aware(now)) is None

    def next_open(self, now: datetime) -> Optional[datetime]:
        win = self._window(now)
        return win[1] if win else None

    def describe(self, now: datetime) -> SessionStatus:
        win = self._window(_aware(now))
        if win is None:
            return SessionStatus(self.name, self.venue_class, True, "全天候运行", None)
        mins = (win[1] - now).total_seconds() / 60
        return SessionStatus(self.name, self.venue_class, False,
                             f"交易所维护中，预计 {mins:.0f} 分钟后恢复", win[1])


# --------------------------------------------------------------------------- #
#  Lane
# --------------------------------------------------------------------------- #

class LaneState(str, Enum):
    RAN = "ran"
    FAILED = "failed"
    SKIPPED_CLOSED = "skipped_closed"
    SKIPPED_NOT_DUE = "skipped_not_due"


@dataclass
class LaneStats:
    runs: int = 0
    failures: int = 0
    skips_closed: int = 0
    skips_not_due: int = 0
    consecutive_failures: int = 0
    last_run_at: Optional[datetime] = None
    last_attempt_at: Optional[datetime] = None
    last_error: str = ""
    last_detail: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "runs": self.runs, "failures": self.failures,
            "skips_closed": self.skips_closed, "skips_not_due": self.skips_not_due,
            "consecutive_failures": self.consecutive_failures,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_attempt_at": self.last_attempt_at.isoformat() if self.last_attempt_at else None,
            "last_error": self.last_error, "last_detail": self.last_detail,
        }

    @property
    def healthy(self) -> bool:
        """连续失败 3 次算不健康。单次失败是网络抖动，连续失败是坏掉了。"""
        return self.consecutive_failures < 3


@dataclass(frozen=True)
class LaneResult:
    lane: str
    state: LaneState
    detail: str = ""
    missed_intervals: int = 0

    @property
    def ran(self) -> bool:
        return self.state is LaneState.RAN

    def to_dict(self) -> Dict[str, object]:
        return {"lane": self.lane, "state": self.state.value, "detail": self.detail,
                "missed_intervals": self.missed_intervals}


HandleFn = Callable[[datetime], None]


class Lane:
    """
    一条独立运行循环。

    `handler` 是唯一真正干活的东西。它抛任何异常都只影响这条 lane 自己。
    """

    def __init__(self, name: str, session, handler: HandleFn,
                 interval_sec: float) -> None:
        if interval_sec <= 0:
            raise ValueError("interval_sec 必须为正")
        self.name = name
        self.session = session
        self.handler = handler
        self.interval = timedelta(seconds=float(interval_sec))
        self.stats = LaneStats()
        self._next_due: Optional[datetime] = None

    @property
    def venue_class(self) -> VenueClass:
        return getattr(self.session, "venue_class", VenueClass.CRYPTO)

    def due(self, now: datetime) -> bool:
        return self._next_due is None or now >= self._next_due

    def status(self, now: datetime) -> SessionStatus:
        return self.session.describe(now)

    def run(self, now: Optional[datetime] = None) -> LaneResult:
        now = _aware(now or utcnow(), "now")
        self.stats.last_attempt_at = now

        if not self.session.is_open(now):
            status = self.session.describe(now)
            self.stats.skips_closed += 1
            self.stats.last_detail = status.detail
            # 关键：休市期间把下一次到期时间推到「现在 + 间隔」。恢复交易时
            # 最多只补一次，不会把整个周末的节拍一次性倒出来。
            self._next_due = now + self.interval
            return LaneResult(self.name, LaneState.SKIPPED_CLOSED, status.detail)

        if not self.due(now):
            self.stats.skips_not_due += 1
            return LaneResult(self.name, LaneState.SKIPPED_NOT_DUE, "未到节拍")

        missed = 0
        if self.stats.last_run_at is not None:
            gap = (now - self.stats.last_run_at).total_seconds()
            missed = max(0, int(gap // self.interval.total_seconds()) - 1)

        try:
            self.handler(now)
        except BaseException as exc:                          # noqa: BLE001
            # 捕获 BaseException 而不是 Exception：handler 里一个 SystemExit 或
            # KeyboardInterrupt 也不该让另一条 lane 停摆。这是刻意的。
            self.stats.failures += 1
            self.stats.consecutive_failures += 1
            self.stats.last_error = f"{type(exc).__name__}: {exc}"
            self._next_due = now + self.interval
            return LaneResult(self.name, LaneState.FAILED,
                              self.stats.last_error, missed)

        self.stats.runs += 1
        self.stats.consecutive_failures = 0
        self.stats.last_run_at = now
        self.stats.last_error = ""
        self._next_due = now + self.interval
        return LaneResult(self.name, LaneState.RAN, "", missed)


# --------------------------------------------------------------------------- #
#  调度器
# --------------------------------------------------------------------------- #

class Scheduler:
    """
    多条 lane 的总调度。

    `tick()` 的语义是"现在该干什么就干什么"，它自己不定时 ——
    定时由外部（cron / systemd timer / 一个 while 循环）负责。
    这样调度逻辑可以在测试里用任意时间戳驱动，不需要真的等 15 秒。
    """

    def __init__(self, lanes: Sequence[Lane]) -> None:
        names = [lane.name for lane in lanes]
        if len(set(names)) != len(names):
            raise ValueError(f"lane 名字重复：{names}")
        self._lanes = list(lanes)

    @property
    def lanes(self) -> Tuple[Lane, ...]:
        return tuple(self._lanes)

    def lane(self, name: str) -> Lane:
        for lane in self._lanes:
            if lane.name == name:
                return lane
        raise KeyError(f"没有名为 {name} 的 lane")

    def tick(self, now: Optional[datetime] = None) -> Dict[str, LaneResult]:
        """
        推进所有 lane 一拍。

        每条 lane 各自捕获异常 —— 这一层的 try 是**第二道**保险，因为 Lane.run
        已经兜了自己。双重保险在这里不算冗余：未来有人给 Lane 加一个会在
        `is_open` 里抛异常的日历，Scheduler 也不会因此全停。
        """
        now = _aware(now or utcnow(), "now")
        out: Dict[str, LaneResult] = {}
        for lane in self._lanes:
            try:
                out[lane.name] = lane.run(now)
            except BaseException as exc:                      # noqa: BLE001
                out[lane.name] = LaneResult(
                    lane.name, LaneState.FAILED,
                    f"调度层兜底捕获 {type(exc).__name__}: {exc}")
        return out

    def snapshot(self, now: Optional[datetime] = None) -> Dict[str, object]:
        now = _aware(now or utcnow(), "now")
        lanes = []
        for lane in self._lanes:
            status = lane.status(now)
            lanes.append({
                "name": lane.name,
                "venue_class": lane.venue_class.value,
                "interval_sec": lane.interval.total_seconds(),
                "open": status.open,
                "detail": status.detail,
                "next_change": (status.next_change.isoformat()
                                if status.next_change else None),
                "healthy": lane.stats.healthy,
                "stats": lane.stats.to_dict(),
            })
        return {
            "as_of": now.isoformat(),
            "lane_count": len(lanes),
            "lanes": lanes,
            "open_lanes": [l["name"] for l in lanes if l["open"]],
            "unhealthy_lanes": [l["name"] for l in lanes if not l["healthy"]],
        }


def boundaries_crossed(prev: datetime, now: datetime, interval_hours: int,
                       anchor: datetime = DEFAULT_ANCHOR) -> List[datetime]:
    """
    (prev, now] 之间的资金费结算时刻。

    进程睡了三小时再醒，中间跨过一个 08:00 结算 —— 这个函数告诉你"漏了哪几笔"。
    交易节拍不补跑，但**账必须补**：漏掉的资金费是已经发生的现金流，
    不补的话 NAV 会一直偏高，风控会以为还有余量继续下单。
    """
    prev = _aware(prev, "prev")
    now = _aware(now, "now")
    if now <= prev:
        return []
    return boundaries_between(prev, now, interval_hours, anchor)
