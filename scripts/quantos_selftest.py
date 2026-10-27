#!/usr/bin/env python3
"""
QuantOS 2.0 契约层自测（B0 DoD）
================================

覆盖 `docs/QuantOS/02-服务数据契约.md` 的可验证断言，不依赖网络、券商、数据库。
全部是确定性输入 → 确定性输出，任何一条失败都说明契约实现与文档不一致。

    .venv/bin/python scripts/quantos_selftest.py

场景：
  S1 订单状态机与幂等          （R-A08）
  S2 资金分配与多策略净额      （R-A07 / R-C03 / R-C04）
  S3 三道防线与权限级          （R-K01..K04）
  S4 复式账本与绩效归因        （R-D01..D04）
  S5 端到端：冲突 → 风控限额 → 成交 → 未知状态 → 对账
"""

from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quantos_core.capital_allocator import (          # noqa: E402
    NettingInput, StrategyPolicy, allocate, allocate_all, build_order_intents,
    net_targets,
)
from quantos_core.contracts import (                  # noqa: E402
    AllocationMode, ContractViolation, D, DuplicateOrder, Fill, IllegalTransition,
    Order, OrderIntent, OrderRegistry, OrderState, OrderType, PermissionLevel,
    Side, StrategyIntent, StrategyRunState, TargetDecision, Verdict,
    make_client_order_id, utcnow,
)
from quantos_core.ledger import (                     # noqa: E402
    AttributionMismatch, Ledger, LedgerEntry, LedgerLine, UnbalancedEntry,
)
from quantos_core.risk_engine import (                # noqa: E402
    HaltAction, RiskContext, RiskEngine, RiskPolicy, RiskState,
    resolve_unknown_order,
)

NOW = datetime(2026, 10, 26, 1, 0, 0, tzinfo=timezone.utc)
NAV = D("105200")


# --------------------------------------------------------------------------- #
#  测试框架（极简，不引 pytest：项目 venv 里没有，零依赖优先）
# --------------------------------------------------------------------------- #

class Runner:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: List[Tuple[str, str]] = []
        self.section_name = ""

    def section(self, title: str) -> None:
        self.section_name = title
        print(f"\n\033[1m{title}\033[0m")

    def run(self, label: str, fn) -> None:
        try:
            fn()
        except Exception as exc:                       # noqa: BLE001
            detail = f"{type(exc).__name__}: {exc}"
            self.failed.append((label, detail))
            print(f"  \033[31mFAIL\033[0m {label}")
            print(f"         {detail}")
            if os.getenv("QOS_TRACE"):
                traceback.print_exc()
        else:
            self.passed += 1
            print(f"  \033[32mPASS\033[0m {label}")

    def summary(self) -> int:
        total = self.passed + len(self.failed)
        print("\n" + "=" * 66)
        if self.failed:
            print(f"\033[31mFAILED\033[0m  {self.passed}/{total} 通过，"
                  f"{len(self.failed)} 条未过：")
            for label, detail in self.failed:
                print(f"  - {label}: {detail}")
            return 1
        print(f"\033[32mALL PASS\033[0m  {self.passed}/{total} 条断言全部通过")
        return 0


R = Runner()


def eq(actual, expected, what: str = "") -> None:
    """数值按 Decimal 严格比较；非数值退化为字符串比较。"""
    try:
        same = D(actual) == D(expected)
        shown = f"期望 {D(expected)}，实际 {D(actual)}"
    except Exception:                                  # noqa: BLE001
        same = str(actual) == str(expected)
        shown = f"期望 {expected!r}，实际 {actual!r}"
    if not same:
        raise AssertionError(f"{what or '值'} {shown}")


def truth(value, what: str = "") -> None:
    if not value:
        raise AssertionError(f"{what or '条件'} 不成立")


def raises(exc_type, fn, what: str = "") -> None:
    try:
        fn()
    except exc_type:
        return
    except Exception as exc:                           # noqa: BLE001
        raise AssertionError(
            f"{what}: 期望 {exc_type.__name__}，实际 {type(exc).__name__}: {exc}")
    raise AssertionError(f"{what}: 期望抛 {exc_type.__name__}，但没有抛")


# --------------------------------------------------------------------------- #
#  构造器
# --------------------------------------------------------------------------- #

def mk_order(qty="1", price="100", coid="C-0001", symbol="BTC/USD",
             venue="OKX", account="OKX_MAIN", side=Side.BUY) -> Order:
    return Order(order_id=f"o-{coid}", client_order_id=coid, account_id=account,
                 venue=venue, symbol=symbol, side=side,
                 quantity=D(qty), ref_price=D(price))


def mk_fill(qty="1", price="100", coid="C-0001", trade_id="T-1",
            fee="0") -> Fill:
    return Fill(fill_id=f"f-{trade_id}", order_id=f"o-{coid}", client_order_id=coid,
                venue_trade_id=trade_id, quantity=D(qty), price=D(price),
                fee=D(fee), fee_currency="USD", ts=NOW)


def mk_oi(notional="2000", qty="1", coid="C-0001", symbol="BTC/USD",
          venue="OKX", account="OKX_MAIN", side=Side.BUY,
          attribution: Dict[str, Decimal] = None,
          reduce_only: bool = False) -> OrderIntent:
    return OrderIntent(
        order_intent_id=f"oi-{coid}", client_order_id=coid, account_id=account,
        venue=venue, symbol=symbol, side=side, quantity=D(qty),
        order_type=OrderType.MARKET, notional=D(notional),
        attribution=attribution or {}, reason_chain=(),
        as_of=NOW, ts=NOW, reduce_only=reduce_only,
    )


