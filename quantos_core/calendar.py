"""
交易日历 —— 调度器的地基，也是最容易被糊弄过去的一层。

为什么单独一个模块
==================
`scheduler.py`（B5）只回答"现在这个场所开着没有"，判据是"工作日 + 手写节假日表"。
那套够用来决定"这一拍要不要干活"，但不够用来排期：

    "每 20 个交易日调一次仓" 里的 20 是**交易日**，不是自然日。
    用自然日算，20 天里永远混进 4-6 个周末和若干节假日，
    实际调仓间隔在 20~32 天之间飘，回测里的"20 日再平衡"就再也对不上实盘。

所以排期需要一个能数交易日的日历。三个来源可选，各有各的坑：

| 来源 | 好处 | 坑 |
|---|---|---|
| 手写节假日表 | 简单 | 每年 11 月才公布次年安排；抄错一天就全错，而且错了没人发现 |
| 规则（工作日） | 不用维护 | 把春节、国庆当交易日 —— 那天没有行情，信号算不出来 |
| **真实行情里的日期** | 数据即事实，不可能抄错 | 只能覆盖"已经有数据"的区间，未来推不出来 |

本模块的立场：**历史用真实数据，未来显式承认是推算，两者都要能对账。**

于是核心是两件事：

1. `TradingCalendar` —— 一个具体、可落盘、可复现的日历对象（节假日集合 + 覆盖区间）；
2. `audit_rules_against_data()` —— 拿真实数据去检验"规则日历"错在哪几天。

第 2 条是本批最重要的一处诚实：任何"我用规则推算未来交易日"的说法，
都必须先在这段已知数据上量一遍它的误差。误差方向也要写清楚：

    A 股没有周末补班交易，所以规则日历的误差**只有单向**：
    它会把节假日当成交易日（多算），不会把交易日漏掉。
    后果是那一拍拿去算信号的日期没有行情 → 被 fail-closed 拦下来 → 晚一天调仓。
    这比反向误差安全得多，但仍然要报出来，而不是假装日历是对的。

未校准区间一律 fail-closed
=========================
`covered(day)` 为 False 时，`RebalanceScheduler` 拒绝运行并说明缺哪一段。
理由和 `scheduler.load_tz` 拒回退 UTC 一样：**在错误的日期交易不会报错，
只会在事后对不上账。**
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

__all__ = [
    "CalendarError", "CoverageGap", "TradingCalendar", "CalendarAudit",
    "build_from_observed", "audit_rules_against_data",
    "weekdays_between", "load_calendar", "save_calendar",
]

#: 扫描上限。日历配错（比如节假日把全年填满）时不能死循环。
_MAX_SCAN_DAYS = 4000


class CalendarError(RuntimeError):
    """日历本身不可用 —— 不是"这一天不开市"，是"这份日历不能用"。"""


@dataclass(frozen=True)
class CoverageGap:
    """某一段时间没有校准过的日历。运维面直接展示它。"""

    name: str
    from_day: date
    reason: str

    def to_dict(self) -> Dict[str, object]:
        return {"calendar": self.name, "from_day": self.from_day.isoformat(),
                "reason": self.reason}


# --------------------------------------------------------------------------- #
#  基本工具
# --------------------------------------------------------------------------- #

def weekdays_between(start: date, end: date) -> List[date]:
    """[start, end] 里的周一至周五。这是"规则日历"的全部内容。"""
    if end < start:
        return []
    out: List[date] = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


# --------------------------------------------------------------------------- #
#  日历
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class TradingCalendar:
    """
    一个具体的交易日历。

    `holidays` 是"工作日但休市"的日子；`extra_open_days` 是"周末但开市"的日子。
    A 股后者恒为空（没有周末补班交易），但美股有半天交易、部分市场有周六盘，
    所以字段留着 —— 表达得了，才不会到了那天临时改代码。

    `verified_until` 是**这份日历敢负责到哪一天**。超过它的日子：
    `is_trading_day()` 仍会按规则给一个答案，但 `covered()` 返回 False，
    调用方（调度器）据自己的策略决定是拒绝还是标注为推算。
    日历本身不替调用方做这个决定，因为只有调用方知道自己经不经得起猜错一天。
    """

    name: str
    holidays: FrozenSet[date] = frozenset()
    extra_open_days: FrozenSet[date] = frozenset()
    first_day: Optional[date] = None
    verified_until: Optional[date] = None
    source: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise CalendarError("日历必须有名 —— 报错时要说清是哪一份")
        if self.first_day and self.verified_until:
            if self.verified_until < self.first_day:
                raise CalendarError(
                    f"{self.name}: verified_until {self.verified_until} 早于 "
                    f"first_day {self.first_day}，覆盖区间是空集")

    # -- 单日判断 -------------------------------------------------------- #

    def is_trading_day(self, day: date) -> bool:
        """
        按本日历给的答案。注意这是**规则判断**，不代表这一天被校准过。
        要看"这个答案可不可信"用 `covered()`。
        """
        if day in self.extra_open_days:
            return True
        if day.weekday() >= 5:
            return False
        return day not in self.holidays

    def is_closed_weekday(self, day: date) -> bool:
        """工作日但休市 —— 这一天最容易被当成"应该交易但没数据"。"""
        return day.weekday() < 5 and not self.is_trading_day(day)

    def covered(self, day: date) -> bool:
        if self.verified_until is None:
            return False
        if self.first_day is not None and day < self.first_day:
            return False
        return day <= self.verified_until

    def gap_for(self, day: date) -> Optional[CoverageGap]:
        """这一天不在覆盖区间时，说清缺的是哪一段。"""
        if self.covered(day):
            return None
        if self.verified_until is None:
            return CoverageGap(self.name, day, "这份日历没有覆盖区间（verified_until 为空）")
        if self.first_day is not None and day < self.first_day:
            return CoverageGap(
                self.name, day,
                f"日历从 {self.first_day} 才开始，{day} 在区间之前")
        return CoverageGap(
            self.name, day,
            f"日历只校准到 {self.verified_until}，"
            f"从 {self.verified_until + timedelta(days=1)} 起是未校准区间"
            f"（本次要用的是 {day}）")

    @property
    def span(self) -> Tuple[Optional[date], Optional[date]]:
        return self.first_day, self.verified_until

    # -- 交易日算术 ------------------------------------------------------- #

    def next_trading_day(self, day: date, *, include_self: bool = False) -> date:
        """
        `day` 之后最近的交易日。`include_self=True` 时 `day` 本身是交易日就返回它。

        找不到（超过 `_MAX_SCAN_DAYS`）直接报错，不返回 None ——
        返回 None 会让调用方写出 `if x is None: 用今天`，那是最糟的兜底。
        """
        cur = day if include_self else day + timedelta(days=1)
        for _ in range(_MAX_SCAN_DAYS):
            if self.is_trading_day(cur):
                return cur
            cur += timedelta(days=1)
        raise CalendarError(
            f"{self.name}: 从 {day} 起 {_MAX_SCAN_DAYS} 天内找不到交易日 "
            "（节假日配置把整段时间填满了？）")

    def prev_trading_day(self, day: date, *, include_self: bool = False) -> date:
        cur = day if include_self else day - timedelta(days=1)
        for _ in range(_MAX_SCAN_DAYS):
            if self.is_trading_day(cur):
                return cur
            cur -= timedelta(days=1)
        raise CalendarError(
            f"{self.name}: 从 {day} 往前 {_MAX_SCAN_DAYS} 天内找不到交易日")

    def shift(self, day: date, n: int) -> date:
        """
        从 `day` 往后（n>0）或往前（n<0）数 n 个交易日。n=0 时返回 `day`
        之后的第一个交易日（不含自身），和 `next_trading_day` 一致。
        """
        if n == 0:
            return self.next_trading_day(day)
        cur = day
        step = 1 if n > 0 else -1
        for _ in range(abs(n)):
            cur = (self.next_trading_day(cur) if step > 0
                   else self.prev_trading_day(cur))
        return cur

    def trading_days(self, start: date, end: date) -> List[date]:
        """[start, end] 内的交易日（升序）。"""
        if end < start:
            return []
        out: List[date] = []
        cur = start
        while cur <= end:
            if self.is_trading_day(cur):
                out.append(cur)
            cur += timedelta(days=1)
        return out

    def count(self, start: date, end: date) -> int:
        """[start, end] 内有多少个交易日（两端都算）。"""
        return len(self.trading_days(start, end))

    def offset_from(self, anchor: date, day: date) -> int:
        """
        `day` 相对 `anchor` 是第几个交易日。`anchor` 本身是第 0 个。

        负值表示在 anchor 之前。这个函数是"每 N 个交易日调一次仓"的唯一依据 ——
        **周期永远从固定锚点算，不从上次运行算**，见 `schedule.RebalanceSpec`。
        """
        if day == anchor:
            return 0
        if day > anchor:
            return self.count(anchor, day) - 1
        return -(self.count(day, anchor) - 1)

    # -- 序列化 ----------------------------------------------------------- #

    def to_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "holidays": [d.isoformat() for d in sorted(self.holidays)],
            "extra_open_days": [d.isoformat() for d in sorted(self.extra_open_days)],
            "first_day": self.first_day.isoformat() if self.first_day else None,
            "verified_until": (self.verified_until.isoformat()
                               if self.verified_until else None),
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, object]) -> "TradingCalendar":
        try:
            holidays = frozenset(date.fromisoformat(str(x)) for x in
                                 (raw.get("holidays") or []))
            extra = frozenset(date.fromisoformat(str(x)) for x in
                              (raw.get("extra_open_days") or []))
        except (TypeError, ValueError) as exc:
            raise CalendarError(f"日历文件里的日期解析失败：{exc}") from exc
        first = raw.get("first_day")
        until = raw.get("verified_until")
        return cls(
            name=str(raw.get("name") or ""),
            holidays=holidays,
            extra_open_days=extra,
            first_day=date.fromisoformat(str(first)) if first else None,
            verified_until=date.fromisoformat(str(until)) if until else None,
            source=str(raw.get("source") or ""),
        )

    @property
    def unhealthy(self) -> List[str]:
        """
        这份日历自己就有毛病的地方。运维面直接显示，不去猜它想表达什么。
        """
        bad: List[str] = []
        if self.verified_until is None:
            bad.append("没有覆盖区间：任何日期都会被判为未校准")
        if self.first_day and self.verified_until:
            days = (self.verified_until - self.first_day).days + 1
            if days > 3650:
                bad.append(f"覆盖区间长达 {days} 天，确认不是手滑")
        both = self.holidays & self.extra_open_days
        if both:
            bad.append(f"{len(both)} 天同时被标为休市和开市：" +
                       ", ".join(d.isoformat() for d in sorted(both)[:5]))
        return bad


# --------------------------------------------------------------------------- #
#  从真实数据构建
# --------------------------------------------------------------------------- #

def build_from_observed(observed: Iterable[date], *, name: str,
                        start: Optional[date] = None,
                        end: Optional[date] = None,
                        source: str = "") -> TradingCalendar:
    """
    已知"哪些天真的有行情" → 一份校准过的日历。

    这是本模块唯一可信的构建路径：节假日不是抄来的，是**推出来的** ——
    区间内的工作日里，没有行情的那些就是休市日。

    `start` / `end` 不传时取 `observed` 的首尾。区间端点必须落在有数据的日子上，
    否则会把"数据还没开始"误判成"休市"（这是最容易搞出来的一个假节假日）。
    """
    days = sorted(set(observed))
    if not days:
        raise CalendarError("没有观察到任何交易日，构建不出日历")
    lo = start or days[0]
    hi = end or days[-1]
    if hi < lo:
        raise CalendarError(f"区间起点 {lo} 晚于终点 {hi}")
    inside = {d for d in days if lo <= d <= hi}
    if not inside:
        raise CalendarError(f"[{lo}, {hi}] 内没有任何行情日期")

    holidays: Set[date] = set()
    for d in weekdays_between(lo, hi):
        if d not in inside:
            holidays.add(d)
    extra_open = {d for d in inside if d.weekday() >= 5}

    return TradingCalendar(
        name=name, holidays=frozenset(holidays),
        extra_open_days=frozenset(extra_open),
        first_day=min(inside),
        verified_until=max(inside),
        source=source or f"从 {len(inside)} 个真实行情日期推出",
    )


# --------------------------------------------------------------------------- #
#  对账：规则日历 vs 真实数据
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class CalendarAudit:
    """
    "这套规则在已知区间里错了几天"的完整答案。

    两个方向分开报，因为它们后果完全不同：

    * `errors_rule_open_data_closed`：规则说开市、实际没交易 → 多算。
      A 股**只可能**出现这一种（周末从不开市）。后果是那一拍算不出信号，
      被 fail-closed 拦住，晚一天调仓。安全但会漏。
    * `errors_rule_closed_data_open`：规则说休市、实际有交易 → 漏算。
      这只会在日历配置错误时出现，后果是**整整一次调仓被跳过**，
      而且没人会发现。这一类必须为 0，否则这份日历不许用。
    """

    name: str
    checked_days: int
    observed_days: int
    errors_rule_open_data_closed: Tuple[date, ...] = ()
    errors_rule_closed_data_open: Tuple[date, ...] = ()
    first_day: Optional[date] = None
    last_day: Optional[date] = None

    @property
    def error_rate(self) -> float:
        return (len(self.errors_rule_open_data_closed) / self.checked_days
                if self.checked_days else 0.0)

    @property
    def silent_skips(self) -> int:
        """会静默漏掉调仓的天数 —— 这类必须为 0。"""
        return len(self.errors_rule_closed_data_open)

    @property
    def ok(self) -> bool:
        return self.silent_skips == 0

    def to_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "checked_days": self.checked_days,
            "observed_days": self.observed_days,
            "first_day": self.first_day.isoformat() if self.first_day else None,
            "last_day": self.last_day.isoformat() if self.last_day else None,
            "error_rate": round(self.error_rate, 6),
            "silent_skips": self.silent_skips,
            "rule_open_data_closed": [d.isoformat() for d in
                                      self.errors_rule_open_data_closed],
            "rule_closed_data_open": [d.isoformat() for d in
                                      self.errors_rule_closed_data_open],
            "ok": self.ok,
        }

    def summary(self) -> str:
        return (f"{self.name}: 在 {self.first_day}~{self.last_day} 的 "
                f"{self.checked_days} 天里，规则日历错判 "
                f"{len(self.errors_rule_open_data_closed)} 天（多算，会漏一拍）"
                f"，漏判 {self.silent_skips} 天（静默跳过，必须为 0）")


def audit_rules_against_data(cal: TradingCalendar,
                             observed: Iterable[date],
                             *, start: Optional[date] = None,
                             end: Optional[date] = None) -> CalendarAudit:
    """
    拿真实数据检验 `cal` 的规则错在哪几天。

    为什么必须做这一步：说"未来按规则推算"很容易，但没有任何东西能证明
    这个推算靠得住。而历史数据恰恰能证明 —— 把同一套规则套在已知区间上，
    数一数它错几天。这个数字就是"未来每个月大概会错几天"的最好估计。
    """
    days = sorted(set(observed))
    if not days:
        raise CalendarError("没有观察到任何交易日，无法对账")
    lo = start or days[0]
    hi = end or days[-1]
    inside = {d for d in days if lo <= d <= hi}
    span = weekdays_between(lo, hi)

    open_but_closed: List[date] = []
    for d in span:
        if d in inside:
            continue
        if cal.is_trading_day(d):
            open_but_closed.append(d)

    # 规则说休市但实际有交易：只可能是 cal 把它塞进了 holidays。
    closed_but_open: List[date] = sorted(d for d in inside
                                         if not cal.is_trading_day(d))
    return CalendarAudit(
        name=cal.name, checked_days=len(span), observed_days=len(inside),
        errors_rule_open_data_closed=tuple(open_but_closed),
        errors_rule_closed_data_open=tuple(closed_but_open),
        first_day=lo, last_day=hi,
    )


# --------------------------------------------------------------------------- #
#  落盘
# --------------------------------------------------------------------------- #

def save_calendar(cal: TradingCalendar, path: str) -> Dict[str, object]:
    """原子替换写入。半写会被下一次读当成损坏文件，那种故障最难查。"""
    payload = cal.to_dict()
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".cal-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return payload


def load_calendar(path: str) -> Optional[TradingCalendar]:
    """
    读回日历。文件缺失或损坏都返回 None，不抛错 ——
    调度器需要能启动并报告「没有校准过的日历」，而不是整个崩掉。
    """
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return TradingCalendar.from_dict(raw)
    except CalendarError:
        return None
