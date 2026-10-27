"""
QuantOS 2.0 — 场所网关抽象（T-501a）
=====================================

为什么要有这一层：OMS 只会说"买 0.5 个 BTC"。至于 BTC/USDT 的幂等键叫
`clientOrderId` 还是 `newClientOrderId`、数量是 3 位还是 5 位小数、请求超时
了那笔单到底下没下出去 —— 全是场所的脏细节。这些细节一旦漏进交易主链路，
换一家交易所就得改风控和 OMS。

本模块只做三件事：

  1. **归一化类型**：VenueOrder / VenuePosition / VenueBalance / FundingPayment
  2. **错误分类**：把"确定没下出去"和"不知道下没下出去"彻底分开
  3. **submit_with_reconcile**：把"不知道"变成"去查"，永远不是"重发"

第 2 点是整个 B5 的地基。绝大多数重复下单事故都不是因为程序员忘了幂等键，
而是因为某处代码把 TimeoutError 当成了"失败了"，然后重试。**超时从来不等于
失败，它只等于不知道。** 不知道就必须去查，查不到就挂着等人，绝不能重发。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from .contracts import D, OrderIntent, OrderState, Side, utcnow
from .venues import Capabilities, CapabilityError

__all__ = [
    "GatewayError", "TransportError", "RateLimited", "VenueRejected",
    "AuthError", "CapabilityError",
    "VenueOrder", "VenuePosition", "VenueBalance", "FundingPayment",
    "VenueGateway", "SubmitOutcome", "submit_with_reconcile",
    "normalize_state", "ACCEPTED", "REJECTED", "UNKNOWN",
]

_ZERO = Decimal(0)


# --------------------------------------------------------------------------- #
#  错误分类
# --------------------------------------------------------------------------- #

class GatewayError(RuntimeError):
    """
    网关错误基类。

    `ambiguous` 是这个类的全部意义所在：
      True  → 请求可能已经生效，**只能去查，不能重发**
      False → 确定没生效，可以安全地不当它发生过
    """

    ambiguous: bool = False


class TransportError(GatewayError):
    """
    网络层故障：超时、连接重置、响应截断、5xx。

    请求在场所侧可能已经落了。这就是"不知道"的来源。
    """

    ambiguous = True


class RateLimited(TransportError):
    """
    被限流。

    严格说 429 通常意味着"没受理"，但并非所有场所都守这个约定，
    所以这里按不确定处理 —— 宁可多查一次，也不能赌它没生效。
    """

    ambiguous = True


class VenueRejected(GatewayError):
    """场所明确拒绝（余额不足、参数非法、标的不存在）。订单肯定不存在。"""

    ambiguous = False


class AuthError(GatewayError):
    """凭证失效或权限不足。fail-closed：这一类错误不允许被当成"偶发"重试掉。"""

    ambiguous = False


# --------------------------------------------------------------------------- #
#  归一化类型
# --------------------------------------------------------------------------- #

ACCEPTED = "accepted"
REJECTED = "rejected"
UNKNOWN = "unknown"

#: 场所自己的状态词 → 契约层状态机。
#: 不在表里的一律 UNKNOWN，**不做猜测**。猜成 FILLED 会让账本凭空多一笔持仓，
#: 猜成 CANCELLED 会让真实敞口从风控视野里消失，两个方向都是事故。
_STATE_MAP: Dict[str, OrderState] = {
    "new": OrderState.ACCEPTED,
    "open": OrderState.ACCEPTED,
    "pending": OrderState.ACCEPTED,
    "live": OrderState.ACCEPTED,
    "partially_filled": OrderState.PARTIALLY_FILLED,
    "partiallyfilled": OrderState.PARTIALLY_FILLED,
    "partial": OrderState.PARTIALLY_FILLED,
    "partial_fill": OrderState.PARTIALLY_FILLED,
    "filled": OrderState.FILLED,
    "closed": OrderState.FILLED,
    "done": OrderState.FILLED,
    "pending_cancel": OrderState.PENDING_CANCEL,
    "pendingcancel": OrderState.PENDING_CANCEL,
    "canceling": OrderState.PENDING_CANCEL,
    "canceled": OrderState.CANCELLED,
    "cancelled": OrderState.CANCELLED,
    "canceled_by_user": OrderState.CANCELLED,
    "canceled_by_system": OrderState.CANCELLED,
    "rejected": OrderState.REJECTED,
    "expired": OrderState.EXPIRED,
    "expired_in_match": OrderState.EXPIRED,
    "ioc_expired": OrderState.EXPIRED,
}


def normalize_state(raw: Any, filled_qty: Decimal = _ZERO,
                    quantity: Optional[Decimal] = None) -> OrderState:
    """
    把场所状态词映射到契约状态机，并对"说成交了但数量不对"做防御。

    两种情况必须纠正，否则账会错：

      · 场所说 filled/closed，但成交量只有一部分 → 判 PARTIALLY_FILLED。
        （见过真实案例：场所先发 closed，成交明细晚到，照抄就把半仓当满仓）
      · 场所说 filled/closed，但成交量为 0 → 判 CANCELLED。
        零成交的 closed 在敞口上和撤销完全等价，两种口径下风险都是 0。

    其余未知词一律 UNKNOWN —— 让上层去查，别在这里替它猜。
    """
    key = str(raw or "").strip().lower().replace("-", "_")
    state = _STATE_MAP.get(key)
    if state is None:
        return OrderState.UNKNOWN

    if state is OrderState.FILLED:
        filled = D(filled_qty)
        if quantity is not None and filled > 0 and filled < D(quantity) * Decimal("0.999999"):
            return OrderState.PARTIALLY_FILLED
        if filled == 0:
            return OrderState.CANCELLED
    return state


@dataclass(frozen=True)
class VenueOrder:
    """场所视角的一笔订单。字段全部归一化，不含任何场所方言。"""

    venue: str
    venue_order_id: str
    client_order_id: str
    symbol: str
    side: Side
    quantity: Decimal
    filled_qty: Decimal
    state: OrderState
    raw_state: str = ""
    avg_price: Optional[Decimal] = None
    ts: Optional[datetime] = None
    reason: str = ""

    def __post_init__(self):
        if D(self.quantity) <= 0:
            raise GatewayError(f"VenueOrder.quantity 必须为正，收到 {self.quantity}")
        if D(self.filled_qty) < 0:
            raise GatewayError("VenueOrder.filled_qty 不能为负")

    @property
    def is_terminal(self) -> bool:
        return self.state in (OrderState.FILLED, OrderState.CANCELLED,
                              OrderState.REJECTED, OrderState.EXPIRED)

    @property
    def remaining_qty(self) -> Decimal:
        return max(_ZERO, D(self.quantity) - D(self.filled_qty))

    @property
    def filled_notional(self) -> Decimal:
        px = D(self.avg_price) if self.avg_price is not None else _ZERO
        return D(self.filled_qty) * px

    def to_dict(self) -> Dict[str, Any]:
        return {
            "venue": self.venue, "venue_order_id": self.venue_order_id,
            "client_order_id": self.client_order_id, "symbol": self.symbol,
            "side": self.side.value, "quantity": str(self.quantity),
            "filled_qty": str(self.filled_qty), "state": self.state.value,
            "raw_state": self.raw_state,
            "avg_price": None if self.avg_price is None else str(self.avg_price),
            "ts": self.ts.isoformat() if self.ts else None, "reason": self.reason,
        }


@dataclass(frozen=True)
class VenuePosition:
    """场所持仓。`qty` 有符号：正=多头，负=空头。"""

    venue: str
    symbol: str
    qty: Decimal
    entry_price: Optional[Decimal] = None
    mark_price: Optional[Decimal] = None
    realized: Optional[Decimal] = None
    raw: Dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def notional(self) -> Decimal:
        px = D(self.mark_price) if self.mark_price is not None else (
            D(self.entry_price) if self.entry_price is not None else _ZERO)
        return D(self.qty) * px


@dataclass(frozen=True)
class VenueBalance:
    venue: str
    currency: str
    free: Decimal
    used: Decimal

    @property
    def total(self) -> Decimal:
        return D(self.free) + D(self.used)


@dataclass(frozen=True)
class FundingPayment:
    """
    一笔已结算的资金费。

    符号约定跟账本 `post_funding` 一致：**正数 = 账户支出**。
    多头在正费率区间付钱，所以 `amount > 0` 对多头是正常的。
    """

    venue: str
    symbol: str
    amount: Decimal
    currency: str
    rate: Decimal
    ts: datetime
    payment_id: str
    interval_hours: int = 8

    def __post_init__(self):
        if not self.payment_id:
            raise GatewayError("FundingPayment.payment_id 不可为空：资金费入账靠它去重")

    @property
    def boundary_key(self) -> str:
        """结算边界键。场所不给 payment_id 时，用 (场所, 标的, 时间) 兜底去重。"""
        return f"{self.venue}|{self.symbol}|{self.ts.strftime('%Y-%m-%dT%H:%M')}"


# --------------------------------------------------------------------------- #
#  网关协议
# --------------------------------------------------------------------------- #

class VenueGateway(ABC):
    """
    一家场所的交易接口。

    子类只负责"翻译"，不负责"决定"。要不要下单、下多少、能不能重试，
    全部由上层的风控与 OMS 决定 —— 网关一旦开始自己判断，就多了一套
    无法审计的规则。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    @abstractmethod
    def capabilities(self) -> Capabilities:
        ...

    @abstractmethod
    def place(self, intent: OrderIntent) -> VenueOrder:
        """
        提交一笔单。

        实现必须保证：
          · 幂等键随请求发出（否则上层永远救不回来）
          · 错误按"确定/不确定"分类抛出，不许把 TransportError 吞成 VenueRejected
        """

    @abstractmethod
    def fetch_order(self, symbol: str, client_order_id: str) -> Optional[VenueOrder]:
        """按幂等键回查。返回 None 表示"查过了，确实没有这笔单"。"""

    @abstractmethod
    def cancel(self, symbol: str, client_order_id: str) -> VenueOrder:
        ...

    def fetch_open_orders(self, symbol: Optional[str] = None) -> List[VenueOrder]:
        return []

    def fetch_positions(self) -> List[VenuePosition]:
        return []

    def fetch_balance(self) -> List[VenueBalance]:
        return []

    def fetch_funding_payments(self, since: Optional[datetime] = None) -> List[FundingPayment]:
        """现货场所没有资金费，默认返回空 —— 不返回空列表和"不支持"是两件事，
        所以非合约场所应当显式抛 CapabilityError 让上层知道没这个账。"""
        raise CapabilityError(f"{self.name} 不支持资金费查询")

    def health(self) -> Dict[str, Any]:
        caps = self.capabilities
        return {
            "venue": self.name,
            "declared": caps.verified_at is None,
            "verified_at": caps.verified_at.isoformat() if caps.verified_at else None,
            "client_order_id_param": caps.client_order_id_param,
        }


