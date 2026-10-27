"""
QuantOS 2.0 — 策略灰度与回滚（T-602）
====================================

对应需求 R-S07 与契约附录 F。解决的问题只有一句话：

    **新版本怎么在没有全额风险的情况下试跑，并且能一个动作退回去。**

四条设计原则，每条都来自一类会真出事的情形：

1. **按版本分流，不按"再发一份意图"分流。**
   `StrategyIntent` 本身带 `strategy_version`，所以路由是在出口处按版本缩权，
   不是让两个版本各自发一份满权重意图再指望下游去重。后者只要有一处下游漏了
   去重，仓位就会变成两倍 —— 而"两倍仓位"不会报错，只会让回撤也变成两倍。

2. **路由不重新归一化。**
   灰度 10% 时，如果稳定版这一轮没信号，总权重是 10%，**不是** 100%。
   把空出来的份额补给在跑的那个版本，等于让灰度版实际承担全额风险，
   却仍然显示"只灰度了 10%"。宁可少做一单，不要多担一份风险。

3. **reduce_only 的意图不缩放。**
   把"清仓"按 0.1 缩放，结果是只清了 10% —— 方向恰好反了。所以减仓类意图
   一律全额通过，并且同标的只取幅度最大的一份，不叠加。

4. **自动化只能降不能升。**
   `auto_adjust()` 可以因为异常把权重砍到 0，但任何自动化路径都**没有**
   提权重的分支。提权重、promote 成稳定版一律要求 `human:` 前缀的 actor。
   这和 `live_gate` 是同一个道理：安全方向的自动化免摩擦，加风险方向的自动化
   一律不存在。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from quantos_core.contracts import (
    ContractViolation, D, StrategyIntent, q, utcnow,
)

#: 灰度权重下限。低于它就不是"灰度"而是"噪音"，不如直接回滚。
MIN_CANARY_WEIGHT = Decimal("0.01")


class CanaryError(ContractViolation):
    """灰度相关的所有错误基类。"""


class UnknownVersion(CanaryError):
    """意图带了路由表里没有的版本号。**必须抛错**，不能默默放行或默默丢弃。"""


class AutomationForbidden(CanaryError):
    """自动化试图提高风险敞口。"""


class RolloutState(str, Enum):
    SINGLE = "single"            # 只有稳定版
    CANARY = "canary"            # 稳定版 + 灰度版并行
    ROLLED_BACK = "rolled_back"  # 灰度已停，稳定版独占，灰度版留档
    PROMOTED = "promoted"        # 灰度版已扶正

    @property
    def is_dual(self) -> bool:
        return self is RolloutState.CANARY


def _is_human(actor: str) -> bool:
    return isinstance(actor, str) and actor.startswith("human:") and len(actor) > 6


@dataclass
class Cohort:
    """一个策略的发布队列：稳定版 + 可选灰度版 + 各自的流量份额。"""

    strategy_id: str
    venue: str = ""
    stable_version: str = ""
    canary_version: Optional[str] = None
    weight: Decimal = Decimal(0)
    state: RolloutState = RolloutState.SINGLE
    opened_at: Optional[datetime] = None
    updated_at: datetime = field(default_factory=utcnow)
    note: str = ""
    peak_weight: Decimal = Decimal(0)
    #: 曾经接流量、现在不该再接的版本（换稳定版时被替下的、扶正后退役的）。
    #: 留着它才能区分「已知但已下线」和「从没见过」—— 前者丢弃并记录，
    #: 后者抛错。混为一谈的后果是要么停摆，要么放野版本进来。
    retired: set = field(default_factory=set)

    def __post_init__(self) -> None:
        self.weight = D(self.weight)
        self.peak_weight = D(self.peak_weight)

    # -- 份额 ------------------------------------------------------------ #

    def shares(self) -> Dict[str, Decimal]:
        """
        版本 → 流量份额。总和恒为 1（SINGLE 时）或 1（CANARY 时稳定+灰度）。

        注意：份额总和恒为 1 说的是"**如果两个版本都有信号**"。某个版本这一轮
        没发意图时，它的份额**不会被**转移给另一个版本 —— 见 `route()`。
        """
        if self.state is RolloutState.CANARY and self.canary_version:
            return {self.stable_version: Decimal(1) - self.weight,
                    self.canary_version: self.weight}
        return {self.stable_version: Decimal(1)}

    def known_versions(self) -> set:
        """登记过的全部版本（含已退役的）。用于区分"退役"和"没见过"。"""
        out = {self.stable_version} | set(self.retired)
        if self.canary_version:
            out.add(self.canary_version)
        return {v for v in out if v}

    def version_share(self, version: str) -> Decimal:
        """
        版本 → 份额。**已知但当前不接流量的版本返回 0**，从未见过的版本抛错。

        为什么不全抛错：回滚之后灰度版的进程可能还活着、还在发意图。
        如果抛错，这个残留进程会把整轮路由打断 —— 所有策略一起停摆，
        于是一个"忘了杀进程"演变成"基金不交易"。丢弃 + 记账才是对的。
        """
        shares = self.shares()
        if version in shares:
            return shares[version]
        if version in self.known_versions():
            return Decimal(0)
        raise UnknownVersion(
            f"策略 {self.strategy_id} 的路由表里没有版本 {version!r}；"
            f"当前只知道 {sorted(self.known_versions())}。"
            f"新版本必须先 open_canary() 才能接流量，否则它下的单无人认账")

    def to_dict(self) -> Dict:
        return {
            "strategy_id": self.strategy_id,
            "venue": self.venue,
            "state": self.state.value,
            "stable_version": self.stable_version,
            "canary_version": self.canary_version,
            "weight": str(q(self.weight, 6)),
            "peak_weight": str(q(self.peak_weight, 6)),
            "shares": {k: str(q(v, 6)) for k, v in self.shares().items()},
            "opened_at": self.opened_at.isoformat() if self.opened_at else None,
            "updated_at": self.updated_at.isoformat(),
            "note": self.note,
        }


@dataclass(frozen=True)
class RoutingReport:
    """一次路由的结果。数字都留着，方便运维对着账看。"""

    strategy_id: str
    state: RolloutState
    emitted: Mapping[str, int]          # version -> 发出的意图数
    routed: int
    dropped: int
    weight_by_symbol: Mapping[str, Decimal]   # symbol -> 路由后 |目标权重| 之和
    notes: Tuple[str, ...] = ()

    @property
    def versions_seen(self) -> Tuple[str, ...]:
        return tuple(sorted(self.emitted))

    def to_dict(self) -> Dict:
        return {
            "strategy_id": self.strategy_id,
            "state": self.state.value,
            "emitted": dict(self.emitted),
            "routed": self.routed,
            "dropped": self.dropped,
            "weight_by_symbol": {k: str(q(v, 6)) for k, v in self.weight_by_symbol.items()},
            "notes": list(self.notes),
        }


class CanaryRouter:
    """
    版本级流量路由 + 回滚。纯内存 + 审计日志（无 I/O、无定时器）。

    路由粒度为「策略 × 版本 × 标的」：同一策略的两个版本对同一标的的意图会被
    按份额缩放后同时下传，由 `capital_allocator` 按同一 `strategy_id` 求和 ——
    净额层因此看到的敞口仍然是"一个策略的额度"，不需要任何改动。
    """

    def __init__(self, clock=utcnow) -> None:
        self._clock = clock
        self._cohorts: Dict[str, Cohort] = {}
        self._journal: List[Dict] = []

    # -- 读 -------------------------------------------------------------- #

    def __len__(self) -> int:
        return len(self._cohorts)

    def get(self, strategy_id: str) -> Optional[Cohort]:
        return self._cohorts.get(strategy_id)

    def cohorts(self) -> List[Cohort]:
        return [self._cohorts[k] for k in sorted(self._cohorts)]

    def dual(self) -> List[Cohort]:
        return [c for c in self.cohorts() if c.state.is_dual]

    def journal(self, limit: Optional[int] = None) -> List[Dict]:
        return self._journal[-limit:] if limit else list(self._journal)

    def digest(self) -> str:
        blob = json.dumps(self._journal, sort_keys=True, ensure_ascii=False,
                          default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def to_dict(self) -> Dict:
        return {
            "cohorts": [c.to_dict() for c in self.cohorts()],
            "dual": len(self.dual()),
            "journal_count": len(self._journal),
            "digest": self.digest(),
        }

    # -- 写：开灰度 ------------------------------------------------------- #

    def register_stable(self, strategy_id: str, version: str, *, venue: str = "",
                        actor: str = "human:system", reason: str = "") -> Cohort:
        """登记一个"只有稳定版"的策略。重复登记同一版本是幂等的。"""
        if not strategy_id or not version:
            raise CanaryError("strategy_id 与 version 都不能为空")
        exist = self._cohorts.get(strategy_id)
        if exist is not None:
            if exist.stable_version == version and exist.state is not RolloutState.CANARY:
                return exist
            if exist.state is RolloutState.CANARY:
                raise CanaryError(
                    f"{strategy_id} 正在灰度（{exist.stable_version} → "
                    f"{exist.canary_version}，权重 {exist.weight}），"
                    f"不能直接换稳定版；先 rollback() 或 promote()")
            self._record(actor, "register_stable", strategy_id,
                         exist.stable_version, version, "applied", reason)
            exist.retired.add(exist.stable_version)
            exist.stable_version = version
            exist.updated_at = self._clock()
            return exist
        c = Cohort(strategy_id=strategy_id, venue=venue, stable_version=version)
        self._cohorts[strategy_id] = c
        self._record(actor, "register_stable", strategy_id, "", version, "applied",
                     reason or "登记稳定版")
        return c

    def open_canary(self, strategy_id: str, canary_version: str, *,
                    weight: Decimal = Decimal("0.1"), actor: str,
                    reason: str = "") -> Cohort:
        """
        开灰度。**必须人工**，因为这一步把没跑过的新代码接进了真实资金。

        `weight` 上限就是 1（等于直接全量），但那样应该走 promote 的流程而不是
        这里；灰度建议 ≤ 0.2。
        """
        if not _is_human(actor):
            self._reject(actor, "open_canary", strategy_id, "-", str(weight),
                         "开灰度会把未验证的新版本接进真实资金，必须由人工执行")
        cohort = self._cohorts.get(strategy_id)
        if cohort is None:
            raise CanaryError(
                f"{strategy_id} 未登记稳定版：先 register_stable()，"
                f"否则灰度版没有可对照的基线")
        if cohort.state is RolloutState.CANARY:
            raise CanaryError(
                f"{strategy_id} 已在灰度（{cohort.canary_version} @ {cohort.weight}），"
                f"不能同时开第二个灰度版")
        if canary_version == cohort.stable_version:
            raise CanaryError(f"灰度版本与稳定版相同（{canary_version}），没有意义")
        w = D(weight)
        if not (MIN_CANARY_WEIGHT <= w <= 1):
            self._reject(actor, "open_canary", strategy_id, "-", str(w),
                         f"灰度权重必须在 [{MIN_CANARY_WEIGHT},1]，收到 {w}")

        before = f"{cohort.state.value}@{cohort.weight}"
        cohort.canary_version = canary_version
        cohort.weight = w
        cohort.peak_weight = max(D(cohort.peak_weight), w)
        cohort.state = RolloutState.CANARY
        cohort.opened_at = self._clock()
        cohort.updated_at = cohort.opened_at
        cohort.note = reason or "开灰度"
        self._record(actor, "open_canary", strategy_id, before,
                     f"canary@{w}", "applied", cohort.note,
                     detail={"canary_version": canary_version,
                             "stable_version": cohort.stable_version})
        return cohort

    # -- 写：调权重 ------------------------------------------------------- #

    def set_weight(self, strategy_id: str, weight: Decimal, *, actor: str,
                   reason: str = "") -> Cohort:
        """
        调灰度权重。**降权重（含降到 0）不需要人工**，提权重必须人工。

        这不是对称的权限设计，而是"谁承担风险"的划分：降低敞口永远可以，
        提高敞口必须有人签名。
        """
        cohort = self._cohorts.get(strategy_id)
        if cohort is None or cohort.state is not RolloutState.CANARY:
            raise CanaryError(f"{strategy_id} 当前不在灰度状态，无法调权重")
        w = D(weight)
        if not (0 <= w <= 1):
            raise CanaryError(f"灰度权重必须在 [0,1]，收到 {w}")
        if w < MIN_CANARY_WEIGHT:
            w = Decimal(0)          # 低于下限视为回滚，不留 0.5% 这种残渣
        if w > D(cohort.weight) and not _is_human(actor):
            self._reject(actor, "set_weight", strategy_id, str(cohort.weight),
                         str(w), "提高灰度权重等于扩大新版本的资金敞口，必须人工执行")
        if w == 0:
            return self.rollback(strategy_id, reason=reason or "权重降至 0",
                                 actor=actor)

        before = cohort.weight
        cohort.weight = w
        cohort.peak_weight = max(D(cohort.peak_weight), w)
        cohort.updated_at = self._clock()
        cohort.note = reason or "调整灰度权重"
        self._record(actor, "set_weight", strategy_id, str(before), str(w),
                     "applied", cohort.note)
        return cohort

    def promote(self, strategy_id: str, *, actor: str, reason: str = "") -> Cohort:
        """
        灰度版扶正。两条前置：人工执行、**权重已经跑到 1**。

        不允许从 10% 直接 promote 成稳定版 —— 那等于跳过 90% 的观察，
        而"在 10% 下没出事"和"在全额下没出事"是两件不同的事。
        """
        if not _is_human(actor):
            self._reject(actor, "promote", strategy_id, "-", "-",
                         "把灰度版扶正为稳定版必须人工执行（AI 不能自己上线）")
        cohort = self._cohorts.get(strategy_id)
        if cohort is None or cohort.state is not RolloutState.CANARY:
            raise CanaryError(f"{strategy_id} 当前不在灰度状态，无法 promote")
        if D(cohort.weight) < 1:
            self._reject(actor, "promote", strategy_id, str(cohort.weight), "1",
                         f"灰度权重只有 {cohort.weight}，必须先跑到 1 才能扶正："
                         f"10% 下没出事不代表全额下没出事")
        before = f"canary@{cohort.weight}"
        old_stable = cohort.stable_version
        cohort.retired.add(old_stable)
        cohort.stable_version = cohort.canary_version
        cohort.canary_version = None
        cohort.weight = Decimal(0)
        cohort.state = RolloutState.PROMOTED
        cohort.updated_at = self._clock()
        cohort.note = reason or "灰度扶正"
        self._record(actor, "promote", strategy_id, before,
                     f"stable={cohort.stable_version}", "applied", cohort.note,
                     detail={"previous_stable": old_stable})
        return cohort

    def rollback(self, strategy_id: str, *, reason: str, actor: str) -> Cohort:
        """
        一键回滚：灰度权重归零，稳定版独占。

        **不需要任何确认**，也不管 actor 是谁 —— 安全方向的动作为什么要拦？
        灰度版留档在 `canary_version` 里，事后复盘要用。
        """
        cohort = self._cohorts.get(strategy_id)
        if cohort is None:
            raise CanaryError(f"{strategy_id} 未登记，无可回滚")
        if cohort.state is not RolloutState.CANARY:
            raise CanaryError(
                f"{strategy_id} 当前状态 {cohort.state.value}，不在灰度中")
        before = f"canary@{cohort.weight}"
        cohort.weight = Decimal(0)
        cohort.state = RolloutState.ROLLED_BACK
        cohort.updated_at = self._clock()
        cohort.note = reason or "回滚"
        self._record(actor, "rollback", strategy_id, before,
                     f"stable={cohort.stable_version}", "applied", cohort.note,
                     detail={"rolled_back_canary": cohort.canary_version})
        return cohort

    # -- 自动化：只能降 --------------------------------------------------- #

    def auto_adjust(self, strategy_id: str, verdict: str, *, actor: str = "system:auto",
                    reason: str = "") -> Tuple[Cohort, Decimal]:
        """
        自动化按异常等级收缩灰度权重。返回 (cohort, 新权重)。

        `verdict`：
          - `"ok"`      → 不变
          - `"degrade"` → 砍半（下限 0，低于 MIN 直接归零）
          - `"abort"`   → 归零并置 ROLLED_BACK

        **没有任何分支会提高权重。** 这是刻意的：一旦允许自动化提权重，
        一个算错的健康指标就能把没验证完的版本推成全量。
        """
        cohort = self._cohorts.get(strategy_id)
        if cohort is None:
            raise CanaryError(f"{strategy_id} 未登记")
        before = D(cohort.weight)

        # 先校验判定值，再判状态。反过来写的话，「已回滚 + 判定值拼错」
        # 会被当成正常情形静默通过 —— 一个错字就让自动化悄悄失效。
        v = (verdict or "").strip().lower()
        if v not in ("ok", "healthy", "", "degrade", "abort"):
            raise CanaryError(f"未知的自动化判定 {verdict!r}（只认 ok/degrade/abort）")

        if cohort.state is not RolloutState.CANARY:
            # 非灰度状态下自动化是幂等空操作，不抛错：它会按周期反复调用，
            # 回滚之后再抛异常只会制造一堆假告警。
            self._record(actor, "auto_adjust", strategy_id, str(before), str(before),
                         "applied", reason or "非灰度状态，无操作")
            return cohort, before

        if v in ("ok", "healthy", ""):
            self._record(actor, "auto_adjust", strategy_id, str(before), str(before),
                         "applied", reason or "自动化判定健康，不调整")
            return cohort, before
        if v == "degrade":
            target = q(before / 2, 6)
            if target < MIN_CANARY_WEIGHT:
                target = Decimal(0)
        else:
            target = Decimal(0)

        if target > before:      # 防御性断言：这条永不应触发，触发即是代码错
            raise AutomationForbidden(
                f"auto_adjust 试图把权重从 {before} 提到 {target}：自动化只允许降风险")

        if target == 0:
            cohort.weight = Decimal(0)
            cohort.state = RolloutState.ROLLED_BACK
            cohort.updated_at = self._clock()
            cohort.note = reason or f"自动化 {v} 归零"
            self._record(actor, "auto_adjust", strategy_id, str(before), "0",
                         "applied", cohort.note,
                         detail={"verdict": v, "rolled_back_canary": cohort.canary_version})
            return cohort, Decimal(0)

        cohort.weight = target
        cohort.updated_at = self._clock()
        cohort.note = reason or f"自动化 {v} 降权重"
        self._record(actor, "auto_adjust", strategy_id, str(before), str(target),
                     "applied", cohort.note, detail={"verdict": v})
        return cohort, target

    # -- 核心：路由 ------------------------------------------------------- #

    def route(self, intents: Sequence[StrategyIntent]
              ) -> Tuple[List[StrategyIntent], List[RoutingReport]]:
        """
        按版本份额缩放意图的 `target_weight`。

        返回 (路由后的意图, 每个策略一份报告)。规则：

          - 不在路由表里的策略：原样通过（不做灰度管理的策略照常交易）
          - 在 CANARY 状态的策略：版本必须在 shares() 里，否则抛 `UnknownVersion`
          - 份额为 0 的版本：意图**丢弃**（记 dropped，不是缩成 0 后继续传）
          - `reduce_only_hint=True`：**不缩放**，同标的取幅度最大的一份，不叠加

        最后一条最关键：把清仓按 0.1 缩放的后果不是"少清一点"，而是"清了 10%
        却被记成一笔成功的减仓"。
        """
        out: List[StrategyIntent] = []
        by_strategy: Dict[str, Dict] = {}
        reduce_kept: Dict[Tuple[str, str, str], StrategyIntent] = {}

        for intent in intents:
            sid = intent.strategy_id
            cohort = self._cohorts.get(sid)
            report = by_strategy.setdefault(sid, {
                "emitted": {}, "routed": 0, "dropped": 0,
                "weights": {}, "notes": [], "state": (
                    cohort.state if cohort else RolloutState.SINGLE),
            })
            ver = intent.strategy_version
            report["emitted"][ver] = report["emitted"].get(ver, 0) + 1

            if cohort is None:
                share = Decimal(1)
            else:
                try:
                    share = cohort.version_share(ver)
                except UnknownVersion:
                    # 抛错前先把已收集的报告丢掉 —— 半截路由结果比不路由更危险。
                    raise UnknownVersion(
                        f"策略 {sid} 收到未知版本 {ver!r} 的意图 "
                        f"（{intent.intent_id}）；路由表只知道 "
                        f"{sorted(cohort.shares())}。"
                        f"新版本必须先 open_canary() 才能接流量")
                if share == 0:
                    report["dropped"] += 1
                    why = ("已退役" if ver in cohort.retired else "当前份额为 0")
                    out_note = f"{sid}@{ver}: {why}，意图丢弃"
                    if out_note not in report["notes"]:
                        report["notes"].append(out_note)
                    continue

            if intent.reduce_only_hint:
                # 键里**不含版本**：两个版本都喊"清仓"是同一件事，叠加会变成
                # 两倍减仓。同标的只保留幅度最大的一份。
                key = (sid, intent.symbol)
                prev = reduce_kept.get(key)
                if prev is not None and abs(D(prev.target_weight)) >= abs(D(intent.target_weight)):
                    report["dropped"] += 1
                    note = f"{sid}@{ver}/{intent.symbol}: reduce_only 重复，保留幅度最大的一份"
                    if note not in report["notes"]:
                        report["notes"].append(note)
                    continue
                routed = intent          # 原样通过，一个数字都不改
                if prev is not None:
                    try:
                        out.remove(prev)
                    except ValueError:
                        pass
                note = (f"{sid}@{ver}/{intent.symbol}: reduce_only 不缩放"
                        f"（缩放会把清仓变成半清仓）")
                if note not in report["notes"]:
                    report["notes"].append(note)
                reduce_kept[key] = routed
                out.append(routed)
                report["routed"] += 1
                report["weights"][intent.symbol] = (
                    report["weights"].get(intent.symbol, Decimal(0))
                    + abs(D(routed.target_weight)))
                continue

            scaled = D(intent.target_weight) * share
            if abs(scaled) > 1:
                # 极端情形：策略本来给满 -1..1，乘份额不会超；真超了说明份额算错。
                scaled = D(1) if scaled > 0 else D(-1)
            routed = replace(intent, target_weight=q(scaled, 8))
            out.append(routed)
            report["routed"] += 1
            report["weights"][intent.symbol] = (
                report["weights"].get(intent.symbol, Decimal(0)) + abs(scaled))

        reports = [RoutingReport(
            strategy_id=sid, state=info["state"],
            emitted=dict(info["emitted"]), routed=info["routed"],
            dropped=info["dropped"],
            weight_by_symbol={k: q(v, 8) for k, v in info["weights"].items()},
            notes=tuple(info["notes"]),
        ) for sid, info in sorted(by_strategy.items())]
        return out, reports

    # -- 审计 ------------------------------------------------------------ #

    def _record(self, actor: str, action: str, strategy_id: str,
                before: str, after: str, result: str, note: str,
                detail: Optional[Dict] = None) -> None:
        self._journal.append({
            "ts": self._clock().isoformat(),
            "actor": actor, "action": action, "strategy_id": strategy_id,
            "from": before, "to": after, "result": result, "note": note,
            "detail": detail or {},
        })

    def _reject(self, actor: str, action: str, strategy_id: str,
                before: str, after: str, reason: str) -> None:
        """先留痕再抛错：拒绝路径同样要能查到"谁想提权重被拒了"。"""
        self._record(actor, action, strategy_id, before, after, "rejected", reason)
        raise AutomationForbidden(reason)


__all__ = [
    "CanaryRouter", "Cohort", "RolloutState", "RoutingReport",
    "CanaryError", "UnknownVersion", "AutomationForbidden", "MIN_CANARY_WEIGHT",
]
