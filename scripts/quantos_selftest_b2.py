#!/usr/bin/env python
"""
QuantOS 2.0 — B2 自检（T-201 ~ T-204）
=====================================

B2 的主题只有一句话：**基础设施坏了，交易必须停，而且停得干净。**

四组用例：
  T-201 风控独立进程：真起 Unix socket 服务，验证跨进程决策、状态落盘、以及
        "服务不可用 = 拒单"（fail-closed，不抛异常让调用方自己看着办）
  T-202 幂等下单：同义意图拿不到第二个 client_order_id；重试 3 次只有 1 笔订单
  T-203 未知订单对账：冻结 → 查询 → 补录/撤销 → 解冻，且重复对账不重复记账
  T-204 账户×网关单活锁：同网关第二个写者立即失败，不同网关互不影响

T-205（kill -9 / 断网 / 磁盘满）在 `quantos_fault_inject.py`，那需要真杀进程。

运行：`.venv/bin/python scripts/quantos_selftest_b2.py`
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "scripts"))

from _asserts import eq, falsy, raises, run, section, truth  # noqa: E402

from quantos_core.contracts import (                       # noqa: E402
    D, Fill, OrderState, OrderType, Side, TargetDecision, TargetPosition,
)
from quantos_core.idempotency import ClientOrderIdStore     # noqa: E402
from quantos_core.ledger import Ledger                      # noqa: E402
from quantos_core.locks import AccountGatewayLock, LockBusy  # noqa: E402
from quantos_core.oms import Oms                            # noqa: E402
from quantos_core.reconciliation import (                   # noqa: E402
    UnknownReconciler, VenueOrderView, VenueQueryError,
)
from quantos_core.risk_engine import RiskContext, RiskPolicy  # noqa: E402
from quantos_core.risk_service import (                     # noqa: E402
    RiskServiceClient, RiskServiceServer,
)

UTC = timezone.utc
T0 = datetime(2026, 10, 26, 13, 30, tzinfo=UTC)
TMP = tempfile.mkdtemp(prefix="qos-b2-")
SOCK = os.path.join(TMP, "risk.sock")

LOOSE_POLICY = RiskPolicy(
    max_order_notional_pct=D("50"), max_strategy_allocation_pct=D("90"),
    min_order_notional=D("50"), max_gross_exposure_pct=D("200"),
    drawdown_pause_pct=D("5"), daily_loss_pause_pct=D("1"),
)


# --------------------------------------------------------------------------- #
#  辅助
# --------------------------------------------------------------------------- #

def make_target(delta="1000", price="400", symbol="QQQ", venue="TIGER",
                strategy="etf_mom") -> TargetPosition:
    delta_d, price_d = D(delta), D(price)
    qty = (delta_d / price_d).quantize(Decimal("0.00000001"))
    return TargetPosition(
        symbol=symbol, venue=venue, per_strategy={strategy: delta_d},
        net_target_notional=delta_d, current_notional=Decimal(0),
        inflight_notional=Decimal(0), delta_notional=delta_d, price=price_d,
        net_target_qty=qty, delta_qty=qty, decision=TargetDecision.TRADE,
    )


def make_ctx(now=T0, nav="100000", age="0") -> RiskContext:
    return RiskContext(nav=D(nav), now=now,
                       market_data_age_sec={"TIGER": D(age), "OKX": D(age)})


def new_idem(name="cid") -> ClientOrderIdStore:
    return ClientOrderIdStore(os.path.join(TMP, f"{name}.json"))


def start_server(state_path=None, policy=None) -> RiskServiceServer:
    server = RiskServiceServer(SOCK, policy=policy or LOOSE_POLICY,
                               state_path=state_path)
    server.start()
    return server


# --------------------------------------------------------------------------- #
#  T-201 风控独立进程
# --------------------------------------------------------------------------- #

def test_risk_service() -> None:
    section("T-201 风控独立进程（fail-closed）")

    state_path = os.path.join(TMP, "risk-state.json")
    server = start_server(state_path)
    client = RiskServiceClient(SOCK, timeout=2.0)
    try:
        truth(client.healthcheck(), "跨进程 ping 通")

        oms = Oms("TIGER_MAIN", "QOS", client, new_idem("rs"))
        target = make_target()
        intent, _ = oms.prepare(target, T0)

        decision = client.check(intent, make_ctx())
        truth(decision.allowed, "正常状态下跨进程决策为 ALLOW")
        eq(decision.verdict.value, "ALLOW", "verdict 序列化往返正确")

        # ---- 单笔超限 → REDUCE_REQUIRED，且 cap_notional 必须是 Decimal ---- #
        strict = RiskServiceServer(SOCK + ".strict",
                                   policy=RiskPolicy(max_order_notional_pct=D("0.5"),
                                                     min_order_notional=D("1")))
        strict.start()
        strict_client = RiskServiceClient(SOCK + ".strict")
        try:
            big = strict_client.check(intent, make_ctx())
            eq(big.verdict.value, "REDUCE_REQUIRED", "超限返回 REDUCE_REQUIRED")
            truth(isinstance(big.cap_notional, Decimal),
                  "cap_notional 跨进程仍是 Decimal（不是字符串/dict）")
            eq(big.cap_notional, D("500"), "cap = 0.5% × NAV 100000 = 500")
        finally:
            strict.stop()

        # ---- 回撤熔断：HALT 必须带处置剧本，而不是一律清仓 ---------------- #
        client.update_fund_state(nav=D("94000"), peak_nav=D("100000"))
        halted = client.check(intent, make_ctx())
        eq(halted.verdict.value, "HALT_FUND", "回撤 6% > 5% → 基金级熔断")
        truth(len(halted.halt_actions) > 0, "熔断给出处置动作")
        truth("cancel_open_orders" in halted.halt_actions,
              "处置动作为『先撤未成交单』而非直接市价清仓")

        # ---- 状态落盘：重启后不能忘记自己已经熔断 ------------------------- #
        truth(os.path.exists(state_path), "风控状态已落盘")
        client.set_permission("NORMAL", "test reset")
        eq(client.state()["permission_level"], "NORMAL", "人工恢复后为 NORMAL")

        client.set_permission("HALTED", "drill")
        server.stop()
        revived = RiskServiceServer(SOCK, policy=LOOSE_POLICY, state_path=state_path)
        revived.start()
        try:
            eq(revived.state.permission_level.value, "HALTED",
               "重启后从磁盘恢复 HALTED（不会自动解除熔断）")
        finally:
            revived.stop()

        # ---- 服务不可用 → 拒单，且不抛异常 -------------------------------- #
        dead = RiskServiceClient(SOCK)          # 服务已停
        result = dead.check(intent, make_ctx())
        truth(result is not None, "服务不可用时仍返回决策（不返回 None）")
        falsy(result.allowed, "服务不可用 → 拒绝下单")
        eq(result.policy_key, "risk_service_unavailable", "拒绝原因可定位到基础设施")
        truth("freeze_new_orders" in result.halt_actions, "同时要求冻结新增订单")
        eq(dead.unavailable_count, 1, "不可用次数被计数（供运维告警）")

        never = RiskServiceClient(os.path.join(TMP, "no-such.sock"))
        falsy(never.check(intent, make_ctx()).allowed, "socket 不存在 → 同样拒单")
    finally:
        server.stop()


# --------------------------------------------------------------------------- #
#  T-202 幂等下单
# --------------------------------------------------------------------------- #

def test_idempotency() -> None:
    section("T-202 幂等下单（client_order_id 全链路）")

    path = os.path.join(TMP, "idem.json")
    store = ClientOrderIdStore(path)
    res1, created1 = store.reserve("TIGER_MAIN", "QOS", "QQQ", Side.BUY, T0)
    truth(created1, "首次申请分配新键")
    res2, created2 = store.reserve("TIGER_MAIN", "QOS", "QQQ", Side.BUY, T0)
    falsy(created2, "同义意图再次申请 → 不分配新键")
    eq(res2.client_order_id, res1.client_order_id, "返回同一个 client_order_id")

    sell, created3 = store.reserve("TIGER_MAIN", "QOS", "QQQ", Side.SELL, T0)
    truth(created3, "方向不同 → 视为不同意图")
    truth(sell.client_order_id != res1.client_order_id, "买卖方向得到不同键")

    # 重启后序号不回退（否则重启即重复下单）
    reopened = ClientOrderIdStore(path)
    res3, _ = reopened.reserve("TIGER_MAIN", "QOS", "SPY", Side.BUY, T0)
    eq(res3.seq, 3, "重启后序号继续为 3（不回退到 1）")
    truth(res3.client_order_id.endswith("-0003"), "键尾序号与 seq 一致")

    # ---- OMS：重试 3 次只有 1 笔订单 ------------------------------------ #
    server = start_server()
    client = RiskServiceClient(SOCK)
    try:
        idem = new_idem("oms")
        oms = Oms("TIGER_MAIN", "QOS", client, idem)
        target = make_target()

        intent, _ = oms.prepare(target, T0)
        first = oms.submit(intent, make_ctx())
        truth(first.accepted and not first.reused, "首次提交成功且不是复用")
        eq(len(oms.open_orders()), 1, "登记 1 笔订单")
        eq(first.attempt, 1, "尝试计数为 1")

        # 上层"重建意图"再来一次：prepare 必须拿回同一个键
        again_intent, _ = oms.prepare(target, T0)
        eq(again_intent.client_order_id, intent.client_order_id,
           "重建意图仍得到同一个键（这是防重复下单的关键）")
        retry = oms.submit(again_intent, make_ctx())
        truth(retry.reused, "第二次提交被识别为重试（reused=True）")
        eq(len(oms.open_orders()), 1, "重试没有产生第二笔订单")

        for _ in range(2):
            oms.submit(again_intent, make_ctx())
        eq(len(oms.open_orders()), 1, "重复提交 3 次仍然只有 1 笔订单")
        eq(idem.get(intent.client_order_id).attempts, 1,
           "命中复用时不增加尝试次数（没有真的再发一次）")

        truth(oms.can_retry(intent.client_order_id), "尝试次数未超限时可重试")

        # ---- 风控不可用 → 一律不下单 ------------------------------------ #
        client.close()
        server.stop()
        dead_client = RiskServiceClient(SOCK)
        oms2 = Oms("TIGER_MAIN", "QOS", dead_client, new_idem("oms2"))
        intent2, _ = oms2.prepare(make_target("2000"), T0)
        blocked = oms2.submit(intent2, make_ctx())
        falsy(blocked.accepted, "风控不可用时提交被拒")
        eq(blocked.reason, "risk_service_unavailable", "拒绝原因指向基础设施")
        eq(len(oms2.open_orders()), 0, "风控不可用时连订单对象都不创建")
    finally:
        server.stop()

    # ---- 成交完成后键关闭，下一笔正常分配新键 ---------------------------- #
    server = start_server()
    client = RiskServiceClient(SOCK)
    try:
        idem3 = new_idem("oms3")
        oms3 = Oms("TIGER_MAIN", "QOS", client, idem3)
        intent3, _ = oms3.prepare(make_target("1200"), T0)
        oms3.submit(intent3, make_ctx())
        fill = Fill(fill_id="f1", order_id="x", client_order_id=intent3.client_order_id,
                    venue_trade_id="T-F1", quantity=D("3"), price=D("400"),
                    fee=D("1"), fee_currency="USD", ts=T0)
        truth(oms3.on_fill(fill), "首笔成交入账")
        falsy(oms3.on_fill(fill), "同一成交重复回报 → 不入账")
        eq(oms3.registry.get(intent3.client_order_id).state, OrderState.FILLED,
           "订单进入 FILLED")

        fresh, _ = oms3.prepare(make_target("1200"), T0 + timedelta(minutes=1))
        truth(fresh.client_order_id != intent3.client_order_id,
              "上一笔完成后，新意图分配新键（不会被历史键卡住）")
    finally:
        client.close()
        server.stop()


# --------------------------------------------------------------------------- #
#  T-203 未知订单对账
# --------------------------------------------------------------------------- #

class FakeGateway:
    def __init__(self, view=None, error=None, cancel_ok=True):
        self.view = view
        self.error = error
        self.cancel_ok = cancel_ok
        self.queries = 0
        self.cancels = 0

    def query_order(self, client_order_id: str) -> VenueOrderView:
        self.queries += 1
        if self.error:
            raise VenueQueryError(self.error)
        return self.view

    def cancel_order(self, client_order_id: str) -> bool:
        self.cancels += 1
        return self.cancel_ok


class FakeRiskAdmin:
    def __init__(self):
        self.calls = []

    def freeze_symbol(self, symbol: str, frozen: bool = True):
        self.calls.append((symbol, frozen))


def _submitted_oms(name="rec"):
    server = start_server()
    client = RiskServiceClient(SOCK)
    oms = Oms("TIGER_MAIN", "QOS", client, new_idem(name))
    intent, _ = oms.prepare(make_target(), T0)
    oms.submit(intent, make_ctx())
    return server, client, oms, intent


def test_reconciliation() -> None:
    section("T-203 未知订单对账闭环")

    # ---- 场景 A：交易所已部分成交，本地未知 ------------------------------ #
    server, client, oms, intent = _submitted_oms("recA")
    try:
        ledger = Ledger()
        ledger.deposit("TIGER_MAIN", "USD", D("100000"), ts=T0)
        booked = []

        def sink(fill: Fill, order) -> bool:
            ledger.apply_fill(fill, venue=order.venue, symbol=order.symbol,
                              account_id=order.account_id, side=order.side,
                              quote_ccy="USD", decision_price=order.ref_price,
                              attribution={}, ts=fill.ts)
            booked.append(fill.venue_trade_id)
            return True

        admin = FakeRiskAdmin()
        gateway = FakeGateway(VenueOrderView(found=True, status="partially_filled",
                                            filled_qty=D("1.5"),
                                            avg_price=D("400.3"), fee=D("0.6")))
        cid = intent.client_order_id

        oms.on_timeout(cid, at=T0 + timedelta(seconds=30))
        eq(oms.registry.get(cid).state, OrderState.UNKNOWN, "超时后为 UNKNOWN")
        truth(cid and oms.registry.symbols_with_unknown_state(), "标的进入未知集合")

        rec = UnknownReconciler(oms.registry, gateway, on_backfill=sink,
                                risk_admin=admin, clock=lambda: T0)
        outcomes = rec.run_once(now=T0 + timedelta(seconds=60))
        eq(len(outcomes), 1, "处理 1 笔未知订单")
        eq(outcomes[0].action, "adopt", "结论为 adopt（交易所确有成交）")
        eq(oms.registry.get(cid).state, OrderState.PARTIALLY_FILLED, "状态对齐为部分成交")
        eq(outcomes[0].backfilled_qty, D("1.5"), "补录 1.5 股")
        eq(len(booked), 1, "账本补记 1 笔成交")
        truth((("TIGER", "QQQ") in ledger.positions()), "账本出现该标的持仓")
        eq(ledger.positions()[("TIGER", "QQQ")].qty, D("1.5"), "持仓数量与交易所一致")
        eq(admin.calls, [("QQQ", False)], "对账收敛后解冻该标的")

        # 重复对账：不得重复记账
        oms.registry.get(cid).transition(OrderState.UNKNOWN, "drill_again", at=T0)
        rec2 = UnknownReconciler(oms.registry, gateway, on_backfill=sink,
                                 risk_admin=admin, clock=lambda: T0)
        rec2.run_once(now=T0 + timedelta(seconds=90))
        eq(len(booked), 1, "重复对账不重复补录（确定性 trade id 去重）")
        eq(ledger.positions()[("TIGER", "QQQ")].qty, D("1.5"), "持仓未被记两遍")
    finally:
        client.close()
        server.stop()

    # ---- 场景 B：交易所明确查无此单 → 判未生效 --------------------------- #
    server, client, oms, intent = _submitted_oms("recB")
    try:
        admin = FakeRiskAdmin()
        gateway = FakeGateway(VenueOrderView(found=False))
        cid = intent.client_order_id
        oms.on_timeout(cid, at=T0 + timedelta(seconds=30))
        rec = UnknownReconciler(oms.registry, gateway, risk_admin=admin,
                                clock=lambda: T0)
        out = rec.run_once(now=T0 + timedelta(seconds=60))[0]
        eq(out.action, "void", "查无此单 → void")
        eq(oms.registry.get(cid).state, OrderState.EXPIRED, "本地判为未生效")
        eq(admin.calls, [("QQQ", False)], "该标的再无未知订单 → 解冻")
        eq(len(rec.run_once(now=T0 + timedelta(seconds=90))), 0, "已收敛的订单不再处理")
    finally:
        client.close()
        server.stop()

    # ---- 场景 C：挂单中 → 撤单 ------------------------------------------ #
    server, client, oms, intent = _submitted_oms("recC")
    try:
        gateway = FakeGateway(VenueOrderView(found=True, status="open",
                                            filled_qty=D("0")))
        cid = intent.client_order_id
        oms.on_timeout(cid, at=T0 + timedelta(seconds=30))
        rec = UnknownReconciler(oms.registry, gateway, clock=lambda: T0)
        out = rec.run_once(now=T0 + timedelta(seconds=60))[0]
        eq(out.action, "cancel", "挂单中 → 撤单")
        eq(gateway.cancels, 1, "确实发出撤单请求")
        falsy(oms.registry.get(cid).is_terminal, "撤单后仍未终态（等待交易所确认）")
    finally:
        client.close()
        server.stop()

    # ---- 场景 D：查不到 → 保持冻结，超时升级人工 ------------------------ #
    server, client, oms, intent = _submitted_oms("recD")
    try:
        gateway = FakeGateway(error="network down")
        cid = intent.client_order_id
        oms.on_timeout(cid, at=T0 + timedelta(seconds=30))
        rec = UnknownReconciler(oms.registry, gateway, max_unknown_sec=60,
                                clock=lambda: T0)
        early = rec.run_once(now=T0 + timedelta(seconds=60))[0]
        eq(early.action, "wait", "查询失败时保持等待（不猜、不重提）")
        eq(oms.registry.get(cid).state, OrderState.UNKNOWN, "状态仍为 UNKNOWN")

        late = rec.run_once(now=T0 + timedelta(seconds=200))[0]
        eq(late.action, "escalate", "超过时限升级为人工处理")
        falsy(oms.registry.get(cid).is_terminal, "升级后不擅自改状态")
    finally:
        client.close()
        server.stop()


# --------------------------------------------------------------------------- #
#  T-204 单活锁
# --------------------------------------------------------------------------- #

def test_lock() -> None:
    section("T-204 账户×网关单活锁")

    root = os.path.join(TMP, "locks")
    first = AccountGatewayLock(root, "TIGER_MAIN", "TIGER")
    owner = first.acquire()
    truth(first.is_held, "首次获取成功")
    eq(owner.pid, os.getpid(), "锁文件记录持有者 pid")

    second = AccountGatewayLock(root, "TIGER_MAIN", "TIGER")
    exc = raises(LockBusy, second.acquire, "第二个写者被拒绝")
    if isinstance(exc, LockBusy):
        eq(exc.owner.pid, os.getpid(), "报错信息带持有者，便于定位是谁在跑")
        truth("TIGER_MAIN" in str(exc), "报错信息含账户名")

    other_gateway = AccountGatewayLock(root, "TIGER_MAIN", "IBKR")
    other_gateway.acquire()
    truth(other_gateway.is_held, "同账户不同网关可并行（这是本锁的设计目的）")
    other_gateway.release()

    other_account = AccountGatewayLock(root, "OKX_MAIN", "TIGER")
    other_account.acquire()
    truth(other_account.is_held, "不同账户互不影响")
    other_account.release()

    first.release()
    falsy(first.is_held, "释放后不再持有")
    reacquired = AccountGatewayLock(root, "TIGER_MAIN", "TIGER")
    reacquired.acquire()
    truth(reacquired.is_held, "释放后可被重新获取")
    reacquired.release()


# --------------------------------------------------------------------------- #

def body() -> None:
    test_risk_service()
    test_idempotency()
    test_reconciliation()
    test_lock()


def main() -> int:
    try:
        return run("B2 风控独立化与幂等执行", body)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
