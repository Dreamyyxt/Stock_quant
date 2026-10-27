#!/usr/bin/env python3
"""
QuantOS 2.0 — B4 自检（T-401 实盘开关 / T-402 日终四项对账 / T-403 归因日报）
=========================================================================

每条断言对应一个**会让真钱出问题**的失败模式，不是"函数能跑"。

    .venv/bin/python scripts/quantos_selftest_b4.py

三段各自回答一个问题：

    A. 三级开关   少一级确认，到底能不能下单？（不能）
    B. 日终对账   钱和券商对不上时，系统会不会以为对上了？（不会）
    C. 归因日报   当日赚的钱，能不能被五项拆干净？（能，且汇率项是独立验的）
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from _asserts import (  # noqa: E402
    eq, falsy, guard, near, ne, raises, run, section, truth,
)

from quantos_core.contracts import (  # noqa: E402
    D, Fill, Order, OrderIntent, OrderRegistry, OrderState, OrderType, Side,
    utcnow,
)
from quantos_core.eod import (  # noqa: E402
    BrokerStatement, DiffKind, EodReconciler, EodService, NavPoint, NavSeries,
    StatementUnavailable, build_daily_attribution,
)
from quantos_core.ledger import Ledger  # noqa: E402
from quantos_core.live_gate import (  # noqa: E402
    Confirmation, ConfirmationError, LiveGate, LiveGateContext, TradingMode,
)

ACC = "ACC1"
STRAT = "s1"


# --------------------------------------------------------------------------- #
#  构造工具
# --------------------------------------------------------------------------- #

def ok_confirm(echo: str, actor: str = "human:owner") -> Confirmation:
    return Confirmation(actor, "模拟盘观察期已满，申请实盘额度", echo)


def make_intent(notional: str = "1000", *, account: str = ACC,
                strategy: str = STRAT, qty: str = "2.5") -> OrderIntent:
    now = utcnow()
    return OrderIntent(
        order_intent_id="oi-1", client_order_id="cid-1", account_id=account,
        venue="TIGER", symbol="QQQ", side=Side.BUY, quantity=D(qty),
        order_type=OrderType.MARKET, notional=D(notional),
        attribution={strategy: D(qty)}, reason_chain=(), as_of=now, ts=now,
    )


def ctx(nav: str = "100000", gross: str = "0",
        used: str = "0") -> LiveGateContext:
    return LiveGateContext(nav=D(nav), account_gross=D(gross),
                           strategy_notional={STRAT: D(used)})


def armed_gate(*, nav_ok: bool = True, gross_cap: str = "60",
               strat_cap: str = "30000", order_cap: str = "5000",
               arm_account: bool = True, arm_strategy: bool = True,
               arm_cap: bool = True) -> LiveGate:
    g = LiveGate()
    if arm_account:
        g.arm_account(ACC, gross_cap_pct=D(gross_cap),
                      confirm=ok_confirm(ACC))
    if arm_strategy:
        g.arm_strategy(ACC, STRAT, notional_cap=D(strat_cap),
                       confirm=ok_confirm(STRAT))
    if arm_cap:
        from quantos_core.contracts import q
        g.set_order_cap(ACC, max_notional=D(order_cap),
                        confirm=ok_confirm(str(q(D(order_cap), 2))))
    g.set_reconciliation(ACC, nav_ok, "四项差异 0" if nav_ok else "现金差 12.4")
    return g


class MemBroker:
    """内存券商账单。`stmt` 为 None 时抛 StatementUnavailable。"""

    def __init__(self, stmt=None):
        self.stmt = stmt

    def fetch_statement(self, account_id, day):
        if self.stmt is None:
            raise StatementUnavailable("券商维护中")
        return self.stmt


def build_books():
    """一本有 1 笔买入成交、1 笔在途挂单的账。"""
    now = utcnow()
    ledger = Ledger(base_currency="USD")
    ledger.deposit(ACC, "USD", D("100000"), ts=now)
    ledger.apply_fill(
        Fill(fill_id="f1", order_id="O1", client_order_id="cid-1",
             venue_trade_id="T-1001", quantity=D("10"), price=D("400"),
             fee=D("2"), fee_currency="USD", ts=now),
        venue="TIGER", symbol="QQQ", account_id=ACC, side=Side.BUY,
        quote_ccy="USD", decision_price=D("400"),
        attribution={STRAT: D("10")}, ts=now)

    book = OrderRegistry()
    book.adopt(Order(order_id="O2", client_order_id="cid-2", account_id=ACC,
                     venue="TIGER", symbol="SPY", side=Side.BUY,
                     quantity=D("5"), ref_price=D("500"),
                     state=OrderState.SUBMITTED))
    return ledger, book, now


def statement_for(ledger, book, day, *, cash_delta="0", pos_qty="10",
                  open_ids=("cid-2",), trade_ids=("T-1001",)) -> BrokerStatement:
    return BrokerStatement(
        account_id=ACC, day=day, as_of=utcnow(),
        cash={(ACC, "USD"): ledger.cash_native()[(ACC, "USD")] + D(cash_delta)},
        positions={("TIGER", "QQQ"): D(pos_qty)},
        open_client_order_ids=tuple(open_ids),
        trade_ids=tuple(trade_ids),
    )


# --------------------------------------------------------------------------- #
#  A. 三级实盘开关（T-401）
# --------------------------------------------------------------------------- #

def test_gate() -> None:
    section("A. 三级开关：少一级就不能下实盘单")

    # A1 未武装 → 拒，且指到账户级
    g = LiveGate()
    dec = g.authorize(make_intent(), ctx(), mode=TradingMode.LIVE)
    falsy(dec.allowed, "A1 未武装时拒绝实盘下单")
    eq(dec.blocked_layer, "account", "A1 阻断层指向账户级")

    # A2 一次列全缺项（不能"改一个刷新一次才发现还缺一个"）
    bad = [c for c in dec.checks if not c.ok]
    truth(len(bad) >= 3, f"A2 一次列出全部缺项（实际 {len(bad)} 项）")
    truth(any(c.level == "reconciliation" for c in dec.checks),
          "A2 缺项里包含对账未清零")

    # A3 纸面单不需要武装
    dec = g.authorize(make_intent(), ctx(), mode=TradingMode.PAPER)
    truth(dec.allowed, "A3 纸面单不走实盘开关")

    # A4~A6 三类确认不完整一律拒
    raises(ConfirmationError,
           lambda: g.arm_account(ACC, gross_cap_pct=D("50"),
                                 confirm=Confirmation("AI:model", "自动申请的额度", ACC)),
           "A4 AI 不能武装实盘")
    raises(ConfirmationError,
           lambda: g.arm_account(ACC, gross_cap_pct=D("50"),
                                 confirm=Confirmation("human:owner", "嗯", ACC)),
           "A5 理由太短被拒")
    raises(ConfirmationError,
           lambda: g.arm_account(ACC, gross_cap_pct=D("50"),
                                 confirm=Confirmation("human:owner", "模拟盘观察期已满", "ACC9")),
           "A6 回显不一致被拒（防点错账户）")

    # A7 只武装账户级 → 仍然拒，指到策略级
    g = armed_gate(arm_strategy=False, arm_cap=False)
    dec = g.authorize(make_intent(), ctx(), mode=TradingMode.LIVE)
    falsy(dec.allowed, "A7 只有账户级授权仍然拒绝")
    truth(dec.blocked_layer.startswith("strategy"), "A7 阻断层指向策略级")

    # A8 账户+策略，缺单笔上限 → 仍然拒
    g = armed_gate(arm_cap=False)
    dec = g.authorize(make_intent(), ctx(), mode=TradingMode.LIVE)
    falsy(dec.allowed, "A8 缺单笔上限仍然拒绝")
    eq(dec.blocked_layer, "order_cap", "A8 阻断层指向单笔上限")

    # A9 三级齐但对账不干净 → 拒
    g = armed_gate(nav_ok=False)
    dec = g.authorize(make_intent(), ctx(), mode=TradingMode.LIVE)
    falsy(dec.allowed, "A9 对账未清零时禁止实盘")
    eq(dec.blocked_layer, "reconciliation", "A9 阻断层指向对账")

    # A10 三级齐 + 对账干净 → 放行
    g = armed_gate()
    dec = g.authorize(make_intent(), ctx(), mode=TradingMode.LIVE)
    truth(dec.allowed, "A10 三级授权齐全 + 对账清零才放行")
    eq(len([c for c in dec.checks if not c.ok]), 0, "A10 放行时所有检查项都通过")

    # A11 超过单笔上限
    dec = g.authorize(make_intent("6000"), ctx(), mode=TradingMode.LIVE)
    falsy(dec.allowed, "A11 超过单笔上限被拒")
    eq(dec.blocked_layer, "order_cap:amount", "A11 阻断层指向单笔金额")

    # A12 NAV 百分比硬顶（单笔上限设成 99999，也必须被 10% × NAV 压住）
    g2 = armed_gate(order_cap="99999")
    dec = g2.authorize(make_intent("20000"), ctx(nav="100000"),
                       mode=TradingMode.LIVE)
    falsy(dec.allowed, "A12 NAV 百分比硬顶压住绝对上限")
    cap_check = next(c for c in dec.checks if c.level == "order_cap:amount")
    truth("10.0% NAV" in cap_check.detail, "A12 明细写明 NAV 百分比硬顶口径")
    truth("10000.00" in cap_check.detail, "A12 硬顶金额 = 100000 × 10%")

    # A13 NAV 不可用 → 拒（0 × 任何比例都通过，必须单独拦）
    dec = g.authorize(make_intent("100"), ctx(nav="0"), mode=TradingMode.LIVE)
    falsy(dec.allowed, "A13 NAV 为 0 时拒绝实盘")
    eq(dec.blocked_layer, "nav", "A13 阻断层指向 NAV")

    # A14 到期即失效（不是"提醒"，是直接当没武装）
    g = armed_gate()
    later = utcnow() + timedelta(hours=13)
    dec = g.authorize(make_intent(), ctx(), mode=TradingMode.LIVE, now=later)
    falsy(dec.allowed, "A14 授权过期后拒绝实盘")
    truth("expired" in dec.reason, "A14 原因写明 expired")

    # A15 撤销策略级：另外两级还在，仍然拒
    g = armed_gate()
    g.revoke(ACC, STRAT, by="human:owner", why="人工撤销")
    dec = g.authorize(make_intent(), ctx(), mode=TradingMode.LIVE)
    falsy(dec.allowed, "A15 撤掉任一策略级后拒绝")
    truth(dec.blocked_layer.startswith("strategy"), "A15 阻断层指向被撤的策略")

    # A16 策略额度已用满
    g = armed_gate(strat_cap="1000")
    dec = g.authorize(make_intent("1000"), ctx(used="500"),
                      mode=TradingMode.LIVE)
    falsy(dec.allowed, "A16 策略额度不足时拒绝")
    eq(dec.blocked_layer, f"strategy_cap:{STRAT}", "A16 阻断层指向策略额度")

    # A17 账户总敞口超限
    g = armed_gate(gross_cap="10")
    dec = g.authorize(make_intent("20000"), ctx(nav="100000", gross="5000"),
                      mode=TradingMode.LIVE)
    falsy(dec.allowed, "A17 下单后超账户总敞口被拒")

    # A18 没有策略归属的订单不能实盘（无人负责的单不许碰真钱）
    now = utcnow()
    orphan = OrderIntent(
        order_intent_id="oi-2", client_order_id="cid-2", account_id=ACC,
        venue="TIGER", symbol="QQQ", side=Side.BUY, quantity=D("1"),
        order_type=OrderType.MARKET, notional=D("1000"),
        attribution={}, reason_chain=(), as_of=now, ts=now)
    g = armed_gate()
    dec = g.authorize(orphan, ctx(), mode=TradingMode.LIVE)
    falsy(dec.allowed, "A18 无策略归属的订单禁止实盘")

    # A19 撤销是"一按就关"，不需要确认
    g = armed_gate()
    g.revoke(ACC, by="human:owner", why="紧急刹车")
    dec = g.authorize(make_intent(), ctx(), mode=TradingMode.LIVE)
    falsy(dec.allowed, "A19 撤销账户级后立刻不能下单")

    # A20 每次武装/撤权/授权都留痕
    kinds = [e["kind"] for e in g.events]
    truth(kinds.count("arm") >= 3, "A20 三次武装都有审计记录")
    truth("revoke" in kinds, "A20 撤销有审计记录")
    truth("authorize" in kinds, "A20 授权判定有审计记录")

    # A21 status() 给出 missing 清单（运维要看到"还差哪几级"）
    g = armed_gate(arm_cap=False)
    st = g.status()
    truth(bool(st["missing"][ACC]), "A21 status 报出缺项清单")
    truth(not st["live_ready"][ACC], "A21 live_ready 为假")
    truth(any("单笔上限" in m for m in st["missing"][ACC]),
          "A21 缺项里写明单笔上限")


# --------------------------------------------------------------------------- #
#  B. 日终四项对账（T-402）
# --------------------------------------------------------------------------- #

def test_eod() -> None:
    section("B. 日终四项对账：现金/持仓/挂单/成交")

    ledger, book, now = build_books()
    day = now.date()
    rec = EodReconciler(ledger, book)

    # B1 完全一致
    rep = rec.run(ACC, day, MemBroker(statement_for(ledger, book, day)))
    truth(rep.clean, "B1 四项一致时判为干净")
    eq(rep.counts["cash"] + rep.counts["position"] + rep.counts["open_order"]
       + rep.counts["trade"], 0, "B1 四项计数全为 0")

    # B2 现金差
    rep = rec.run(ACC, day, MemBroker(statement_for(ledger, book, day,
                                                    cash_delta="12.4")))
    falsy(rep.clean, "B2 现金差一分也不干净")
    eq(rep.counts["cash"], 1, "B2 现金差异 1 条")
    eq(rep.by_kind(DiffKind.CASH)[0].delta, "12.40000000", "B2 差额方向正确")

    # B3 持仓差
    rep = rec.run(ACC, day, MemBroker(statement_for(ledger, book, day,
                                                    pos_qty="10.5")))
    eq(rep.counts["position"], 1, "B3 持仓差异 1 条")
    truth("数量不一致" in rep.by_kind(DiffKind.POSITION)[0].note,
          "B3 差异说明是数量不一致")

    # B4 本地以为还挂着、券商已结束
    rep = rec.run(ACC, day, MemBroker(statement_for(ledger, book, day,
                                                    open_ids=())))
    eq(rep.counts["open_order"], 1, "B4 本地有挂单券商无 → 1 条")
    truth("回报" in rep.by_kind(DiffKind.OPEN_ORDER)[0].note,
          "B4 说明指向成交/撤单回报丢失")

    # B5 券商挂着本地不知道的单
    rep = rec.run(ACC, day, MemBroker(statement_for(ledger, book, day,
                                                    open_ids=("cid-2", "cid-9"))))
    eq(rep.counts["open_order"], 1, "B5 券商有挂单本地无 → 1 条")
    truth("查明来源" in rep.by_kind(DiffKind.OPEN_ORDER)[0].note,
          "B5 说明要求查明来源")

    # B6 账本漏记真实成交（净值会虚高）
    rep = rec.run(ACC, day, MemBroker(statement_for(ledger, book, day,
                                                    trade_ids=("T-1001", "T-9999"))))
    eq(rep.counts["trade"], 1, "B6 券商有成交本地无 → 1 条")
    truth("虚高" in rep.by_kind(DiffKind.TRADE)[0].note, "B6 说明指出净值会虚高")

    # B7 本地记了券商不认的成交（synthetic 补录）
    rep = rec.run(ACC, day, MemBroker(statement_for(ledger, book, day,
                                                    trade_ids=())))
    eq(rep.counts["trade"], 1, "B7 本地有成交券商无 → 1 条")
    truth("synthetic" in rep.by_kind(DiffKind.TRADE)[0].note,
          "B7 说明指向 synthetic 补录")

    # B8 拿不到账单 ≠ 对上了
    rep = rec.run(ACC, day, MemBroker(None))
    falsy(rep.clean, "B8 拿不到券商账单时判为不干净")
    falsy(rep.statement_ok, "B8 statement_ok 为假")
    truth("券商维护中" in rep.statement_error, "B8 记录失败原因")

    # B9 差异按危险度排序：成交 > 挂单 > 持仓 > 现金
    rep = rec.run(ACC, day, MemBroker(statement_for(
        ledger, book, day, cash_delta="1", pos_qty="9", open_ids=(),
        trade_ids=("T-1001", "T-9999"))))
    order = [d.kind for d in rep.diffs]
    truth(order.index(DiffKind.TRADE) < order.index(DiffKind.CASH),
          "B9 成交差异排在现金差异之前")
    truth(order.index(DiffKind.OPEN_ORDER) < order.index(DiffKind.POSITION),
          "B9 挂单差异排在持仓差异之前")

    # B10 对账结果落盘
    tmp = tempfile.mkdtemp(prefix="qos-b4-")
    svc = EodService(ledger, book, series=NavSeries(os.path.join(tmp, "nav.json")),
                     report_dir=os.path.join(tmp, "eod"))
    prices = {("TIGER", "QQQ"): D("410"), ("TIGER", "SPY"): D("500")}
    run1 = svc.run(ACC, day, MemBroker(statement_for(ledger, book, day)),
                   prices)
    report_path = os.path.join(tmp, "eod", f"{day.isoformat()}.json")
    truth(os.path.isfile(report_path), "B10 对账报告落盘")
    saved = json.load(open(report_path, encoding="utf-8"))
    truth(saved["clean"] is True, "B10 落盘内容 = 内存报告")

    # B11 对账干净 → 实盘开关的"对账资格"通过
    gate = LiveGate()
    svc2 = EodService(ledger, book, series=NavSeries(os.path.join(tmp, "n2.json")),
                      report_dir=os.path.join(tmp, "eod2"), gate=gate)
    svc2.run(ACC, day, MemBroker(statement_for(ledger, book, day)), prices)
    truth(gate.reconciliation_clean(ACC), "B11 对账干净后 gate 放行资格")

    # B12 对账不干净 → 已武装的实盘立刻被拦（T-401 × T-402 的耦合）
    gate = armed_gate()
    svc3 = EodService(ledger, book, series=NavSeries(os.path.join(tmp, "n3.json")),
                      report_dir=os.path.join(tmp, "eod3"), gate=gate)
    svc3.run(ACC, day, MemBroker(statement_for(ledger, book, day,
                                               cash_delta="50")), prices)
    dec = gate.authorize(make_intent(), ctx(), mode=TradingMode.LIVE)
    falsy(dec.allowed, "B12 日终对不上后实盘立刻被拦")
    eq(dec.blocked_layer, "reconciliation", "B12 阻断层指向对账")

    # B13 对账恢复后重新可以放行（不需要重新武装）
    svc3.run(ACC, day, MemBroker(statement_for(ledger, book, day)), prices)
    dec = gate.authorize(make_intent(), ctx(), mode=TradingMode.LIVE)
    truth(dec.allowed, "B13 对账修好后实盘恢复（授权未过期）")


# --------------------------------------------------------------------------- #
#  C. 归因日报（T-403）
# --------------------------------------------------------------------------- #

def test_attribution() -> None:
    section("C. 归因日报：五项之和 = 当日净收益")

    ledger, book, now = build_books()
    day1, day2 = date(2026, 10, 22), date(2026, 10, 23)
    tmp = tempfile.mkdtemp(prefix="qos-b4attr-")
    series = NavSeries(os.path.join(tmp, "nav.json"))
    svc = EodService(ledger, book, series=series,
                     report_dir=os.path.join(tmp, "eod"))

    p1 = {("TIGER", "QQQ"): D("410"), ("TIGER", "SPY"): D("500")}
    p2 = {("TIGER", "QQQ"): D("420"), ("TIGER", "SPY"): D("500")}

    run1 = svc.run(ACC, day1, MemBroker(statement_for(ledger, book, day1)), p1)
    # C1 首日无基线要说清楚
    truth(run1.attribution.first_day, "C1 首日标记为 first_day")
    truth(any("基线" in w for w in run1.attribution.warnings),
          "C1 首日给出基线缺失告警")

    # 第二天价格 410 → 420，10 股 = +100
    stmt2 = statement_for(ledger, book, day2)
    run2 = svc.run(ACC, day2, MemBroker(stmt2), p2)
    a = run2.attribution

    eq(a.nav_open, D("100098.0000"), "C2 期初净值取上一日收盘")
    eq(a.nav_close, D("100198.0000"), "C2 期末净值正确")
    eq(a.net_return, D("100.0000"), "C2 当日净收益 = 100")
    eq(a.trading, D("100.0000"), "C3 策略收益项 = 10 股 × 10 元")

    # C4 五项之和等于净收益
    truth(a.closes, "C4 五项之和 = 当日净收益")
    eq(a.attribution_sum, a.net_return, "C4 归因和与净收益精确相等")

    # C5 单币种下汇率项必须精确为 0（这才是能抓错的独立校验）
    eq(a.fx, Decimal("0"), "C5 单一计价货币下汇率项为 0")
    truth(not any("汇率项应" in w for w in a.warnings),
          "C5 汇率项异常时才会告警（此处无）")

    # C6 手续费只会是负贡献
    truth(a.fee <= 0, "C6 手续费项不为正")
    eq(a.fee, Decimal("0"), "C6 当日无新成交则手续费增量为 0")

    # C7 策略分摊与交易收益同口径
    eq(a.by_strategy.get(STRAT), D("100.0000"), "C7 策略分摊 = 100")
    eq(sum(a.by_strategy.values()), a.trading, "C7 策略分摊合计 = 交易收益")

    # C8 构造一笔"记错方向"的手续费，必须被抓出来
    bad_point = NavPoint(
        day="2026-10-24", ts=now, nav=D("100198"), net_pnl=D("100"),
        trading=D("100"), slippage=Decimal("0"), fee=D("5"),
        funding=Decimal("0"), fx=Decimal("0"), external_flow=D("100000"),
        gross_exposure=D("4200"), net_exposure=D("4200"))
    prev_point = NavPoint(
        day="2026-10-23", ts=now, nav=D("100198"), net_pnl=Decimal("0"),
        trading=Decimal("0"), slippage=Decimal("0"), fee=Decimal("0"),
        funding=Decimal("0"), fx=Decimal("0"), external_flow=D("100000"),
        gross_exposure=D("4200"), net_exposure=D("4200"))
    bad = build_daily_attribution(bad_point, prev_point)
    truth(any("手续费项为正" in w for w in bad.warnings),
          "C8 手续费为正时给出告警（不假装闭合）")

    # C9 构建一个汇率项非零的场景，必须告警
    fx_point = NavPoint(
        day="2026-10-25", ts=now, nav=D("100300"), net_pnl=D("300"),
        trading=D("200"), slippage=Decimal("0"), fee=Decimal("0"),
        funding=Decimal("0"), fx=D("100"), external_flow=D("100000"),
        gross_exposure=D("4200"), net_exposure=D("4200"))
    fxr = build_daily_attribution(fx_point, prev_point, {"USD": Decimal("1")})
    truth(any("汇率项应为 0" in w for w in fxr.warnings),
          "C9 单币种下汇率项非 0 会被抓出来")

    # C10 净值时序：同日重跑不产生两个点
    before = len(series)
    svc.run(ACC, day2, MemBroker(stmt2), p2)
    eq(len(series), before, "C10 同一天重跑日终不会写出重复点")

    # C11 previous() 严格取更早的一天
    eq(series.previous(day2.isoformat()).day, day1.isoformat(),
       "C11 previous 取上一交易日")
    truth(series.previous(day1.isoformat()) is None,
          "C11 最早一天没有更早的基线")

    # C12 时序可跨进程恢复（落盘后重新加载一致）
    reloaded = NavSeries(os.path.join(tmp, "nav.json"))
    eq(len(reloaded), len(series), "C12 重新加载后点数一致")
    eq(reloaded.latest().nav, series.latest().nav, "C12 重新加载后净值一致")

    # C13 净值为负的极端情形不能崩（破产也要能出报表）
    guard("C13 净值归零不崩", lambda: build_daily_attribution(
        NavPoint(day="2026-10-26", ts=now, nav=Decimal("0"),
                 net_pnl=D("-100000"), trading=D("-100"), slippage=Decimal("0"),
                 fee=Decimal("0"), funding=Decimal("0"), fx=Decimal("0"),
                 external_flow=D("100000"), gross_exposure=Decimal("0"),
                 net_exposure=Decimal("0")), prev_point))


# --------------------------------------------------------------------------- #
#  D. 运维面
# --------------------------------------------------------------------------- #

def test_ops() -> None:
    section("D. 运维面：净值时序与实盘开关可读")

    from quantos_core.ops_server import OpsService
    from quantos_core.strategy_registry import StrategyRegistry
    from quantos_core.metrics import OpsMetrics

    # D1 未接时序时明确说"没有"，而不是返回空数组画平线
    svc = OpsService(metrics=OpsMetrics(), registry=StrategyRegistry())
    nav = svc.nav_history()
    falsy(nav["available"], "D1 未接净值时序时 available=False")
    truth(bool(nav["reason"]), "D1 给出原因而不是空数组")

    # D2 未接实盘开关时也不谎称正常
    live = svc.live()
    falsy(live["available"], "D2 未接实盘开关时 available=False")

    # D3 指标里必须出现 live.* —— "能不能碰真钱"要能看到
    snap = OpsMetrics().sample()
    keys = {m.key for m in snap.metrics}
    truth("live.switch" in keys, "D3 未接开关时指标报 live.switch")
    by_key = {m.key: m for m in snap.metrics}
    eq(by_key["live.switch"].severity.value, "not_configured",
       "D3 未接开关是 not_configured，不是 ok")

    # D4 接了开关后逐账户报可实盘状态
    g = armed_gate()
    snap = OpsMetrics(live_gate=g).sample()
    by_key = {m.key: m for m in snap.metrics}
    truth(f"live.ready.{ACC}" in by_key, "D4 指标按账户报可实盘状态")
    eq(by_key[f"live.ready.{ACC}"].value, 1, "D4 三级齐全时 ready=1")
    eq(by_key[f"live.ready.{ACC}"].severity.value, "ok", "D4 ready=1 时为 ok")

    # D5 少一级时 ready=0 且 detail 说明缺什么
    g = armed_gate(arm_cap=False)
    snap = OpsMetrics(live_gate=g).sample()
    by_key = {m.key: m for m in snap.metrics}
    eq(by_key[f"live.ready.{ACC}"].value, 0, "D5 少一级时 ready=0")
    truth("单笔上限" in by_key[f"live.ready.{ACC}"].detail,
          "D5 detail 写明缺哪一级")

    # D6 对账不干净时它是 critical（不是 warn —— 这是钱对不上）
    g = armed_gate(nav_ok=False)
    snap = OpsMetrics(live_gate=g).sample()
    by_key = {m.key: m for m in snap.metrics}
    eq(by_key[f"live.reconcile.{ACC}"].severity.value, "critical",
       "D6 对账不干净是 critical")


def main() -> None:
    print("QuantOS 2.0 · B4 自检（实盘开关 / 日终对账 / 归因日报）")
    print("=" * 66)
    test_gate()
    test_eod()
    test_attribution()
    test_ops()


if __name__ == "__main__":
    raise SystemExit(run("B4 实盘就绪", main))