def mk_sintent(intent_id="i1", strategy_id="trend", weight="0.5",
               confidence="0.5", symbol="BTC/USD", venue="OKX",
               expires_in: timedelta = timedelta(hours=4),
               ttl_from: datetime = None) -> StrategyIntent:
    base = ttl_from or NOW
    return StrategyIntent(
        intent_id=intent_id, strategy_id=strategy_id, strategy_version="1.0.0",
        symbol=symbol, venue=venue, target_weight=D(weight),
        confidence=D(confidence), reason="test",
        signal_ids=(f"{strategy_id}:{symbol}:1",),
        as_of=base, ts=base, expires_at=base + expires_in,
    )


# --------------------------------------------------------------------------- #
#  S1 订单状态机与幂等
# --------------------------------------------------------------------------- #

def s1_state_machine() -> None:
    R.section("S1 订单状态机与幂等（R-A08）")

    def happy_path():
        o = mk_order()
        for state in (OrderState.SUBMITTED, OrderState.ACCEPTED,
                      OrderState.PARTIALLY_FILLED, OrderState.FILLED):
            o.transition(state)
        truth(o.is_terminal, "FILLED 应为终态")
        truth(o.state is OrderState.FILLED, "终态为 FILLED")

    def illegal_from_terminal():
        o = mk_order()
        o.transition(OrderState.SUBMITTED)
        o.transition(OrderState.REJECTED)
        raises(IllegalTransition, lambda: o.transition(OrderState.ACCEPTED),
               "终态订单不该能回退")

    def timeout_goes_unknown():
        o = mk_order()
        o.transition(OrderState.SUBMITTED)
        o.transition(OrderState.ACCEPTED)
        o.mark_timeout()
        truth(o.state is OrderState.UNKNOWN, "超时必须落到 UNKNOWN")

    def unknown_has_no_resubmit_path():
        o = mk_order()
        o.transition(OrderState.SUBMITTED)
        o.mark_timeout()
        for forbidden in (OrderState.CREATED, OrderState.SUBMITTED):
            raises(IllegalTransition, lambda f=forbidden: o.transition(f),
                   f"UNKNOWN 不得迁移到 {forbidden.value}")

    def unknown_can_recover_by_query():
        o = mk_order()
        o.transition(OrderState.SUBMITTED)
        o.mark_timeout()
        o.transition(OrderState.ACCEPTED, "exchanged confirmed open")
        truth(o.state is OrderState.ACCEPTED, "查交易所后可恢复跟踪")
        truth(o.unknown_since is None, "恢复后应清空 unknown_since")

    def overfill_rejected():
        o = mk_order(qty="1")
        o.transition(OrderState.SUBMITTED)
        o.transition(OrderState.ACCEPTED)
        o.apply_fill(mk_fill(qty="0.6", trade_id="T-1"))
        raises(ContractViolation,
               lambda: o.apply_fill(mk_fill(qty="0.6", trade_id="T-2")),
               "成交量超过剩余必须拒绝")

    def partial_fill_accumulates():
        o = mk_order(qty="1")
        o.transition(OrderState.SUBMITTED)
        o.transition(OrderState.ACCEPTED)
        o.apply_fill(mk_fill(qty="0.3", price="100", trade_id="T-1"))
        eq(o.filled_qty, "0.3", "部分成交数量")
        o.apply_fill(mk_fill(qty="0.7", price="200", trade_id="T-2"))
        eq(o.filled_qty, 1, "累计成交数量")
        eq(o.avg_fill_price, 170, "加权均价")     # 0.3*100 + 0.7*200 = 170
        truth(o.state is OrderState.FILLED, "全部成交后应为 FILLED")

    def duplicate_client_order_id():
        reg = OrderRegistry()
        intent = mk_oi(coid="DUP-1")
        reg.register(intent, "o-1")
        raises(DuplicateOrder, lambda: reg.register(intent, "o-2"),
               "同 client_order_id 二次登记必须报错")

    def duplicate_venue_trade_id():
        reg = OrderRegistry()
        intent = mk_oi(coid="DUP-2")
        order = reg.register(intent, "o-3")
        order.transition(OrderState.SUBMITTED)
        order.transition(OrderState.ACCEPTED)
        fill = mk_fill(qty="0.5", coid="DUP-2", trade_id="VT-9")
        truth(reg.accept_fill(order, fill) is True, "首次成交应入账")
        truth(reg.accept_fill(order, fill) is False, "重复回报必须忽略")
        eq(order.filled_qty, "0.5", "重复回报不改仓位")

    def inflight_excludes_terminal():
        reg = OrderRegistry()
        o = reg.register(mk_oi(coid="INF-1", notional="1000", qty="10"), "o-4")
        o.transition(OrderState.SUBMITTED)
        eq(reg.inflight_notional("OKX", "BTC/USD"), 1000, "在途名义")
        o.transition(OrderState.REJECTED)
        eq(reg.inflight_notional("OKX", "BTC/USD"), 0, "终态不计入在途")

    def stable_client_order_id():
        a = make_client_order_id("OKX_MAIN", "trend", "BTC/USDT", NOW, 7)
        b = make_client_order_id("OKX_MAIN", "trend", "BTC/USDT", NOW, 7)
        truth(a == b, "同一输入必须得到同一幂等键")
        c = make_client_order_id("OKX_MAIN", "trend", "BTC/USDT", NOW, 8)
        truth(a != c, "不同序号必须不同键")

    for label, fn in [
        ("正常状态链 CREATED→…→FILLED", happy_path),
        ("终态不可回退", illegal_from_terminal),
        ("超时迁 UNKNOWN 而非失败", timeout_goes_unknown),
        ("UNKNOWN 无重新提交路径", unknown_has_no_resubmit_path),
        ("查交易所后可恢复跟踪", unknown_can_recover_by_query),
        ("超量成交被拒", overfill_rejected),
        ("部分成交累计与均价", partial_fill_accumulates),
        ("client_order_id 幂等", duplicate_client_order_id),
        ("venue_trade_id 去重", duplicate_venue_trade_id),
        ("在途敞口不含终态", inflight_excludes_terminal),
        ("幂等键确定性", stable_client_order_id),
    ]:
        R.run(label, fn)


