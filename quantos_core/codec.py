"""
QuantOS 2.0 — 领域对象线格式（单一来源）
=======================================

同一个 `OrderIntent` 有三处要编码：
  - 风控服务：客户端 → 服务端（跨进程）
  - OMS：落盘快照 → 重启恢复
  - 事件日志：审计与回放

如果这三处各写一套，迟早会出现"某一处少了个字段"的情况。最典型的后果是
`reduce_only` 在传输中丢掉，于是 HALTED 状态下的减仓单被当成新增风险拒掉，
或者反过来 —— 一个反转的布尔值就能让熔断失效。

所以这里只放一份。任何新字段都先加到本文件，再改调用方。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Tuple

from quantos_core.contracts import (
    D, Fill, Order, OrderIntent, OrderState, OrderType, RiskLayer, Side, Verdict,
)
from quantos_core.risk_engine import RiskContext, RiskDecision

SymbolKey = Tuple[str, str]


# --------------------------------------------------------------------------- #
#  OrderIntent
# --------------------------------------------------------------------------- #

def intent_to_payload(intent: OrderIntent) -> Dict[str, Any]:
    return {
        "order_intent_id": intent.order_intent_id,
        "client_order_id": intent.client_order_id,
        "account_id": intent.account_id,
        "venue": intent.venue,
        "symbol": intent.symbol,
        "side": intent.side.value,
        "quantity": intent.quantity,
        "order_type": intent.order_type.value,
        "notional": intent.notional,
        "attribution": {str(k): D(v) for k, v in intent.attribution.items()},
        "reason_chain": [dict(step) for step in intent.reason_chain],
        "as_of": intent.as_of,
        "ts": intent.ts,
        "limit_price": intent.limit_price,
        "reduce_only": intent.reduce_only,
    }


def intent_from_payload(payload: Mapping[str, Any]) -> OrderIntent:
    return OrderIntent(
        order_intent_id=str(payload["order_intent_id"]),
        client_order_id=str(payload["client_order_id"]),
        account_id=str(payload["account_id"]),
        venue=str(payload["venue"]), symbol=str(payload["symbol"]),
        side=Side(str(payload["side"])),
        quantity=D(payload["quantity"]),
        order_type=OrderType(str(payload["order_type"])),
        notional=D(payload["notional"]),
        attribution={str(k): D(v) for k, v in (payload.get("attribution") or {}).items()},
        reason_chain=tuple(dict(s) for s in (payload.get("reason_chain") or ())),
        as_of=payload["as_of"], ts=payload["ts"],
        limit_price=D(payload["limit_price"]) if payload.get("limit_price") else None,
        reduce_only=bool(payload.get("reduce_only", False)),
    )


# --------------------------------------------------------------------------- #
#  RiskContext
# --------------------------------------------------------------------------- #

def ctx_to_payload(ctx: RiskContext) -> Dict[str, Any]:
    return {
        "nav": ctx.nav, "now": ctx.now,
        # 元组 key 转 "venue|symbol"：JSON 不支持元组 key，回读时再拆开。
        "position_notional": {f"{k[0]}|{k[1]}": v
                              for k, v in ctx.position_notional.items()},
        "strategy_notional": dict(ctx.strategy_notional),
        "strategy_turnover": dict(ctx.strategy_turnover),
        "strategy_capacity": dict(ctx.strategy_capacity),
        "market_data_age_sec": dict(ctx.market_data_age_sec),
        "total_gross_notional": ctx.total_gross_notional,
    }


def ctx_from_payload(payload: Mapping[str, Any]) -> RiskContext:
    position: Dict[SymbolKey, Decimal] = {}
    for key, value in (payload.get("position_notional") or {}).items():
        venue, _, symbol = str(key).partition("|")
        position[(venue, symbol)] = D(value)
    def _dec_map(name: str) -> Dict[str, Decimal]:
        return {str(k): D(v) for k, v in (payload.get(name) or {}).items()}
    return RiskContext(
        nav=D(payload["nav"]), now=payload["now"],
        position_notional=position,
        strategy_notional=_dec_map("strategy_notional"),
        strategy_turnover=_dec_map("strategy_turnover"),
        strategy_capacity=_dec_map("strategy_capacity"),
        market_data_age_sec=_dec_map("market_data_age_sec"),
        total_gross_notional=D(payload.get("total_gross_notional", 0)),
    )


# --------------------------------------------------------------------------- #
#  RiskDecision
# --------------------------------------------------------------------------- #

def decision_to_payload(decision: RiskDecision) -> Dict[str, Any]:
    return decision.to_dict()


def decision_from_payload(payload: Mapping[str, Any]) -> RiskDecision:
    return RiskDecision(
        decision_id=str(payload["decision_id"]),
        layer=RiskLayer(str(payload["layer"])),
        policy_key=str(payload["policy_key"]),
        verdict=Verdict(str(payload["verdict"])),
        subject_id=str(payload["subject_id"]),
        inputs={str(k): str(v) for k, v in (payload.get("inputs") or {}).items()},
        reason=str(payload.get("reason", "")),
        ts=payload["ts"],
        cap_notional=D(payload["cap_notional"]) if payload.get("cap_notional") else None,
        halt_actions=tuple(str(a) for a in (payload.get("halt_actions") or ())),
    )


# --------------------------------------------------------------------------- #
#  Order 快照（重启恢复用）
# --------------------------------------------------------------------------- #

def order_to_snapshot(order: Order, intent: Optional[OrderIntent] = None) -> Dict[str, Any]:
    """
    快照必须同时含 intent。只存订单状态是不够的：恢复后要拿 attribution 做
    策略归属、拿 reduce_only 判断权限，缺了就只能在恢复时瞎猜。
    """
    snap: Dict[str, Any] = {
        "client_order_id": order.client_order_id,
        "order_id": order.order_id,
        "state": order.state.value,
        "filled_qty": order.filled_qty,
        "avg_fill_price": order.avg_fill_price,
    }
    if intent is not None:
        snap["intent"] = intent_to_payload(intent)
    return snap


def order_from_snapshot(snap: Mapping[str, Any]) -> Tuple[Order, Optional[OrderIntent]]:
    intent = intent_from_payload(snap["intent"]) if snap.get("intent") else None
    if intent is None:
        raise ValueError("订单快照缺少 intent，无法恢复（会导致归属与权限信息丢失）")
    order = Order(
        order_id=str(snap["order_id"]), client_order_id=intent.client_order_id,
        account_id=intent.account_id, venue=intent.venue, symbol=intent.symbol,
        side=intent.side, quantity=D(intent.quantity),
        ref_price=D(intent.notional) / D(intent.quantity),
        filled_qty=D(snap.get("filled_qty", 0)),
        avg_fill_price=D(snap["avg_fill_price"]) if snap.get("avg_fill_price") else None,
    )
    # 快照恢复是"直接置状态"，不走 transition：历史迁移已经在旧进程里发生过了。
    order.state = OrderState(str(snap["state"]))
    if order.state is OrderState.UNKNOWN:
        order.unknown_since = intent.ts
    return order, intent


__all__ = [
    "intent_to_payload", "intent_from_payload",
    "ctx_to_payload", "ctx_from_payload",
    "decision_to_payload", "decision_from_payload",
    "order_to_snapshot", "order_from_snapshot",
]
