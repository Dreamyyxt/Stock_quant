"""
QuantOS 2.0 — 订单管理（OMS，T-202 / T-203 的执行侧）
====================================================

这条链路上任何一处放松，结果都是"多下一单"或"该拒的单被放过去了"。
所以本模块的顺序是**写死的**：

    预分配幂等键 → 风控检查 → 注册订单 → 置为 SUBMITTED

不允许调整顺序。特别是不能"先注册再问风控"——那样风控拒绝后还得回滚订单，
回滚本身又是一次可能失败的状态变更。

三条硬规则
----------
1. **风控不可用 = 拒单**。`RiskServiceClient.check()` 已经保证不抛异常，
   这里再加一道：decision 为 None 或非 ALLOW，一律不下单。
2. **同一个 cid 不产生第二笔订单**。命中已有订单时返回 `reused=True`，
   这是"重试"的正确语义，不是"再下一单"。
3. **超时只迁 UNKNOWN，不重提**。重提的唯一合法路径是幂等键被交易所判定为同一笔。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from quantos_core.capital_allocator import build_order_intents
from quantos_core.codec import order_from_snapshot, order_to_snapshot
from quantos_core.contracts import (
    ContractViolation, D, DuplicateOrder, Fill, IllegalTransition, Order,
    OrderIntent, OrderRegistry, OrderState, OrderType, Side, TargetDecision,
    TargetPosition, Verdict,
)
from quantos_core.idempotency import ClientOrderIdStore, Reservation
from quantos_core.risk_engine import RiskContext, RiskDecision

UTC = timezone.utc
EventSink = Callable[[str, Mapping[str, Any]], None]


def _aware(dt: datetime, name: str = "at") -> datetime:
    if not isinstance(dt, datetime):
        raise TypeError(f"{name} 必须是 datetime")
    if dt.tzinfo is None:
        raise ValueError(f"{name} 必须带时区")
    return dt.astimezone(UTC)


@dataclass
class SubmitResult:
    """一次提交的结果。`reused=True` 表示这是重试命中，不是新订单。"""

    accepted: bool
    reason: str
    client_order_id: str
    decision: Optional[RiskDecision] = None
    order: Optional[Order] = None
    reused: bool = False
    attempt: int = 0

    def __bool__(self) -> bool:
        return self.accepted


class Oms:
    """
    单个 (账户, 策略域) 的订单管理器。

    多账户/多场所各自一个实例；跨实例的唯一共享物是 `ClientOrderIdStore`
    与风控服务，这样"单活锁"只需要保证同一实例不被两个进程同时持有。
    """

    def __init__(self, account_id: str, strategy_scope: str,
                 risk_client, idem: ClientOrderIdStore,
                 *, order_type: OrderType = OrderType.LIMIT,
                 limit_slippage_bps: Decimal = Decimal("10"),
                 event_sink: Optional[EventSink] = None,
                 order_id_prefix: str = "o"):
        if not account_id or not strategy_scope:
            raise ContractViolation("account_id / strategy_scope 不可为空")
        self.account_id = account_id
        self.strategy_scope = strategy_scope
        self.risk = risk_client
        self.idem = idem
        self.order_type = order_type
        self.limit_slippage_bps = limit_slippage_bps
        self.registry = OrderRegistry()
        self._event_sink = event_sink
        self._order_seq = 0
        self._intents: Dict[str, OrderIntent] = {}
        self.decisions: List[RiskDecision] = []

    # ------------------------------------------------------------------ #
    #  预分配：组合层目标 → 带持久化幂等键的下单意图
    # ------------------------------------------------------------------ #

    def prepare(self, target: TargetPosition, when: datetime) -> Tuple[Optional[OrderIntent], Reservation]:
        """
        把 TargetPosition 变成 OrderIntent，但**先**从持久化存储拿幂等键。

        这里的关键在于：cid 是 `(账户, 策略域, 标的, 日期, 序号)` 的纯函数，
        所以用存储里的序号重新生成，得到的就是那个已经存在的 cid。
        上层即使"重建意图"，也拿不到第二个号。
        """
        when = _aware(when, "when")
        if target.decision is not TargetDecision.TRADE or target.delta_qty == 0:
            raise ContractViolation("只有 TRADE 且有差额的目标才能准备下单")

        side = Side.BUY if target.delta_notional > 0 else Side.SELL
        res, _created = self.idem.reserve(
            self.account_id, self.strategy_scope, target.symbol, side, when)

        intents, _next = build_order_intents(
            [target], self.account_id, when, seq_start=res.seq,
            strategy_scope=self.strategy_scope, order_type=self.order_type,
            limit_slippage_bps=self.limit_slippage_bps)

        if not intents:
            return None, res
        intent = intents[0]
        if intent.client_order_id != res.client_order_id:
            # 纯函数性质被破坏，说明 make_client_order_id 的入参口径变了。
            raise ContractViolation(
                f"幂等键不一致：存储 {res.client_order_id} vs 生成 {intent.client_order_id}")
        return intent, res

    # ------------------------------------------------------------------ #
    #  提交
    # ------------------------------------------------------------------ #

    def submit(self, intent: OrderIntent, ctx: RiskContext) -> SubmitResult:
        cid = intent.client_order_id
        res = self.idem.get(cid)
        if res is None:
            raise ContractViolation(
                f"{cid} 未在幂等键存储登记，必须先调用 prepare()（R-A08）")

        # ---- 1. 已经存在订单 → 这是重试，直接复用 --------------------- #
        existing = self.registry.get(cid)
        if existing is not None:
            self._intents.setdefault(cid, intent)
            return SubmitResult(
                accepted=True, reason="duplicate_ignored", client_order_id=cid,
                order=existing, reused=True, attempt=res.attempts)

        # ---- 1b. 键显示"发过"但本地没订单 → 必须先对账，不许重发 ------ #
        #        这条挡的是"进程重启后忘了 restore()"：内存里空，但交易所那
        #        边可能已经有这笔单了。此时重发就是真实的重复下单。
        if res.attempts >= 1:
            self._emit("order_needs_reconcile", {
                "client_order_id": cid, "attempts": res.attempts,
                "reason": "幂等键显示已提交过，但本地无对应订单对象",
            })
            return SubmitResult(
                accepted=False, reason="needs_reconciliation", client_order_id=cid,
                attempt=res.attempts)

        # ---- 2. 风控检查（不可用即拒单） ------------------------------- #
        decision = self.risk.check(intent, ctx)
        self.decisions.append(decision)
        if decision is None or not decision.allowed:
            # 首次尝试就被拒 = 这笔从未发出，可以释放幂等键；
            # 若已经有尝试记录，则不能释放（不然等于允许重新下一单）。
            if res.attempts == 0:
                self.idem.mark(cid, "rejected")
            self._emit("order_rejected", {
                "client_order_id": cid, "policy_key": decision.policy_key
                if decision else "no_decision",
                "reason": decision.reason if decision else "风控未返回决策",
            })
            return SubmitResult(
                accepted=False,
                reason=decision.policy_key if decision else "no_decision",
                client_order_id=cid, decision=decision, attempt=res.attempts)

        # ---- 3. 注册订单并置为已提交 ---------------------------------- #
        self._order_seq += 1
        order_id = f"{intent.venue[:3].upper()}-{self._order_seq:06d}"
        try:
            order = self.registry.register(intent, order_id=order_id)
        except DuplicateOrder:
            existing = self.registry.get(cid)
            return SubmitResult(accepted=True, reason="duplicate_ignored",
                                client_order_id=cid, order=existing, reused=True)

        order.transition(OrderState.SUBMITTED, "risk_allowed")
        self._intents[cid] = intent
        attempt = self.idem.bump_attempt(cid)
        self.idem.mark(cid, "submitted", order.order_id)
        self._emit("order_submitted", {
            "client_order_id": cid, "order_id": order.order_id,
            "venue": order.venue, "symbol": order.symbol,
            "side": order.side.value, "quantity": order.quantity,
            "ref_price": order.ref_price, "attempt": attempt,
            "decision_id": decision.decision_id,
        })
        return SubmitResult(accepted=True, reason="submitted", client_order_id=cid,
                            decision=decision, order=order, attempt=attempt)

    # ------------------------------------------------------------------ #
    #  回报处理
    # ------------------------------------------------------------------ #

    def on_ack(self, client_order_id: str, at: Optional[datetime] = None) -> Optional[Order]:
        order = self.registry.get(client_order_id)
        if order is None:
            return None
        try:
            order.transition(OrderState.ACCEPTED, "venue_ack", at=_aware(at or _now()))
        except IllegalTransition:
            return order
        self.idem.mark(client_order_id, "accepted")
        return order

    def on_timeout(self, client_order_id: str, at: Optional[datetime] = None) -> Optional[Order]:
        """
        超时 → UNKNOWN → 冻结标的。**绝不重提**。

        冻结这一步很重要：如果不冻结，下一轮调度会以为"这笔没下成"，
        于是再下一笔，而交易所那边其实已经成交了。
        """
        order = self.registry.get(client_order_id)
        if order is None:
            return None
        before = order.state
        order.mark_timeout(at=_aware(at or _now()))
        if order.state is OrderState.UNKNOWN and before is not OrderState.UNKNOWN:
            self.idem.mark(client_order_id, "unknown")
            self._emit("order_unknown", {
                "client_order_id": client_order_id, "order_id": order.order_id,
                "symbol": order.symbol, "venue": order.venue,
            })
        return order

    def on_fill(self, fill: Fill, at: Optional[datetime] = None) -> bool:
        """成交入账。返回 True 表示是新成交（账本只对新成交记账）。"""
        order = self.registry.get(fill.client_order_id)
        if order is None:
            self._emit("fill_orphan", {"client_order_id": fill.client_order_id,
                                      "venue_trade_id": fill.venue_trade_id})
            return False
        fresh = self.registry.accept_fill(order, fill)
        if not fresh:
            return False
        state = "filled" if order.state is OrderState.FILLED else "partial"
        try:
            self.idem.mark(fill.client_order_id, state)
        except ContractViolation:
            pass
        self._emit("fill", {
            "client_order_id": fill.client_order_id,
            "venue_trade_id": fill.venue_trade_id,
            "quantity": fill.quantity, "price": fill.price, "fee": fill.fee,
            "state": order.state.value,
        })
        return True

    # ------------------------------------------------------------------ #
    #  查询
    # ------------------------------------------------------------------ #

    def open_orders(self) -> List[Order]:
        return self.registry.active_orders()

    def unknown_orders(self) -> List[Order]:
        return self.registry.unknown_orders()

    def frozen_symbols(self) -> set:
        return self.registry.symbols_with_unknown_state()

    def can_retry(self, client_order_id: str) -> bool:
        """重试次数用尽后必须转人工/查交易所，不许继续重试。"""
        res = self.idem.get(client_order_id)
        if res is None:
            return False
        return res.attempts < self.idem.max_attempts

    # ------------------------------------------------------------------ #
    #  重启恢复
    # ------------------------------------------------------------------ #

    def snapshot(self) -> List[Dict[str, Any]]:
        """
        导出未终态订单。进程重启前必须落盘，重启后 `restore()` 读回来。

        少了这一步，重启后的新进程在内存里看不到任何在途订单 ——
        它会以为"这笔还没下"，于是再下一次。这是最隐蔽的重复下单路径，
        因为它在正常情况下永远不出现，只在崩溃重启后出现。
        """
        out: List[Dict[str, Any]] = []
        for cid, intent in self._intents.items():
            order = self.registry.get(cid)
            if order is None or order.is_terminal:
                continue
            out.append(order_to_snapshot(order, intent))
        return out

    def restore(self, snapshots: Iterable[Mapping[str, Any]]) -> int:
        """从快照接管未终态订单，返回恢复条数。损坏的快照跳过而不是崩掉。"""
        restored = 0
        for snap in snapshots:
            try:
                order, intent = order_from_snapshot(snap)
            except (KeyError, TypeError, ValueError):
                continue
            if order.is_terminal:
                continue
            try:
                self.registry.adopt(order)
            except DuplicateOrder:
                continue
            self._intents[order.client_order_id] = intent
            self._order_seq = max(self._order_seq, _trailing_int(order.order_id))
            restored += 1
        return restored

    # ------------------------------------------------------------------ #
    #  内部
    # ------------------------------------------------------------------ #

    def _emit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        if self._event_sink is not None:
            self._event_sink(event_type, payload)


def _now() -> datetime:
    return datetime.now(UTC)


def _trailing_int(text: str) -> int:
    match = re.search(r"(\d+)$", str(text))
    return int(match.group(1)) if match else 0


__all__ = ["SubmitResult", "Oms", "EventSink"]