# --------------------------------------------------------------------------- #
#  S2 资金分配与多策略净额
# --------------------------------------------------------------------------- #

def s2_capital_allocator() -> None:
    R.section("S2 资金分配与多策略净额（R-A07 / R-C03 / R-C04）")

    policies = {
        p.strategy_id: p for p in (
            StrategyPolicy("trend", "OKX", quota_pct=D("0.20")),
            StrategyPolicy("mr", "OKX", quota_pct=D("0.10")),
        )
    }
    allocs = allocate_all(list(policies.values()), NAV)

    def quota_math():
        eq(allocs["trend"].quota_usd, 21040, "trend 配额 = NAV 20%")
        eq(allocs["mr"].quota_usd, 10520, "mr 配额 = NAV 10%")
        eq(allocs["trend"].effective_capacity_usd, 21040, "固定配额下容量=配额")

    def vol_target_scaling():
        p = StrategyPolicy("vt", "OKX", mode=AllocationMode.VOL_TARGET,
                           quota_pct=D("0.20"), vol_target=D("0.15"))
        hot = allocate(p, NAV, realized_vol=D("0.30"))
        eq(hot.vol_scalar, "0.5", "实测波动翻倍 → 额度减半")
        eq(hot.effective_capacity_usd, 10520, "缩放后容量")
        calm = allocate(p, NAV, realized_vol=D("0.05"))
        eq(calm.vol_scalar, 1, "波动低于目标 → 不放大（封顶 1）")
        eq(calm.effective_capacity_usd, 21040, "封顶后容量")

    def netting_offsets_conflict():
        """两策略反向意图必须净额，而不是各下一笔（R-A07）。"""
        intents = [mk_sintent("i1", "trend", "0.75"), mk_sintent("i2", "mr", "-0.75")]
        targets = net_targets(NettingInput(
            intents=intents, allocations=allocs, policies=policies,
            prices={("OKX", "BTC/USD"): D("60000")},
            current_notional={},
            inflight_notional={},
            strategy_states={}, now=NOW,
            min_notional={"OKX": D("200")},
        ))
        eq(len(targets), 1, "同一标的不应产生两个目标")
        t = targets[0]
        eq(t.per_strategy["trend"], 15780, "trend 目标名义 = 21040×0.75")
        eq(t.per_strategy["mr"], -7890, "mr 目标名义 = 10520×(-0.75)")
        eq(t.net_target_notional, 7890, "净额 = 两者抵消后的差额")
        eq(t.delta_notional, 7890, "无持仓无在途 → 差额=净额")
        truth(t.decision is TargetDecision.TRADE, "应产生交易")

    def inflight_is_deducted():
        """在途订单必须从差额里扣掉，否则两次调度会重复下单。"""
        intents = [mk_sintent("i1", "trend", "0.75")]
        base = dict(intents=intents, allocations=allocs, policies=policies,
                    prices={("OKX", "BTC/USD"): D("60000")},
                    current_notional={("OKX", "BTC/USD"): D("3000")},
                    strategy_states={}, now=NOW,
                    min_notional={"OKX": D("200")})
        no_inflight = net_targets(NettingInput(inflight_notional={}, **base))[0]
        eq(no_inflight.delta_notional, 12780, "净额 15780 - 持仓 3000")
        with_inflight = net_targets(NettingInput(
            inflight_notional={("OKX", "BTC/USD"): D("5000")}, **base))[0]
        eq(with_inflight.delta_notional, 7780, "再扣在途 5000")

    def confidence_does_not_move_size():
        """R-A06：confidence 只记录，不参与计算。"""
        low = net_targets(NettingInput(
            intents=[mk_sintent("a", "trend", "0.5", confidence="0.01")],
            allocations=allocs, policies=policies,
            prices={("OKX", "BTC/USD"): D("60000")},
            current_notional={}, inflight_notional={},
            strategy_states={}, now=NOW, min_notional={"OKX": D("200")},
        ))[0]
        high = net_targets(NettingInput(
            intents=[mk_sintent("a", "trend", "0.5", confidence="0.99")],
            allocations=allocs, policies=policies,
            prices={("OKX", "BTC/USD"): D("60000")},
            current_notional={}, inflight_notional={},
            strategy_states={}, now=NOW, min_notional={"OKX": D("200")},
        ))[0]
        eq(low.delta_notional, high.delta_notional, "confidence 不得影响下单量")
        eq(low.delta_qty, high.delta_qty, "confidence 不得影响下单数量")

    def expired_intent_blocks_symbol():
        stale = mk_sintent("s", "trend", "0.5",
                           ttl_from=NOW - timedelta(days=1),
                           expires_in=timedelta(hours=1))
        t = net_targets(NettingInput(
            intents=[stale], allocations=allocs, policies=policies,
            prices={("OKX", "BTC/USD"): D("60000")},
            current_notional={("OKX", "BTC/USD"): D("3000")},
            inflight_notional={}, strategy_states={}, now=NOW,
        ))[0]
        truth(t.decision is TargetDecision.BLOCKED, "过期意图必须阻断")
        eq(t.blocked_reason, "stale_intent", "阻断原因")

    def paused_strategy_blocks_symbol():
        """
        暂停策略时整标的阻断（fail-closed）：因为当前版本拿不到"每策略现有
        持仓"，盲目按剩余意图净额会把被暂停策略的存量仓位卖掉。
        B1 接入 per-strategy 持仓后放开为"仅暂停份额归零"。
        """
        t = net_targets(NettingInput(
            intents=[mk_sintent("p", "mr", "0.5")], allocations=allocs,
            policies=policies, prices={("OKX", "BTC/USD"): D("60000")},
            current_notional={("OKX", "BTC/USD"): D("3000")},
            inflight_notional={},
            strategy_states={"mr": StrategyRunState.PAUSED}, now=NOW,
        ))[0]
        truth(t.decision is TargetDecision.BLOCKED, "暂停策略必须阻断")
        eq(t.blocked_reason, "strategy_paused", "阻断原因")

    def min_notional_nobbling():
        t = net_targets(NettingInput(
            intents=[mk_sintent("m", "mr", "0.001")], allocations=allocs,
            policies=policies, prices={("OKX", "BTC/USD"): D("60000")},
            current_notional={}, inflight_notional={}, strategy_states={},
            now=NOW, min_notional={"OKX": D("200")},
        ))[0]
        truth(t.decision is TargetDecision.NOOP, "过小差额应 NOOP")
        eq(t.blocked_reason, "below_min_notional", "原因")

    def gross_weight_cap_scales():
        p = StrategyPolicy("multi", "OKX", quota_pct=D("0.10"),
                           max_gross_weight=D("1.0"))
        a = {"multi": allocate(p, NAV)}
        targets = net_targets(NettingInput(
            intents=[mk_sintent("x", "multi", "1.0", symbol="BTC/USD"),
                     mk_sintent("y", "multi", "1.0", symbol="ETH/USD")],
            allocations=a, policies={"multi": p},
            prices={("OKX", "BTC/USD"): D("60000"), ("OKX", "ETH/USD"): D("3000")},
            current_notional={}, inflight_notional={}, strategy_states={}, now=NOW,
        ))
        total = sum((abs(t.net_target_notional) for t in targets), D(0))
        eq(total, 10520, "两标的合计被压回 max_gross_weight 上限")

    def build_intents_direction():
        t = net_targets(NettingInput(
            intents=[mk_sintent("i1", "trend", "0.75")], allocations=allocs,
            policies=policies, prices={("OKX", "BTC/USD"): D("60000")},
            current_notional={}, inflight_notional={}, strategy_states={},
            now=NOW, min_notional={"OKX": D("200")},
        ))
        intents, next_seq = build_order_intents(
            t, "OKX_MAIN", NOW, seq_start=1, strategy_scope="PF")
        eq(len(intents), 1, "产生一笔订单意图")
        intent = intents[0]
        truth(intent.side is Side.BUY, "净买入方向")
        eq(intent.notional, 15780, "下单名义")
        eq(intent.client_order_id, "OKXMAIN-PF-BTCUSD-20261026-0001", "幂等键格式")
        truth(intent.limit_price > D("60000"), "买入限价应高于参考价")
        eq(next_seq, 2, "序号推进")

    for label, fn in [
        ("固定配额换算", quota_math),
        ("波动率目标缩放额度", vol_target_scaling),
        ("冲突意图净额抵消", netting_offsets_conflict),
        ("在途订单被扣除", inflight_is_deducted),
        ("confidence 不影响下单量", confidence_does_not_move_size),
        ("过期意图阻断", expired_intent_blocks_symbol),
        ("暂停策略阻断", paused_strategy_blocks_symbol),
        ("碎单转 NOOP", min_notional_nobbling),
        ("单策略总敞口压缩", gross_weight_cap_scales),
        ("意图转订单（方向/限价/幂等键）", build_intents_direction),
    ]:
        R.run(label, fn)


