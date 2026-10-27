"""
QuantOS 2.0 — Capital Allocator（T-002）
=======================================

对应需求 R-C01..C04 与契约第 2.3 / 2.4 节。职责只有三件事：

  1. 给每个策略分配额度（固定配额 + 波动率约束，第一版口径）；
  2. 把多个策略的意图**汇总成净敞口**，扣掉现有持仓与在途订单；
  3. 产出可交给风控的 TargetPosition 与 OrderIntent。

本模块是纯函数式（无 I/O、无时间读取、无随机），因此同一个输入永远得到同一个
输出 —— 这是事件回放能做到逐字段一致的前提（R-S06）。

绝不做的事：
  - 不判断信号好坏（那是策略的事）
  - 不检查单笔风险（那是 Risk Engine 的事）
  - 不读 NAV 以外的账户细节（那是 Ledger 的事）

confidence 不参与任何计算（R-A06）：本文件里唯一出现 confidence 的地方是
把它原样透传进 reason_chain 供审计。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from quantos_core.contracts import (
    AllocationMode, ContractViolation, D, OrderIntent, OrderType, Side,
    StrategyAllocation, StrategyIntent, StrategyRunState, TargetDecision,
    TargetPosition, make_client_order_id, q, slug,
)

SymbolKey = Tuple[str, str]          # (venue, symbol)

#: 波动率缩放的下限，避免实测波动极端时把额度算成 0 导致策略永久静默。
_MIN_VOL_SCALAR = Decimal("0.05")


@dataclass(frozen=True)
class StrategyPolicy:
    """组合层对单策略的预算声明（配置侧，不随行情变化）。"""

    strategy_id: str
    venue: str
    mode: AllocationMode = AllocationMode.FIXED_QUOTA
    #: 占 NAV 的配额比例。第一版用固定配额（R-C03）。
    quota_pct: Decimal = Decimal("0.20")
    #: 相对 quota 的总敞口上限，防止单策略内部多标的自相加杠杆。
    max_gross_weight: Decimal = Decimal("1.0")
    #: 波动率目标模式下的年化目标波动率（如 0.15 = 15%）。
    vol_target: Optional[Decimal] = None
    #: 占基金风险预算的比例，第一版只记录不参与分配，为 T-601 预留。
    risk_budget_pct: Decimal = Decimal("0")

    def __post_init__(self):
        if D(self.quota_pct) < 0:
            raise ContractViolation("quota_pct 不能为负")
        if D(self.max_gross_weight) <= 0:
            raise ContractViolation("max_gross_weight 必须为正")
        if self.mode is AllocationMode.VOL_TARGET and not self.vol_target:
            raise ContractViolation("vol_target 模式必须提供 vol_target")


# --------------------------------------------------------------------------- #
#  1. 额度分配
# --------------------------------------------------------------------------- #

def allocate(policy: StrategyPolicy, nav: Decimal,
             realized_vol: Optional[Decimal] = None) -> StrategyAllocation:
    """
    单策略额度。波动率目标的实现方式：quota 不变，用 vol_scalar 缩放可用额度。

    这样「配额」和「风险」两个概念保持分离（R-C04）：
    配额回答"这笔钱归你管"，vol_scalar 回答"现在这个市场你敢用多少"。
    """
    nav = D(nav)
    if nav <= 0:
        raise ContractViolation(f"NAV 必须为正，收到 {nav}")

    vol_scalar = Decimal(1)
    if policy.mode is AllocationMode.VOL_TARGET and realized_vol is not None:
        rv = D(realized_vol)
        if rv <= 0:
            raise ContractViolation(f"realized_vol 必须为正，收到 {rv}")
        vol_scalar = min(Decimal(1), D(policy.vol_target) / rv)
        vol_scalar = max(_MIN_VOL_SCALAR, vol_scalar)

    return StrategyAllocation(
        strategy_id=policy.strategy_id,
        mode=policy.mode,
        quota_usd=q(nav * D(policy.quota_pct), 2),
        max_gross_weight=D(policy.max_gross_weight),
        vol_scalar=q(vol_scalar, 6),
        risk_budget_pct=D(policy.risk_budget_pct),
    )


def allocate_all(policies: Sequence[StrategyPolicy], nav: Decimal,
                 vols: Optional[Mapping[str, Decimal]] = None
                 ) -> Dict[str, StrategyAllocation]:
    vols = vols or {}
    return {p.strategy_id: allocate(p, nav, vols.get(p.strategy_id)) for p in policies}


# --------------------------------------------------------------------------- #
#  2. 净额汇总（R-A07 的核心）
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class NettingInput:
    """净额计算的全部外部输入。显式传参，避免函数内部偷偷读全局状态。"""

    intents: Sequence[StrategyIntent]
    allocations: Mapping[str, StrategyAllocation]
    policies: Mapping[str, StrategyPolicy]
    prices: Mapping[SymbolKey, Decimal]
    current_notional: Mapping[SymbolKey, Decimal]
    inflight_notional: Mapping[SymbolKey, Decimal]
    strategy_states: Mapping[str, StrategyRunState]
    now: "object"                     # datetime，标注为 object 避免循环 import 噪音
    min_notional: Mapping[str, Decimal] = None   # venue -> 最小下单金额
    #: 策略 → 标的 → 当前名义敞口。提供时按策略各自算差额（B3）：
    #: 暂停策略的存量被冻结而不是拖累整个标的。不提供则退化为整标的算法。
    current_by_strategy: Optional[Mapping[str, Mapping[SymbolKey, Decimal]]] = None

    def min_for(self, venue: str) -> Decimal:
        table = self.min_notional or {}
        return D(table.get(venue, Decimal("0")))


def net_targets(inp: NettingInput) -> List[TargetPosition]:
    """
    汇总各策略意图 → 每个标的的净目标 → 本次应成交的差额。

    关键设计：delta = 净目标 - 现有持仓 - **在途订单**。
    扣在途是必须的：否则两次调度之间同一意图会被下单两次（R-A07）。

    B3 起支持 `current_by_strategy`（策略 → 标的 → 当前名义）。提供时差额
    按**策略各自**的持仓计算，因此：

      - PAUSED 策略的本轮意图被丢弃，其**存量记为 frozen 不动**（不再整标的阻断）；
      - REDUCE_ONLY 策略只允许减小自己的敞口，不许加仓、不许反手建仓。

    不提供时退化为「整标的」算法（B0 行为），保证旧调用方不受影响。
    """
    # ---- 第一步：单意图 → 名义金额，按策略额度换算 ---------------------- #
    raw: Dict[SymbolKey, Dict[str, Decimal]] = {}
    notes: Dict[SymbolKey, List[str]] = {}
    block_reason: Dict[SymbolKey, str] = {}
    price_used: Dict[SymbolKey, Decimal] = {}

    for intent in inp.intents:
        key = (intent.venue, intent.symbol)
        book = notes.setdefault(key, [])
        state = inp.strategy_states.get(intent.strategy_id, StrategyRunState.ACTIVE)
        alloc = inp.allocations.get(intent.strategy_id)

        if intent.is_expired(inp.now):
            book.append(f"{intent.strategy_id}:stale_intent")
            block_reason.setdefault(key, "stale_intent")
            continue
        if state is StrategyRunState.PAUSED:
            # 不写 raw：该策略本轮目标视为 0；存量在第三步记为 frozen。
            book.append(f"{intent.strategy_id}:paused")
            block_reason.setdefault(key, "strategy_paused")
            continue
        if alloc is None:
            book.append(f"{intent.strategy_id}:no_allocation")
            block_reason.setdefault(key, "no_allocation")
            continue

        # target_weight 相对策略可用额度；confidence 不参与（R-A06）。
        notional = D(alloc.effective_capacity_usd) * D(intent.target_weight)
        raw.setdefault(key, {})[intent.strategy_id] = \
            raw.setdefault(key, {}).get(intent.strategy_id, Decimal(0)) + notional

        if key not in inp.prices:
            block_reason.setdefault(key, "no_price")
        else:
            price_used[key] = D(inp.prices[key])

    # ---- 第二步：单策略跨标的总敞口超过 max_gross_weight 时按比例压缩 -- #
    strategy_gross: Dict[str, Decimal] = {}
    for key, per in raw.items():
        for sid, notional in per.items():
            strategy_gross[sid] = strategy_gross.get(sid, Decimal(0)) + abs(notional)

    scale: Dict[str, Decimal] = {}
    for sid, gross in strategy_gross.items():
        alloc = inp.allocations.get(sid)
        if alloc is None or gross <= 0:
            scale[sid] = Decimal(1)
            continue
        cap = D(alloc.effective_capacity_usd) * D(alloc.max_gross_weight)
        scale[sid] = min(Decimal(1), cap / gross) if cap > 0 else Decimal(0)

    # ---- 第三步：净额与差额 -------------------------------------------- #
    by_strategy = inp.current_by_strategy
    # 空字典等价于"没提供分账"：账本里一个策略持仓都没有时，按策略算没有
    # 任何意义，必须退回整标的算法，否则 total 里的持仓会被静默忽略。
    use_strategy_level = bool(by_strategy)

    keys = set(raw) | set(notes) | set(price_used) | set(inp.prices)
    if use_strategy_level:
        keys |= {key for per in by_strategy.values() for key in per}

    out: List[TargetPosition] = []
    for key in sorted(keys):
        venue, symbol = key
        # 注意：这里**不过滤 0 值**。target_weight=0 是"清仓"的显式表达，
        # 过滤掉它会让清仓意图被静默忽略（策略想卖，系统不动）。
        # "没发意图"和"发了 0"是两件事，只有前者才算不动。
        per = {sid: q(D(v) * scale.get(sid, Decimal(1)), 2)
               for sid, v in raw.get(key, {}).items()}
        net_target = q(sum(per.values(), Decimal(0)), 2)
        current = q(D(inp.current_notional.get(key, Decimal(0))), 2)
        inflight = q(D(inp.inflight_notional.get(key, Decimal(0))), 2)
        price = price_used.get(key, Decimal(0))
        item_notes: List[str] = list(notes.get(key, ()))
        frozen = Decimal(0)

        if not use_strategy_level:
            delta = q(net_target - current - inflight, 2)
        else:
            delta, frozen = _strategy_level_delta(
                key, per, by_strategy, inp.strategy_states, item_notes, current)
            delta = q(delta - inflight, 2)

        decision = TargetDecision.TRADE
        reason: Optional[str] = None

        if not per:
            # 本轮没有任何策略对该标的提出有效目标：不是"要交易"。
            if block_reason.get(key):
                decision, reason = TargetDecision.BLOCKED, block_reason[key]
            else:
                decision, reason = TargetDecision.NOOP, "flat_target"
        elif price <= 0:
            decision, reason = TargetDecision.BLOCKED, "no_price"
        elif abs(delta) < inp.min_for(venue):
            decision, reason = TargetDecision.NOOP, "below_min_notional"

        delta_qty = Decimal(0)
        net_qty = Decimal(0)
        if price > 0:
            # 数量向零取整：宁可少买一点，不要超买导致现金不足。
            net_qty = _qty_from_notional(net_target, price)
            delta_qty = _qty_from_notional(delta, price)

        out.append(TargetPosition(
            symbol=symbol, venue=venue, per_strategy=per,
            net_target_notional=net_target, current_notional=current,
            inflight_notional=inflight, delta_notional=delta, price=price,
            net_target_qty=net_qty, delta_qty=delta_qty,
            decision=decision, blocked_reason=reason,
            frozen_notional=q(frozen, 2), notes=tuple(item_notes),
        ))
    return out


def _strategy_level_delta(key: SymbolKey, per: Mapping[str, Decimal],
                          by_strategy: Mapping[str, Mapping[SymbolKey, Decimal]],
                          states: Mapping[str, StrategyRunState],
                          notes: List[str],
                          total_current: Decimal = Decimal(0)) -> Tuple[Decimal, Decimal]:
    """
    按策略算本次差额，返回 (delta, frozen)。

    动过的每一笔都能说清「为什么是这个数」：被限的策略会写进 notes。
    对不上账的残差（有持仓但无策略归属）一律冻结，不猜、不动 —— 这类
    「无主持仓」在实盘里通常是手工交易或历史迁移留下的。
    """
    delta = Decimal(0)
    frozen = Decimal(0)
    attributed_current = Decimal(0)
    holding = {sid for sid, per_key in by_strategy.items()
               if abs(D(per_key.get(key, Decimal(0)))) > 0}

    for sid in sorted(set(per) | holding):
        own_current = q(D(by_strategy.get(sid, {}).get(key, Decimal(0))), 2)
        attributed_current += own_current
        state = states.get(sid, StrategyRunState.ACTIVE)

        if state is StrategyRunState.PAUSED:
            # 存量冻结：不交易、不平仓，也不让别的策略把它算成可用额度。
            frozen += own_current
            if abs(own_current) > 0:
                notes.append(f"{sid}:frozen")
            continue

        if sid not in per:
            # 本轮没有意图 ≠ 要求清仓。策略没信号时保持原仓位，否则每次
            # 信号间隙都会被动卖光（这是最容易被忽略的"假调仓"来源）。
            # 要清仓必须显式发 target_weight=0 的意图。
            frozen += own_current
            if abs(own_current) > 0:
                notes.append(f"{sid}:no_intent_frozen")
            continue

        own_target = q(D(per[sid]), 2)
        own_delta = own_target - own_current
        if state is StrategyRunState.REDUCE_ONLY:
            if own_current * own_target < 0:
                # 想反手：只允许平掉自己那一份，反向仓要重新排期审批。
                own_delta = -own_current
                notes.append(f"{sid}:reduce_only_flat_only")
            elif abs(own_target) > abs(own_current):
                own_delta = Decimal(0)
                notes.append(f"{sid}:reduce_only_no_add")
        delta += own_delta

    residual = q(D(total_current) - attributed_current, 2)
    if abs(residual) > Decimal("0.01"):
        frozen += residual
        notes.append("unattributed:frozen")

    return q(delta, 2), q(frozen, 2)


def _qty_from_notional(notional: Decimal, price: Decimal) -> Decimal:
    if price <= 0:
        return Decimal(0)
    raw = D(notional) / D(price)
    return raw.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)


# --------------------------------------------------------------------------- #
#  3. 生成 OrderIntent
# --------------------------------------------------------------------------- #

def build_order_intents(targets: Sequence[TargetPosition], account_id: str,
                        when, seq_start: int = 1,
                        strategy_scope: str = "PF",
                        order_type: OrderType = OrderType.LIMIT,
                        limit_slippage_bps: Decimal = Decimal("10"),
                        reduce_only: bool = False
                        ) -> Tuple[List[OrderIntent], int]:
    """
    把 TRADE 状态的 TargetPosition 转成下单请求。

    返回 (intents, next_seq)。序号由调用方持有并落盘，保证跨进程重启后
    client_order_id 不重复（R-A08）。
    """
    from datetime import datetime  # 局部 import：本模块其余部分是纯函数

    if not isinstance(when, datetime):
        raise ContractViolation("when 必须是 datetime")

    intents: List[OrderIntent] = []
    seq = seq_start
    for t in targets:
        if t.decision is not TargetDecision.TRADE:
            continue
        if t.price <= 0 or t.delta_qty == 0:
            continue

        side = Side.BUY if t.delta_notional > 0 else Side.SELL
        qty = abs(D(t.delta_qty))
        notional = abs(D(t.delta_notional))

        attribution = _split_attribution(t.per_strategy, qty)
        if not attribution:
            attribution = {"PF": qty}

        limit_price = None
        if order_type is OrderType.LIMIT:
            drift = D(limit_slippage_bps) / Decimal(10000)
            limit_price = q(D(t.price) * (Decimal(1) + drift if side is Side.BUY
                                          else Decimal(1) - drift), 8)

        intents.append(OrderIntent(
            order_intent_id=f"oi-{slug(account_id)}-{when.strftime('%Y%m%d')}-{seq:04d}",
            client_order_id=make_client_order_id(
                account_id, strategy_scope, t.symbol, when, seq),
            account_id=account_id, venue=t.venue, symbol=t.symbol, side=side,
            quantity=qty, order_type=order_type, notional=notional,
            attribution=attribution,
            reason_chain=tuple({
                "net_target_notional": str(t.net_target_notional),
                "per_strategy": {k: str(v) for k, v in t.per_strategy.items()},
            } for _ in (0,)),
            as_of=when, ts=when,
            limit_price=limit_price,
            reduce_only=reduce_only,
        ))
        seq += 1
    return intents, seq


def _split_attribution(per_strategy: Mapping[str, Decimal],
                       qty: Decimal) -> Dict[str, Decimal]:
    """
    把本次成交数量按各策略的目标名义占比分摊（R-D05 的成交侧归属）。

    净额交易由多策略共同"造成"，按目标名义占比分摊是最可解释的口径：
    谁的目标仓位占比大，谁承担这次成交的份额就多。
    """
    if not per_strategy or qty <= 0:
        return {}
    total = sum((abs(D(v)) for v in per_strategy.values()), Decimal(0))
    if total <= 0:
        return {}
    out: Dict[str, Decimal] = {}
    running = Decimal(0)
    items = sorted(per_strategy.items(), key=lambda kv: -abs(D(kv[1])))
    for i, (sid, v) in enumerate(items):
        if i == len(items) - 1:
            share = qty - running          # 末位吃掉舍入余数，保证严格相等
        else:
            share = q((qty * abs(D(v)) / total).quantize(
                Decimal("0.00000001"), rounding=ROUND_DOWN), 8)
            running += share
        if share > 0:
            out[sid] = share
    return out


__all__ = [
    "StrategyPolicy", "allocate", "allocate_all",
    "NettingInput", "net_targets", "build_order_intents",
]
