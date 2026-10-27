"""
QuantOS 2.0 — 未知订单对账闭环（T-203）
======================================

场景：下单请求超时，我们不知道交易所到底有没有收到。

错误做法（很多个人系统的做法）
------------------------------
"超时了，重试一次吧。" → 交易所其实第一次就成交了 → 翻倍持仓 → 事后才发现。

正确做法（本模块）
------------------
1. **冻结**：该标的立即禁止新单（由 OMS/风控的状态集合保证）。
2. **查询**：拿 client_order_id 去交易所查——这就是幂等键存在的意义。
3. **判定**：成交/部分成交/挂单中/已撤/查无此单，五种结论各自处理。
4. **补录**：交易所已成交但本地没记 → 补一笔成交进账本（标记 synthetic）。
5. **解冻**：该标的再无未知订单时才解冻。

关于"补录"的诚实说明
--------------------
合成成交的 `venue_trade_id` 形如 `recon:{cid}:{累计成交量}`，是确定性的。
这样重复对账不会重复记账。日终对账（T-402）用交易所账单校正时，可以把
synthetic 记录替换成真实记录 —— 在此之前，宁可账面略有偏差，也不能有未知敞口。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Dict, List, Mapping, Optional, Protocol, Set, Tuple

from quantos_core.contracts import (
    ContractViolation, D, Fill, IllegalTransition, Order, OrderState,
    OrderRegistry, q, utcnow,
)
from quantos_core.risk_engine import UnknownResolution, resolve_unknown_order

UTC = timezone.utc
BackfillSink = Callable[[Fill, Order], bool]


class VenueQueryError(RuntimeError):
    """查不到（网络/超时/权限）。此时必须保持 UNKNOWN，不许猜。"""


@dataclass(frozen=True)
class VenueOrderView:
    """交易所侧订单视图。`found=False` 表示明确查无此单。"""

    found: bool
    status: Optional[str] = None
    filled_qty: Decimal = Decimal(0)
    avg_price: Optional[Decimal] = None
    fee: Decimal = Decimal(0)

    @property
    def is_complete(self) -> bool:
        return self.avg_price is not None


class VenueGateway(Protocol):
    """生产实现是券商/交易所适配器；测试里是 FakeGateway。"""

    def query_order(self, client_order_id: str) -> VenueOrderView: ...

    def cancel_order(self, client_order_id: str) -> bool: ...


@dataclass
class Outcome:
    """一次对账的结果。`action` 与契约 §3 的 UnknownResolution 保持一致。"""

    client_order_id: str
    order_id: str
    symbol: str
    action: str                      # adopt / cancel / void / wait / escalate
    new_state: Optional[str] = None
    backfilled_qty: Decimal = Decimal(0)
    note: str = ""

    @property
    def resolved(self) -> bool:
        return self.action in ("adopt", "cancel", "void")


class UnknownReconciler:
    """
    把 UNKNOWN 订单收敛成确定状态。

    只在 `run_once()` 里推进状态，调用方（调度器）决定跑多频繁。
    这样可以确定性地重放"第 3 轮对账时交易所回报了什么"。
    """

    def __init__(self, registry: OrderRegistry, gateway: VenueGateway, *,
                 on_backfill: Optional[BackfillSink] = None,
                 risk_admin=None,
                 max_unknown_sec: int = 300,
                 clock: Callable[[], datetime] = utcnow):
        self.registry = registry
        self.gateway = gateway
        self.on_backfill = on_backfill
        self.risk_admin = risk_admin          # RiskServiceClient 之类，提供 freeze_symbol
        self.max_unknown_sec = max_unknown_sec
        self.clock = clock
        self.history: List[Outcome] = []

    # ------------------------------------------------------------------ #
    #  主流程
    # ------------------------------------------------------------------ #

    def run_once(self, now: Optional[datetime] = None) -> List[Outcome]:
        now = (now or self.clock()).astimezone(UTC)
        outcomes: List[Outcome] = []
        for order in list(self.registry.unknown_orders()):
            outcomes.append(self._reconcile_one(order, now))
        for outcome in outcomes:
            self.history.append(outcome)
        return outcomes

    def _reconcile_one(self, order: Order, now: datetime) -> Outcome:
        try:
            view = self.gateway.query_order(order.client_order_id)
        except VenueQueryError as exc:
            return self._escalate(order, now, f"查询失败：{exc}")

        resolution = resolve_unknown_order(
            order_id=order.order_id,
            venue_status=view.status,
            venue_filled_qty=D(view.filled_qty),
            venue_found=view.found,
        )

        # ---- 等待型：保持 UNKNOWN，但超过时限就升级为人工作业 ------------- #
        if resolution.new_state is None or resolution.action == "wait":
            return self._escalate(order, now, resolution.reason)

        note = resolution.reason
        backfilled = Decimal(0)

        # ---- 先补录成交（若有）：订单自己的数量账才是权威 ------------------ #
        if D(view.filled_qty) > D(order.filled_qty) and view.avg_price is not None:
            backfilled = self._backfill(order, view, now)
            if backfilled > 0:
                note += f"；补录成交 {backfilled}@{view.avg_price}"
            else:
                note += "；补录被拒（数量超出剩余，保持 UNKNOWN 等人工）"

        # ---- 再对齐状态（已由成交推进过就不重复迁移） --------------------- #
        if order.state is OrderState.UNKNOWN:
            try:
                order.transition(resolution.new_state,
                                 f"reconcile:{resolution.action}", at=now)
            except IllegalTransition as exc:
                return self._escalate(order, now, f"状态迁移被拒：{exc}")

        # ---- 需要撤单的，主动撤一次（不重试、不重提） -------------------- #
        if resolution.action == "cancel":
            try:
                cancelled = self.gateway.cancel_order(order.client_order_id)
                note += "；撤单%s" % ("成功" if cancelled else "未成功，保持跟踪")
            except VenueQueryError as exc:
                note += f"；撤单失败：{exc}"

        # ---- 解冻 -------------------------------------------------------- #
        self._release_symbol_if_clear(order.symbol)

        return Outcome(
            client_order_id=order.client_order_id, order_id=order.order_id,
            symbol=order.symbol, action=resolution.action,
            new_state=order.state.value, backfilled_qty=q(backfilled, 8),
            note=note,
        )

    # ------------------------------------------------------------------ #
    #  补录与解冻
    # ------------------------------------------------------------------ #

    def _backfill(self, order: Order, view: VenueOrderView, now: datetime) -> Decimal:
        """
        把交易所已有、本地未记的成交量补进来。

        顺序是「先让订单状态机接纳，再写账本」：如果订单侧的数量账都通不过
        （例如交易所报的成交量超过我们下单量），说明数据本身有问题，
        此时写账本只会制造更难解的差异 —— 保持 UNKNOWN 交给人工。
        """
        delta = D(view.filled_qty) - D(order.filled_qty)
        if delta <= 0:
            return Decimal(0)
        price = D(view.avg_price)
        fee = D(view.fee) if view.fee else Decimal(0)
        # 确定性 trade id：同一 (cid, 累计成交量) 永远得到同一个 id，重复对账不重复记账。
        trade_id = f"recon:{order.client_order_id}:{D(view.filled_qty)}"
        fill = Fill(
            fill_id=trade_id, order_id=order.order_id,
            client_order_id=order.client_order_id, venue_trade_id=trade_id,
            quantity=q(delta, 8), price=price, fee=fee, fee_currency="USD", ts=now,
        )
        try:
            order.apply_fill(fill, at=now)
        except (ContractViolation, IllegalTransition):
            return Decimal(0)

        if self.on_backfill is None:
            return Decimal(0)
        posted = self.on_backfill(fill, order)
        return delta if posted else Decimal(0)

    def _release_symbol_if_clear(self, symbol: str) -> None:
        remaining = [o for o in self.registry.unknown_orders() if o.symbol == symbol]
        if remaining:
            return
        if self.risk_admin is not None:
            try:
                self.risk_admin.freeze_symbol(symbol, frozen=False)
            except Exception:                                # noqa: BLE001
                # 解冻失败不致命：风控侧仍冻结，下一轮继续尝试。
                pass

    def _escalate(self, order: Order, now: datetime, reason: str) -> Outcome:
        age = (now - (order.unknown_since or now)).total_seconds()
        action = "escalate" if age >= self.max_unknown_sec else "wait"
        return Outcome(
            client_order_id=order.client_order_id, order_id=order.order_id,
            symbol=order.symbol, action=action,
            new_state=order.state.value, note=f"{reason}（已未知 {int(age)}s）",
        )

    # ------------------------------------------------------------------ #
    #  观测
    # ------------------------------------------------------------------ #

    def summary(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for o in self.history:
            counts[o.action] = counts.get(o.action, 0) + 1
        return counts


__all__ = [
    "VenueQueryError", "VenueOrderView", "VenueGateway", "Outcome",
    "UnknownReconciler", "BackfillSink",
]
