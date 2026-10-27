"""
QuantOS 2.0 — 领域契约（T-001）
=============================

本模块是 `docs/QuantOS/02-服务数据契约.md` 第 2 节的代码实现，五个服务之间的
所有数据交换都必须走这里的类型。设计约束：

  G-1 单向依赖：本模块不 import 任何券商/交易模块，纯领域模型。
  G-2 金额一律 Decimal，禁止 float 参与累加。
  G-3 事件同时携带 as_of（数据时间）与 ts（产生时间）。
  G-4 幂等键 client_order_id 由上游生成，重试复用。
  G-5 失败默认关闭：拿不到确定答案就不许新增风险。

关于 confidence
--------------
`StrategyIntent.confidence` 字段存在，但**不参与任何计算**（需求 R-A06）。
它只用于展示与审计。下游模块（capital_allocator）不得把它写进算术表达式。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from enum import Enum
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

UTC = timezone.utc


# --------------------------------------------------------------------------- #
#  基础工具
# --------------------------------------------------------------------------- #

def D(value) -> Decimal:
    """把任何数值安全转成 Decimal（走 str，避免 float 二进制误差）。"""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise TypeError("bool 不是金额，拒绝转换")
    return Decimal(str(value))


def q(value, places: int = 8) -> Decimal:
    """量化到指定小数位（默认 8 位，覆盖加密资产最小单位）。"""
    return D(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _require_aware(dt: datetime, name: str) -> datetime:
    if not isinstance(dt, datetime):
        raise TypeError(f"{name} 必须是 datetime，收到 {type(dt).__name__}")
    if dt.tzinfo is None:
        raise ValueError(f"{name} 必须带时区（UTC），收到 naive datetime")
    return dt.astimezone(UTC)


def slug(text: str) -> str:
    """把 'BTC/USDT' / 'QQQ US' 归一成只含大写字母数字的片段。"""
    out = re.sub(r"[^A-Za-z0-9]+", "", str(text)).upper()
    if not out:
        raise ValueError(f"无法归一化标识: {text!r}")
    return out


# --------------------------------------------------------------------------- #
#  枚举
# --------------------------------------------------------------------------- #

class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> Decimal:
        return Decimal(1) if self is Side.BUY else Decimal(-1)


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class OrderState(str, Enum):
    CREATED = "CREATED"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    PENDING_CANCEL = "PENDING_CANCEL"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


TERMINAL_STATES = frozenset({
    OrderState.FILLED, OrderState.CANCELLED,
    OrderState.REJECTED, OrderState.EXPIRED,
})

#: 契约第 2.6 节的合法迁移表。未列出的迁移一律 IllegalTransition。
TRANSITIONS: Dict[OrderState, frozenset] = {
    OrderState.CREATED:          frozenset({OrderState.SUBMITTED, OrderState.REJECTED}),
    # SUBMITTED 允许直接到成交：成交回报本身就是"交易所已接受此单"的证据。
    # 现实中 WS 可能漏掉 ack 消息而先到 fill（快速成交、断线重连补发），
    # 若强行要求先 ACCEPTED，会把正常成交判成非法迁移而丢失成交记录。
    OrderState.SUBMITTED:        frozenset({OrderState.ACCEPTED, OrderState.REJECTED,
                                            OrderState.UNKNOWN, OrderState.EXPIRED,
                                            OrderState.PARTIALLY_FILLED,
                                            OrderState.FILLED}),
    OrderState.ACCEPTED:         frozenset({OrderState.PARTIALLY_FILLED, OrderState.FILLED,
                                            OrderState.PENDING_CANCEL, OrderState.CANCELLED,
                                            OrderState.EXPIRED, OrderState.UNKNOWN}),
    OrderState.PARTIALLY_FILLED: frozenset({OrderState.PARTIALLY_FILLED, OrderState.FILLED,
                                            OrderState.PENDING_CANCEL, OrderState.CANCELLED,
                                            OrderState.EXPIRED, OrderState.UNKNOWN}),
    OrderState.PENDING_CANCEL:   frozenset({OrderState.CANCELLED, OrderState.PARTIALLY_FILLED,
                                            OrderState.FILLED, OrderState.UNKNOWN}),
    # UNKNOWN 只能通过「查交易所」走出去，没有任何通往"重新提交"的边（R-A08）。
    OrderState.UNKNOWN:          frozenset({OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED,
                                            OrderState.FILLED, OrderState.CANCELLED,
                                            OrderState.REJECTED, OrderState.EXPIRED}),
    OrderState.FILLED:           frozenset(),
    OrderState.CANCELLED:        frozenset(),
    OrderState.REJECTED:         frozenset(),
    OrderState.EXPIRED:          frozenset(),
}


class SignalKind(str, Enum):
    ENTER_LONG = "ENTER_LONG"
    EXIT_LONG = "EXIT_LONG"
    ENTER_SHORT = "ENTER_SHORT"
    EXIT_SHORT = "EXIT_SHORT"


class PermissionLevel(str, Enum):
    NORMAL = "NORMAL"
    REDUCE_ONLY = "REDUCE_ONLY"
    HALTED = "HALTED"


class StrategyRunState(str, Enum):
    ACTIVE = "ACTIVE"
    REDUCE_ONLY = "REDUCE_ONLY"
    PAUSED = "PAUSED"


class RiskLayer(str, Enum):
    ORDER = "ORDER"
    STRATEGY = "STRATEGY"
    FUND = "FUND"


class Verdict(str, Enum):
    ALLOW = "ALLOW"
    REJECT = "REJECT"
    #: 风控不许自己改单量，只能要求上游按 cap 重算（契约第 1 节）。
    REDUCE_REQUIRED = "REDUCE_REQUIRED"
    PAUSE_STRATEGY = "PAUSE_STRATEGY"
    HALT_FUND = "HALT_FUND"


class AllocationMode(str, Enum):
    FIXED_QUOTA = "fixed_quota"
    VOL_TARGET = "vol_target"


class TargetDecision(str, Enum):
    TRADE = "TRADE"
    NOOP = "NOOP"
    BLOCKED = "BLOCKED"


# --------------------------------------------------------------------------- #
#  异常
# --------------------------------------------------------------------------- #

class ContractViolation(ValueError):
    """领域对象字段非法。"""


class IllegalTransition(RuntimeError):
    """订单状态机非法迁移。"""


class DuplicateOrder(RuntimeError):
    """同一 client_order_id 已存在活动订单 —— 上游重试必须复用，不许新建。"""


# --------------------------------------------------------------------------- #
#  Signal / StrategyIntent
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Signal:
    """策略信号：不可变事实，带过期时间。"""

    signal_id: str
    strategy_id: str
    strategy_version: str
    symbol: str
    venue: str
    kind: SignalKind
    strength: Decimal
    reason_code: str
    as_of: datetime
    ts: datetime
    expires_at: datetime

    def __post_init__(self):
        _require_aware(self.as_of, "Signal.as_of")
        _require_aware(self.ts, "Signal.ts")
        _require_aware(self.expires_at, "Signal.expires_at")
        if not self.strategy_id or not self.symbol or not self.venue:
            raise ContractViolation("Signal 的 strategy_id/symbol/venue 不可为空")
        if not (Decimal(0) <= D(self.strength) <= Decimal(1)):
            raise ContractViolation(f"Signal.strength 必须在 0..1，收到 {self.strength}")
        if self.expires_at <= self.ts:
            raise ContractViolation("Signal.expires_at 必须晚于 ts")

    @staticmethod
    def build(strategy_id: str, strategy_version: str, symbol: str, venue: str,
              kind: SignalKind, strength, reason_code: str,
              as_of: datetime, ts: Optional[datetime] = None,
              ttl: timedelta = timedelta(hours=4)) -> "Signal":
        ts = _require_aware(ts or utcnow(), "ts")
        as_of = _require_aware(as_of, "as_of")
        return Signal(
            signal_id=f"{strategy_id}:{symbol}:{int(as_of.timestamp())}",
            strategy_id=strategy_id, strategy_version=strategy_version,
            symbol=symbol, venue=venue, kind=kind, strength=D(strength),
            reason_code=reason_code, as_of=as_of, ts=ts, expires_at=ts + ttl,
        )

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        return _require_aware(now or utcnow(), "now") >= self.expires_at


@dataclass(frozen=True)
class StrategyIntent:
    """
    策略 → 组合层的唯一合法出口（R-A06）。

    注意：target_weight 是**相对该策略分配额度**的权重（-1..1），不是相对 NAV。
    策略不知道 NAV —— 知道 NAV 的策略就在做资金分配了。
    """

    intent_id: str
    strategy_id: str
    strategy_version: str
    symbol: str
    venue: str
    target_weight: Decimal
    confidence: Decimal          # 只记录，不参与计算
    reason: str
    signal_ids: Tuple[str, ...]
    as_of: datetime
    ts: datetime
    expires_at: datetime
    reduce_only_hint: bool = False

    def __post_init__(self):
        for name in ("as_of", "ts", "expires_at"):
            _require_aware(getattr(self, name), f"StrategyIntent.{name}")
        if not (-1 <= D(self.target_weight) <= 1):
            raise ContractViolation(
                f"target_weight 必须在 -1..1（相对策略额度），收到 {self.target_weight}")
        if not (0 <= D(self.confidence) <= 1):
            raise ContractViolation(
                f"confidence 必须在 0..1，收到 {self.confidence}")
        if self.expires_at <= self.ts:
            raise ContractViolation("StrategyIntent.expires_at 必须晚于 ts")
        if not self.signal_ids:
            raise ContractViolation("StrategyIntent 必须可追溯到至少一个 Signal")

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        return _require_aware(now or utcnow(), "now") >= self.expires_at


# --------------------------------------------------------------------------- #
#  资金分配 / 目标持仓
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class StrategyAllocation:
    """组合层给单个策略的预算。effective_capacity 是唯一可用于下单的口径。"""

    strategy_id: str
    mode: AllocationMode
    quota_usd: Decimal
    max_gross_weight: Decimal
    vol_scalar: Decimal
    risk_budget_pct: Decimal

    @property
    def effective_capacity_usd(self) -> Decimal:
        """quota × 波动率缩放。波动率越高，可用额度越小。"""
        return q(D(self.quota_usd) * D(self.vol_scalar), 2)

    def __post_init__(self):
        if D(self.quota_usd) < 0:
            raise ContractViolation("quota_usd 不能为负")
        if not (Decimal(0) < D(self.vol_scalar) <= Decimal(1)):
            raise ContractViolation(f"vol_scalar 必须在 (0,1]，收到 {self.vol_scalar}")


@dataclass(frozen=True)
class TargetPosition:
    """
    组合层净额结果（R-A07）。

    per_strategy 必须保留每个策略的名义归属，否则无法算策略盈亏（R-D05）
    与风险贡献（R-C04）。
    """

    symbol: str
    venue: str
    per_strategy: Mapping[str, Decimal]
    net_target_notional: Decimal
    current_notional: Decimal
    inflight_notional: Decimal
    delta_notional: Decimal
    price: Decimal
    net_target_qty: Decimal
    delta_qty: Decimal
    decision: TargetDecision
    blocked_reason: Optional[str] = None
    #: 暂停/受限策略的存量名义，本次**刻意不动**（B3 解掉整标的阻断）。
    #: 这部分在风控眼里仍算敞口，必须在页面上可见，否则会以为系统漏算了。
    frozen_notional: Decimal = Decimal(0)
    #: 本标的的处置说明（哪些策略被限、为什么），供审计与运维定位。
    notes: Tuple[str, ...] = ()

    def capped(self, cap_notional: Decimal) -> "TargetPosition":
        """
        风控返回 REDUCE_REQUIRED 时，由组合层按上限重算（契约 T3→T4）。

        缩放的是 delta（本次要交易的部分），不是目标持仓 —— 因为目标持仓代表
        策略判断，风险约束只限制"这一次下单多少"。
        """
        cap = D(cap_notional)
        if cap < 0:
            raise ContractViolation("cap_notional 不能为负")
        scaled_delta = cap if self.delta_notional > 0 else -cap
        if abs(scaled_delta) >= abs(self.delta_notional):
            return self
        return TargetPosition(
            symbol=self.symbol, venue=self.venue, per_strategy=self.per_strategy,
            net_target_notional=self.net_target_notional,
            current_notional=self.current_notional,
            inflight_notional=self.inflight_notional,
            delta_notional=q(scaled_delta, 2),
            price=self.price,
            net_target_qty=self.net_target_qty,
            delta_qty=q(scaled_delta / D(self.price), 8) if D(self.price) else Decimal(0),
            decision=self.decision, blocked_reason=self.blocked_reason,
            frozen_notional=self.frozen_notional, notes=self.notes,
        )


# --------------------------------------------------------------------------- #
#  OrderIntent
# --------------------------------------------------------------------------- #

def make_client_order_id(account_id: str, strategy_scope: str, symbol: str,
                         when: datetime, seq: int) -> str:
    """
    稳定幂等键（G-4 / R-A08）。同一个 (账户, 策略, 标的, 日期, 序号) 永远得到
    同一个字符串，所以超时重试时券商侧会判定为同一笔订单。
    """
    when = _require_aware(when, "when")
    if seq < 0:
        raise ContractViolation("seq 不能为负")
    return "-".join([
        slug(account_id), slug(strategy_scope), slug(symbol),
        when.strftime("%Y%m%d"), f"{seq:04d}",
    ])


@dataclass(frozen=True)
class OrderIntent:
    """唯一合法的下单请求。风控必须在此对象上做订单级检查（R-K01）。"""

    order_intent_id: str
    client_order_id: str
    account_id: str
    venue: str
    symbol: str
    side: Side
    quantity: Decimal
    order_type: OrderType
    notional: Decimal
    attribution: Mapping[str, Decimal]
    reason_chain: Tuple[Mapping, ...]
    as_of: datetime
    ts: datetime
    limit_price: Optional[Decimal] = None
    reduce_only: bool = False

    def __post_init__(self):
        _require_aware(self.as_of, "OrderIntent.as_of")
        _require_aware(self.ts, "OrderIntent.ts")
        if D(self.quantity) <= 0:
            raise ContractViolation(f"quantity 必须为正，收到 {self.quantity}")
        if D(self.notional) <= 0:
            raise ContractViolation(f"notional 必须为正，收到 {self.notional}")
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ContractViolation("LIMIT 单必须提供 limit_price")
        if self.order_type is OrderType.MARKET and self.limit_price is not None:
            raise ContractViolation("MARKET 单不应有 limit_price（避免误用限价逻辑）")
        if not self.client_order_id:
            raise ContractViolation("client_order_id 不可为空（幂等键，R-A08）")
        total_attr = sum((D(v) for v in self.attribution.values()), Decimal(0))
        if self.attribution and abs(total_attr - D(self.quantity)) > Decimal("1e-8"):
            raise ContractViolation(
                f"attribution 数量之和 {total_attr} 与 quantity {self.quantity} 不一致")


# --------------------------------------------------------------------------- #
#  Order / Fill
# --------------------------------------------------------------------------- #

@dataclass
class Order:
    """订单状态机载体。所有状态变更必须走 transition()。"""

    order_id: str
    client_order_id: str
    account_id: str
    venue: str
    symbol: str
    side: Side
    quantity: Decimal
    ref_price: Decimal
    state: OrderState = OrderState.CREATED
    filled_qty: Decimal = Decimal(0)
    avg_fill_price: Optional[Decimal] = None
    history: List[Tuple[datetime, OrderState, str]] = field(default_factory=list)
    unknown_since: Optional[datetime] = None

    def __post_init__(self):
        if D(self.quantity) <= 0:
            raise ContractViolation("Order.quantity 必须为正")
        if D(self.ref_price) <= 0:
            raise ContractViolation("Order.ref_price 必须为正（用于在途敞口估算）")
        if not self.history:
            self.history.append((utcnow(), OrderState.CREATED, "created"))

    # -- 状态机 ---------------------------------------------------------- #

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def remaining_qty(self) -> Decimal:
        return max(Decimal(0), D(self.quantity) - D(self.filled_qty))

    @property
    def remaining_notional(self) -> Decimal:
        """在途名义敞口。组合层净额计算必须扣掉它，否则会重复下单。"""
        if self.is_terminal:
            return Decimal(0)
        return q(self.remaining_qty * D(self.ref_price), 2)

    @property
    def signed_inflight(self) -> Decimal:
        return self.remaining_notional * self.side.sign

    def transition(self, new_state: OrderState, reason: str = "",
                   at: Optional[datetime] = None) -> "Order":
        at = _require_aware(at or utcnow(), "at")
        if new_state is self.state:
            # 幂等：重复回报同一状态是正常现象，不算非法迁移。
            if new_state is OrderState.PARTIALLY_FILLED:
                self.history.append((at, new_state, reason or "repeat_partial_report"))
                return self
            return self
        allowed = TRANSITIONS.get(self.state, frozenset())
        if new_state not in allowed:
            raise IllegalTransition(
                f"{self.order_id}: {self.state.value} -> {new_state.value} 非法"
                f"（允许: {sorted(s.value for s in allowed) or '无，终态'}）")
        self.state = new_state
        if new_state is OrderState.UNKNOWN:
            self.unknown_since = at
        elif self.unknown_since is not None and new_state is not OrderState.UNKNOWN:
            self.unknown_since = None
        self.history.append((at, new_state, reason))
        return self

    def mark_timeout(self, at: Optional[datetime] = None) -> "Order":
        """
        超时处理（R-A08 核心）：超时 **不等于** 失败。

        只允许迁到 UNKNOWN，然后进入 reconcile 流程。绝不允许在这里重试提交。
        """
        if self.state in (OrderState.SUBMITTED, OrderState.ACCEPTED,
                          OrderState.PARTIALLY_FILLED, OrderState.PENDING_CANCEL):
            return self.transition(OrderState.UNKNOWN, "timeout_no_terminal_state", at)
        return self

    def apply_fill(self, fill: "Fill", at: Optional[datetime] = None) -> "Order":
        """记录成交（含增量部分成交）。成交数量不得超过剩余数量。"""
        if fill.client_order_id != self.client_order_id:
            raise ContractViolation("Fill 与 Order 的 client_order_id 不一致")
        if self.is_terminal and self.state is not OrderState.FILLED:
            raise IllegalTransition(f"{self.order_id} 已在终态 {self.state.value}，不接受新成交")
        qty = D(fill.quantity)
        if qty <= 0:
            raise ContractViolation("Fill.quantity 必须为正")
        if qty > self.remaining_qty + Decimal("1e-8"):
            raise ContractViolation(
                f"成交量 {qty} 超过剩余 {self.remaining_qty}，说明回报重复或配对错误")

        prev_notional = D(self.filled_qty) * D(self.avg_fill_price or 0)
        new_filled = D(self.filled_qty) + qty
        self.avg_fill_price = q((prev_notional + qty * D(fill.price)) / new_filled, 10)
        self.filled_qty = new_filled

        target = OrderState.FILLED if self.remaining_qty <= Decimal("1e-8") \
            else OrderState.PARTIALLY_FILLED
        if self.state in TERMINAL_STATES and self.state is not OrderState.FILLED:
            return self
        self.transition(target, f"fill {qty} @ {fill.price}", at)
        return self


@dataclass(frozen=True)
class Fill:
    """成交回报。venue_trade_id 用于去重（重复回报必须幂等）。"""

    fill_id: str
    order_id: str
    client_order_id: str
    venue_trade_id: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    fee_currency: str
    ts: datetime
    liquidity: str = "TAKER"

    def __post_init__(self):
        _require_aware(self.ts, "Fill.ts")
        if D(self.quantity) <= 0:
            raise ContractViolation("Fill.quantity 必须为正")
        if D(self.price) <= 0:
            raise ContractViolation("Fill.price 必须为正")
        if D(self.fee) < 0:
            raise ContractViolation("Fill.fee 不能为负（返佣请用负数记入 pnl:fee 的贷方）")
        if not self.venue_trade_id:
            raise ContractViolation("venue_trade_id 不可为空（成交去重依赖它）")


class OrderRegistry:
    """
    幂等订单登记簿（T-202 的领域侧一半）。

    职责：保证同一 client_order_id 不会产生第二笔活动订单；提供在途敞口。
    """

    def __init__(self) -> None:
        self._orders: Dict[str, Order] = {}
        self._seen_trade_ids: Set[str] = set()

    # -- 幂等 ------------------------------------------------------------ #

    def register(self, intent: OrderIntent, order_id: str) -> Order:
        existing = self._orders.get(intent.client_order_id)
        if existing is not None:
            raise DuplicateOrder(
                f"client_order_id {intent.client_order_id} 已存在订单 {existing.order_id}"
                f"（状态 {existing.state.value}）—— 重试必须复用，不许新建")
        order = Order(
            order_id=order_id, client_order_id=intent.client_order_id,
            account_id=intent.account_id, venue=intent.venue, symbol=intent.symbol,
            side=intent.side, quantity=D(intent.quantity), ref_price=D(intent.notional)
            / D(intent.quantity),
        )
        self._orders[intent.client_order_id] = order
        return order

    def get(self, client_order_id: str) -> Optional[Order]:
        return self._orders.get(client_order_id)

    def orders(self) -> List["Order"]:
        """
        全部订单的只读快照（按 client_order_id 排序）。

        供指标采集与审计使用。调用方**不得**通过它改订单状态 —— 状态只能
        在 OMS 里通过 transition() 变更。
        """
        return [self._orders[k] for k in sorted(self._orders)]

    def adopt(self, order: Order) -> Order:
        """
        接管一个已存在的订单对象（进程重启后从快照恢复用）。

        这不是"新建订单"：状态、成交量都来自快照，不重新走一遍状态机，
        否则会用今天的规则重新解释昨天的历史。
        """
        if order.client_order_id in self._orders:
            raise DuplicateOrder(
                f"client_order_id {order.client_order_id} 已登记，重复接管")
        self._orders[order.client_order_id] = order
        return order

    def active_orders(self, venue: str = None, symbol: str = None) -> List[Order]:
        out = []
        for order in self._orders.values():
            if order.is_terminal:
                continue
            if venue and order.venue != venue:
                continue
            if symbol and order.symbol != symbol:
                continue
            out.append(order)
        return out

    def inflight_notional(self, venue: str, symbol: str) -> Decimal:
        """带符号的在途名义敞口（BUY 正 / SELL 负）。"""
        return q(sum((o.signed_inflight for o in self.active_orders(venue, symbol)),
                     Decimal(0)), 2)

    # -- 成交去重 -------------------------------------------------------- #

    def accept_fill(self, order: Order, fill: Fill) -> bool:
        """
        幂等入账入口。返回 True 表示这是新成交，False 表示重复回报已忽略。
        账本只对返回 True 的成交记账，避免重复回报把仓位算两遍。
        """
        if fill.venue_trade_id in self._seen_trade_ids:
            return False
        self._seen_trade_ids.add(fill.venue_trade_id)
        order.apply_fill(fill)
        return True

    def unknown_orders(self) -> List[Order]:
        return [o for o in self._orders.values() if o.state is OrderState.UNKNOWN]

    def symbols_with_unknown_state(self) -> Set[str]:
        return {o.symbol for o in self.unknown_orders()}


__all__ = [
    "D", "q", "utcnow", "slug",
    "Side", "OrderType", "OrderState", "TERMINAL_STATES", "TRANSITIONS",
    "SignalKind", "PermissionLevel", "StrategyRunState", "RiskLayer", "Verdict",
    "AllocationMode", "TargetDecision",
    "ContractViolation", "IllegalTransition", "DuplicateOrder",
    "Signal", "StrategyIntent", "StrategyAllocation", "TargetPosition",
    "make_client_order_id", "OrderIntent", "Order", "Fill", "OrderRegistry",
]