# --------------------------------------------------------------------------- #
#  S3 三道防线与权限级
# --------------------------------------------------------------------------- #

def s3_risk_engine() -> None:
    R.section("S3 三道防线与权限级（R-K01..K04）")

    engine = RiskEngine(RiskPolicy())
    base_ctx = dict(nav=D("100000"), now=NOW, market_data_age_sec={"OKX": D("1")})

    def order_cap_returns_reduce_required():
        d = engine.check_order(mk_oi(notional="3000"), RiskContext(**base_ctx), RiskState())
        truth(d.verdict is Verdict.REDUCE_REQUIRED, "超单笔限额应要求重算")
        eq(d.cap_notional, 2000, "cap = NAV 2%")
        truth(not d.allowed, "REDUCE_REQUIRED 不等于放行")

    def below_min_notional():
        d = engine.check_order(mk_oi(notional="50", qty="0.001"),
                               RiskContext(**base_ctx), RiskState())
        truth(d.verdict is Verdict.REJECT, "低于最小金额应拒单")
        eq(d.policy_key, "min_order_notional", "命中策略键")

    def leverage_blocked():
        ctx = RiskContext(nav=D("100000"), now=NOW,
                          market_data_age_sec={"OKX": D("1")},
                          total_gross_notional=D("99500"))
        d = engine.check_order(mk_oi(notional="1000"), ctx, RiskState())
        truth(d.verdict is Verdict.REJECT, "总敞口超 NAV 且不允许杠杆应拒单")
        eq(d.policy_key, "allow_leverage", "命中杠杆策略")

    def stale_data_blocks():
        ctx = RiskContext(nav=D("100000"), now=NOW,
                          market_data_age_sec={"OKX": D("600")})
        d = engine.check_order(mk_oi(notional="1000"), ctx, RiskState())
        truth(d.verdict is Verdict.REJECT, "陈旧行情应阻断")
        eq(d.policy_key, "stale_market_data_action", "命中行情策略")

    def reconciliation_blocks_account():
        state = RiskState()
        state.reconciliation_broken_accounts.add("OKX_MAIN")
        d = engine.check_order(mk_oi(notional="1000"),
                               RiskContext(**base_ctx), state)
        truth(d.verdict is Verdict.HALT_FUND, "对账不平应熔断")
        truth(HaltAction.FREEZE_NEW_ORDERS.value in d.halt_actions, "含冻结新增")
        truth(HaltAction.MANUAL_REVIEW.value in d.halt_actions, "含人工排查")

    def unknown_order_blocks_symbol():
        state = RiskState()
        state.symbols_with_unknown_orders.add("BTC/USD")
        d = engine.check_order(mk_oi(notional="1000"),
                               RiskContext(**base_ctx), state)
        truth(d.verdict is Verdict.REJECT, "存在状态未知订单应拒新单")
        eq(d.policy_key, "unknown_order_status_action", "命中未知状态策略")

    def halted_rejects_new_risk():
        state = RiskState()
        state.halt("drawdown 6.2% >= 5.0%")
        d = engine.check_order(mk_oi(notional="1000"),
                               RiskContext(**base_ctx), state)
        truth(d.verdict is Verdict.HALT_FUND, "HALTED 拒绝开仓")
        truth(HaltAction.CONTROLLED_REDUCE.value in d.halt_actions,
              "回撤熔断应给出受控减仓而非市价清仓")
        truth("liquidate" not in " ".join(d.halt_actions), "不得出现一刀清仓动作")

    def halted_allows_controlled_reduction():
        state = RiskState()
        state.halt("drawdown 6.2% >= 5.0%")
        ctx = RiskContext(nav=D("100000"), now=NOW,
                          market_data_age_sec={"OKX": D("1")},
                          position_notional={("OKX", "BTC/USD"): D("5000")})
        d = engine.check_order(
            mk_oi(notional="1000", side=Side.SELL, reduce_only=True), ctx, state)
        truth(d.verdict is Verdict.ALLOW, "受控减仓应放行")

    def reduce_only_blocks_increase():
        state = RiskState()
        state.reduce_only("strategy risk contribution 24.1% > 20%")
        d = engine.check_order(mk_oi(notional="1000"),
                               RiskContext(**base_ctx), state)
        truth(d.verdict is Verdict.REJECT, "REDUCE_ONLY 下开仓应拒")

    def reduce_only_allows_decrease():
        state = RiskState()
        state.reduce_only("exposure cap")
        ctx = RiskContext(nav=D("100000"), now=NOW,
                          market_data_age_sec={"OKX": D("1")},
                          position_notional={("OKX", "BTC/USD"): D("5000")})
        d = engine.check_order(mk_oi(notional="1000", side=Side.SELL), ctx, state)
        truth(d.verdict is Verdict.ALLOW, "REDUCE_ONLY 下减仓应放行")

    def flip_counts_as_new_risk():
        """翻仓（多翻空）绝对敞口不变，但方向是全新风险，必须判为增敞口。"""
        state = RiskState()
        state.reduce_only("exposure cap")
        ctx = RiskContext(nav=D("100000"), now=NOW,
                          market_data_age_sec={"OKX": D("1")},
                          position_notional={("OKX", "BTC/USD"): D("-1000")})
        d = engine.check_order(mk_oi(notional="2000", side=Side.BUY), ctx, state)
        truth(d.verdict is Verdict.REJECT, "翻仓应视为新增风险")

    def paused_strategy_rejects():
        state = RiskState()
        state.set_strategy("mr", StrategyRunState.PAUSED)
        d = engine.check_order(
            mk_oi(notional="1000", attribution={"mr": D("1")}),
            RiskContext(**base_ctx), state)
        truth(d.verdict is Verdict.REJECT, "暂停策略不接受新仓位")
        eq(d.layer.value, "STRATEGY", "命中层级为策略级")

    def turnover_anomaly():
        ctx = RiskContext(nav=D("100000"), now=NOW,
                          market_data_age_sec={"OKX": D("1")},
                          strategy_capacity={"s1": D("20000")},
                          strategy_turnover={"s1": D("59000")})
        d = engine.check_order(
            mk_oi(notional="2000", attribution={"s1": D("1")}), ctx, RiskState())
        truth(d.verdict is Verdict.REJECT, "异常换手应拒单")
        eq(d.policy_key, "max_strategy_turnover_multiple", "命中换手策略")

    def fund_state_computes_drawdown():
        state = engine.update_fund_state(
            RiskState(), nav=D("94000"), peak_nav=D("100000"),
            day_start_nav=D("95000"))
        eq(state.drawdown_pct, 6, "回撤 6%")
        truth(state.permission_level is PermissionLevel.HALTED,
              "回撤超 5% 应自动 HALTED")
        eq(state.daily_pnl_pct, D("-1.0526"), "当日盈亏（百分比，4 位）")

    def unknown_resolution_playbook():
        r = resolve_unknown_order("o1", None, D(0), venue_found=False)
        truth(r.action == "void" and r.new_state is OrderState.EXPIRED, "交易所无此单→作废")
        r = resolve_unknown_order("o2", "closed", D("1"), venue_found=True)
        truth(r.action == "adopt" and r.new_state is OrderState.FILLED, "已成交→采用")
        r = resolve_unknown_order("o3", "canceled", D(0))
        truth(r.new_state is OrderState.CANCELLED, "已撤→同步")
        r = resolve_unknown_order("o4", "new", D(0))
        truth(r.new_state is OrderState.ACCEPTED and r.action == "cancel", "挂单未成交→撤")
        r = resolve_unknown_order("o5", "", D(0))
        truth(r.action == "wait" and r.new_state is None, "查不到→保持冻结")
        truth(not hasattr(r, "resubmit"), "对账结论里不存在重提动作")

    for label, fn in [
        ("单笔超限 → REDUCE_REQUIRED（风控不改单量）", order_cap_returns_reduce_required),
        ("低于最小金额拒单", below_min_notional),
        ("不允许杠杆 → 总敞口拒单", leverage_blocked),
        ("行情陈旧 → 阻断", stale_data_blocks),
        ("对账不平 → 熔断并给处置动作", reconciliation_blocks_account),
        ("状态未知订单 → 冻结标的", unknown_order_blocks_symbol),
        ("HALTED 拒绝开仓且不市价清仓", halted_rejects_new_risk),
        ("HALTED 放行受控减仓", halted_allows_controlled_reduction),
        ("REDUCE_ONLY 拒绝增敞口", reduce_only_blocks_increase),
        ("REDUCE_ONLY 放行减仓", reduce_only_allows_decrease),
        ("翻仓视为新增风险", flip_counts_as_new_risk),
        ("暂停策略拒单（策略级）", paused_strategy_rejects),
        ("异常换手拒单", turnover_anomaly),
        ("回撤自动升级权限级", fund_state_computes_drawdown),
        ("未知订单对账四种结论", unknown_resolution_playbook),
    ]:
        R.run(label, fn)


