"""
QuantOS 2.0 — 事件回放器（T-103）
================================

把事件流按 seq 顺序重放，重建出：行情视图、组合目标、风控决策、订单状态、账本。

为什么这件事必须做
------------------
回测赚钱、实盘亏钱，最常见的原因不是策略不行，而是**两边算法不是同一套**。
回放器存在的意义是：给定同一段事件流，任何人都能复现出当时的目标持仓与账本，
并且两次回放结果必须逐字段一致（`result.digest` 相同）。

确定性从哪来
------------
1. 不读系统时间：所有 `now` 来自 `cycle` 事件的 payload。
2. 不读随机数、不读全局状态：每次 run 都 `_reset()`。
3. 订单号由回放内序号生成（`o-000001`），不依赖券商返回值。
4. **卖出/买入的价格来自行情事件**，缺失就 fail-closed 跳过该轮，不用 0 兜底。

支持的事件类型
--------------
  deposit / withdraw      资金变动（进出金不进 pnl，R-D03）
  market_snapshot         行情快照（MarketSnapshot）
  strategy_intent         策略意图（AdapterConfig 产出）
  cycle                   触发一次「净额 → 风控 → 下单」
  order_ack               券商确认（SUBMITTED → ACCEPTED）
  order_timeout           超时（→ UNKNOWN，冻结标的，绝不重提）
  venue_status            对账结果（UNKNOWN → 确定状态）
  fill                    成交回报（venue_trade_id 去重）
  permission              权限级变更（NORMAL/REDUCE_ONLY/HALTED）
  strategy_state          单策略状态变更（ACTIVE/REDUCE_ONLY/PAUSED）
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from quantos_core.capital_allocator import (
    StrategyPolicy, allocate_all, build_order_intents, net_targets, NettingInput,
)
from quantos_core.contracts import (
    ContractViolation, D, Fill, IllegalTransition, Order, OrderIntent, OrderState,
    OrderType, PermissionLevel, StrategyIntent, StrategyRunState, TargetDecision,
    TargetPosition, Verdict, q,
)
from quantos_core.cost import CostEstimate, CostModel
from quantos_core.events import EventEnvelope, EventStore, canonical_json
from quantos_core.ledger import Ledger, PerformanceReport
from quantos_core.market import MarketDataBook, MarketSnapshot, PriceUnavailable
from quantos_core.risk_engine import (
    RiskContext, RiskDecision, RiskEngine, RiskPolicy, RiskState, resolve_unknown_order,
)
from quantos_core.strategy_adapter import intent_from_event

UTC = timezone.utc
SymbolKey = Tuple[str, str]

#: 会被回放器消费的事件类型。未知类型一律忽略（前向兼容，但会计数）。
KNOWN_EVENTS = frozenset({
    "deposit", "withdraw", "market_snapshot", "strategy_intent", "cycle",
    "order_ack", "order_timeout", "order_cancel", "venue_status", "fill",
    "permission", "strategy_state",
})


# --------------------------------------------------------------------------- #
#  配置与结果
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ReplayConfig:
    account_id: str
    strategy_policies: Sequence[StrategyPolicy]
    risk_policy: RiskPolicy = field(default_factory=RiskPolicy)
    cost_model: CostModel = field(default_factory=CostModel)
    base_currency: str = "USD"
    strategy_scope: str = "QOS"
    order_type: OrderType = OrderType.LIMIT
    limit_slippage_bps: Decimal = Decimal("10")
    #: 各场所最小下单金额。低于此值组合层判 NOOP，不产生碎单。
    min_notional: Mapping[str, Decimal] = field(default_factory=dict)

    def __post_init__(self):
        if not self.account_id:
            raise ContractViolation("ReplayConfig.account_id 不可为空")


@dataclass
class ReplayResult:
    """回放产物。`digest` 是"两次回放是否一致"的唯一判据。"""

    config: ReplayConfig
    ledger: Ledger
    risk_state: RiskState
    targets: List[TargetPosition] = field(default_factory=list)
    decisions: List[RiskDecision] = field(default_factory=list)
    orders: Dict[str, Any] = field(default_factory=dict)
    cost_estimates: List[CostEstimate] = field(default_factory=list)
    fills: int = 0
    cycles: int = 0
    blocked_cycles: List[Dict[str, str]] = field(default_factory=list)
    unknown_events: List[str] = field(default_factory=list)
    orphan_fills: List[str] = field(default_factory=list)
    events_read: int = 0
    digest: str = ""

    # -- 汇总 ------------------------------------------------------------ #

    @property
    def rejected(self) -> int:
        return sum(1 for d in self.decisions if not d.allowed)

    def performance(self, prices: Mapping[SymbolKey, Decimal]) -> PerformanceReport:
        return self.ledger.performance(prices)

    def summary(self) -> Dict[str, Any]:
        by_verdict: Dict[str, int] = {}
        for d in self.decisions:
            by_verdict[d.verdict.value] = by_verdict.get(d.verdict.value, 0) + 1
        return {
            "digest": self.digest,
            "events_read": self.events_read,
            "cycles": self.cycles,
            "blocked_cycles": len(self.blocked_cycles),
            "targets": len(self.targets),
            "decisions": len(self.decisions),
            "by_verdict": by_verdict,
            "orders": len(self.orders),
            "fills": self.fills,
            "orphan_fills": list(self.orphan_fills),
            "unknown_events": list(self.unknown_events),
            "ledger_entries": self.ledger.entry_count,
            "permission_level": self.risk_state.permission_level.value,
            "halted_reasons": list(self.risk_state.halted_reasons),
            "frozen_symbols": sorted(self.risk_state.symbols_with_unknown_orders),
        }


# --------------------------------------------------------------------------- #
#  成交归属换算
# --------------------------------------------------------------------------- #

def _rescale_attribution(attribution: Optional[Mapping[str, Decimal]],
                         filled: Decimal, ordered: Decimal
                         ) -> Dict[str, Decimal]:
    """
    把「按下单量算好的策略归属」换算成「按这次成交量算的归属」。

    为什么必须有这一步：归属是在下单那一刻按整单量切好的，而真实成交
    经常不是整单（缩量、整手取整、零股残量、部分成交）。直接把整单归属
    记到一笔小成交上，`attr_qty` 就会比真实持仓还多 —— 之后每轮净额算出的
    卖单都比持仓大一点，风控判它"穿过零轴开空单"拒掉，组合就只进不出。

    尾差补到最大的一笔上，保证 `sum(结果) == filled` 严格成立：否则每次
    成交都会漏下一点零头，几千笔之后策略归属和真实持仓对不上账。
    """
    if not attribution:
        return {}
    ordered = D(ordered)
    filled = D(filled)
    if ordered <= 0:
        return {sid: D(0) for sid in attribution}
    ratio = filled / ordered
    out = {sid: q(D(v) * ratio, 8) for sid, v in attribution.items()}
    resid = filled - sum(out.values(), D(0))
    if resid != 0:
        big = max(out, key=lambda s: (abs(out[s]), s))
        out[big] = q(out[big] + resid, 8)
    return out


# --------------------------------------------------------------------------- #
#  回放器
# --------------------------------------------------------------------------- #

class Replayer:
    """
    有状态回放机。**每次 run 前必须 reset**，否则第二次跑会带着上一次的账本，
    确定性就没了 —— 这是本类唯一的易错点，所以 run() 自己先 reset。
    """

    def __init__(self, config: ReplayConfig):
        self.config = config
        self.engine = RiskEngine(config.risk_policy)
        self._reset()

    # ------------------------------------------------------------------ #
    #  状态初始化
    # ------------------------------------------------------------------ #

    def _reset(self) -> None:
        self.ledger = Ledger(base_currency=self.config.base_currency)
        self.risk_state = RiskState()
        self.book = MarketDataBook()
        self._pending: List[StrategyIntent] = []
        self._targets: List[TargetPosition] = []
        self._decisions: List[RiskDecision] = []
        self._cost_estimates: List[CostEstimate] = []
        self._intent_by_cid: Dict[str, OrderIntent] = {}
        self._orders: Dict[str, Order] = {}
        self._trade_ids: Set[str] = set()
        self._inflight: Dict[SymbolKey, Decimal] = {}
        self._turnover: Dict[str, Decimal] = {}
        self._order_seq = 0
        self._cid_seq = 0
        self._fills = 0
        self._cycles = 0
        self._events = 0
        self._blocked: List[Dict[str, str]] = []
        self._unknown_events: List[str] = []
        self._orphan_fills: List[str] = []
        self._policies = {p.strategy_id: p for p in self.config.strategy_policies}

    # ------------------------------------------------------------------ #
    #  增量接口（B12）：让回测能一根 bar 一根 bar 地推进同一个回放器
    # ------------------------------------------------------------------ #
    #
    #  为什么需要这个而不是"回测自己写一套循环"
    #  ------------------------------------
    #  回测最怕的东西是「回测的账和实盘的账是两份代码」。一旦分成两份，
    #  两边对不上时没人能判断是策略问题还是记账问题。
    #  有了 apply()，回测就只是**事件的生产者**：它负责把历史 bar 翻译成
    #  market_snapshot / cycle / fill 这些事件，账务、风控、净额全部还是
    #  这套回放器在做 —— 和实盘回放时走的是同一条路。

    def apply(self, env: EventEnvelope) -> None:
        """喂一个事件。调用方保证 seq 递增（回放器不校验，因为它不该管编号）。"""
        self._events += 1
        self._dispatch(env)

    def pending_orders(self) -> List[Order]:
        """
        还没走到终态的订单（SUBMITTED / ACCEPTED / PARTIAL 等），按提交顺序。

        回测用它决定"明天开盘要撮合哪些单"。**只读**：不改状态，
        所以调用方可以放心地在撮合前先看一眼。
        """
        return [o for o in self._orders.values() if not o.is_terminal]

    def snapshot(self) -> ReplayResult:
        """当前状态的结果快照。`run()` 结束时也走这里，保证两条路产物一致。"""
        result = ReplayResult(
            config=self.config, ledger=self.ledger, risk_state=self.risk_state,
            targets=list(self._targets), decisions=list(self._decisions),
            orders={cid: self._order_view(cid) for cid in self._intent_by_cid},
            cost_estimates=list(self._cost_estimates),
            fills=self._fills, cycles=self._cycles,
            blocked_cycles=list(self._blocked),
            unknown_events=sorted(set(self._unknown_events)),
            orphan_fills=list(self._orphan_fills),
            events_read=self._events,
        )
        result.digest = self._digest(result)
        return result

    # ------------------------------------------------------------------ #
    #  主循环
    # ------------------------------------------------------------------ #

    def run(self, store: EventStore, upto_seq: Optional[int] = None) -> ReplayResult:
        self._reset()
        for env in store.read(1, upto_seq):
            self.apply(env)
        return self.snapshot()

    def _dispatch(self, env: EventEnvelope) -> None:
        handler = {
            "deposit": self._on_deposit,
            "withdraw": self._on_withdraw,
            "market_snapshot": self._on_market,
            "strategy_intent": self._on_intent,
            "cycle": self._on_cycle,
            "order_ack": self._on_ack,
            "order_timeout": self._on_timeout,
            "order_cancel": self._on_cancel,
            "venue_status": self._on_venue_status,
            "fill": self._on_fill,
            "permission": self._on_permission,
            "strategy_state": self._on_strategy_state,
        }.get(env.event_type)
        if handler is None:
            self._unknown_events.append(env.event_type)
            return
        handler(env)

    # ------------------------------------------------------------------ #
    #  资金
    # ------------------------------------------------------------------ #

    def _on_deposit(self, env: EventEnvelope) -> None:
        p = env.payload
        self.ledger.deposit(
            str(p.get("account_id", self.config.account_id)),
            str(p.get("currency", self.config.base_currency)),
            D(p["amount"]), fx_to_base=D(p.get("fx_to_base", 1)), ts=env.ts)

    def _on_withdraw(self, env: EventEnvelope) -> None:
        p = env.payload
        self.ledger.withdraw(
            str(p.get("account_id", self.config.account_id)),
            str(p.get("currency", self.config.base_currency)),
            D(p["amount"]), fx_to_base=D(p.get("fx_to_base", 1)), ts=env.ts)

    # ------------------------------------------------------------------ #
    #  行情 / 意图
    # ------------------------------------------------------------------ #

    def _on_market(self, env: EventEnvelope) -> None:
        p = env.payload
        snap = MarketSnapshot(
            venue=str(p["venue"]), symbol=str(p["symbol"]),
            as_of=p["as_of"], received_at=p.get("received_at", env.ts),
            bid=p.get("bid"), ask=p.get("ask"), last=p.get("last"),
            source=str(p.get("source", "replay")), sequence=p.get("sequence"),
        )
        # 回放时用事件自身时间判乱序，而不是墙钟，保证可复现。
        self.book.update(snap, now=max(env.ts, snap.received_at))

    def _on_intent(self, env: EventEnvelope) -> None:
        try:
            self._pending.append(intent_from_event(env))
        except ContractViolation as exc:
            self._blocked.append({"seq": str(env.seq), "reason": f"bad_intent:{exc}"})

    # ------------------------------------------------------------------ #
    #  决策轮：净额 → 风控 → 下单
    # ------------------------------------------------------------------ #

    def _on_cycle(self, env: EventEnvelope) -> None:
        now = env.payload.get("now") or env.as_of
        if not self._pending:
            return
        self._cycles += 1

        # ---- 1. 估值：任何持仓拿不到价就整轮跳过（fail-closed） -------- #
        try:
            prices = self._valuation_prices()
        except (PriceUnavailable, ContractViolation) as exc:
            self._blocked.append({"seq": str(env.seq), "reason": f"no_price:{exc}"})
            return

        nav = self.ledger.nav(prices)
        allocations = allocate_all(self.config.strategy_policies, nav)

        # ---- 2. 净额汇总（含在途扣除） -------------------------------- #
        inp = NettingInput(
            intents=tuple(self._pending),
            allocations=allocations,
            policies=self._policies,
            prices=prices,
            current_notional=self._position_notional(prices),
            inflight_notional=dict(self._inflight),
            strategy_states=self.risk_state.strategy_status,
            now=now,
            min_notional=dict(self.config.min_notional),
            # 按策略算差额：暂停策略的存量只冻结，不再拖累整只标的（B3）。
            current_by_strategy=self.ledger.exposure_by_strategy_symbol(prices),
        )
        targets = net_targets(inp)
        self._targets.extend(targets)

        # ---- 3. 逐个目标过风控 ---------------------------------------- #
        for target in targets:
            if target.decision is not TargetDecision.TRADE:
                continue
            self._process_target(target, now, nav, allocations, prices)

        self._pending.clear()

    def _process_target(self, target: TargetPosition, now, nav: Decimal,
                        allocations: Mapping, prices: Mapping[SymbolKey, Decimal]) -> None:
        intents, self._cid_seq = build_order_intents(
            [target], self.config.account_id, now, seq_start=self._cid_seq + 1,
            strategy_scope=self.config.strategy_scope,
            order_type=self.config.order_type,
            limit_slippage_bps=self.config.limit_slippage_bps)

        for intent in intents:
            decision = self.engine.check_order(
                intent, self._risk_context(now, nav, allocations, prices),
                self.risk_state)
            self._decisions.append(decision)

            if decision.verdict is Verdict.ALLOW:
                self._accept(intent, nav)
                continue

            if decision.verdict is Verdict.REDUCE_REQUIRED and decision.cap_notional:
                # 风控只给上限，重算由组合层做（契约 T3→T4）。只重试一次。
                capped = target.capped(decision.cap_notional)
                if capped.delta_qty == 0:
                    continue
                retry, self._cid_seq = build_order_intents(
                    [capped], self.config.account_id, now,
                    seq_start=self._cid_seq + 1,
                    strategy_scope=self.config.strategy_scope,
                    order_type=self.config.order_type,
                    limit_slippage_bps=self.config.limit_slippage_bps)
                for r in retry:
                    d2 = self.engine.check_order(
                        r, self._risk_context(now, nav, allocations, prices),
                        self.risk_state)
                    self._decisions.append(d2)
                    if d2.verdict is Verdict.ALLOW:
                        self._accept(r, nav)

    def _accept(self, intent: OrderIntent, nav: Decimal) -> None:
        self._order_seq += 1
        order = Order(
            order_id=f"o-{self._order_seq:06d}",
            client_order_id=intent.client_order_id,
            account_id=intent.account_id, venue=intent.venue, symbol=intent.symbol,
            side=intent.side, quantity=D(intent.quantity),
            ref_price=D(intent.notional) / D(intent.quantity),
        )
        order.transition(OrderState.SUBMITTED, "risk_allowed")
        self._intent_by_cid[intent.client_order_id] = intent
        self._orders[intent.client_order_id] = order
        self._refresh_inflight()

        estimate = self.config.cost_model.estimate(
            intent.venue, intent.side, intent.quantity, order.ref_price, nav=nav)
        self._cost_estimates.append(estimate)

    # ------------------------------------------------------------------ #
    #  订单状态
    # ------------------------------------------------------------------ #

    def _on_ack(self, env: EventEnvelope) -> None:
        order = self._order(env.payload.get("client_order_id"))
        if order is None:
            return
        try:
            order.transition(OrderState.ACCEPTED, "venue_ack", at=env.as_of)
        except Exception:                                  # noqa: BLE001
            pass

    def _on_timeout(self, env: EventEnvelope) -> None:
        order = self._order(env.payload.get("client_order_id"))
        if order is None:
            return
        before = order.state
        order.mark_timeout(at=env.as_of)
        if order.state is OrderState.UNKNOWN and before is not OrderState.UNKNOWN:
            # 超时 = 状态未知，冻结标的；绝不重提（R-A08）。
            self.risk_state.symbols_with_unknown_orders.add(order.symbol)

    def _on_cancel(self, env: EventEnvelope) -> None:
        """
        主动撤单（B12 回测需要，实盘撤单走同一条路）。

        为什么不能复用 `venue_status`：`resolve_unknown_order` 是给「状态未知」
        设计的状态消解器，它看到 `filled_qty > 0` 就一定会落到 PARTIALLY_FILLED。
        而"部分成交后撤掉残量"恰恰是它表达不了的结论 —— 那种单子会永远挂在
        在途里，把该标的的额度一直占着。撤单是一件独立的事实，就该有独立的事件。
        """
        order = self._order(env.payload.get("client_order_id"))
        if order is None:
            return
        try:
            order.transition(OrderState.CANCELLED, "cancel", at=env.as_of)
        except Exception:                                  # noqa: BLE001
            return
        self._refresh_inflight()

    def _on_venue_status(self, env: EventEnvelope) -> None:
        p = env.payload
        order = self._order(p.get("client_order_id"))
        if order is None:
            return
        res = resolve_unknown_order(
            order_id=order.order_id,
            venue_status=p.get("venue_status"),
            venue_filled_qty=D(p.get("venue_filled_qty", 0)),
            venue_found=bool(p.get("venue_found", True)),
        )
        if res.new_state is None:
            return
        try:
            order.transition(res.new_state, f"reconcile:{res.action}", at=env.as_of)
        except Exception:                                  # noqa: BLE001
            return
        if order.state is not OrderState.UNKNOWN:
            self.risk_state.symbols_with_unknown_orders.discard(order.symbol)
        self._refresh_inflight()

    def _on_fill(self, env: EventEnvelope) -> None:
        p = env.payload
        cid = str(p.get("client_order_id", ""))
        order = self._order(cid)
        if order is None:
            self._orphan_fills.append(f"seq={env.seq} cid={cid}")
            return

        fill = Fill(
            fill_id=str(p.get("fill_id") or p["venue_trade_id"]),
            order_id=order.order_id, client_order_id=cid,
            venue_trade_id=str(p["venue_trade_id"]),
            quantity=D(p["quantity"]), price=D(p["price"]),
            fee=D(p.get("fee", 0)),
            fee_currency=str(p.get("fee_currency", self.config.base_currency)),
            ts=p.get("ts") or env.as_of,
            liquidity=str(p.get("liquidity", "TAKER")),
        )

        # 去重：同一 venue_trade_id 只记一次账（重复回报是常态）。
        if fill.venue_trade_id in self._trade_ids:
            return
        try:
            order.apply_fill(fill, at=env.as_of)
        except (ContractViolation, IllegalTransition):
            self._orphan_fills.append(f"seq={env.seq} bad_fill cid={cid}")
            return
        self._trade_ids.add(fill.venue_trade_id)

        intent = self._intent_by_cid.get(cid)
        if intent is None:
            self._orphan_fills.append(f"seq={env.seq} no_intent cid={cid}")
            return

        posted = self.ledger.apply_fill(
            fill, venue=order.venue, symbol=order.symbol,
            account_id=order.account_id, side=order.side,
            quote_ccy=str(p.get("quote_ccy", self.config.base_currency)),
            decision_price=order.ref_price,
            # 归属必须按**成交量**换算，不是按下单量。
            #
            # `intent.attribution` 是下单那一刻按整单量算好的。部分成交
            # （缩量、整手取整、零股残量）时直接把它传下去，等于让策略凭空
            # 多出股份：`attr_qty` 会超过真实持仓，于是"策略敞口"虚高，
            # 下一次 netting 算出的卖单比持仓还大 1% —— 风控判它"穿过零轴
            # 开空单"直接拒掉，组合只进不出，而报告上只表现为"换手不高"。
            attribution=_rescale_attribution(intent.attribution,
                                             D(fill.quantity), D(intent.quantity)),
            ts=fill.ts)
        if posted:
            self._fills += 1
            notional = D(fill.quantity) * D(fill.price)
            for sid, qty in intent.attribution.items():
                share = (D(qty) / D(intent.quantity)) * notional if D(intent.quantity) else Decimal(0)
                self._turnover[sid] = self._turnover.get(sid, Decimal(0)) + share
        self._refresh_inflight()

    # ------------------------------------------------------------------ #
    #  权限与策略状态
    # ------------------------------------------------------------------ #

    def _on_permission(self, env: EventEnvelope) -> None:
        p = env.payload
        level = str(p.get("level", "")).upper()
        reason = str(p.get("reason", "replay_event"))
        if level == PermissionLevel.NORMAL.value:
            self.risk_state.resume(reason)
        elif level == PermissionLevel.REDUCE_ONLY.value:
            self.risk_state.reduce_only(reason)
        elif level == PermissionLevel.HALTED.value:
            self.risk_state.halt(reason)

    def _on_strategy_state(self, env: EventEnvelope) -> None:
        p = env.payload
        try:
            state = StrategyRunState(str(p["state"]).upper())
        except (KeyError, ValueError):
            return
        self.risk_state.set_strategy(str(p["strategy_id"]), state)

    # ------------------------------------------------------------------ #
    #  工具
    # ------------------------------------------------------------------ #

    def _order(self, cid) -> Optional[Order]:
        if not cid:
            return None
        return self._orders.get(str(cid))

    def _order_view(self, cid: str) -> Dict[str, str]:
        order = self._orders.get(cid)
        if order is None:
            return {}
        return {
            "order_id": order.order_id,
            "client_order_id": order.client_order_id,
            "state": order.state.value,
            "filled_qty": str(order.filled_qty),
            "avg_fill_price": str(order.avg_fill_price or 0),
            "remaining_qty": str(order.remaining_qty),
        }

    def _refresh_inflight(self) -> None:
        self._inflight = {}
        for order in self._orders.values():
            key = (order.venue, order.symbol)
            self._inflight[key] = self._inflight.get(key, Decimal(0)) + order.signed_inflight

    def _valuation_prices(self) -> Dict[SymbolKey, Decimal]:
        """持仓 + 待处理意图涉及的标的，全部要有价。缺一个就抛。"""
        keys: Set[SymbolKey] = set()
        for key, pos in self.ledger.positions().items():
            keys.add(key)
        for intent in self._pending:
            keys.add((intent.venue, intent.symbol))
        for key in self._inflight:
            if self._inflight[key] != 0:
                keys.add(key)
        return self.book.prices(keys)

    def _position_notional(self, prices: Mapping[SymbolKey, Decimal]
                           ) -> Dict[SymbolKey, Decimal]:
        out: Dict[SymbolKey, Decimal] = {}
        for key, pos in self.ledger.positions().items():
            out[key] = q(pos.qty * D(prices[key]), 2)
        return out

    def _risk_context(self, now, nav: Decimal, allocations: Mapping,
                      prices: Mapping[SymbolKey, Decimal]) -> RiskContext:
        # 在途订单要算进总敞口，否则同一轮里两笔递增单会互相"看不见"。
        inflight_gross = sum((abs(v) for v in self._inflight.values()), Decimal(0))
        return RiskContext(
            nav=nav, now=now,
            position_notional=self._position_notional(prices),
            strategy_notional=self.ledger.exposure_by_strategy(prices),
            strategy_turnover=dict(self._turnover),
            strategy_capacity={sid: a.effective_capacity_usd
                               for sid, a in allocations.items()},
            market_data_age_sec=self.book.age_sec_by_venue(now),
            total_gross_notional=self.ledger.gross_exposure(prices) + inflight_gross,
        )

    def _digest(self, result: ReplayResult) -> str:
        """
        指纹只覆盖**确定性内容**：目标、决策、订单状态、账本、绩效。
        故意排除 Order.history 的时间戳（那里有 utcnow），否则每次跑都不一样。
        """
        payload = {
            "targets": [{
                "venue": t.venue, "symbol": t.symbol,
                "net_target_notional": t.net_target_notional,
                "delta_notional": t.delta_notional,
                "net_target_qty": t.net_target_qty, "delta_qty": t.delta_qty,
                "decision": t.decision.value, "blocked_reason": t.blocked_reason,
                "per_strategy": {k: str(v) for k, v in t.per_strategy.items()},
            } for t in result.targets],
            "decisions": [{
                "decision_id": d.decision_id, "layer": d.layer.value,
                "policy_key": d.policy_key, "verdict": d.verdict.value,
                "subject_id": d.subject_id,
                "cap_notional": str(d.cap_notional) if d.cap_notional is not None else None,
            } for d in result.decisions],
            "orders": result.orders,
            "ledger": result.ledger.journal(),
            "fills": result.fills,
            "cycles": result.cycles,
            "blocked": result.blocked_cycles,
            "permission": result.risk_state.permission_level.value,
            "frozen": sorted(result.risk_state.symbols_with_unknown_orders),
        }
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


__all__ = ["KNOWN_EVENTS", "ReplayConfig", "ReplayResult", "Replayer"]
