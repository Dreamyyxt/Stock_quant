#!/usr/bin/env python
"""
QuantOS 2.0 — 故障注入（T-205）
==============================

验收标准不是"跑得通"，而是：**每个故障要么正确恢复，要么安全停止，不允许静默错误。**

七个场景（都是真跑，不是 mock 掉要验证的那一环）：
  1. risk_service_killed   kill -9 风控进程 → OMS 必须拒单
  2. submit_timeout        下单超时 → UNKNOWN + 冻结，不重提
  3. duplicate_fill_report 重复成交回报 → 只记一次账
  4. partial_fill_then_timeout  部分成交后超时 → 对账补齐差额，不重复
  5. process_restart       进程重启 → 恢复快照后不重发；忘恢复则强制转对账
  6. disk_full             磁盘满 → 写入失败必须显式抛出，不许当成功
  7. exchange_outage       交易所失联 60s 后恢复 → 等待、不猜、恢复后收敛

运行：`.venv/bin/python scripts/quantos_fault_inject.py`
退出码 0 = 全部场景通过。
"""

from __future__ import annotations

import builtins
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, List, Tuple

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "scripts"))

import _asserts as A                                          # noqa: E402
from _asserts import eq, falsy, section, truth                # noqa: E402

from quantos_core.contracts import (                          # noqa: E402
    D, Fill, OrderState, Side, TargetDecision, TargetPosition,
)
from quantos_core.events import (                          # noqa: E402
    EventStoreFull, JsonlEventStore, canonical_json, from_jsonable,
)
from quantos_core.idempotency import ClientOrderIdStore        # noqa: E402
from quantos_core.ledger import Ledger                         # noqa: E402
from quantos_core.oms import Oms                               # noqa: E402
from quantos_core.reconciliation import (                      # noqa: E402
    UnknownReconciler, VenueOrderView, VenueQueryError,
)
from quantos_core.risk_engine import RiskContext, RiskPolicy   # noqa: E402
from quantos_core.risk_service import (                        # noqa: E402
    RiskServiceClient, RiskServiceServer,
)

UTC = timezone.utc
T0 = datetime(2026, 10, 26, 13, 30, tzinfo=UTC)
TMP = tempfile.mkdtemp(prefix="qos-fault-")

POLICY = RiskPolicy(
    max_order_notional_pct=D("50"), max_strategy_allocation_pct=D("90"),
    min_order_notional=D("50"), max_gross_exposure_pct=D("200"),
)

SCENARIOS: List[Tuple[str, Callable[[], None]]] = []
VERDICTS: List[Tuple[str, bool, int]] = []


def scenario(title: str):
    def wrap(fn):
        SCENARIOS.append((title, fn))
        return fn
    return wrap


# --------------------------------------------------------------------------- #
#  公共件
# --------------------------------------------------------------------------- #

def make_target(delta="1000", price="400", symbol="QQQ") -> TargetPosition:
    delta_d, price_d = D(delta), D(price)
    qty = (delta_d / price_d).quantize(Decimal("0.00000001"))
    return TargetPosition(
        symbol=symbol, venue="TIGER", per_strategy={"etf_mom": delta_d},
        net_target_notional=delta_d, current_notional=Decimal(0),
        inflight_notional=Decimal(0), delta_notional=delta_d, price=price_d,
        net_target_qty=qty, delta_qty=qty, decision=TargetDecision.TRADE,
    )


def make_ctx(now=T0) -> RiskContext:
    return RiskContext(nav=D("100000"), now=now, market_data_age_sec={"TIGER": D("0")})


def sock(name: str) -> str:
    return os.path.join(TMP, f"{name}.sock")


def idem(name: str) -> ClientOrderIdStore:
    return ClientOrderIdStore(os.path.join(TMP, f"{name}.json"))


class FakeGateway:
    """可控的交易所：能查、能撤、能坏。"""

    def __init__(self, view=None, fail_rounds=0):
        self.view = view
        self.fail_rounds = fail_rounds
        self.queries = 0
        self.cancels = 0

    def query_order(self, client_order_id: str) -> VenueOrderView:
        self.queries += 1
        if self.queries <= self.fail_rounds:
            raise VenueQueryError(f"网络超时（第 {self.queries} 次）")
        return self.view

    def cancel_order(self, client_order_id: str) -> bool:
        self.cancels += 1
        return True


# --------------------------------------------------------------------------- #
#  1. kill -9 风控进程
# --------------------------------------------------------------------------- #