# --------------------------------------------------------------------------- #
#  S4 复式账本与绩效归因
# --------------------------------------------------------------------------- #

def s4_ledger() -> None:
    R.section("S4 复式账本与绩效归因（R-D01..D04）")

    def buy_then_mark():
        L = Ledger()
        L.deposit("OKX_MAIN", "USD", D("100000"), ts=NOW)
        L.apply_fill(mk_fill(qty="0.1", price="60000", trade_id="T1", fee="6"),
                     venue="OKX", symbol="BTC/USD", account_id="OKX_MAIN",
                     side=Side.BUY, quote_ccy="USD", decision_price=D("60000"),
                     ts=NOW)
        eq(L.nav({("OKX", "BTC/USD"): D("60000")}), 99994, "买入后 NAV 只少手续费")
        rep = L.assert_attribution_closes({("OKX", "BTC/USD"): D("60000")})
        eq(rep.net_pnl, -6, "净收益=手续费")
        eq(rep.fee, -6, "手续费归因")
        eq(rep.trading, 0, "未涨跌无交易收益")
        eq(rep.fx, 0, "单一币种不应有汇率损益")

    def mark_to_market_closes():
        L = Ledger()
        L.deposit("OKX_MAIN", "USD", D("100000"), ts=NOW)
        L.apply_fill(mk_fill(qty="0.1", price="60000", trade_id="T1", fee="6"),
                     venue="OKX", symbol="BTC/USD", account_id="OKX_MAIN",
                     side=Side.BUY, quote_ccy="USD", decision_price=D("60000"), ts=NOW)
        rep = L.assert_attribution_closes({("OKX", "BTC/USD"): D("61000")})
        eq(rep.nav, 100094, "涨到 61000 后 NAV")
        eq(rep.trading, 100, "交易收益 = 0.1×1000")
        eq(rep.net_pnl, 94, "净收益 = 交易 - 手续费")

    def slippage_is_separated():
        """成交价劣于决策价时，损失必须落在 slippage 而不是混进 trading。"""
        L = Ledger()
        L.deposit("OKX_MAIN", "USD", D("100000"), ts=NOW)
        L.deposit("OKX_MAIN", "USD", D("150000"), ts=NOW)   # 第二笔入金
        L.apply_fill(mk_fill(qty="0.1", price="60010", trade_id="T1", fee="6"),
                     venue="OKX", symbol="BTC/USD", account_id="OKX_MAIN",
                     side=Side.BUY, quote_ccy="USD", decision_price=D("60000"), ts=NOW)
        rep = L.assert_attribution_closes({("OKX", "BTC/USD"): D("60010")})
        eq(rep.slippage, -1, "滑点成本 = 0.1×(60000-60010)")
        eq(rep.trading, 1, "交易收益按成交市值计 = 0.1×60010-6000")
        eq(rep.net_pnl, -6, "净收益=1-1-6，且入金未污染")
        eq(rep.external_flow, 250000, "入金总额单独列示")

    def round_trip_closes():
        L = Ledger()
        L.deposit("OKX_MAIN", "USD", D("100000"), ts=NOW)
        L.apply_fill(mk_fill(qty="0.1", price="60000", trade_id="T1", fee="6"),
                     venue="OKX", symbol="BTC/USD", account_id="OKX_MAIN",
                     side=Side.BUY, quote_ccy="USD", decision_price=D("60000"), ts=NOW)
        L.apply_fill(mk_fill(qty="0.1", price="61000", trade_id="T2", fee="6"),
                     venue="OKX", symbol="BTC/USD", account_id="OKX_MAIN",
                     side=Side.SELL, quote_ccy="USD", decision_price=D("61000"), ts=NOW)
        rep = L.assert_attribution_closes({("OKX", "BTC/USD"): D("61000")})
        eq(rep.nav, 100088, "平仓后现金")
        eq(rep.trading, 100, "已实现交易收益")
        eq(rep.fee, -12, "两笔手续费")
        eq(rep.net_pnl, 88, "净收益")
        eq(len(L.positions()), 0, "已无持仓")

    def unbalanced_entry_rejected():
        entry = LedgerEntry(
            entry_id="bad", ts=NOW, kind="TRADE", ref_type="x", ref_id="y",
            lines=(LedgerLine("cash:A:USD", "USD", debit=D("100")),
                   LedgerLine("position:V:S", "USD", credit=D("90"))))
        raises(UnbalancedEntry, entry.validate, "借贷不平必须拒绝")

    def duplicate_fill_not_double_booked():
        L = Ledger()
        L.deposit("OKX_MAIN", "USD", D("100000"), ts=NOW)
        fill = mk_fill(qty="0.1", price="60000", trade_id="T1", fee="6")
        first = L.apply_fill(fill, venue="OKX", symbol="BTC/USD",
                             account_id="OKX_MAIN", side=Side.BUY,
                             quote_ccy="USD", decision_price=D("60000"), ts=NOW)
        second = L.apply_fill(fill, venue="OKX", symbol="BTC/USD",
                              account_id="OKX_MAIN", side=Side.BUY,
                              quote_ccy="USD", decision_price=D("60000"), ts=NOW)
        truth(first is True and second is False, "重复成交必须被忽略")
        eq(L.positions()[("OKX", "BTC/USD")].qty, "0.1", "仓位只加一次")

    def funding_is_tracked():
        L = Ledger()
        L.deposit("OKX_MAIN", "USD", D("100000"), ts=NOW)
        L.post_funding("OKX_MAIN", "USD", D("25"), ts=NOW)
        rep = L.assert_attribution_closes({})
        eq(rep.funding, -25, "资金费支出记入 funding")
        eq(rep.nav, 99975, "NAV 扣减")

    def per_strategy_attribution():
        L = Ledger()
        L.deposit("OKX_MAIN", "USD", D("100000"), ts=NOW)
        L.apply_fill(mk_fill(qty="0.1", price="60000", trade_id="T1", fee="0"),
                     venue="OKX", symbol="BTC/USD", account_id="OKX_MAIN",
                     side=Side.BUY, quote_ccy="USD", decision_price=D("60000"),
                     attribution={"trend": D("0.06"), "mr": D("0.04")}, ts=NOW)
        prices = {("OKX", "BTC/USD"): D("61000")}
        pnl = L.strategy_pnl(prices)
        eq(pnl["trend"], 60, "trend 归属 0.06 BTC × 1000")
        eq(pnl["mr"], 40, "mr 归属 0.04 BTC × 1000")
        eq(sum(pnl.values()), 100, "策略损益之和等于总交易收益")
        eq(L.exposure_by_strategy(prices)["trend"], 3660, "trend 市值敞口")

    def reconcile_clean_and_broken():
        L = Ledger()
        L.deposit("OKX_MAIN", "USD", D("100000"), ts=NOW)
        L.apply_fill(mk_fill(qty="0.1", price="60000", trade_id="T1", fee="6"),
                     venue="OKX", symbol="BTC/USD", account_id="OKX_MAIN",
                     side=Side.BUY, quote_ccy="USD", decision_price=D("60000"), ts=NOW)
        clean = L.reconcile({("OKX_MAIN", "USD"): D("93994")},
                            {("OKX", "BTC/USD"): D("0.1")}, ts=NOW)
        truth(clean.is_clean, "现金流一致时应平账")

        broken = L.reconcile({("OKX_MAIN", "USD"): D("93000")},
                             {("OKX", "BTC/USD"): D("0.1")}, ts=NOW)
        truth(not broken.is_clean, "现金差 994 必须判为不平")
        eq(broken.cash_diffs["OKX_MAIN|USD"], "-994", "差异方向：券商 - 账本")

        missing = L.reconcile({("OKX_MAIN", "USD"): D("93994")}, {}, ts=NOW)
        truth(not missing.is_clean, "券商侧缺持仓也是不平")
        eq(missing.missing_at_broker, ("OKX|BTC/USD",), "缺持仓标记")

    def withdraw_does_not_distort():
        L = Ledger()
        L.deposit("OKX_MAIN", "USD", D("100000"), ts=NOW)
        L.apply_fill(mk_fill(qty="0.1", price="60000", trade_id="T1", fee="0"),
                     venue="OKX", symbol="BTC/USD", account_id="OKX_MAIN",
                     side=Side.BUY, quote_ccy="USD", decision_price=D("60000"), ts=NOW)
        before = L.assert_attribution_closes({("OKX", "BTC/USD"): D("61000")})
        L.withdraw("OKX_MAIN", "USD", D("50000"), ts=NOW)
        after = L.assert_attribution_closes({("OKX", "BTC/USD"): D("61000")})
        eq(after.net_pnl, before.net_pnl, "出金不得改变净收益")
        eq(after.nav, before.nav - 50000, "NAV 减少出金金额")

    for label, fn in [
        ("买入记账与 NAV", buy_then_mark),
        ("市值变动后归因闭合", mark_to_market_closes),
        ("滑点独立归因 + 入金不污染收益", slippage_is_separated),
        ("完整买卖循环闭合", round_trip_closes),
        ("借贷不平拒绝入账", unbalanced_entry_rejected),
        ("重复成交不重复记账", duplicate_fill_not_double_booked),
        ("资金费归因", funding_is_tracked),
        ("按策略归属盈亏", per_strategy_attribution),
        ("对账：平账 / 现金差 / 缺持仓", reconcile_clean_and_broken),
        ("出金不扭曲收益率", withdraw_does_not_distort),
    ]:
        R.run(label, fn)