# --------------------------------------------------------------------------- #
#  提交 + 对账
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SubmitOutcome:
    """一次提交尝试的最终裁决。三个状态，语义互斥。"""

    status: str
    reason: str
    order: Optional[VenueOrder] = None
    place_calls: int = 0
    queries: int = 0

    @property
    def ok(self) -> bool:
        return self.status == ACCEPTED

    @property
    def needs_manual(self) -> bool:
        """UNKNOWN：既不能当成功，也不能当失败。只能挂起等对账或人工。"""
        return self.status == UNKNOWN

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status, "reason": self.reason,
            "client_order_id": self.order.client_order_id if self.order else None,
            "state": self.order.state.value if self.order else None,
            "place_calls": self.place_calls, "queries": self.queries,
        }


def submit_with_reconcile(
    gateway: VenueGateway,
    intent: OrderIntent,
    *,
    max_queries: int = 4,
    query_delay_sec: float = 0.0,
    sleeper: Callable[[float], None] = time.sleep,
) -> SubmitOutcome:
    """
    提交一笔单，并且**在不确定时只查不重发**。

    流程只有一条分支是危险的：

        place() 抛 TransportError
                │
                ├─ 错误做法：重试 place()  ←—— 重复下单就发生在这里
                └─ 正确做法：按 client_order_id 回查
                        ├─ 查到 → 当成功（可能已成交/已撤，以场所状态为准）
                        └─ 查不到 → UNKNOWN，挂起，等对账或人工，**绝不重发**

    为什么查不到也不能重发：查不到有两种可能 —— 真的没下出去，或者下出去了
    但还没在查询接口里可见（`order_visibility_lag_ms` 就是描述这个窗口的）。
    前者重发是对的，后者重发就是双倍仓位。分不清的时候，成本不对称：
    挂起一笔单的代价是错过一次机会，重发的代价是真实亏损。
    """
    caps = gateway.capabilities
    if not caps.supports_client_order_id:
        raise CapabilityError(
            f"{caps.venue} 不支持客户端订单号，无法保证幂等 —— 拒绝提交")
    if not caps.supports_query_by_client_id:
        raise CapabilityError(
            f"{caps.venue} 无法按客户端订单号回查：一旦超时，这笔单的状态永远查不清"
            f" —— 拒绝提交")

    cid = intent.client_order_id
    place_calls = 0
    try:
        place_calls += 1
        order = gateway.place(intent)
        return SubmitOutcome(ACCEPTED, "已提交", order, place_calls, 0)
    except VenueRejected as exc:
        return SubmitOutcome(REJECTED, f"场所拒绝：{exc}", None, place_calls, 0)
    except AuthError as exc:
        return SubmitOutcome(REJECTED, f"鉴权失败（fail-closed）：{exc}", None, place_calls, 0)
    except CapabilityError as exc:
        return SubmitOutcome(REJECTED, f"能力不足：{exc}", None, place_calls, 0)
    except TransportError as exc:
        ambiguous_reason = f"{type(exc).__name__}: {exc}"

    # ---- 到这里只有一个事实：不知道下没下出去。开始查。 ---- #
    queries = 0
    for _ in range(max(1, max_queries)):
        if query_delay_sec:
            sleeper(query_delay_sec)
        queries += 1
        try:
            found = gateway.fetch_order(intent.symbol, cid)
        except AuthError as exc:
            return SubmitOutcome(
                UNKNOWN, f"提交超时后回查鉴权失败，状态未知：{exc}",
                None, place_calls, queries)
        except TransportError:
            continue          # 查询本身也会超时，继续查，不重发
        if found is not None:
            return SubmitOutcome(
                ACCEPTED, f"提交报告不确定（{ambiguous_reason}），回查命中", found,
                place_calls, queries)

    return SubmitOutcome(
        UNKNOWN,
        f"提交报告不确定（{ambiguous_reason}），回查 {queries} 次未见此单。"
        f"**不得重发**：需人工确认或纳入下一轮对账消解",
        None, place_calls, queries)