@scenario("kill -9 风控进程 → OMS 必须拒单")
def risk_service_killed() -> None:
    path = sock("kill")
    state_path = os.path.join(TMP, "kill-state.json")
    proc = subprocess.Popen(
        [sys.executable, "-m", "quantos_core.risk_service",
         "--socket", path, "--state", state_path],
        cwd=_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    client = RiskServiceClient(path, timeout=2.0)
    try:
        for _ in range(100):
            if client.healthcheck():
                break
            time.sleep(0.1)
        truth(client.healthcheck(), "子进程风控服务已就绪")

        oms = Oms("TIGER_MAIN", "QOS", client, idem("kill"))
        intent, _ = oms.prepare(make_target(), T0)
        before = oms.submit(intent, make_ctx())
        truth(before.accepted, "杀进程前正常放行（证明链路本来是通的）")

        proc.kill()                      # SIGKILL：不给任何清理机会
        proc.wait(timeout=10)
        eq(proc.returncode, -signal.SIGKILL, "风控进程被 SIGKILL 终止")

        dead = client.check(intent, make_ctx())
        falsy(dead.allowed, "风控进程死后 → 拒单")
        eq(dead.policy_key, "risk_service_unavailable", "拒绝原因定位到基础设施")
        truth("freeze_new_orders" in dead.halt_actions, "同时要求冻结新增订单")

        oms2 = Oms("TIGER_MAIN", "QOS", client, idem("kill2"))
        intent2, _ = oms2.prepare(make_target("2000", symbol="SPY"), T0)
        blocked = oms2.submit(intent2, make_ctx())
        falsy(blocked.accepted, "风控不可用时新订单提交被拒")
        eq(len(oms2.open_orders()), 0, "且不产生订单对象（不留半截状态）")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


# --------------------------------------------------------------------------- #
#  2. 下单超时
# --------------------------------------------------------------------------- #

@scenario("下单超时 → UNKNOWN 冻结，不重提")
def submit_timeout() -> None:
    srv = RiskServiceServer(sock("timeout"), policy=POLICY).start()
    client = RiskServiceClient(sock("timeout"))
    try:
        store = idem("timeout")
        oms = Oms("TIGER_MAIN", "QOS", client, store)
        target = make_target()
        intent, _ = oms.prepare(target, T0)
        oms.submit(intent, make_ctx())

        oms.on_timeout(intent.client_order_id, at=T0 + timedelta(seconds=30))
        order = oms.registry.get(intent.client_order_id)
        eq(order.state, OrderState.UNKNOWN, "超时落到 UNKNOWN（不是判失败）")
        truth(intent.client_order_id in
              [o.client_order_id for o in oms.unknown_orders()], "进入未知订单集合")
        truth(oms.registry.symbols_with_unknown_state() == {"QQQ"}, "标的被冻结")

        # 上层"重试"：拿回同一个键，不产生第二笔
        again, _ = oms.prepare(target, T0 + timedelta(seconds=31))
        eq(again.client_order_id, intent.client_order_id, "重试复用同一个幂等键")
        retry = oms.submit(again, make_ctx(T0 + timedelta(seconds=31)))
        truth(retry.reused, "重试被识别为复用，不是新订单")
        eq(len(oms.open_orders()), 1, "在途订单数仍为 1")

        # 风控侧也必须拒绝：该标的有未知订单
        client.freeze_symbol("QQQ", frozen=True)
        new_intent, _ = oms.prepare(make_target("3000", symbol="QQQ"),
                                    T0 + timedelta(seconds=32))
        # 注意：prepare 会命中未关闭的预留并复用键，这里只验风控口径
        decision = client.check(new_intent, make_ctx(T0 + timedelta(seconds=32)))
        falsy(decision.allowed, "该标的存在未知订单时，风控拒绝新单")
        eq(decision.policy_key, "unknown_order_status_action",
           "拒绝原因指向未知订单（不是笼统的『系统异常』）")
    finally:
        client.close()
        srv.stop()


# --------------------------------------------------------------------------- #
#  3. 重复成交回报
# --------------------------------------------------------------------------- #

@scenario("重复成交回报 → 只记一次账")
def duplicate_fill_report() -> None:
    srv = RiskServiceServer(sock("dup"), policy=POLICY).start()
    client = RiskServiceClient(sock("dup"))
    try:
        oms = Oms("TIGER_MAIN", "QOS", client, idem("dup"))
        ledger = Ledger()
        ledger.deposit("TIGER_MAIN", "USD", D("100000"), ts=T0)

        intent, _ = oms.prepare(make_target(), T0)
        oms.submit(intent, make_ctx())
        order = oms.registry.get(intent.client_order_id)

        fill = Fill(fill_id="f1", order_id=order.order_id,
                    client_order_id=intent.client_order_id,
                    venue_trade_id="T-DUP-1", quantity=D("1"),
                    price=D("400"), fee=D("0.2"), fee_currency="USD", ts=T0)

        booked = 0
        for _ in range(3):
            if oms.on_fill(fill):
                ledger.apply_fill(fill, venue="TIGER", symbol="QQQ",
                                  account_id="TIGER_MAIN", side=Side.BUY,
                                  quote_ccy="USD", decision_price=order.ref_price,
                                  attribution={"etf_mom": D("1")}, ts=T0)
                booked += 1

        eq(booked, 1, "3 次相同回报只入账 1 次")
        eq(order.filled_qty, D("1"), "订单成交量未被重复累加")
        eq(ledger.positions()[("TIGER", "QQQ")].qty, D("1"), "持仓只增加 1 股")
        eq(order.state, OrderState.PARTIALLY_FILLED, "订单仍为部分成交（总量 2.5 股）")
    finally:
        client.close()
        srv.stop()


# --------------------------------------------------------------------------- #
#  4. 部分成交后超时
# --------------------------------------------------------------------------- #

@scenario("部分成交后超时 → 对账补齐差额，不重复")
def partial_fill_then_timeout() -> None:
    srv = RiskServiceServer(sock("partial"), policy=POLICY).start()
    client = RiskServiceClient(sock("partial"))
    try:
        oms = Oms("TIGER_MAIN", "QOS", client, idem("partial"))
        ledger = Ledger()
        ledger.deposit("TIGER_MAIN", "USD", D("100000"), ts=T0)

        intent, _ = oms.prepare(make_target("1000", price="400"), T0)   # 2.5 股
        oms.submit(intent, make_ctx())
        cid = intent.client_order_id

        # 先到一笔 1 股的部分成交，然后超时
        first = Fill(fill_id="p1", order_id="x", client_order_id=cid,
                     venue_trade_id="T-P-1", quantity=D("1"), price=D("400"),
                     fee=D("0.2"), fee_currency="USD", ts=T0)
        truth(oms.on_fill(first), "部分成交入账")
        ledger.apply_fill(first, venue="TIGER", symbol="QQQ", account_id="TIGER_MAIN",
                          side=Side.BUY, quote_ccy="USD",
                          decision_price=D("400"), attribution={"etf_mom": D("1")}, ts=T0)
        oms.on_timeout(cid, at=T0 + timedelta(seconds=30))
        eq(oms.registry.get(cid).state, OrderState.UNKNOWN, "部分成交后超时 → UNKNOWN")

        # 交易所其实已经全部成交 2.5 股，本地只有 1 股
        gateway = FakeGateway(VenueOrderView(found=True, status="filled",
                                            filled_qty=D("2.5"), avg_price=D("400.4"),
                                            fee=D("0.5")))
        backfills = []

        def sink(fill: Fill, order) -> bool:
            ledger.apply_fill(fill, venue="TIGER", symbol="QQQ",
                              account_id="TIGER_MAIN", side=Side.BUY,
                              quote_ccy="USD", decision_price=order.ref_price,
                              attribution={"etf_mom": fill.quantity}, ts=fill.ts)
            backfills.append(fill.venue_trade_id)
            return True

        rec = UnknownReconciler(oms.registry, gateway, on_backfill=sink,
                                clock=lambda: T0)
        out = rec.run_once(now=T0 + timedelta(seconds=60))[0]
        eq(out.action, "adopt", "对账结论为 adopt")
        eq(out.backfilled_qty, D("1.5"), "只补差额 1.5 股（不是 2.5）")
        eq(oms.registry.get(cid).state, OrderState.FILLED, "订单收敛为全部成交")
        eq(ledger.positions()[("TIGER", "QQQ")].qty, D("2.5"), "账本持仓与交易所一致")
        eq(len(backfills), 1, "只补录一次")

        # 再来一轮对账：不得重复补
        oms.registry.get(cid).state = OrderState.UNKNOWN
        rec2 = UnknownReconciler(oms.registry, gateway, on_backfill=sink,
                                 clock=lambda: T0)
        rec2.run_once(now=T0 + timedelta(seconds=90))
        eq(len(backfills), 1, "重复对账不重复补录")
        eq(ledger.positions()[("TIGER", "QQQ")].qty, D("2.5"), "持仓仍为 2.5 股")
    finally:
        client.close()
        srv.stop()


# --------------------------------------------------------------------------- #
#  5. 进程重启
# --------------------------------------------------------------------------- #

@scenario("进程重启 → 恢复快照不重发；忘恢复则强制转对账")
def process_restart() -> None:
    srv = RiskServiceServer(sock("restart"), policy=POLICY).start()
    client = RiskServiceClient(sock("restart"))
    try:
        store_path = os.path.join(TMP, "restart-idem.json")
        before_oms = Oms("TIGER_MAIN", "QOS", client, ClientOrderIdStore(store_path))
        target = make_target()
        intent, _ = before_oms.prepare(target, T0)
        before_oms.submit(intent, make_ctx())
        eq(before_oms.idem.get(intent.client_order_id).attempts, 1, "重启前已提交 1 次")

        snapshot = before_oms.snapshot()
        eq(len(snapshot), 1, "快照含 1 笔在途订单")
        snap_path = os.path.join(TMP, "oms-snapshot.json")
        with open(snap_path, "w", encoding="utf-8") as fh:
            fh.write(canonical_json(snapshot))
        truth(os.path.getsize(snap_path) > 0, "快照已落盘")

        # ---- 重启：同一个幂等键文件 + 同一份快照 ------------------------ #
        with open(snap_path, "r", encoding="utf-8") as fh:
            reloaded = from_jsonable(json.load(fh))
        truth(isinstance(reloaded[0]["intent"]["notional"], Decimal),
              "快照回读后金额仍是 Decimal（不是字符串）")
        after_oms = Oms("TIGER_MAIN", "QOS", client, ClientOrderIdStore(store_path))
        restored = after_oms.restore(reloaded)
        eq(restored, 1, "恢复 1 笔在途订单")
        eq(len(after_oms.open_orders()), 1, "重启后能看到在途订单")

        same_intent, _ = after_oms.prepare(target, T0 + timedelta(seconds=10))
        eq(same_intent.client_order_id, intent.client_order_id, "重启后仍是同一个键")
        result = after_oms.submit(same_intent, make_ctx(T0 + timedelta(seconds=10)))
        truth(result.reused, "重启后提交被识别为复用，不重发")
        eq(len(after_oms.open_orders()), 1, "订单总数仍为 1")

        # ---- 负向对照：忘调 restore() 会怎样 ---------------------------- #
        amnesiac = Oms("TIGER_MAIN", "QOS", client, ClientOrderIdStore(store_path))
        forget, _ = amnesiac.prepare(target, T0 + timedelta(seconds=20))
        blocked = amnesiac.submit(forget, make_ctx(T0 + timedelta(seconds=20)))
        falsy(blocked.accepted, "忘了恢复快照时不放行")
        eq(blocked.reason, "needs_reconciliation",
           "而是要求先对账（把『忘了恢复』变成可发现的错误，不是重复下单）")
        eq(len(amnesiac.open_orders()), 0, "且不创建任何订单")
    finally:
        client.close()
        srv.stop()


# --------------------------------------------------------------------------- #
#  6. 磁盘满
# --------------------------------------------------------------------------- #

@scenario("磁盘满 → 写入失败必须显式抛出")
def disk_full() -> None:
    path = os.path.join(TMP, "diskfull.jsonl")
    store = JsonlEventStore(path, stream="fault")
    store.append("deposit", {"amount": D("100000")}, ts=T0)
    seq_before = store.last_seq
    count_before = store.count

    real_open = builtins.open
    target = store.path

    def failing_open(file, mode="r", *args, **kwargs):
        if str(file) == target and any(flag in str(mode) for flag in ("a", "w")):
            raise OSError(28, "No space left on device")
        return real_open(file, mode, *args, **kwargs)

    builtins.open = failing_open
    try:
        A.raises(EventStoreFull, lambda: store.append("fill", {"x": D("1")}),
                 "磁盘满时抛出 EventStoreFull（不许静默当成功）")
    finally:
        builtins.open = real_open

    eq(store.last_seq, seq_before, "失败后 seq 未推进（不会留下空号）")
    eq(store.count, count_before, "失败后事件数不变")
    env, _ = store.append("after_recovery", {"ok": True}, ts=T0)
    eq(env.seq, seq_before + 1, "磁盘恢复后继续写入，序号连续")

    # 崩溃留下的半行：只截尾部，不丢已确认事件
    with open(store.path, "a", encoding="utf-8") as fh:
        fh.write('{"seq":99,"event_id":"half')
    repaired = JsonlEventStore(store.path, stream="fault")
    truth(repaired.repaired_bytes > 0, "重新加载时截断残缺半行")
    eq(repaired.count, count_before + 1, "已确认事件全部保留")
    repaired.close()
    store.close()


# --------------------------------------------------------------------------- #
#  7. 交易所失联后恢复
# --------------------------------------------------------------------------- #

@scenario("交易所失联 60s 后恢复 → 等待、不猜、恢复后收敛")
def exchange_outage() -> None:
    srv = RiskServiceServer(sock("outage"), policy=POLICY).start()
    client = RiskServiceClient(sock("outage"))
    try:
        oms = Oms("TIGER_MAIN", "QOS", client, idem("outage"))
        intent, _ = oms.prepare(make_target(), T0)
        oms.submit(intent, make_ctx())
        cid = intent.client_order_id
        oms.on_timeout(cid, at=T0 + timedelta(seconds=5))

        # 前 2 轮查询全部超时，第 3 轮恢复
        gateway = FakeGateway(VenueOrderView(found=True, status="filled",
                                            filled_qty=D("2.5"),
                                            avg_price=D("400"), fee=D("0.5")),
                              fail_rounds=2)
        rec = UnknownReconciler(oms.registry, gateway, max_unknown_sec=600,
                                clock=lambda: T0)

        first = rec.run_once(now=T0 + timedelta(seconds=30))[0]
        eq(first.action, "wait", "第 1 次查不到 → 继续等待")
        eq(oms.registry.get(cid).state, OrderState.UNKNOWN, "状态保持 UNKNOWN（不猜）")

        second = rec.run_once(now=T0 + timedelta(seconds=60))[0]
        eq(second.action, "wait", "第 2 次仍失联 → 仍然等待")

        ledger = Ledger()
        ledger.deposit("TIGER_MAIN", "USD", D("100000"), ts=T0)

        def backfill(fill: Fill, order) -> bool:
            return ledger.apply_fill(
                fill, venue="TIGER", symbol="QQQ", account_id="TIGER_MAIN",
                side=Side.BUY, quote_ccy="USD", decision_price=order.ref_price,
                attribution={"etf_mom": fill.quantity}, ts=fill.ts)

        rec3 = UnknownReconciler(oms.registry, gateway, on_backfill=backfill,
                                 max_unknown_sec=600, clock=lambda: T0)
        third = rec3.run_once(now=T0 + timedelta(seconds=90))[0]
        eq(third.action, "adopt", "恢复后收敛为 adopt")
        eq(oms.registry.get(cid).state, OrderState.FILLED, "订单补记为全部成交")
        eq(ledger.positions()[("TIGER", "QQQ")].qty, D("2.5"), "账本与交易所一致")
        eq(len(oms.open_orders()), 0, "无遗留未完成订单")
        truth(not oms.registry.symbols_with_unknown_state(), "标的已解冻，可继续交易")
    finally:
        client.close()
        srv.stop()


# --------------------------------------------------------------------------- #

def main() -> int:
    print("QuantOS 故障注入（T-205）")
    print(f"临时目录：{TMP}")
    try:
        for title, fn in SCENARIOS:
            section(title)
            before = A.failure_count()
            try:
                fn()
            except BaseException as exc:                      # noqa: BLE001
                A.truth(False, f"场景中断：{type(exc).__name__}: {exc}")
            VERDICTS.append((title, A.failure_count() == before,
                             A.failure_count() - before))

        print("\n" + "=" * 66)
        print("故障注入结果")
        print("-" * 66)
        for title, ok, failures in VERDICTS:
            mark = "PASS" if ok else f"FAIL({failures})"
            print(f"  [{mark:>8}] {title}")
        passed = sum(1 for _, ok, _ in VERDICTS if ok)
        print("-" * 66)
        print(f"  {passed}/{len(VERDICTS)} 场景通过，共 {A.total_count()} 条断言")
        return 0 if passed == len(VERDICTS) else 1
    finally:
        shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