# --------------------------------------------------------------------------- #
#  S5 端到端
# --------------------------------------------------------------------------- #

def s5_end_to_end() -> None:
    R.section("S5 端到端：冲突→风控限额→成交→未知状态→对账")

    def full_chain():
        policy = RiskPolicy()
        engine = RiskEngine(policy)
        state = RiskState()
        ledger = Ledger()
        registry = OrderRegistry()

        ledger.deposit("OKX_MAIN", "USD", NAV, ts=NOW)
        prices = {("OKX", "BTC/USD"): D("60000")}

        # --- T1 两策略各出意图（一多一空，方向冲突） ------------------- #
        books = {p.strategy_id: p for p in (
            StrategyPolicy("trend", "OKX", quota_pct=D("0.20")),
            StrategyPolicy("mr", "OKX", quota_pct=D("0.10")),
        )}
        allocs = allocate_all(list(books.values()), NAV)
        intents = [mk_sintent("i1", "trend", "0.95"), mk_sintent("i2", "mr", "-0.50")]

        # --- T2 净额 --------------------------------------------------- #
        target = net_targets(NettingInput(
            intents=intents, allocations=allocs, policies=books,
            prices=prices, current_notional={}, inflight_notional={},
            strategy_states={}, now=NOW, min_notional={"OKX": D("200")},
        ))[0]
        truth(target.decision is TargetDecision.TRADE, "应产出一笔净额交易")
        gross = target.delta_notional
        truth(gross > 0, "净敞口为正（trend 额度更大）")

        # --- T3 风控要求按单笔上限重算 --------------------------------- #
        first, seq = build_order_intents([target], "OKX_MAIN", NOW, seq_start=1)
        eq(len(first), 1, "生成 1 笔订单意图")
        decision = engine.check_order(
            first[0],
            RiskContext(nav=NAV, now=NOW,
                        market_data_age_sec={"OKX": D("2")},
                        position_notional={}), state)
        truth(decision.verdict is Verdict.REDUCE_REQUIRED,
              f"单笔 {first[0].notional} 应超 2% 限额，实际 {decision.verdict}")
        eq(decision.cap_notional, 2104, "cap = NAV 210520×2%")

        # --- T4 组合层按 cap 重算（风控不改单量） ---------------------- #
        capped = target.capped(decision.cap_notional)
        truth(abs(capped.delta_notional) < abs(target.delta_notional), "差额被压小")
        second, seq2 = build_order_intents([capped], "OKX_MAIN", NOW, seq_start=1)

        ctx2 = RiskContext(nav=NAV, now=NOW,
                           market_data_age_sec={"OKX": D("2")},
                           position_notional={})
        decision2 = engine.check_order(second[0], ctx2, state)
        truth(decision2.verdict is Verdict.ALLOW,
              f"压到 cap 后应放行，实际 {decision2.verdict}: {decision2.reason}")

        # --- T5 下单 + 部分成交 --------------------------------------- #
        order = registry.register(second[0], "o-e2e-1")
        order.transition(OrderState.SUBMITTED)
        order.transition(OrderState.ACCEPTED)

        qty = second[0].quantity
        part = D(qty) / 2
        fill1 = mk_fill(qty=str(part), price="60000",
                        coid=second[0].client_order_id, trade_id="VT-1", fee="1")
        truth(registry.accept_fill(order, fill1) is True, "首次成交入账")
        truth(registry.accept_fill(order, fill1) is False, "重复回报忽略")
        ledger.apply_fill(fill1, venue="OKX", symbol="BTC/USD",
                          account_id="OKX_MAIN", side=Side.BUY, quote_ccy="USD",
                          decision_price=capped.price,
                          attribution={"trend": part}, ts=NOW)
        truth(order.state is OrderState.PARTIALLY_FILLED, "部分成交状态")

        # --- T6 超时 → UNKNOWN → 查交易所 --------------------------------- #
        order.mark_timeout()
        truth(order.state is OrderState.UNKNOWN, "超时进 UNKNOWN")
        state.symbols_with_unknown_orders.add(order.symbol)
        blocked = engine.check_order(
            mk_oi(notional="1000", coid="C-NEW"), ctx2, state)
        truth(blocked.verdict is Verdict.REJECT, "未知状态期间该标的禁新单")

        resolution = resolve_unknown_order(order.order_id, "closed", D(str(qty)))
        eq(resolution.action, "adopt", "交易所确认已全部成交 → 采用")
        order.transition(resolution.new_state, resolution.reason)

        # --- T7 补录剩余成交 ------------------------------------------- #
        rest = D(str(qty)) - part
        fill2 = mk_fill(qty=str(rest), price="60000",
                        coid=second[0].client_order_id, trade_id="VT-2", fee="1")
        truth(registry.accept_fill(order, fill2) is True, "补录剩余成交")
        ledger.apply_fill(fill2, venue="OKX", symbol="BTC/USD",
                          account_id="OKX_MAIN", side=Side.BUY, quote_ccy="USD",
                          decision_price=capped.price,
                          attribution={"trend": rest}, ts=NOW)
        truth(order.state is OrderState.FILLED, "补录后终态 FILLED")
        eq(order.filled_qty, D(qty), "累计成交量与订单数量一致")
        state.symbols_with_unknown_orders.clear()

        # --- T8 账本与对账 --------------------------------------------- #
        pos_qty = ledger.positions()[("OKX", "BTC/USD")].qty
        broker = {("OKX", "BTC/USD"): pos_qty}
        cash = ledger.cash_native()
        clean = ledger.reconcile(cash, broker, ts=NOW)
        truth(clean.is_clean, f"自洽状态应平账：{clean.to_dict()}")

        report = ledger.assert_attribution_closes(prices)
        truth(report.closes, "归因闭合")
        eq(report.fee, -2, "两笔手续费合计")

        # 人为制造差异 → 必须判不平
        bad_cash = {k: v - D("1") if k == ("OKX_MAIN", "USD") else v
                    for k, v in cash.items()}
        truth(not ledger.reconcile(bad_cash, broker, ts=NOW).is_clean,
              "差 1 美元也必须在报告里暴露")

    def halt_freezes_chain():
        """风控熔断后，整条链必须停止新增风险（R-A02 的行为侧验证）。"""
        engine = RiskEngine(RiskPolicy())
        state = RiskState()
        engine.update_fund_state(state, nav=D("93000"), peak_nav=D("100000"),
                                 day_start_nav=D("95000"))
        truth(state.permission_level is PermissionLevel.HALTED, "自动熔断")

        ctx = RiskContext(nav=D("93000"), now=NOW,
                          market_data_age_sec={"OKX": D("1")})
        for notional in ("500", "1000", "2000"):
            d = engine.check_order(mk_oi(notional=notional), ctx, state)
            truth(d.verdict in (Verdict.HALT_FUND, Verdict.REJECT),
                  f"{notional} 在 HALTED 下必须被拦")

        # 人工恢复权限，但回撤事实仍存在 → 必须继续拦（事实优先于权限）
        state.resume("manual review passed")
        still = engine.check_order(mk_oi(notional="500"), ctx, state)
        truth(still.verdict is Verdict.HALT_FUND,
              "回撤未修复前，清掉 halted_reasons 也无效")

        # 只有回撤事实消除后才允许下单
        state.drawdown_pct = Decimal(0)
        state.daily_pnl_pct = Decimal(0)
        truth(engine.check_order(mk_oi(notional="500"), ctx, state).allowed,
              "回撤消除后才可下单（风控不靠人工喊话复活）")

    for label, fn in [
        ("完整链路 T1→T8", full_chain),
        ("熔断后全链路停新增风险", halt_freezes_chain),
    ]:
        R.run(label, fn)


# --------------------------------------------------------------------------- #
#  main
# --------------------------------------------------------------------------- #

def main() -> int:
    print("QuantOS 2.0 · 契约层自测")
    print(f"契约文档: docs/QuantOS/02-服务数据契约.md")
    s1_state_machine()
    s2_capital_allocator()
    s3_risk_engine()
    s4_ledger()
    s5_end_to_end()
    return R.summary()


if __name__ == "__main__":
    sys.exit(main())
