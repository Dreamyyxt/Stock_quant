"""
QuantOS 2.0 — Risk Engine（T-003）
=================================

对应需求 R-K01..K05 与契约第 3 节。三道防线 + 三级权限 + 失败默认关闭。

三条不可违反的规则：

  1. 风控**不许自己改单量**。只能 ALLOW / REJECT / REDUCE_REQUIRED（要求上游
     用原意图按 cap 重算）。否则"为什么最终下了 2104 而不是 7000"无法归因。
  2. 风控**不许静默放行**。任何拒绝都必须带 policy_key + 可读 reason + 输入值。
  3. HALTED **不许一律市价清仓**。按异常类型选择冻结/撤单/受控减仓（契约 §3）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from quantos_core.contracts import (
    D, OrderIntent, OrderState, PermissionLevel, RiskLayer, Side,
    StrategyRunState, Verdict, q, utcnow,
)

SymbolKey = Tuple[str, str]


# --------------------------------------------------------------------------- #
#  策略配置（与需求原文的 risk_policy YAML 一一对应）
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class RiskPolicy:
    """
    需求 R-K02 的 `risk_policy`。字段名保持与 YAML 一致，便于配置直接映射。

    所有 pct 字段都是「百分数」（2.0 表示 2%），不是小数，避免配置时数错零。
    """

    max_order_notional_pct: Decimal = Decimal("2.0")
    max_strategy_allocation_pct: Decimal = Decimal("20.0")
    daily_loss_pause_pct: Decimal = Decimal("1.0")
    drawdown_pause_pct: Decimal = Decimal("5.0")

    stale_market_data_action: str = "block_orders"
    position_mismatch_action: str = "block_orders"
    unknown_order_status_action: str = "reconcile"

    allow_leverage: bool = False
    withdrawal_api_permission: bool = False

    # -- 以下为契约补充项（原文未给值，按中低频口径取保守默认） -------- #
    #: 行情超过多少秒视为陈旧。
    stale_market_data_seconds: int = 120
    #: 单策略当日换手超过额度几倍视为异常（R-K01 策略级）。
    max_strategy_turnover_multiple: Decimal = Decimal("3.0")
    #: 最小下单金额，低于此值直接拒绝（避免碎单吃手续费）。
    min_order_notional: Decimal = Decimal("200")
    #: 基金级总敞口上限（占 NAV 百分比）。
    max_gross_exposure_pct: Decimal = Decimal("100.0")

    def __post_init__(self):
        for name in ("max_order_notional_pct", "max_strategy_allocation_pct",
                     "daily_loss_pause_pct", "drawdown_pause_pct",
                     "max_strategy_turnover_multiple", "min_order_notional",
                     "max_gross_exposure_pct"):
            if D(getattr(self, name)) < 0:
                raise ValueError(f"risk_policy.{name} 不能为负")


# --------------------------------------------------------------------------- #
#  风控状态（必须落盘，R-K05）
# --------------------------------------------------------------------------- #

@dataclass
class RiskState:
    permission_level: PermissionLevel = PermissionLevel.NORMAL
    halted_reasons: List[str] = field(default_factory=list)
    strategy_status: Dict[str, StrategyRunState] = field(default_factory=dict)
    drawdown_pct: Decimal = Decimal(0)
    daily_pnl_pct: Decimal = Decimal(0)
    reconciliation_broken_accounts: Set[str] = field(default_factory=set)
    symbols_with_unknown_orders: Set[str] = field(default_factory=set)

    # -- 状态迁移 -------------------------------------------------------- #

    def halt(self, reason: str) -> None:
        if reason not in self.halted_reasons:
            self.halted_reasons.append(reason)
        self.permission_level = PermissionLevel.HALTED

    def reduce_only(self, reason: str) -> None:
        if self.permission_level is not PermissionLevel.HALTED:
            self.permission_level = PermissionLevel.REDUCE_ONLY
        if reason not in self.halted_reasons:
            self.halted_reasons.append(reason)

    def resume(self, reason: str = "") -> None:
        """只能人工/显式调用。风控不会自己恢复（fail-closed 的一部分）。"""
        self.permission_level = PermissionLevel.NORMAL
        self.halted_reasons.clear()

    def set_strategy(self, strategy_id: str, state: StrategyRunState) -> None:
        self.strategy_status[strategy_id] = state

    def strategy_state(self, strategy_id: str) -> StrategyRunState:
        return self.strategy_status.get(strategy_id, StrategyRunState.ACTIVE)

    def to_dict(self) -> Dict:
        return {
            "permission_level": self.permission_level.value,
            "halted_reasons": list(self.halted_reasons),
            "strategy_status": {k: v.value for k, v in self.strategy_status.items()},
            "drawdown_pct": str(self.drawdown_pct),
            "daily_pnl_pct": str(self.daily_pnl_pct),
            "reconciliation_broken_accounts": sorted(self.reconciliation_broken_accounts),
            "symbols_with_unknown_orders": sorted(self.symbols_with_unknown_orders),
        }


# --------------------------------------------------------------------------- #
#  决策与上下文
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class RiskDecision:
    decision_id: str
    layer: RiskLayer
    policy_key: str
    verdict: Verdict
    subject_id: str
    inputs: Mapping[str, str]
    reason: str
    ts: datetime
    #: REDUCE_REQUIRED 时由上游使用；风控自己不改单量。
    cap_notional: Optional[Decimal] = None
    #: HALT 时给出的处置建议（契约 §3）。
    halt_actions: Tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW

    def to_dict(self) -> Dict:
        return {
            "decision_id": self.decision_id,
            "layer": self.layer.value,
            "policy_key": self.policy_key,
            "verdict": self.verdict.value,
            "subject_id": self.subject_id,
            "inputs": dict(self.inputs),
            "reason": self.reason,
            "ts": self.ts.isoformat(),
            "cap_notional": str(self.cap_notional) if self.cap_notional is not None else None,
            "halt_actions": list(self.halt_actions),
        }


@dataclass(frozen=True)
class RiskContext:
    """一次检查所需的全部市场/账户事实。显式传入，便于回放与测试。"""

    nav: Decimal
    now: datetime
    #: 现有持仓的带符号名义金额（BUY 正 / SELL 负）。
    position_notional: Mapping[SymbolKey, Decimal] = field(default_factory=dict)
    #: 各策略当前占用的名义金额（绝对值）。
    strategy_notional: Mapping[str, Decimal] = field(default_factory=dict)
    #: 各策略当日已成交名义金额。
    strategy_turnover: Mapping[str, Decimal] = field(default_factory=dict)
    #: 各策略的额度（用于换手率与分配比例判断）。
    strategy_capacity: Mapping[str, Decimal] = field(default_factory=dict)
    #: 行情数据年龄（秒），按 venue。
    market_data_age_sec: Mapping[str, Decimal] = field(default_factory=dict)
    #: 当前基金总敞口（绝对值合计）。
    total_gross_notional: Decimal = Decimal(0)


class HaltAction(str, Enum):
    """HALTED 时的处置动作。对应契约 §3 的表，禁止一律清仓。"""

    FREEZE_NEW_ORDERS = "freeze_new_orders"
    CANCEL_OPEN_ORDERS = "cancel_open_orders"
    CONTROLLED_REDUCE = "controlled_reduce"
    RECONCILE = "reconcile"
    MANUAL_REVIEW = "manual_review"


#: 异常类型 → 处置动作。这是 R-K04 的唯一实现口径。
HALT_PLAYBOOK: Dict[str, Tuple[HaltAction, ...]] = {
    "market_data_stale": (HaltAction.FREEZE_NEW_ORDERS, HaltAction.CANCEL_OPEN_ORDERS),
    "unknown_order_status": (HaltAction.FREEZE_NEW_ORDERS, HaltAction.RECONCILE),
    "position_mismatch": (HaltAction.FREEZE_NEW_ORDERS, HaltAction.MANUAL_REVIEW),
    "drawdown_breach": (HaltAction.CANCEL_OPEN_ORDERS, HaltAction.CONTROLLED_REDUCE),
    "daily_loss_breach": (HaltAction.FREEZE_NEW_ORDERS, HaltAction.CONTROLLED_REDUCE),
    "exchange_outage": (HaltAction.FREEZE_NEW_ORDERS, HaltAction.CANCEL_OPEN_ORDERS),
}


def _playbook_key_for(state: "RiskState") -> str:
    """从 HALT 原因反查处置剧本。归因清晰的 HALT 才能给出正确动作。"""
    reasons = " ".join(state.halted_reasons).lower()
    if "drawdown" in reasons:
        return "drawdown_breach"
    if "daily loss" in reasons:
        return "daily_loss_breach"
    if "mismatch" in reasons or "对账" in reasons:
        return "position_mismatch"
    if "unknown" in reasons:
        return "unknown_order_status"
    if "stale" in reasons:
        return "market_data_stale"
    return "exchange_outage"


# --------------------------------------------------------------------------- #
#  Risk Engine
# --------------------------------------------------------------------------- #

class RiskEngine:
    """无状态检查器 + 显式状态对象。进程化时可原样搬到独立服务（T-201）。"""

    def __init__(self, policy: Optional[RiskPolicy] = None):
        self.policy = policy or RiskPolicy()
        self._seq = 0

    # -- 基金级状态推进 -------------------------------------------------- #

    def update_fund_state(self, state: RiskState, nav: Decimal,
                          peak_nav: Optional[Decimal] = None,
                          day_start_nav: Optional[Decimal] = None) -> RiskState:
        """
        先更新回撤/当日盈亏，再决定权限级。顺序很重要：先算事实，再下结论。
        """
        nav = D(nav)
        if peak_nav and D(peak_nav) > 0:
            dd = (D(peak_nav) - nav) / D(peak_nav) * Decimal(100)
            state.drawdown_pct = q(max(Decimal(0), dd), 4)
        if day_start_nav and D(day_start_nav) > 0:
            state.daily_pnl_pct = q((nav / D(day_start_nav) - 1) * Decimal(100), 4)

        if state.drawdown_pct >= D(self.policy.drawdown_pause_pct):
            state.halt(f"drawdown {state.drawdown_pct}% >= {self.policy.drawdown_pause_pct}%")
        if state.daily_pnl_pct <= -D(self.policy.daily_loss_pause_pct):
            state.halt(f"daily loss {state.daily_pnl_pct}% <= -{self.policy.daily_loss_pause_pct}%")
        return state

    # -- 订单级主检查 ---------------------------------------------------- #

    def check_order(self, intent: OrderIntent, ctx: RiskContext,
                    state: RiskState) -> RiskDecision:
        """
        从基金级 → 策略级 → 订单级的顺序检查，第一个命中即返回。
        顺序是有意的：越上游的问题越严重，先报上游，避免下游噪音掩盖根因。
        """
        self._seq += 1
        decision_id = f"rd-{ctx.now.strftime('%Y%m%d')}-{self._seq:06d}"
        nav = D(ctx.nav)
        if nav <= 0:
            return self._reject(decision_id, RiskLayer.FUND, "nav_invalid",
                                intent, {"nav": str(nav)}, "NAV 非正，无法评估任何风险", ctx)

        # ---------- 基金级 ---------- #
        if intent.account_id in state.reconciliation_broken_accounts:
            return self._halt(decision_id, "position_mismatch_action", intent,
                              {"account": intent.account_id, "action": self.policy.position_mismatch_action},
                              f"账户 {intent.account_id} 对账不平，禁止新增风险（R-D04）",
                              ctx, "position_mismatch")

        age = D(ctx.market_data_age_sec.get(intent.venue, Decimal(0)))
        if age > D(self.policy.stale_market_data_seconds):
            if self.policy.stale_market_data_action == "block_orders":
                return self._reject(decision_id, RiskLayer.FUND, "stale_market_data_action",
                                    intent,
                                    {"age_sec": str(age), "limit": str(self.policy.stale_market_data_seconds)},
                                    f"{intent.venue} 行情已陈旧 {age}s，超过 {self.policy.stale_market_data_seconds}s 上限",
                                    ctx)

        if state.drawdown_pct >= D(self.policy.drawdown_pause_pct):
            return self._halt(decision_id, "drawdown_pause_pct", intent,
                              {"drawdown_pct": str(state.drawdown_pct),
                               "limit": str(self.policy.drawdown_pause_pct)},
                              f"回撤 {state.drawdown_pct}% 已达熔断线", ctx, "drawdown_breach")

        if state.daily_pnl_pct <= -D(self.policy.daily_loss_pause_pct):
            return self._halt(decision_id, "daily_loss_pause_pct", intent,
                              {"daily_pnl_pct": str(state.daily_pnl_pct),
                               "limit": str(self.policy.daily_loss_pause_pct)},
                              f"当日亏损 {state.daily_pnl_pct}% 已达熔断线", ctx, "daily_loss_breach")

        if intent.symbol in state.symbols_with_unknown_orders:
            return self._reject(decision_id, RiskLayer.FUND, "unknown_order_status_action",
                                intent, {"symbol": intent.symbol,
                                         "action": self.policy.unknown_order_status_action},
                                f"{intent.symbol} 存在状态未知订单，先对账再交易（不重试、不补单）",
                                ctx)

        # ---------- 权限级 ---------- #
        increasing = self._increases_exposure(intent, ctx)

        if state.permission_level is PermissionLevel.HALTED:
            if not intent.reduce_only:
                return self._halt(decision_id, "permission_level", intent,
                                  {"permission_level": state.permission_level.value,
                                   "reduce_only": "false"},
                                  "基金处于 HALTED，仅允许受控减仓通道", ctx,
                                  _playbook_key_for(state))
            # 受控减仓放行（但仍要过订单级金额检查）。

        if state.permission_level is PermissionLevel.REDUCE_ONLY and increasing:
            return self._reject(decision_id, RiskLayer.FUND, "permission_level", intent,
                                {"permission_level": state.permission_level.value},
                                "基金处于 REDUCE_ONLY，此单会增加敞口", ctx)

        # ---------- 策略级 ---------- #
        for sid in self._attributed_strategies(intent):
            sstate = state.strategy_state(sid)
            if sstate is StrategyRunState.PAUSED:
                return self._reject(decision_id, RiskLayer.STRATEGY, "strategy_status", intent,
                                    {"strategy_id": sid, "status": sstate.value},
                                    f"策略 {sid} 已暂停，不接受新仓位", ctx)
            if sstate is StrategyRunState.REDUCE_ONLY and increasing:
                return self._reject(decision_id, RiskLayer.STRATEGY, "strategy_status", intent,
                                    {"strategy_id": sid, "status": sstate.value},
                                    f"策略 {sid} 处于仅减仓状态，此单会增加敞口", ctx)

            capacity = D(ctx.strategy_capacity.get(sid, Decimal(0)))
            if capacity > 0:
                used = D(ctx.strategy_notional.get(sid, Decimal(0)))
                projected = used + (D(intent.notional) if increasing else Decimal(0))
                pct = projected / capacity * Decimal(100)
                if pct > D(self.policy.max_strategy_allocation_pct):
                    return self._reject(decision_id, RiskLayer.STRATEGY,
                                        "max_strategy_allocation_pct", intent,
                                        {"strategy_id": sid, "projected_pct": str(q(pct, 2)),
                                         "limit": str(self.policy.max_strategy_allocation_pct)},
                                        f"策略 {sid} 占用将达额度的 {q(pct, 2)}%，超过上限", ctx)

                turnover_cap = capacity * D(self.policy.max_strategy_turnover_multiple)
                turnover = D(ctx.strategy_turnover.get(sid, Decimal(0))) + D(intent.notional)
                if turnover > turnover_cap:
                    return self._reject(decision_id, RiskLayer.STRATEGY,
                                        "max_strategy_turnover_multiple", intent,
                                        {"strategy_id": sid, "turnover": str(q(turnover, 2)),
                                         "cap": str(q(turnover_cap, 2))},
                                        f"策略 {sid} 当日换手 {q(turnover, 2)} 超过额度 {self.policy.max_strategy_turnover_multiple} 倍",
                                        ctx)

        # ---------- 订单级 ---------- #
        notional = D(intent.notional)
        if notional < D(self.policy.min_order_notional):
            return self._reject(decision_id, RiskLayer.ORDER, "min_order_notional", intent,
                                {"notional": str(notional),
                                 "limit": str(self.policy.min_order_notional)},
                                f"单笔金额 {notional} 低于最小下单金额", ctx)

        order_pct = notional / nav * Decimal(100)
        if order_pct > D(self.policy.max_order_notional_pct):
            cap = q(nav * D(self.policy.max_order_notional_pct) / Decimal(100), 2)
            return RiskDecision(
                decision_id=decision_id, layer=RiskLayer.ORDER,
                policy_key="max_order_notional_pct", verdict=Verdict.REDUCE_REQUIRED,
                subject_id=intent.order_intent_id,
                inputs={"order_notional_pct": str(q(order_pct, 3)),
                        "limit": str(self.policy.max_order_notional_pct),
                        "cap_notional": str(cap)},
                reason=(f"单笔占 NAV {q(order_pct, 3)}% 超过 {self.policy.max_order_notional_pct}%；"
                        f"要求上游按 cap {cap} 用原意图重算，风控不代改单量"),
                ts=ctx.now, cap_notional=cap,
            )

        if not self.policy.allow_leverage:
            gross = D(ctx.total_gross_notional) + (notional if increasing else Decimal(0))
            gross_pct = gross / nav * Decimal(100)
            limit = min(D(self.policy.max_gross_exposure_pct), Decimal(100))
            if gross_pct > limit:
                return self._reject(decision_id, RiskLayer.ORDER, "allow_leverage", intent,
                                    {"gross_pct": str(q(gross_pct, 2)), "limit": str(limit)},
                                    f"总敞口将达 NAV 的 {q(gross_pct, 2)}%，不允许加杠杆", ctx)

        return RiskDecision(
            decision_id=decision_id, layer=RiskLayer.ORDER, policy_key="pass",
            verdict=Verdict.ALLOW, subject_id=intent.order_intent_id,
            inputs={"order_notional_pct": str(q(order_pct, 3)),
                    "permission_level": state.permission_level.value,
                    "reduces_exposure": str(not increasing)},
            reason="通过三道防线检查", ts=ctx.now,
        )

    # -- 工具 ------------------------------------------------------------ #

    def _increases_exposure(self, intent: OrderIntent, ctx: RiskContext) -> bool:
        """
        判断此单是否「新增风险」。

        翻仓（100 多 → 100 空）虽然绝对敞口不变，但方向是全新的风险，判为新增。
        """
        key = (intent.venue, intent.symbol)
        current = D(ctx.position_notional.get(key, Decimal(0)))
        order_signed = D(intent.notional) * intent.side.sign
        resulting = current + order_signed
        if current == 0:
            return True
        if (current > 0) != (resulting > 0) and resulting != 0:
            return True                       # 穿过零轴，新方向
        return abs(resulting) > abs(current)

    @staticmethod
    def _attributed_strategies(intent: OrderIntent) -> List[str]:
        sids = list(intent.attribution.keys())
        if not sids:
            for step in intent.reason_chain:
                sids.extend((step.get("per_strategy") or {}).keys())
        return sorted(set(sids))

    def _reject(self, decision_id, layer, policy_key, intent, inputs, reason, ctx):
        return RiskDecision(
            decision_id=decision_id, layer=layer, policy_key=policy_key,
            verdict=Verdict.REJECT, subject_id=intent.order_intent_id,
            inputs=inputs, reason=reason, ts=ctx.now,
        )

    def _halt(self, decision_id, policy_key, intent, inputs, reason, ctx,
              playbook_key: str):
        actions = HALT_PLAYBOOK.get(playbook_key, (HaltAction.FREEZE_NEW_ORDERS,))
        return RiskDecision(
            decision_id=decision_id, layer=RiskLayer.FUND, policy_key=policy_key,
            verdict=Verdict.HALT_FUND, subject_id=intent.order_intent_id,
            inputs=inputs, reason=reason, ts=ctx.now,
            halt_actions=tuple(a.value for a in actions),
        )


# --------------------------------------------------------------------------- #
#  UNKNOWN 订单对账（T-203 的判定逻辑，纯函数便于测试）
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class UnknownResolution:
    order_id: str
    new_state: Optional[OrderState]
    action: str          # adopt / cancel / void / wait
    reason: str


def resolve_unknown_order(order_id: str, venue_status: Optional[str],
                          venue_filled_qty: Decimal = Decimal(0),
                          venue_found: bool = True) -> UnknownResolution:
    """
    把「状态未知」变成确定结论（R-A08）。

    四种结论：
      adopt  —— 交易所确有此单且已成交/部分成交 → 迁到对应状态，补录成交
      cancel —— 交易所确有此单但未成交 → 撤单
      void   —— 交易所确认无此单 → 视为从未存在（EXPIRED）
      wait   —— 查不到确定答案 → 保持 UNKNOWN，不许下新单（fail-closed）

    永远不会有 "resubmit" 这个选项。重复下单的唯一入口是幂等键冲突，不是这里。
    """
    if not venue_found:
        return UnknownResolution(order_id, OrderState.EXPIRED, "void",
                                 "交易所无此订单且本地无成交，判为未生效")
    status = (venue_status or "").strip().lower()
    if not status:
        return UnknownResolution(order_id, None, "wait",
                                 "无法取得交易所状态，保持 UNKNOWN 并冻结该标的")

    # 注意：必须先排除 "partially_filled"，否则子串 "filled" 会把部分成交判成全部成交。
    is_partial_status = "partial" in status
    if (D(venue_filled_qty) > 0 and not is_partial_status
            and any(w in status for w in ("filled", "closed", "done"))):
        return UnknownResolution(order_id, OrderState.FILLED, "adopt",
                                 f"交易所回报已全部成交（filled={venue_filled_qty}），补录成交")
    if D(venue_filled_qty) > 0:
        return UnknownResolution(order_id, OrderState.PARTIALLY_FILLED, "adopt",
                                 f"交易所回报部分成交（filled={venue_filled_qty}），补录并继续跟踪")
    if any(w in status for w in ("cancel", "reject", "expire", "dead")):
        return UnknownResolution(order_id, OrderState.CANCELLED, "adopt",
                                 f"交易所回报 {status}，本地同步为止损状态")
    if any(w in status for w in ("new", "open", "accepted", "live", "pending")):
        return UnknownResolution(order_id, OrderState.ACCEPTED, "cancel",
                                 f"交易所回报 {status} 且无成交，恢复跟踪后撤单")
    return UnknownResolution(order_id, None, "wait",
                             f"未识别的交易所状态 {status!r}，保持冻结等待人工")


__all__ = [
    "RiskPolicy", "RiskState", "RiskDecision", "RiskContext",
    "RiskEngine", "HaltAction", "HALT_PLAYBOOK",
    "UnknownResolution", "resolve_unknown_order",
]
