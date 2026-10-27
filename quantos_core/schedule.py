"""
策略排期 —— 把 `run_once()` 从"要人手动敲"变成"到点自己跑"，并保证它**只跑一次**。

为什么这一层不能省
==================
B8 结束时 `StrategyRuntime.run_once()` 已经能把整条链跑通，但它是被脚本手工调用的。
手工调用的东西不叫系统，叫工具 —— 没人敲就永远不跑，而"没人敲"这件事
在报表上看不出来：净值曲线是一条平的直线，看起来像"策略没信号"。

于是这一层要回答三个问题，而且每个都必须是可执行的事实，不是纪律要求：

    1. 什么时候该跑？        → `RebalanceSpec.due_on()`，按**交易日**数，从**固定锚点**算
    2. 跑过一次没有？        → `ScheduleStore` 落盘，重启后还在
    3. 那一拍到底跑没跑成？  → claim → run → complete，三段都有落盘

三个最容易做错的地方
====================
**一、周期从"上次运行"算 → 永久漂移。**
`next = last_run + 20 个交易日` 看起来自然，实际上：某天因为数据缺失晚跑了一天，
之后**每一次都晚一天**，而且永远不会自己纠回来。一年之后回测的"20 日"和实盘的
"20 日"已经错开十几个交易日，而两边都觉得自己是对的。
所以周期一律 `anchor + k × N`：漏跑只是少跑一次，下一个到期点不受影响。

**二、用自然日当交易日。**
20 个自然日里永远混进 4-6 个周末，实际间隔在 20~32 天之间飘。

**三、收盘前用当日收盘价算信号。**
这是最安静的一种前视偏差：回测里"用当日收盘价决定、次日开盘成交"是对的，
实盘如果在 14:00 跑，拿到的"当日收盘价"其实是 14:00 的价格，
而回测里那个数字是 15:00 的。两边都叫 close，差的却是真金白银。
`signal_day()` 因此规定：**当日没收盘 + 结算延迟之前，一律用上一个交易日**。

关于"跑没跑成"的三种状态
========================
跑完落盘是一个**有洞**的流程：进程可能在下单之后、落盘之前被 kill。
这种情况下"重跑"就是重复下单，"不重跑"就是漏一次调仓。两个都不对。

所以引入 claim：

    claim(signal_day) → 落盘 → 执行 → complete → 落盘

启动时看到"已 claim 但没 complete"的记录，判定为 **IN_DOUBT**，
不自动重跑，也不自动放过 —— 报告里点名，要求人工 `resolve()`。
这和 OMS 的 `needs_reconciliation` 是同一条原则：
**把不确定变成可发现的错误，而不是猜一个方向然后祈祷。**

反过来，如果 runner 明确知道自己还没碰任何有副作用的东西（比如行情不够、
参数校验没过），它抛 `PreflightAbort` —— 调度器据此认定"确定没跑"，
清掉 claim 并允许下一拍重试。区分「确定没跑」和「不知道跑没跑」是这一层的核心，
和 B5 错误分类里 `ambiguous` 的语义完全一致。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from .calendar import CalendarError, TradingCalendar
from .contracts import utcnow

__all__ = [
    "ScheduleError", "PreflightAbort", "Decision", "RebalanceSpec", "RunOutcome",
    "ScheduleEntry", "ScheduleStore", "ItemReport", "TickReport",
    "RebalanceScheduler", "load_tz_or_fail", "signal_day_for", "status_snapshot",
]


class ScheduleError(RuntimeError):
    """排期配置或状态本身不对 —— 不是"这一拍不跑"，是"这套排期不能用"。"""


class PreflightAbort(RuntimeError):
    """
    runner 在产生任何副作用**之前**主动放弃（行情不足、参数没过、日历不覆盖…）。

    调度器据此判定"确定没跑"，清 claim 并允许下一拍重试。
    只该在"我还什么都没动"的时候抛；已经下过单再抛这个，等于把
    重复下单的风险塞回给调度器。
    """


# --------------------------------------------------------------------------- #
#  时区
# --------------------------------------------------------------------------- #

def load_tz_or_fail(name: str, purpose: str = ""):
    """
    取时区，拿不到就报错，**绝不退化成 UTC**。

    理由同 `scheduler.load_tz`：退化之后系统不崩、日志全绿，
    只是在错误的时间交易。排期把日期算错一天，比任何一次下单失败都贵。
    """
    try:
        from zoneinfo import ZoneInfo
    except ImportError as exc:                                # pragma: no cover
        raise ScheduleError("需要 Python 3.9+ 的 zoneinfo") from exc
    try:
        return ZoneInfo(name)
    except Exception as exc:                                  # noqa: BLE001
        raise ScheduleError(
            f"{purpose or '排期'} 取不到时区 {name}：{exc}。"
            "不要用固定偏移代替 —— 夏令时会错一小时") from exc


# --------------------------------------------------------------------------- #
#  到期判定
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class RebalanceSpec:
    """
    一个策略的调仓排期。

    `anchor` 是**第一次调仓的交易日**，也是所有后续周期的计算原点。
    它一旦登记就不许静默改：改了等于把整个历史换了一个相位，
    而"我明明每周都跑"和"怎么周期对不上"都只会在半年后才发现。
    """

    strategy_id: str
    version: str
    anchor: date
    rebalance_days: int = 20
    kind: str = "rebalance"

    def __post_init__(self) -> None:
        if not self.strategy_id:
            raise ScheduleError("排期必须绑定 strategy_id")
        if not self.version:
            raise ScheduleError(f"{self.strategy_id}: 排期必须绑定版本号")
        if self.kind not in ("rebalance", "signal"):
            raise ScheduleError(
                f"{self.strategy_id}: kind={self.kind!r} 不支持（rebalance / signal）")
        if int(self.rebalance_days) < 1:
            raise ScheduleError(
                f"{self.strategy_id}: rebalance_days 至少为 1（0 等于每根 bar 都调）")

    @property
    def key(self) -> str:
        return f"{self.strategy_id}@{self.version}"

    def due_on(self, cal: TradingCalendar, day: date) -> bool:
        """
        这一天是不是调仓日。

        `kind="signal"`：每个交易日都给信号（策略自己决定要不要动）。
        `kind="rebalance"`：只有 `anchor + k×N` 那些交易日。
        """
        if not cal.is_trading_day(day):
            return False
        if self.kind == "signal":
            return True
        offset = cal.offset_from(self.anchor, day)
        return offset >= 0 and offset % int(self.rebalance_days) == 0

    def nth_due(self, cal: TradingCalendar, k: int) -> date:
        """第 k 个调仓日（k=0 就是 anchor 本身）。"""
        if k < 0:
            raise ScheduleError(f"{self.strategy_id}: k={k} 不能为负")
        return self.anchor if k == 0 else cal.shift(self.anchor, k * int(self.rebalance_days))

    def next_due(self, cal: TradingCalendar, after: date) -> date:
        """
        `after` 之后（不含）的第一个调仓日。

        注意这里是**从 anchor 重新算**的，不是 `after + N` ——
        这就是"漏跑不漂移"的全部实现。
        """
        if self.kind == "signal":
            return cal.next_trading_day(after)
        offset = cal.offset_from(self.anchor, after)
        if offset < 0:
            return self.anchor
        return self.nth_due(cal, offset // int(self.rebalance_days) + 1)

    def cycles_between(self, cal: TradingCalendar, start: date, end: date) -> int:
        """
        (start, end] 之间有几个调仓日。用来回答"漏了几拍"。

        只报数不补跑：补跑历史信号意味着今天用今天的价格去补昨天的单，
        那是把一次漏跑换成一次错价成交。要补历史请走 `replay`。
        """
        if end <= start:
            return 0
        if self.kind == "signal":
            return cal.count(cal.next_trading_day(start), end)
        count = 0
        cur = self.next_due(cal, start)
        guard = 0
        while cur <= end:
            count += 1
            cur = self.next_due(cal, cur)
            guard += 1
            if guard > 5000:
                raise ScheduleError(f"{self.strategy_id}: 周期计算没有收敛，检查配置")
        return count

    def to_dict(self) -> Dict[str, Any]:
        return {"strategy_id": self.strategy_id, "version": self.version,
                "anchor": self.anchor.isoformat(),
                "rebalance_days": int(self.rebalance_days), "kind": self.kind,
                "key": self.key}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RebalanceSpec":
        return cls(
            strategy_id=str(raw["strategy_id"]), version=str(raw["version"]),
            anchor=date.fromisoformat(str(raw["anchor"])),
            rebalance_days=int(raw.get("rebalance_days", 20)),
            kind=str(raw.get("kind", "rebalance")),
        )


# --------------------------------------------------------------------------- #
#  一次运行的产物
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class RunOutcome:
    """runner 的返回值。字段不多，但每个都要能让运维一眼判断这一拍干了什么。"""

    detail: str = ""
    signals: int = 0
    submitted: int = 0
    fills: int = 0
    rejected: int = 0
    errors: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {"detail": self.detail, "signals": self.signals,
                "submitted": self.submitted, "fills": self.fills,
                "rejected": self.rejected, "errors": self.errors}

    def summary(self) -> str:
        return (f"信号 {self.signals} / 下单 {self.submitted} / 成交 {self.fills} / "
                f"被拒 {self.rejected} / 异常 {self.errors}")


# --------------------------------------------------------------------------- #
#  状态
# --------------------------------------------------------------------------- #

class Decision(str, Enum):
    RAN = "ran"
    WOULD_RUN = "would_run"                        # dry-run
    SKIPPED_NOT_DUE = "skipped_not_due"
    SKIPPED_ALREADY_RAN = "skipped_already_ran"
    SKIPPED_CLOSED = "skipped_closed"
    REFUSED_NO_CALENDAR = "refused_no_calendar"
    REFUSED_IN_DOUBT = "refused_in_doubt"
    FAILED = "failed"                              # 确定没跑，可重试
    FAILED_IN_DOUBT = "failed_in_doubt"            # 不知道跑没跑，要人看


@dataclass
class ScheduleEntry:
    """
    一个策略的运行状态。**这份东西必须能活过一次重启**，否则幂等是假的。

    `claim_day` 非空表示"这一天已经开工但没收尾"。它有两种来源：
    进程在跑的时候被 kill（真的不确定），或者 `mark_in_doubt()` 手工标。
    两种都必须由人清 —— 自动清等于自动猜。
    """

    spec: RebalanceSpec
    last_run_day: Optional[date] = None
    last_run_at: Optional[datetime] = None
    last_decision: str = ""
    last_detail: str = ""
    last_outcome: Dict[str, Any] = field(default_factory=dict)
    runs: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    claim_day: Optional[date] = None
    claim_at: Optional[datetime] = None
    total_fills: int = 0

    # -- 派生 ------------------------------------------------------------ #

    @property
    def in_doubt(self) -> bool:
        return self.claim_day is not None

    @property
    def healthy(self) -> bool:
        """连续失败 3 次算不健康；in_doubt 直接算不健康 —— 那是"可能重复下单"。"""
        return not self.in_doubt and self.consecutive_failures < 3

    def to_dict(self) -> Dict[str, Any]:
        return {
            **self.spec.to_dict(),
            "last_run_day": self.last_run_day.isoformat() if self.last_run_day else None,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_decision": self.last_decision,
            "last_detail": self.last_detail,
            "last_outcome": dict(self.last_outcome),
            "runs": self.runs,
            "failures": self.failures,
            "consecutive_failures": self.consecutive_failures,
            "claim_day": self.claim_day.isoformat() if self.claim_day else None,
            "claim_at": self.claim_at.isoformat() if self.claim_at else None,
            "total_fills": self.total_fills,
            "in_doubt": self.in_doubt,
            "healthy": self.healthy,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ScheduleEntry":
        def _day(value):
            return date.fromisoformat(str(value)) if value else None

        def _ts(value):
            return datetime.fromisoformat(str(value)) if value else None

        return cls(
            spec=RebalanceSpec.from_dict(raw),
            last_run_day=_day(raw.get("last_run_day")),
            last_run_at=_ts(raw.get("last_run_at")),
            last_decision=str(raw.get("last_decision") or ""),
            last_detail=str(raw.get("last_detail") or ""),
            last_outcome=dict(raw.get("last_outcome") or {}),
            runs=int(raw.get("runs") or 0),
            failures=int(raw.get("failures") or 0),
            consecutive_failures=int(raw.get("consecutive_failures") or 0),
            claim_day=_day(raw.get("claim_day")),
            claim_at=_ts(raw.get("claim_at")),
            total_fills=int(raw.get("total_fills") or 0),
        )


class ScheduleStore:
    """
    排期状态的落盘。整体重写 + 原子替换。

    为什么不是追加日志：排期状态是**当前事实**（上次跑到哪天、有没有卡在 claim），
    不是事件流。追加日志要靠回放才能知道当前状态，而"回放错了"本身又是一种故障。
    事件流在 `events.py` 里已经有了，那是审计用的，不是这里用的。
    """

    VERSION = 1

    def __init__(self, path: str) -> None:
        self.path = path
        self._entries: Dict[str, ScheduleEntry] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.isfile(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        for item in (data.get("entries") or []):
            entry = ScheduleEntry.from_dict(item)
            self._entries[entry.spec.key] = entry

    def _flush(self) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        payload = {"version": self.VERSION,
                   "entries": [e.to_dict() for e in self.entries()]}
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".sched-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # -- 读 -------------------------------------------------------------- #

    def __len__(self) -> int:
        return len(self._entries)

    def entries(self) -> List[ScheduleEntry]:
        return [self._entries[k] for k in sorted(self._entries)]

    def get(self, key: str) -> Optional[ScheduleEntry]:
        return self._entries.get(key)

    def find(self, strategy_id: str,
             version: Optional[str] = None) -> Optional[ScheduleEntry]:
        if version is not None:
            return self._entries.get(f"{strategy_id}@{version}")
        matches = [e for k, e in self._entries.items()
                   if e.spec.strategy_id == strategy_id]
        if len(matches) > 1:
            raise ScheduleError(
                f"{strategy_id} 有 {len(matches)} 个版本的排期"
                f"（{sorted(e.spec.version for e in matches)}），必须指定版本")
        return matches[0] if matches else None

    # -- 写 -------------------------------------------------------------- #

    def register(self, spec: RebalanceSpec, cal: TradingCalendar, *,
                 rebase: bool = False) -> ScheduleEntry:
        """
        登记一个排期。已存在时**只接受完全一致的配置**。

        为什么不允许"顺手改一下 anchor"：改了之后 `last_run_day` 还留着，
        于是"上次跑到哪天"和"下次哪天到期"来自两套相位，没人能解释那张表。
        真要换相位走 `rebase=True`，它会把运行历史清零并写明原因。
        """
        if not cal.is_trading_day(spec.anchor):
            raise ScheduleError(
                f"{spec.strategy_id}: anchor {spec.anchor} 不是交易日 —— "
                "锚点落在休市日会让整个周期平移，且没人看得出来")
        gap = cal.gap_for(spec.anchor)
        if gap is not None:
            raise ScheduleError(
                f"{spec.strategy_id}: anchor {spec.anchor} 未校准（{gap.reason}）")

        existing = self._entries.get(spec.key)
        if existing is not None:
            if (existing.spec.anchor == spec.anchor
                    and int(existing.spec.rebalance_days) == int(spec.rebalance_days)
                    and existing.spec.kind == spec.kind):
                return existing
            if not rebase:
                raise ScheduleError(
                    f"{spec.key} 已有排期（anchor={existing.spec.anchor}, "
                    f"每 {existing.spec.rebalance_days} 个交易日），"
                    f"新配置是（anchor={spec.anchor}, 每 {spec.rebalance_days} 个）。"
                    "改相位会让历史与未来对不上；确认要换请传 rebase=True。")
            existing.spec = spec
            existing.last_run_day = None
            existing.last_run_at = None
            existing.last_decision = "rebased"
            existing.last_detail = "排期相位被显式重置"
            existing.claim_day = None
            existing.claim_at = None
            self._flush()
            return existing

        entry = ScheduleEntry(spec=spec)
        self._entries[spec.key] = entry
        self._flush()
        return entry

    # -- claim ----------------------------------------------------------- #

    def claim(self, entry: ScheduleEntry, day: date, now: datetime) -> None:
        """开跑之前先落盘。**顺序不能反** —— 反了就等于没有 claim。"""
        entry.claim_day = day
        entry.claim_at = now
        self._flush()

    def complete(self, entry: ScheduleEntry, day: date, now: datetime,
                 outcome: RunOutcome, decision: Decision,
                 detail: str = "") -> None:
        entry.claim_day = None
        entry.claim_at = None
        entry.last_run_day = day
        entry.last_run_at = now
        entry.last_decision = decision.value
        entry.last_detail = detail or outcome.detail
        entry.last_outcome = outcome.to_dict()
        entry.total_fills += int(outcome.fills)
        entry.runs += 1
        entry.consecutive_failures = 0
        self._flush()

    def fail(self, entry: ScheduleEntry, decision: Decision, detail: str,
             *, keep_claim: bool) -> None:
        entry.last_decision = decision.value
        entry.last_detail = detail
        entry.failures += 1
        entry.consecutive_failures += 1
        if not keep_claim:
            entry.claim_day = None
            entry.claim_at = None
        self._flush()

    def resolve(self, strategy_id: str, version: Optional[str], *,
                actor: str, reason: str, decision: str = "cleared") -> ScheduleEntry:
        """
        人工处理一条 in_doubt 记录。

        两种结论都要写清楚，因为它们对"那天到底有没有下单"给出相反的判断：

            cleared      确认那天没成交 → 清 claim，下一次到期正常跑
            assumed_done 确认那天已经成交 → 记成已跑，不再重跑
        """
        if not str(actor).startswith("human:"):
            raise ScheduleError(
                f"清 in_doubt 必须是人工操作（actor 要以 human: 开头，收到 {actor!r}）—— "
                "这一条决定『那天有没有下单』，AI 不能替人拍板")
        if len(str(reason).strip()) < 6:
            raise ScheduleError("清 in_doubt 必须写至少 6 个字的理由")
        if decision not in ("cleared", "assumed_done"):
            raise ScheduleError(f"decision 只能是 cleared / assumed_done，收到 {decision!r}")

        entry = self.find(strategy_id, version)
        if entry is None:
            raise ScheduleError(f"没有 {strategy_id} 的排期记录")
        if not entry.in_doubt:
            raise ScheduleError(f"{entry.spec.key} 当前没有 in_doubt 记录，无需处理")

        claimed_day = entry.claim_day
        entry.claim_day = None
        entry.claim_at = None
        entry.last_decision = f"resolved:{decision}"
        entry.last_detail = f"{actor}: {reason}"
        entry.consecutive_failures = 0
        if decision == "assumed_done":
            entry.last_run_day = claimed_day
            entry.last_run_at = utcnow()
        self._flush()
        return entry


# --------------------------------------------------------------------------- #
#  报告
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ItemReport:
    strategy_id: str
    version: str
    decision: Decision
    reason: str = ""
    signal_day: Optional[date] = None
    missed_periods: int = 0
    outcome: Optional[RunOutcome] = None

    @property
    def ran(self) -> bool:
        return self.decision is Decision.RAN

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy_id": self.strategy_id, "version": self.version,
            "decision": self.decision.value, "reason": self.reason,
            "signal_day": self.signal_day.isoformat() if self.signal_day else None,
            "missed_periods": self.missed_periods,
            "outcome": self.outcome.to_dict() if self.outcome else None,
        }


@dataclass(frozen=True)
class TickReport:
    """
    一拍的结果。

    `items` 里每一个策略都有一条记录，包括"没到期"和"已跑过" ——
    只列跑了的那些，运维就没法回答"为什么今天什么都没发生"。
    """

    as_of: datetime
    calendar_name: str
    signal_day: Optional[date]
    items: Tuple[ItemReport, ...] = ()
    refused: bool = False
    calendar_gap: str = ""

    def _by(self, *decisions: Decision) -> List[ItemReport]:
        return [i for i in self.items if i.decision in decisions]

    @property
    def ran(self) -> List[ItemReport]:
        return self._by(Decision.RAN)

    @property
    def would_run(self) -> List[ItemReport]:
        return self._by(Decision.WOULD_RUN)

    @property
    def needs_attention(self) -> List[ItemReport]:
        return self._by(Decision.REFUSED_IN_DOUBT, Decision.FAILED_IN_DOUBT)

    @property
    def failed(self) -> List[ItemReport]:
        return self._by(Decision.FAILED, Decision.FAILED_IN_DOUBT)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "calendar": self.calendar_name,
            "signal_day": self.signal_day.isoformat() if self.signal_day else None,
            "refused": self.refused,
            "calendar_gap": self.calendar_gap,
            "items": [i.to_dict() for i in self.items],
            "ran": [i.strategy_id for i in self.ran],
            "needs_attention": [i.strategy_id for i in self.needs_attention],
        }

    def summary(self) -> str:
        if self.refused:
            return f"{self.as_of:%Y-%m-%d %H:%M}: 拒绝运行 —— {self.calendar_gap}"
        head = (f"{self.as_of:%Y-%m-%d %H:%M}: 数据日 {self.signal_day} · "
                f"跑了 {len(self.ran)} 个 / 待跑 {len(self.would_run)} 个 / "
                f"共 {len(self.items)} 个")
        if self.needs_attention:
            head += ("　⚠️ 需人工：" +
                     ", ".join(i.strategy_id for i in self.needs_attention))
        return head


# --------------------------------------------------------------------------- #
#  调度器
# --------------------------------------------------------------------------- #

RunnerFn = Callable[[RebalanceSpec, date], Optional[RunOutcome]]


# --------------------------------------------------------------------------- #
#  纯函数视图（运维面只读，不需要也不应该拿到一个能 tick 的对象）
# --------------------------------------------------------------------------- #

def signal_day_for(calendar: TradingCalendar, now: datetime, *,
                   close_at: time = time(15, 0),
                   tz_name: str = "Asia/Shanghai",
                   settle_minutes: int = 30) -> date:
    """
    `now` 这一刻能拿到收盘价的**最近一个交易日**。

    规则：本地时间过了「收盘 + 结算延迟」才算今天收盘，否则退回上一个交易日。
    这条挡的是"14:00 拿 14:00 的价格当收盘价"——那是最安静的一种前视偏差：
    回测里 close 是 15:00 的，实盘里那个 14:00 的数字也叫 close，两边看起来一样。
    """
    if now.tzinfo is None:
        raise ScheduleError("now 必须是带时区的时间")
    if settle_minutes < 0:
        raise ScheduleError("settle_minutes 不能为负")
    tz = load_tz_or_fail(tz_name, "排期")
    local = now.astimezone(tz)
    cutoff = (datetime.combine(local.date(), close_at, tzinfo=tz)
              + timedelta(minutes=int(settle_minutes)))
    candidate = local.date() if local >= cutoff else local.date() - timedelta(days=1)
    if calendar.is_trading_day(candidate):
        return candidate
    return calendar.prev_trading_day(candidate)


def status_snapshot(calendar: Optional[TradingCalendar],
                    store: Optional[ScheduleStore], *,
                    now: Optional[datetime] = None,
                    close_at: time = time(15, 0),
                    tz_name: str = "Asia/Shanghai",
                    settle_minutes: int = 30,
                    clock: Callable[[], datetime] = utcnow) -> Dict[str, Any]:
    """
    只读快照。缺日历、有 in_doubt、连续失败都要如实体现，**不挂绿灯**
    （B7 T-703 的同一条规矩：宁可显示缺口，也不显示假绿灯）。

    单独抽成函数而不是方法，是为了让运维面在结构上就拿不到 `tick()` ——
    一个只读的看板不该握着"能推进一拍"的对象。
    """
    now = now or clock()
    if calendar is None:
        return {"as_of": now.isoformat(), "available": False,
                "reason": "没有校准过的交易日历（先跑 quantos_schedule.py calendar --build）",
                "entries": [], "entry_count": 0, "needs_attention": [],
                "healthy": False}
    if store is None:
        return {"as_of": now.isoformat(), "available": False,
                "reason": "未接入排期状态存储", "entries": [],
                "entry_count": 0, "needs_attention": [], "healthy": False}

    day = signal_day_for(calendar, now, close_at=close_at, tz_name=tz_name,
                         settle_minutes=settle_minutes)
    gap = calendar.gap_for(day)
    rows: List[Dict[str, Any]] = []
    attention: List[str] = []
    for entry in store.entries():
        spec = entry.spec
        try:
            next_day = spec.next_due(calendar, day)
            nxt, nxt_verified = next_day.isoformat(), calendar.covered(next_day)
        except CalendarError:
            nxt, nxt_verified = None, False
        rows.append({
            **entry.to_dict(),
            "due_today": spec.due_on(calendar, day),
            "next_due": nxt,
            "next_due_verified": nxt_verified,
            "offset": calendar.offset_from(spec.anchor, day),
        })
        if not entry.healthy:
            attention.append(spec.strategy_id)

    first, until = calendar.span
    return {
        "as_of": now.isoformat(),
        "available": True,
        "calendar": {
            "name": calendar.name,
            "first_day": first.isoformat() if first else None,
            "verified_until": until.isoformat() if until else None,
            "source": calendar.source,
            "holidays": len(calendar.holidays),
            "warnings": calendar.unhealthy,
        },
        "signal_day": day.isoformat(),
        "signal_day_covered": gap is None,
        "calendar_gap": gap.reason if gap else "",
        "close_at": close_at.isoformat(timespec="minutes"),
        "timezone": tz_name,
        "entry_count": len(rows),
        "entries": rows,
        "needs_attention": sorted(set(attention)),
        "healthy": not attention and gap is None,
    }



class RebalanceScheduler:
    """
    按交易日排期、按拍推进的调度器。

    它自己不定时 —— 定时由外部（systemd timer / cron / while 循环）负责，
    `tick(now)` 的语义是"现在该干什么就干什么"。这样测试可以用任意时间戳驱动，
    不需要真的等到收盘。
    """

    def __init__(self, *, calendar: TradingCalendar, store: ScheduleStore,
                 runner: RunnerFn, close_at: time = time(15, 0),
                 tz_name: str = "Asia/Shanghai", settle_minutes: int = 30,
                 clock: Callable[[], datetime] = utcnow) -> None:
        if settle_minutes < 0:
            raise ScheduleError("settle_minutes 不能为负")
        self.calendar = calendar
        self.store = store
        self.runner = runner
        self.close_at = close_at
        self.tz = load_tz_or_fail(tz_name, "排期")
        self.tz_name = tz_name
        self.settle = timedelta(minutes=int(settle_minutes))
        self._clock = clock

    # -- 时间 → 数据日 --------------------------------------------------- #

    def signal_day(self, now: datetime) -> date:
        """`now` 这一刻能拿到收盘价的最近一个交易日。见 `signal_day_for`。"""
        return signal_day_for(self.calendar, now, close_at=self.close_at,
                              tz_name=self.tz_name,
                              settle_minutes=int(self.settle.total_seconds() // 60))

    # -- 登记 ------------------------------------------------------------ #

    def register(self, spec: RebalanceSpec, *, rebase: bool = False) -> ScheduleEntry:
        return self.store.register(spec, self.calendar, rebase=rebase)

    # -- 推进一拍 -------------------------------------------------------- #

    def tick(self, now: Optional[datetime] = None, *, dry_run: bool = False,
             only: Optional[Iterable[str]] = None) -> TickReport:
        now = now or self._clock()
        entries = self.store.entries()
        if only:
            wanted = {str(x) for x in only}
            entries = [e for e in entries if e.spec.strategy_id in wanted]
            missing = wanted - {e.spec.strategy_id for e in entries}
            if missing:
                raise ScheduleError(f"没有这些策略的排期：{sorted(missing)}")

        day = self.signal_day(now)
        gap = self.calendar.gap_for(day)
        if gap is not None:
            items = tuple(ItemReport(e.spec.strategy_id, e.spec.version,
                                     Decision.REFUSED_NO_CALENDAR, gap.reason, day)
                          for e in entries)
            return TickReport(now, self.calendar.name, day, items,
                              refused=True, calendar_gap=gap.reason)

        reports: List[ItemReport] = []
        for entry in entries:
            reports.append(self._tick_one(entry, day, now, dry_run))
        return TickReport(now, self.calendar.name, day, tuple(reports))

    def _tick_one(self, entry: ScheduleEntry, day: date, now: datetime,
                  dry_run: bool) -> ItemReport:
        spec = entry.spec
        sid, ver = spec.strategy_id, spec.version

        # 1. 上一拍卡在半路 —— 不许重跑，也不许当它没发生。
        if entry.in_doubt:
            return ItemReport(
                sid, ver, Decision.REFUSED_IN_DOUBT,
                f"{entry.claim_day} 那一拍已开工但没落结果。可能已经下过单，"
                f"需要人工确认后 resolve()", day)

        # 2. 还没到周期。
        if not spec.due_on(self.calendar, day):
            nxt = spec.next_due(self.calendar, day)
            return ItemReport(sid, ver, Decision.SKIPPED_NOT_DUE,
                              f"下次到期 {nxt}", day)

        # 3. 这一天已经跑过 —— 幂等。重启、重放 tick、手滑敲两次都走这里。
        if entry.last_run_day is not None and entry.last_run_day >= day:
            return ItemReport(sid, ver, Decision.SKIPPED_ALREADY_RAN,
                              f"{entry.last_run_day} 已跑过（第 {entry.runs} 次）", day)

        missed = 0
        if entry.last_run_day is not None:
            missed = spec.cycles_between(self.calendar, entry.last_run_day, day)

        if dry_run:
            return ItemReport(sid, ver, Decision.WOULD_RUN,
                              f"到期；{entry.last_run_day or '从未跑过'} → {day}",
                              day, missed)

        # 4. 先 claim 再跑。顺序反了就等于没有 claim。
        self.store.claim(entry, day, now)
        try:
            outcome = self.runner(spec, day) or RunOutcome()
        except PreflightAbort as exc:
            # 明确"还没碰任何有副作用的东西" → 清 claim，下一拍可重试。
            self.store.fail(entry, Decision.FAILED,
                            f"预检放弃（未执行）：{exc}", keep_claim=False)
            return ItemReport(sid, ver, Decision.FAILED, str(exc), day, missed)
        except BaseException as exc:                          # noqa: BLE001
            # 不知道跑到哪一步了 → **保留 claim**，要求人工确认。
            # 自动清掉等于替人决定"那天没下单"，而这个假设一旦错了就是重复下单。
            self.store.fail(entry, Decision.FAILED_IN_DOUBT,
                            f"{type(exc).__name__}: {exc}", keep_claim=True)
            return ItemReport(sid, ver, Decision.FAILED_IN_DOUBT,
                              f"{type(exc).__name__}: {exc}", day, missed)

        detail = outcome.detail
        if missed > 1:
            detail = (detail + f"；跳过 {missed - 1} 拍未补跑"
                      "（补历史信号会在今天用今天的价格成交）").strip("；")
        self.store.complete(entry, day, now, outcome, Decision.RAN, detail)
        return ItemReport(sid, ver, Decision.RAN, detail, day, missed, outcome)

    # -- 运维视图 -------------------------------------------------------- #

    def status(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """只读快照。实现见 `status_snapshot`（运维面直接用它，不必持有本对象）。"""
        return status_snapshot(
            self.calendar, self.store, now=now, close_at=self.close_at,
            tz_name=self.tz_name,
            settle_minutes=int(self.settle.total_seconds() // 60),
            clock=self._clock)
