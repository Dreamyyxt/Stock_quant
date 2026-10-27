"""
QuantOS 2.0 — 策略实例注册表与审计（T-303）
==========================================

对应需求 R-S01..S06（策略像软件一样管生命周期）与 R-O05（审计可回放）。

解决的问题：策略「写完 Python 就上线」，以及暂停/恢复只在控制台点一下、
进程重启后状态丢失。本模块做三件事：

  1. **实例化**：每个策略是带版本、阶段、配额、灰度权重的实例，不是一个脚本名；
  2. **准入闸门**：RESEARCH → BACKTEST → PAPER → LIVE 逐级推进，
     LIVE 必须同时具备回测报告、人工授权、模拟盘观察期，禁止跳级；
  3. **审计闭环**：每一次状态变更（含被拒的）都 append 到只追加日志，
     可用同一个日志**回放出**最终状态，且 digest 一致。

设计约束：
  - 纯 stdlib、无网络；
  - 审计先落盘再改内存：宁可操作失败，也不能出现「改了但没记录」；
  - 灰度权重不是标签：`policies()` 返回的配额已乘以 canary_weight，
    所以 20% 灰度就是真的只用 20% 的钱；
  - 本模块**不碰基金级权限**（那是 RiskEngine 的事），只设策略级状态。

绝不做的事：
  - 不自己恢复 LIVE：从 PAUSED 恢复必须显式确认 live=True；
  - 不允许 AI 或自动流程直接调 promote 到 LIVE（actor 必须是人）；
  - 不因回测指标好看就自动晋级。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from quantos_core.capital_allocator import StrategyPolicy
from quantos_core.contracts import (
    ContractViolation, D, StrategyRunState, q, utcnow,
)
from quantos_core.events import EventStore, MemoryEventStore, canonical_json

# --------------------------------------------------------------------------- #
#  异常
# --------------------------------------------------------------------------- #


class StrategyRegistryError(ContractViolation):
    """注册表操作被拒。所有拒绝都会先写审计再抛出。"""


class UnknownStrategy(StrategyRegistryError):
    pass


class IllegalStageTransition(StrategyRegistryError):
    pass


class PromotionBlocked(StrategyRegistryError):
    """准入条件不满足（缺回测报告 / 缺授权 / 观察期不足）。"""


class AutomationForbidden(StrategyRegistryError):
    """自动流程（AI/调度器）试图做需要人工授权的动作。"""


# --------------------------------------------------------------------------- #
#  生命周期
# --------------------------------------------------------------------------- #


class StrategyStage(str, Enum):
    RESEARCH = "RESEARCH"
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    LIVE = "LIVE"


#: 允许的阶段迁移。未列出的迁移一律拒绝（与订单状态机同一个思路）。
STAGE_TRANSITIONS: Dict[StrategyStage, frozenset] = {
    StrategyStage.RESEARCH: frozenset({StrategyStage.BACKTEST}),
    StrategyStage.BACKTEST: frozenset({StrategyStage.PAPER, StrategyStage.RESEARCH}),
    StrategyStage.PAPER:   frozenset({StrategyStage.LIVE, StrategyStage.BACKTEST}),
    StrategyStage.LIVE:    frozenset({StrategyStage.PAPER, StrategyStage.RESEARCH}),
}

#: 自动化 actor 白名单前缀以外的动作需要人工。判断方式见 `_is_human`。
AUTOMATION_ACTORS = ("ai:", "scheduler:", "auto:", "risk:")

#: 模拟盘最短观察期（天）。
DEFAULT_MIN_PAPER_DAYS = 7


def _is_human(actor: str) -> bool:
    actor = (actor or "").strip()
    if not actor:
        return False
    return not actor.lower().startswith(AUTOMATION_ACTORS)


# --------------------------------------------------------------------------- #
#  审计日志
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AuditRecord:
    """一条审计记录。字段刻意保持扁平，便于落库与页面展示。"""

    seq: int
    audit_id: str
    ts: datetime
    actor: str
    action: str
    strategy_id: str
    from_value: str
    to_value: str
    result: str                 # applied / rejected
    reason: str
    correlation_id: str = ""
    detail: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "seq": self.seq,
            "audit_id": self.audit_id,
            "ts": self.ts.isoformat(),
            "actor": self.actor,
            "action": self.action,
            "strategy_id": self.strategy_id,
            "from": self.from_value,
            "to": self.to_value,
            "result": self.result,
            "reason": self.reason,
            "correlation_id": self.correlation_id,
            "detail": dict(self.detail),
        }


class AuditLog:
    """
    只追加审计日志。

    底层复用 `EventStore`（JSONL/SQLite），因此天然具备：单调 seq、
    event_id 幂等、尾部半行自愈、`EventStoreFull` 显式失败。
    """

    EVENT_TYPE = "audit"

    def __init__(self, store: Optional[EventStore] = None):
        self.store = store if store is not None else MemoryEventStore("audit")

    def append(self, *, actor: str, action: str, strategy_id: str,
               from_value: str, to_value: str, result: str, reason: str,
               correlation_id: str = "",
               detail: Optional[Mapping[str, str]] = None) -> AuditRecord:
        payload = {
            "actor": actor,
            "action": action,
            "strategy_id": strategy_id,
            "from": from_value,
            "to": to_value,
            "result": result,
            "reason": reason,
            "correlation_id": correlation_id,
            "detail": dict(detail or {}),
        }
        env, _is_new = self.store.append(self.EVENT_TYPE, payload)
        return self._to_record(env)

    def _to_record(self, env) -> AuditRecord:
        p = env.payload
        return AuditRecord(
            seq=env.seq,
            audit_id=env.event_id,
            ts=env.ts,
            actor=str(p.get("actor", "")),
            action=str(p.get("action", "")),
            strategy_id=str(p.get("strategy_id", "")),
            from_value=str(p.get("from", "")),
            to_value=str(p.get("to", "")),
            result=str(p.get("result", "")),
            reason=str(p.get("reason", "")),
            correlation_id=str(p.get("correlation_id", "")),
            detail=dict(p.get("detail") or {}),
        )

    def records(self, limit: Optional[int] = None,
                strategy_id: Optional[str] = None) -> List[AuditRecord]:
        out: List[AuditRecord] = []
        for env in self.store.read():
            if env.event_type != self.EVENT_TYPE:
                continue
            rec = self._to_record(env)
            if strategy_id and rec.strategy_id != strategy_id:
                continue
            out.append(rec)
        if limit is not None:
            out = out[-limit:]
        return out

    def digest(self) -> str:
        """审计日志整体摘要。任何一条被篡改都会变。"""
        return self.store.digest()

    def count(self) -> int:
        return len(self.records())


# --------------------------------------------------------------------------- #
#  策略实例
# --------------------------------------------------------------------------- #


@dataclass
class StrategyInstance:
    """
    一个可部署的策略版本。

    `stage` 回答「它能不能碰真钱」，`run_state` 回答「它现在是否被允许下单」。
    两者是正交的：LIVE 策略可以被 PAUSED；PAPER 策略永远是 PAPER。
    """

    strategy_id: str
    version: str
    venue: str
    policy: StrategyPolicy
    stage: StrategyStage = StrategyStage.PAPER
    run_state: StrategyRunState = StrategyRunState.ACTIVE

    # -- 准入材料（R-S05） ---------------------------------------------- #
    code_commit: str = ""
    data_version: str = ""
    cost_model_version: str = ""
    backtest_report_id: str = ""
    approved_by: str = ""
    approved_at: Optional[datetime] = None
    paper_started_at: Optional[datetime] = None

    # -- 发布控制 -------------------------------------------------------- #
    #: 灰度权重 0<w<=1。LIVE 且 w<1 时只按比例给额度。
    canary_weight: Decimal = Decimal(1)
    updated_at: datetime = field(default_factory=utcnow)
    last_change: str = ""

    def __post_init__(self) -> None:
        if not self.strategy_id:
            raise StrategyRegistryError("strategy_id 不能为空")
        if not self.version:
            raise StrategyRegistryError("version 不能为空")
        if self.policy.strategy_id != self.strategy_id:
            raise StrategyRegistryError(
                f"policy.strategy_id={self.policy.strategy_id!r} 与实例 "
                f"{self.strategy_id!r} 不一致")
        w = D(self.canary_weight)
        if not (0 < w <= 1):
            raise StrategyRegistryError(f"canary_weight 必须在 (0,1]，收到 {w}")
        self.canary_weight = w

    @property
    def is_live(self) -> bool:
        return self.stage is StrategyStage.LIVE

    @property
    def tradable(self) -> bool:
        """能否进入本轮交易候选。PAUSED 一律不进。"""
        return self.run_state is not StrategyRunState.PAUSED

    def effective_quota_pct(self) -> Decimal:
        """灰度后的实际配额。PAPER/RESEARCH 阶段不真下单，但保留配额供模拟。"""
        return q(D(self.policy.quota_pct) * D(self.canary_weight), 6)

    def to_dict(self) -> Dict:
        return {
            "strategy_id": self.strategy_id,
            "version": self.version,
            "venue": self.venue,
            "stage": self.stage.value,
            "run_state": self.run_state.value,
            "quota_pct": str(D(self.policy.quota_pct)),
            "canary_weight": str(D(self.canary_weight)),
            "effective_quota_pct": str(self.effective_quota_pct()),
            "max_gross_weight": str(D(self.policy.max_gross_weight)),
            "code_commit": self.code_commit,
            "data_version": self.data_version,
            "cost_model_version": self.cost_model_version,
            "backtest_report_id": self.backtest_report_id,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
            "paper_started_at": (self.paper_started_at.isoformat()
                                 if self.paper_started_at else None),
            "updated_at": self.updated_at.isoformat(),
            "last_change": self.last_change,
            "tradable": self.tradable,
        }


# --------------------------------------------------------------------------- #
#  注册表
# --------------------------------------------------------------------------- #


class StrategyRegistry:
    """
    策略实例的唯一真相来源。

    所有写操作都是「先审计、后生效、失败也留痕」：
    拒绝路径同样写一条 `result=rejected`，否则运维永远查不到「谁想在
    HALTED 期间恢复策略但被拒了」。
    """

    def __init__(self, audit: Optional[AuditLog] = None,
                 clock: Callable[[], datetime] = utcnow,
                 min_paper_days: int = DEFAULT_MIN_PAPER_DAYS):
        self.audit = audit if audit is not None else AuditLog()
        self._clock = clock
        self.min_paper_days = int(min_paper_days)
        self._instances: Dict[str, StrategyInstance] = {}

    # -- 读 -------------------------------------------------------------- #

    def __len__(self) -> int:
        return len(self._instances)

    def __iter__(self) -> Iterator[StrategyInstance]:
        for sid in sorted(self._instances):
            yield self._instances[sid]

    def get(self, strategy_id: str) -> StrategyInstance:
        inst = self._instances.get(strategy_id)
        if inst is None:
            raise UnknownStrategy(f"未注册的策略：{strategy_id}")
        return inst

    def find(self, strategy_id: str) -> Optional[StrategyInstance]:
        return self._instances.get(strategy_id)

    def ids(self) -> List[str]:
        return sorted(self._instances)

    def live(self) -> List[StrategyInstance]:
        return [i for i in self if i.stage is StrategyStage.LIVE]

    def policies(self, include_paused: bool = False,
                 include_stages: Optional[Tuple[StrategyStage, ...]] = None
                 ) -> List[StrategyPolicy]:
        """
        交给 Capital Allocator 的配额列表。

        两道过滤缺一不可：

        1. **PAUSED 默认不在列表里**：没有额度就没有目标，也就不会下单。
           同时 `apply_to()` 会把 PAUSED 写进 RiskState，让净额层冻结其存量。
        2. **RESEARCH / BACKTEST 永远不在列表里**（B8 补）。这条以前是缺的 ——
           `tradable` 只看 run_state 不看 stage，于是一个刚导入的研究策略
           会带着配额被交进资金分配器。只要有调度器按这份列表决定跑谁，
           研究策略就会开始下单，而它的准入材料一件都没交。
           `effective_quota_pct()` 的注释写着「RESEARCH 不真下单，但保留配额供模拟」，
           这句话得靠这里兑现，不能只写在注释里。
        """
        allowed = include_stages or (StrategyStage.PAPER, StrategyStage.LIVE)
        out: List[StrategyPolicy] = []
        for inst in self:
            if inst.stage not in allowed:
                continue
            if not include_paused and not inst.tradable:
                continue
            out.append(replace(inst.policy,
                               quota_pct=inst.effective_quota_pct()))
        return out

    # -- 写：注册 -------------------------------------------------------- #

    def register(self, inst: StrategyInstance, *, actor: str,
                 replace_existing: bool = False) -> StrategyInstance:
        """
        注册/替换实例。

        替换规则：旧版本已在 LIVE 时禁止直接替换 —— 必须先降级到 PAPER，
        否则等于「热换代码」绕过准入。这是有意为之的摩擦。
        """
        existing = self._instances.get(inst.strategy_id)
        if existing is not None and not replace_existing:
            self._reject(actor, "register", inst.strategy_id,
                         existing.version, inst.version,
                         f"策略已存在（{existing.version}）")
        if existing is not None and existing.is_live:
            self._reject(actor, "register", inst.strategy_id,
                         existing.version, inst.version,
                         "旧版本处于 LIVE，必须先降级到 PAPER 再替换")

        before = existing.version if existing else ""
        self._instances[inst.strategy_id] = inst
        inst.updated_at = self._clock()
        inst.last_change = "register"
        self._record(actor, "register", inst.strategy_id, before, inst.version,
                     "applied", "注册/替换实例",
                     detail={"stage": inst.stage.value,
                             "run_state": inst.run_state.value,
                             "quota_pct": str(D(inst.policy.quota_pct))})
        return inst

    # -- 写：运行状态 ---------------------------------------------------- #

    def pause(self, strategy_id: str, reason: str, *, actor: str) -> StrategyInstance:
        """
        暂停新增仓位。存量仓位**保留**，由净额层冻结（B3 解掉整标的阻断），
        不做市价清仓 —— 清仓是另一个需要显式授权的动作。
        """
        inst = self.get(strategy_id)
        if inst.run_state is StrategyRunState.PAUSED:
            self._reject(actor, "pause", strategy_id,
                         inst.run_state.value, inst.run_state.value, "已经是 PAUSED")
        return self._set_run_state(inst, StrategyRunState.PAUSED, reason, actor,
                                  "pause")

    def degrade(self, strategy_id: str, reason: str, *,
                actor: str) -> StrategyInstance:
        """降级为仅减仓：允许降低风险，不允许加仓/反手。"""
        inst = self.get(strategy_id)
        if inst.run_state is StrategyRunState.REDUCE_ONLY:
            self._reject(actor, "degrade", strategy_id,
                         inst.run_state.value, inst.run_state.value,
                         "已经是 REDUCE_ONLY")
        return self._set_run_state(inst, StrategyRunState.REDUCE_ONLY, reason,
                                  actor, "degrade")

    def resume(self, strategy_id: str, reason: str, *, actor: str,
               live: bool = False) -> StrategyInstance:
        """
        恢复交易。

        `live=True` 表示「明确知道这会把真钱交给它」。LIVE 阶段策略不传
        live 就拒绝 —— 防止在页面上误点一下把停掉的实盘策略放出来。
        """
        inst = self.get(strategy_id)
        if inst.is_live and not live:
            self._reject(actor, "resume", strategy_id, inst.run_state.value,
                         StrategyRunState.ACTIVE.value,
                         "LIVE 策略恢复必须显式 live=true 二次确认")
        if inst.run_state is StrategyRunState.ACTIVE:
            self._reject(actor, "resume", strategy_id,
                         inst.run_state.value, inst.run_state.value,
                         "已经是 ACTIVE")
        return self._set_run_state(inst, StrategyRunState.ACTIVE, reason, actor,
                                  "resume")

    def _set_run_state(self, inst: StrategyInstance, to_state: StrategyRunState,
                       reason: str, actor: str, action: str) -> StrategyInstance:
        before = inst.run_state
        inst.run_state = to_state
        inst.updated_at = self._clock()
        inst.last_change = action
        self._record(actor, action, inst.strategy_id, before.value, to_state.value,
                     "applied", reason)
        return inst

    # -- 写：阶段推进 ---------------------------------------------------- #

    def promote(self, strategy_id: str, to_stage: StrategyStage, *,
                actor: str, backtest_report_id: str = "",
                code_commit: str = "", data_version: str = "",
                cost_model_version: str = "") -> StrategyInstance:
        """
        推进生命周期。这是唯一能进入 LIVE 的入口。

        三道闸门（R-S05），任一不满足即拒：
          1. 迁移本身合法（不许跳级）；
          2. 材料齐全（回测报告 / 代码提交 / 数据版本 / 成本模型版本）；
          3. 进 LIVE 必须人工授权，且模拟盘观察期已满。
        """
        inst = self.get(strategy_id)
        before = inst.stage
        allowed = STAGE_TRANSITIONS.get(before, frozenset())
        if to_stage not in allowed:
            self._reject(actor, "promote", strategy_id, before.value, to_stage.value,
                         f"非法阶段迁移（允许：{sorted(s.value for s in allowed)}）",
                         exc=IllegalStageTransition)

        if backtest_report_id:
            inst.backtest_report_id = backtest_report_id
        if code_commit:
            inst.code_commit = code_commit
        if data_version:
            inst.data_version = data_version
        if cost_model_version:
            inst.cost_model_version = cost_model_version

        if to_stage in (StrategyStage.PAPER, StrategyStage.LIVE):
            missing = [name for name, value in (
                ("backtest_report_id", inst.backtest_report_id),
                ("code_commit", inst.code_commit),
                ("data_version", inst.data_version),
                ("cost_model_version", inst.cost_model_version),
            ) if not value]
            if missing:
                self._reject(actor, "promote", strategy_id, before.value,
                             to_stage.value, f"缺少准入材料：{','.join(missing)}",
                             exc=PromotionBlocked)

        if to_stage is StrategyStage.LIVE:
            # 闸门 1：自动化通道（AI/调度器）只能提议，不能自己放上实盘。
            if not _is_human(actor):
                self._reject(actor, "promote", strategy_id, before.value,
                             to_stage.value,
                             "LIVE 授权只能由人工执行（自动化 actor 被拒）",
                             exc=AutomationForbidden)
            # 闸门 2：必须真的走过 PAPER，不能直接空降。
            if inst.paper_started_at is None:
                self._reject(actor, "promote", strategy_id, before.value,
                             to_stage.value,
                             "从未进入 PAPER，不能直接晋级 LIVE",
                             exc=PromotionBlocked)
            # 闸门 3：模拟盘观察期。
            waited = self._clock() - inst.paper_started_at
            if waited < timedelta(days=self.min_paper_days):
                self._reject(actor, "promote", strategy_id, before.value,
                             to_stage.value,
                             f"模拟盘观察期不足：{waited.days}/"
                             f"{self.min_paper_days} 天",
                             exc=PromotionBlocked)
            inst.approved_by = actor
            inst.approved_at = self._clock()

        if to_stage is StrategyStage.PAPER:
            inst.paper_started_at = self._clock()

        inst.stage = to_stage
        inst.updated_at = self._clock()
        inst.last_change = f"promote:{to_stage.value}"
        # 降级到 RESEARCH 时，运行状态不能还停在「可交易」。
        if to_stage is StrategyStage.RESEARCH and \
                inst.run_state is StrategyRunState.ACTIVE:
            inst.run_state = StrategyRunState.PAUSED

        self._record(actor, "promote", strategy_id, before.value, to_stage.value,
                     "applied", "生命周期推进",
                     detail={"backtest_report_id": inst.backtest_report_id,
                             "code_commit": inst.code_commit,
                             "approved_by": inst.approved_by})
        return inst

    # -- 写：配额与灰度 -------------------------------------------------- #

    def set_canary(self, strategy_id: str, weight: Decimal, *, actor: str,
                   reason: str = "") -> StrategyInstance:
        inst = self.get(strategy_id)
        w = D(weight)
        if not (0 < w <= 1):
            self._reject(actor, "set_canary", strategy_id, str(inst.canary_weight),
                         str(w), "canary_weight 必须在 (0,1]")
        before = inst.canary_weight
        inst.canary_weight = w
        inst.updated_at = self._clock()
        inst.last_change = "set_canary"
        self._record(actor, "set_canary", strategy_id, str(before), str(w),
                     "applied", reason or "调整灰度权重")
        return inst

    def set_quota(self, strategy_id: str, quota_pct: Decimal, *, actor: str,
                  reason: str = "") -> StrategyInstance:
        inst = self.get(strategy_id)
        value = D(quota_pct)
        if value < 0 or value > 1:
            self._reject(actor, "set_quota", strategy_id,
                         str(inst.policy.quota_pct), str(value),
                         "quota_pct 必须在 [0,1]（占总 NAV 比例）")
        before = D(inst.policy.quota_pct)
        inst.policy = replace(inst.policy, quota_pct=value)
        inst.updated_at = self._clock()
        inst.last_change = "set_quota"
        self._record(actor, "set_quota", strategy_id, str(before), str(value),
                     "applied", reason or "调整策略配额",
                     detail={"effective_quota_pct": str(inst.effective_quota_pct())})
        return inst

    def retire(self, strategy_id: str, reason: str, *, actor: str) -> None:
        """下线：先降级到 RESEARCH 并 PAUSED，再从候选池移除。审计记录保留。"""
        inst = self.get(strategy_id)
        before = f"{inst.stage.value}/{inst.run_state.value}"
        # 先落审计再删内存：append 失败时不能出现「移除了但没记录」。
        self._record(actor, "retire", strategy_id, before, "REMOVED", "applied", reason)
        inst.stage = StrategyStage.RESEARCH
        inst.run_state = StrategyRunState.PAUSED
        inst.updated_at = self._clock()
        inst.last_change = "retire"
        del self._instances[strategy_id]

    # -- 与风控对接 ------------------------------------------------------ #

    def apply_to(self, risk_state) -> None:
        """
        把策略级状态同步进 RiskState。

        只写 `strategy_status`，不动 `permission_level`：策略暂停不该影响
        其它策略，基金级熔断由 RiskEngine 决定。
        """
        for inst in self:
            risk_state.set_strategy(inst.strategy_id, inst.run_state)

    # -- 审计回放 -------------------------------------------------------- #

    def replay_from_audit(self) -> Dict[str, Dict[str, str]]:
        """
        只用审计日志重建「策略 → {stage, run_state}」。

        用于校验：注册表被绕过（有人直接改内存/配置文件）时，回放结果会
        与当前状态不一致 —— 这是发现「非审计变更」的唯一手段。
        """
        restored: Dict[str, Dict[str, str]] = {}
        for env in self.audit.store.read():
            if env.event_type != AuditLog.EVENT_TYPE:
                continue
            p = env.payload
            if str(p.get("result")) != "applied":
                continue
            sid = str(p.get("strategy_id", ""))
            if not sid:
                continue
            cur = restored.setdefault(sid, {"stage": "", "run_state": ""})
            action = str(p.get("action", ""))
            to_value = str(p.get("to", ""))
            if action == "promote":
                cur["stage"] = to_value
            elif action in ("pause", "degrade", "resume"):
                cur["run_state"] = to_value
            elif action == "retire":
                cur["stage"] = "RESEARCH"
                cur["run_state"] = "PAUSED"
            elif action == "register":
                cur["stage"] = str(p.get("detail", {}).get("stage", cur["stage"]))
                cur["run_state"] = str(p.get("detail", {}).get("run_state",
                                                               cur["run_state"]))
        return restored

    def verify_against_audit(self) -> List[str]:
        """返回不一致的条目（空列表 = 当前状态与审计日志完全对得上）。"""
        restored = self.replay_from_audit()
        problems: List[str] = []
        for sid, inst in self._instances.items():
            want = restored.get(sid)
            if want is None:
                problems.append(f"{sid}: 审计日志里没有该策略的注册记录")
                continue
            if want["stage"] and want["stage"] != inst.stage.value:
                problems.append(
                    f"{sid}: stage 当前 {inst.stage.value}，审计回放 {want['stage']}")
            if want["run_state"] and want["run_state"] != inst.run_state.value:
                problems.append(
                    f"{sid}: run_state 当前 {inst.run_state.value}，"
                    f"审计回放 {want['run_state']}")
        for sid in restored:
            if sid not in self._instances:
                problems.append(f"{sid}: 审计里有记录，但注册表中已不存在")
        return problems

    # -- 内部 ------------------------------------------------------------ #

    def _record(self, actor: str, action: str, strategy_id: str,
                from_value: str, to_value: str, result: str, reason: str,
                detail: Optional[Mapping[str, str]] = None) -> AuditRecord:
        return self.audit.append(
            actor=actor, action=action, strategy_id=strategy_id,
            from_value=str(from_value), to_value=str(to_value),
            result=result, reason=reason, detail=detail)

    def _reject(self, actor: str, action: str, strategy_id: str,
                from_value: str, to_value: str, reason: str,
                exc: type = StrategyRegistryError):
        """拒绝路径同样写审计，再抛指定异常类型。"""
        self._record(actor, action, strategy_id, from_value, to_value,
                     "rejected", reason)
        raise exc(f"{strategy_id}: {reason}")

    def snapshot(self) -> List[Dict]:
        return [inst.to_dict() for inst in self]


__all__ = [
    "StrategyStage", "STAGE_TRANSITIONS", "DEFAULT_MIN_PAPER_DAYS",
    "StrategyRegistryError", "UnknownStrategy", "IllegalStageTransition",
    "PromotionBlocked", "AutomationForbidden",
    "AuditRecord", "AuditLog", "StrategyInstance", "StrategyRegistry",
]
