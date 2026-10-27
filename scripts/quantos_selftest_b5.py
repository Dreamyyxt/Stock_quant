#!/usr/bin/env python3
"""
QuantOS 2.0 — B5 自检（T-501 网关/CCXT · T-502 能力矩阵 · T-503 资金费 · T-504 调度）
================================================================================

每条断言对应一个**会让钱出问题**的失败模式，不是"函数能跑"。

    .venv/bin/python scripts/quantos_selftest_b5.py

六段各自回答一个问题：

    A. 错误分类   超时和"被拒绝"，系统分得开吗？（必须分得开）
    B. 提交对账   超时之后，会不会重发？（绝不重发，只查）
    C. CCXT 适配  场所方言、取整方向、资金费符号，翻译对吗？
    D. 能力矩阵   一家救不回来的交易所，会被拦在门外吗？（会）
    E. 资金费     同一笔结算重跑一百次，会记一百遍吗？（不会）
    F. 调度       美股收盘，币还跑不跑？（跑）；周末后开盘会补跑吗？（不会）
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from _asserts import (  # noqa: E402
    eq, falsy, guard, near, ne, raises, run, section, truth,
)

from quantos_core.ccxt_gateway import (  # noqa: E402
    CcxtGateway, ProbeResult, gateway_for_venue, translate_exception,
)
from quantos_core.contracts import (  # noqa: E402
    D, OrderIntent, OrderState, OrderType, Side, utcnow,
)
from quantos_core.funding import (  # noqa: E402
    FundingAccrual, FundingBook, FundingReconciler, boundaries_between,
    day_boundaries, estimate_error_bps, funding_amount, next_settlement, window_of,
)
from quantos_core.gateway import (  # noqa: E402
    ACCEPTED, REJECTED, UNKNOWN, AuthError, CapabilityError, FundingPayment,
    GatewayError, RateLimited, TransportError, VenueGateway, VenueOrder,
    VenueRejected, normalize_state, submit_with_reconcile,
)
from quantos_core.ledger import Ledger  # noqa: E402
from quantos_core.scheduler import (  # noqa: E402
    CryptoSession, EquitySession, Lane, LaneState, Scheduler, boundaries_crossed,
    load_tz, nyse_session,
)
from quantos_core.venues import (  # noqa: E402
    Capabilities, SymbolRules, admit, all_admissions, assert_admitted,
    ceil_to_step, floor_to_step, profile, quantize_price, quantize_qty,
)

UTC = timezone.utc
VENUE = "binanceusdm"
SYMBOL = "BTC/USDT:USDT"


# --------------------------------------------------------------------------- #
#  假场所
# --------------------------------------------------------------------------- #
#  类名刻意跟 ccxt 的异常同名 —— 适配器就是靠类名翻译的，
#  用别的名字测不出真实行为。

class NetworkError(Exception):
    pass


class RequestTimeout(NetworkError):
    pass


class InsufficientFunds(Exception):
    pass


class InvalidOrder(Exception):
    pass


class OrderNotFound(Exception):
    pass


class AuthenticationError(Exception):
    pass


class RateLimitExceeded(Exception):
    pass


class NotSupported(Exception):
    pass


class WeirdSdkBug(Exception):
    """谁也没见过、ccxt 将来可能新增的异常。"""


class StubTransport:
    """
    长得像 ccxt 的假场所。

    两个能力是这套测试的核心：
      · `create_faults` 依次弹出的异常，用来制造"超时"
      · `create_persists` 决定抛异常前订单是否已真的落地
        —— 这是"发出了但响应丢了"和"根本没发出去"的唯一区别
    """

    def __init__(self, cid_param: str = "newClientOrderId") -> None:
        self.cid_param = cid_param
        self.orders = {}
        self.create_calls = []
        self.fetch_calls = []
        self.cancel_calls = []
        self.create_faults = []
        self.fetch_faults = []
        self.create_persists = True
        self.return_empty_id = False
        self.market_meta = {}
        self.funding_rows = []
        self.positions_rows = []
        self.balance_rows = {}
        self.fetch_funding_calls = 0
        self._seq = 0

    # -- 下单 ------------------------------------------------------------ #

    def _store(self, symbol, side, amount, price, params):
        self._seq += 1
        cid = ""
        for key in ("newClientOrderId", "clientOrderId", "clOrdId", "orderLinkId"):
            if params and params.get(key):
                cid = str(params[key])
                break
        raw = {
            "id": f"V{self._seq:04d}", "clientOrderId": cid, "symbol": symbol,
            "side": side, "amount": amount, "filled": 0.0,
            "price": price, "average": None,
            "status": "open", "timestamp": 1793000000000,
        }
        if self.return_empty_id:
            raw["id"] = None
        self.orders[cid] = raw
        return dict(raw)

    def create_order(self, symbol, type, side, amount, price=None, params=None):
        self.create_calls.append({
            "symbol": symbol, "type": type, "side": side, "amount": amount,
            "price": price, "params": dict(params or {}),
        })
        fault = self.create_faults.pop(0) if self.create_faults else None
        if fault is not None:
            if self.create_persists:
                self._store(symbol, side, amount, price, params)
            raise fault
        return self._store(symbol, side, amount, price, params)

    def fetch_order(self, id=None, symbol=None, params=None):
        self.fetch_calls.append({"id": id, "symbol": symbol,
                                 "params": dict(params or {})})
        fault = self.fetch_faults.pop(0) if self.fetch_faults else None
        if fault is not None:
            raise fault
        cid = id or (params or {}).get(self.cid_param) or ""
        raw = self.orders.get(cid)
        if raw is None:
            raise OrderNotFound(f"order {cid} does not exist")
        return dict(raw)

    def cancel_order(self, id=None, symbol=None, params=None):
        self.cancel_calls.append({"id": id, "symbol": symbol})
        cid = id or (params or {}).get(self.cid_param) or ""
        raw = self.orders.get(cid)
        if raw is None:
            raise OrderNotFound(f"order {cid} does not exist")
        raw = dict(raw)
        raw["status"] = "canceled"
        self.orders[cid] = raw
        return dict(raw)

    def fetch_open_orders(self, symbol=None, params=None):
        return [dict(o) for o in self.orders.values() if o["status"] == "open"]

    # -- 账户 ------------------------------------------------------------ #

    def fetch_positions(self, symbols=None, params=None):
        return [dict(r) for r in self.positions_rows]

    def fetch_balance(self, params=None):
        return dict(self.balance_rows)

    def fetch_funding_history(self, symbol=None, since=None, limit=None):
        self.fetch_funding_calls += 1
        return [dict(r) for r in self.funding_rows]

    def market(self, symbol):
        return dict(self.market_meta.get(symbol, {}))


def make_intent(*, venue: str = VENUE, symbol: str = SYMBOL, side: Side = Side.BUY,
                qty: str = "0.5", notional: str = "15000", cid: str = "cid-1",
                order_type: OrderType = OrderType.MARKET,
                limit_price=None, reduce_only: bool = False) -> OrderIntent:
    now = utcnow()
    return OrderIntent(
        order_intent_id=f"oi-{cid}", client_order_id=cid, account_id="ACC_CRYPTO",
        venue=venue, symbol=symbol, side=side, quantity=D(qty),
        order_type=order_type, notional=D(notional),
        attribution={"crypto_mom": D(qty)}, reason_chain=(),
        as_of=now, ts=now, limit_price=None if limit_price is None else D(limit_price),
        reduce_only=reduce_only,
    )


def make_gateway(*, venue: str = VENUE, max_notional=None,
                 transport: StubTransport = None) -> tuple:
    caps = profile(venue)
    t = transport or StubTransport(cid_param=caps.client_order_id_param)
    return CcxtGateway(t, caps, max_order_notional=max_notional), t


# --------------------------------------------------------------------------- #
#  A. 错误分类
# --------------------------------------------------------------------------- #

def part_a() -> None:
    section("A. 错误分类：确定失败 vs 不知道")

    cases = [
        (NetworkError("conn reset"), TransportError, True),
        (RequestTimeout("read timeout"), TransportError, True),
        (RateLimitExceeded("429"), RateLimited, True),
        (InsufficientFunds("no money"), VenueRejected, False),
        (InvalidOrder("bad qty"), VenueRejected, False),
        (AuthenticationError("key expired"), AuthError, False),
        (NotSupported("not on this venue"), CapabilityError, False),
    ]
    for exc, expected_type, ambiguous in cases:
        translated = translate_exception(exc)
        truth(isinstance(translated, expected_type),
              f"{type(exc).__name__} → {expected_type.__name__}")
        eq(translated.ambiguous, ambiguous,
           f"  └ ambiguous={ambiguous}（{'必须去查' if ambiguous else '可当没发生'}）")

    # 最容易被忽略的一条：不认识的异常怎么办
    unknown = translate_exception(WeirdSdkBug("who knows"))
    truth(isinstance(unknown, TransportError),
          "未知异常 → TransportError（当不确定处理，才不会被重发）")
    truth(unknown.ambiguous, "  └ 未知异常 ambiguous=True")

    # 状态词归一化
    eq(normalize_state("open"), OrderState.ACCEPTED, "状态 open → ACCEPTED")
    eq(normalize_state("NEW"), OrderState.ACCEPTED, "状态 NEW（大写）→ ACCEPTED")
    eq(normalize_state("closed", D("1"), D("1")), OrderState.FILLED,
       "状态 closed 且全成交 → FILLED")
    eq(normalize_state("closed", D("0.5"), D("1")), OrderState.PARTIALLY_FILLED,
       "状态 closed 但只成交一半 → PARTIALLY_FILLED（不能当满仓）")
    eq(normalize_state("closed", D("0"), D("1")), OrderState.CANCELLED,
       "状态 closed 且零成交 → CANCELLED（敞口为 0，两种口径等价）")
    eq(normalize_state("expired_in_match"), OrderState.EXPIRED, "状态 expired_in_match → EXPIRED")
    eq(normalize_state("some_new_status"), OrderState.UNKNOWN,
       "没见过的状态词 → UNKNOWN（不猜）")
    eq(normalize_state(""), OrderState.UNKNOWN, "空状态 → UNKNOWN")

    guard("VenueOrder 拒绝非法数量", lambda: raises(
        GatewayError, lambda: VenueOrder("binance", "V1", "cid", SYMBOL, Side.BUY,
                                         D("0"), D("0"), OrderState.ACCEPTED),
        "数量为 0 的 VenueOrder 必须报错"))
    guard("FundingPayment 必须有 id", lambda: raises(
        GatewayError, lambda: FundingPayment("binance", SYMBOL, D("1"), "USDT",
                                             D("0.0001"), utcnow(), ""),
        "资金费缺少 payment_id 必须报错（否则无法去重）"))


# --------------------------------------------------------------------------- #
#  B. 提交 + 对账
# --------------------------------------------------------------------------- #

def part_b() -> None:
    section("B. 提交对账：超时之后只查不重发")

    # --- 顺利路径 --- #
    gw, t = make_gateway()
    out = submit_with_reconcile(gw, make_intent())
    eq(out.status, ACCEPTED, "正常提交 → accepted")
    eq(len(t.create_calls), 1, "  └ 只调用了一次下单")
    eq(out.queries, 0, "  └ 没有触发回查")

    # --- 明确的拒绝 --- #
    gw, t = make_gateway()
    t.create_faults = [InsufficientFunds("余额不足")]
    t.create_persists = False
    out = submit_with_reconcile(gw, make_intent(cid="cid-rej"))
    eq(out.status, REJECTED, "场所拒绝 → rejected")
    eq(out.queries, 0, "  └ 拒绝是确定结论，不去回查")
    truth("余额不足" in out.reason, "  └ 拒绝原因透传")

    # --- 鉴权失败 --- #
    gw, t = make_gateway()
    t.create_faults = [AuthenticationError("key 过期")]
    t.create_persists = False
    out = submit_with_reconcile(gw, make_intent(cid="cid-auth"))
    eq(out.status, REJECTED, "鉴权失败 → rejected（fail-closed，不当偶发重试）")

    # --- 超时 + 订单其实落地了 → 回查命中 --- #
    gw, t = make_gateway()
    t.create_faults = [RequestTimeout("连上了但没等到响应")]
    t.create_persists = True
    out = submit_with_reconcile(gw, make_intent(cid="cid-lost"))
    eq(out.status, ACCEPTED, "超时但回查命中 → accepted（这是成功的单）")
    eq(len(t.create_calls), 1, "  └ 关键：下单只调了 1 次")
    truth(out.queries >= 1, "  └ 至少回查了 1 次")

    # --- 超时 + 真的没下出去 → UNKNOWN，绝不重发 --- #
    gw, t = make_gateway()
    t.create_faults = [RequestTimeout("压根没连上")]
    t.create_persists = False
    out = submit_with_reconcile(gw, make_intent(cid="cid-never"), max_queries=3)
    eq(out.status, UNKNOWN, "超时且回查始终未命中 → UNKNOWN")
    eq(len(t.create_calls), 1, "  └ **核心断言**：依然只下了一次")
    eq(out.queries, 3, "  └ 按 max_queries 查了 3 次")
    truth(out.needs_manual, "  └ 标记为需要人工/对账消解")
    truth("不得重发" in out.reason, "  └ 结论里写清了不许重发")

    # --- 连续多次超时，仍然只下一次 --- #
    gw, t = make_gateway()
    t.create_faults = [RequestTimeout("1"), RequestTimeout("2"), RequestTimeout("3")]
    t.create_persists = False
    out = submit_with_reconcile(gw, make_intent(cid="cid-3x"), max_queries=2)
    eq(len(t.create_calls), 1, "连抛 3 个超时异常，下单调用仍是 1 次")
    eq(out.status, UNKNOWN, "  └ 结果仍是 UNKNOWN")

    # --- 回查本身也超时 → 继续查，不放弃也不重发 --- #
    gw, t = make_gateway()
    t.create_faults = [RequestTimeout("提交超时")]
    t.create_persists = True
    t.fetch_faults = [RequestTimeout("查询也超时")]
    out = submit_with_reconcile(gw, make_intent(cid="cid-q1"), max_queries=3)
    eq(out.status, ACCEPTED, "回查第一次超时、第二次命中 → accepted")
    eq(len(t.create_calls), 1, "  └ 依然没有重发")
    eq(out.queries, 2, "  └ 查询了 2 次")

    # --- 回查时鉴权失败 → 不能乐观当成功 --- #
    gw, t = make_gateway()
    t.create_faults = [RequestTimeout("提交超时")]
    t.create_persists = True
    t.fetch_faults = [AuthenticationError("key 到期")]
    out = submit_with_reconcile(gw, make_intent(cid="cid-q-auth"), max_queries=3)
    eq(out.status, UNKNOWN, "回查鉴权失败 → UNKNOWN（不能因为查不了就当成交了）")

    # --- 能力不足在本地就拦住，一个请求都不发 --- #
    bad_caps = profile(VENUE).__class__(**{**profile(VENUE).__dict__,
                                           "supports_client_order_id": False})
    gw = CcxtGateway(StubTransport(), bad_caps)
    raises(CapabilityError, lambda: submit_with_reconcile(gw, make_intent()),
           "无客户端单号 → 直接拒绝提交")
    eq(gw.place_calls, 0, "  └ 一次下单都没发出去")

    bad_caps2 = profile(VENUE).__class__(**{**profile(VENUE).__dict__,
                                            "supports_query_by_client_id": False})
    gw2 = CcxtGateway(StubTransport(), bad_caps2)
    raises(CapabilityError, lambda: submit_with_reconcile(gw2, make_intent()),
           "无法按单号回查 → 直接拒绝提交（超时后永远查不清）")
    eq(gw2.place_calls, 0, "  └ 一次下单都没发出去")

    # --- SubmitOutcome 序列化 --- #
    gw, t = make_gateway()
    out = submit_with_reconcile(gw, make_intent())
    payload = out.to_dict()
    eq(payload["status"], ACCEPTED, "SubmitOutcome.to_dict 状态正确")
    eq(payload["place_calls"], 1, "  └ place_calls 上报")


# --------------------------------------------------------------------------- #
#  C. CCXT 适配
# --------------------------------------------------------------------------- #

def part_c() -> None:
    section("C. CCXT 适配：方言、取整、符号")

    # --- 幂等键参数名按场所不同 --- #
    for venue, param in (("binanceusdm", "newClientOrderId"),
                         ("okx", "clOrdId"), ("bybit", "orderLinkId")):
        gw, t = make_gateway(venue=venue)
        gw.place(make_intent(venue=venue, cid="cid-key"))
        eq(t.create_calls[0]["params"].get(param), "cid-key",
           f"{venue} 用 {param} 传幂等键")

    # --- 数量向下取整 --- #
    gw, t = make_gateway()
    gw.place(make_intent(qty="0.0012345", notional="30"))
    near(t.create_calls[0]["amount"], 0.001, "1e-12",
         "数量 0.0012345 按步长 0.001 向下取整为 0.001（宁少不多）")

    # --- 限价取整方向 --- #
    gw, t = make_gateway()
    gw.place(make_intent(side=Side.BUY, qty="0.01", limit_price="30123.456",
                         order_type=OrderType.LIMIT, notional="301"))
    near(t.create_calls[0]["price"], 30123.4, "1e-9",
         "买入限价 30123.456 向下取整（不越过愿意付的上限）")

    gw, t = make_gateway()
    gw.place(make_intent(side=Side.SELL, qty="0.01", limit_price="30123.412",
                         order_type=OrderType.LIMIT, notional="301"))
    near(t.create_calls[0]["price"], 30123.5, "1e-9",
         "卖出限价 30123.412 向上取整（不低于愿意收的下限）")

    # --- 本地边界校验：不通过就一个字节都不发 --- #
    gw, t = make_gateway()
    raises(CapabilityError,
           lambda: gw.place(make_intent(qty="0.001", limit_price="100",
                                        order_type=OrderType.LIMIT, notional="0.1")),
           "名义 0.1 < 最小名义 5 → 拒绝")
    eq(len(t.create_calls), 0, "  └ 没有发出任何请求")

    gw, t = make_gateway()
    raises(CapabilityError,
           lambda: gw.place(make_intent(qty="0.00001", notional="30")),
           "数量低于最小下单量 → 拒绝")
    eq(len(t.create_calls), 0, "  └ 没有发出任何请求")

    gw, t = make_gateway(max_notional=D("10"))
    raises(CapabilityError,
           lambda: gw.place(make_intent(qty="0.01", limit_price="30000",
                                        order_type=OrderType.LIMIT, notional="300")),
           "名义 300 超单笔上限 10 → 拒绝")

    # --- reduce-only --- #
    gw, t = make_gateway(venue="binance")       # 现货，不支持 reduce-only
    raises(CapabilityError,
           lambda: gw.place(make_intent(venue="binance", reduce_only=True)),
           "现货场所要求 reduce-only → 拒绝（不能退化成普通单）")

    gw, t = make_gateway()
    gw.place(make_intent(reduce_only=True, side=Side.SELL))
    truth(t.create_calls[0]["params"].get("reduceOnly") is True,
          "合约场所 reduce-only → params.reduceOnly=True")

    # --- 时间有效性 --- #
    gw, t = make_gateway()
    gw.place(make_intent(qty="0.01", limit_price="30000",
                         order_type=OrderType.LIMIT, notional="300"))
    eq(t.create_calls[0]["params"].get("timeInForce"), "GTC",
       "限价单自动带 timeInForce=GTC")
    gw, t = make_gateway()
    gw.place(make_intent())
    truth(t.create_calls[0]["price"] is None, "市价单不传价格")

    # --- 响应异常 --- #
    gw, t = make_gateway()
    t.return_empty_id = True
    raises(TransportError, lambda: gw.place(make_intent(cid="cid-noid")),
           "响应缺 order id → 抛不确定错误（有幂等键还能回查）")

    gw, t = make_gateway()
    raise_out = gw.fetch_order(SYMBOL, "cid-missing")
    truth(raise_out is None, "回查未命中（OrderNotFound）→ 返回 None，不是异常")

    t.fetch_faults = [NetworkError("查询超时")]
    raises(TransportError, lambda: gw.fetch_order(SYMBOL, "cid-x"),
           "回查网络错误 → 抛 TransportError（又一次不知道）")

    # --- 场地不匹配 --- #
    gw, t = make_gateway()
    raises(CapabilityError, lambda: gw.place(make_intent(venue="okx")),
           "意图场所与网关不一致 → 拒绝")

    # --- 账户归一化 --- #
    gw, t = make_gateway()
    t.balance_rows = {"total": {"USDT": 1000, "BTC": 0.5, "DOGE": 0},
                      "free": {"USDT": 800, "BTC": 0.5, "DOGE": 0},
                      "used": {"USDT": 200, "BTC": 0, "DOGE": 0}}
    balances = gw.fetch_balance()
    eq(len(balances), 2, "余额列表跳过全零币种")
    usdt = [b for b in balances if b.currency == "USDT"][0]
    eq(usdt.total, D("1000"), "  └ 总额 = 可用 + 占用")

    t.positions_rows = [
        {"symbol": SYMBOL, "contracts": 2, "side": "short",
         "entryPrice": 30000, "markPrice": 30100},
        {"symbol": "ETH/USDT:USDT", "contracts": 0, "side": "long"},
    ]
    positions = gw.fetch_positions()
    eq(len(positions), 1, "持仓列表跳过空仓")
    eq(positions[0].qty, D("-2"), "  └ 空头持仓数量为负")
    eq(positions[0].notional, D("-60200"), "  └ 空头名义为负敞口")

    # --- 资金费符号陷阱 --- #
    gw, t = make_gateway()
    t.funding_rows = [
        {"id": "F1", "symbol": SYMBOL, "amount": -6.0, "currency": "USDT",
         "rate": 0.0001, "timestamp": 1793001600000},
    ]
    payments = gw.fetch_funding_payments()
    eq(len(payments), 1, "资金费流水解析出 1 笔")
    eq(payments[0].amount, D("6"),
       "**CCXT 的 -6（现金流付出）→ 账本的 +6（费用支出）**")
    eq(payments[0].rate, D("0.0001"), "  └ 费率透传")

    # 空头收钱，现金流为正 → 账本为负（收入）
    t.funding_rows = [
        {"id": "F2", "symbol": SYMBOL, "amount": 8.0, "currency": "USDT",
         "rate": 0.0001, "timestamp": 1793028800000},
    ]
    payments = gw.fetch_funding_payments()
    eq(payments[0].amount, D("-8"), "空头收到 8 → 账本 -8（收入）")

    gw, t = make_gateway(venue="binance")       # 现货无资金费
    raises(CapabilityError, lambda: gw.fetch_funding_payments(),
           "现货场所查资金费 → 明确报不支持（不返回空列表冒充「没有」）")

    # --- probe 实测 --- #
    gw, t = make_gateway()
    t.market_meta = {SYMBOL: {
        "limits": {"amount": {"min": 0.002}, "cost": {"min": 10}},
        "precision": {"amount": 0.0001, "price": 0.5},
    }}
    result = gw.probe(SYMBOL)
    truth(isinstance(result, ProbeResult) and result.fully_verified is False,
          "probe 只测到步长/精度/最小额，仍标为未完全验证")
    eq(result.capabilities.rules_for(SYMBOL).qty_step, D("0.0001"),
       "  └ 用真实元数据覆盖步长 0.001 → 0.0001")
    eq(result.capabilities.rules_for(SYMBOL).min_notional, D("10"),
       "  └ 用真实元数据覆盖最小名义 5 → 10")
    truth(result.capabilities.verified, "  └ 三项都测到 → verified_at 有值")
    truth(any("order_visibility_lag" in u for u in result.unverified),
          "  └ 可见延迟未实测，如实列进 unverified")

    gw, t = make_gateway()
    t.market_meta = {SYMBOL: {"limits": {}, "precision": {"amount": 0.0001, "price": 0.5}}}
    partial = gw.probe(SYMBOL)
    falsy(partial.capabilities.verified,
          "缺最小名义时不算 verified（半步验证比没验证更危险）")
    truth(len(partial.unverified) > 0, "  └ unverified 非空")


# --------------------------------------------------------------------------- #
#  D. 能力矩阵与准入
# --------------------------------------------------------------------------- #

def part_d() -> None:
    section("D. 能力矩阵：救不回来的场所不许进门")

    truth(profile("binance").default_rules.qty_step > 0, "声明档案可加载")
    raises(CapabilityError, lambda: profile("fakeex"),
           "未知场所直接报错（不返回「什么都支持」的默认值）")

    base = profile(VENUE)

    # --- 完全合格 --- #
    good = admit(base, max_order_notional=D("5000"))
    truth(good.allowed, "四项关键能力齐全 → 允许自动交易")
    eq(len(good.blockers), 0, "  └ 无阻断项")
    truth(any("未实测" in w for w in good.warnings),
          "  └ 但仍警告「能力声明未实测」（要 probe）")

    # --- 缺幂等键 --- #
    no_cid = base.__class__(**{**base.__dict__, "supports_client_order_id": False})
    verdict = admit(no_cid)
    falsy(verdict.allowed, "不支持客户端单号 → 禁止自动交易")
    truth(any("重复下单" in b for b in verdict.blockers),
          "  └ 阻断理由说清了后果")

    # --- 能发但查不回：最隐蔽的一种 --- #
    no_query = base.__class__(**{**base.__dict__,
                                 "supports_query_by_client_id": False})
    verdict = admit(no_query)
    falsy(verdict.allowed, "能发单号但无法回查 → 禁止自动交易")
    truth(any("永远无法确定" in b for b in verdict.blockers),
          "  └ **核心**：阻断理由点明「超时后永远查不清」")

    # --- 最小名义高于单笔上限 --- #
    big_min = base.with_rules(SYMBOL, SymbolRules(
        qty_step=D("0.001"), price_tick=D("0.1"), min_qty=D("0.001"),
        min_notional=D("20000")))
    verdict = admit(big_min, max_order_notional=D("5000"))
    falsy(verdict.allowed, "最小名义 20000 > 单笔上限 5000 → 禁止（假接通）")
    truth(any("最小名义" in b for b in verdict.blockers), "  └ 指认到具体冲突")

    # --- 可见延迟超出对账预算 --- #
    slow = base.__class__(**{**base.__dict__, "order_visibility_lag_ms": 60000})
    verdict = admit(slow, reconcile_budget_ms=15000)
    falsy(verdict.allowed, "下单后 60s 才可见，超出 15s 对账预算 → 禁止")

    # --- 告警类（不阻断） --- #
    no_ws = base.__class__(**{**base.__dict__, "supports_ws": False})
    verdict = admit(no_ws)
    truth(verdict.allowed, "无 WebSocket 只告警，不阻断")
    truth(any("轮询" in w for w in verdict.warnings), "  └ 提示只能轮询")

    no_reduce = base.__class__(**{**base.__dict__, "supports_reduce_only": False})
    verdict = admit(no_reduce)
    truth(verdict.allowed, "不支持 reduce-only 只告警")
    truth(any("reduce-only" in w for w in verdict.warnings), "  └ 提示减仓单会退化")

    weird_funding = base.__class__(**{**base.__dict__, "funding_interval_hours": 3})
    verdict = admit(weird_funding)
    truth(any("非标准" in w for w in verdict.warnings),
          "资金费 3h 非标准周期 → 告警（结算边界要实测）")

    # --- assert_admitted --- #
    raises(CapabilityError, lambda: assert_admitted(no_query),
           "assert_admitted 在不合格时抛错（不能只看返回值）")
    truth(assert_admitted(base).allowed, "assert_admitted 在合格时通过")

    # --- 量化工具 --- #
    eq(quantize_qty(D("0.00019"), D("0.0001")), D("0.0001"),
       "数量 0.00019 向下取整（0.0001，绝不向上到 0.0002）")
    eq(floor_to_step(D("1234.56"), D("1")), D("1234"), "floor_to_step 向下")
    eq(ceil_to_step(D("1234.01"), D("1")), D("1235"), "ceil_to_step 向上")
    eq(quantize_price(D("100.005"), D("0.01"), Side.BUY), D("100.00"),
       "买入价向下取整")
    eq(quantize_price(D("100.001"), D("0.01"), Side.SELL), D("100.01"),
       "卖出价向上取整")
    raises(CapabilityError, lambda: SymbolRules(qty_step=D("0")),
           "步长为 0 的规则必须报错")
    raises(CapabilityError, lambda: quantize_qty(D("1"), D("0")),
           "步长为 0 时量化必须报错")

    # --- 覆盖单标的 --- #
    overridden = base.with_rules("ETH/USDT:USDT", SymbolRules(
        qty_step=D("0.01"), price_tick=D("0.01"), min_qty=D("0.01"),
        min_notional=D("20")))
    eq(overridden.rules_for("ETH/USDT:USDT").min_notional, D("20"),
       "按标的覆盖规则生效")
    eq(overridden.rules_for(SYMBOL).min_notional, base.default_rules.min_notional,
       "  └ 其他标的仍用默认规则")

    # --- 取整说明要进审计 --- #
    check = base.validate_order(SYMBOL, Side.BUY, D("0.0012345"), D("30123.456"))
    truth(len(check.notes) == 2, "取整动作产生 2 条说明（数量 + 价格）")
    truth(any("向下取整" in n for n in check.notes), "  └ 说明里写明了取整方向")

    # --- 全量准入清单 --- #
    admissions = all_admissions(max_order_notional=D("5000"))
    truth(len(admissions) >= 5, "全量准入清单覆盖所有声明场所")
    # `xshg` 是 B12 为 A 股回测新增的场所（整手/涨跌停/最小金额的规则矩阵）。
    # 它声明了 `supports_query_by_client_id=False`，所以**结构准入必须拒**它：
    # 超时那一刻既没有 orderId 也查不回，订单状态永远无法确定。
    # 这不是配置遗漏 —— 它现在真的只能进回测，不能进实盘；等券商接口具备
    # 回查能力、把这条声明改成 True 并跑过 probe 之后再说。
    blocked = sorted(a.venue for a in admissions if not a.allowed)
    eq(blocked, ["xshg"], "只有 xshg 被拦（无法按客户端订单号回查 → 状态不可确定）")
    truth(all(a.allowed for a in admissions if a.venue != "xshg"),
          "  └ 其余声明场所全部通过结构准入（其余风险靠 probe 覆盖）")

    # --- 资金费周期：现货 0，永续 8 --- #
    eq(profile("binance").funding_interval_hours, 0, "现货资金费周期为 0（无资金费）")
    eq(profile("binanceusdm").funding_interval_hours, 8, "永续资金费周期为 8h")


# --------------------------------------------------------------------------- #
#  E. 资金费记账
# --------------------------------------------------------------------------- #

def part_e() -> None:
    section("E. 资金费：符号、幂等、应计不入账")

    # --- 符号 --- #
    eq(funding_amount(D("2"), D("30000"), D("0.0001")), D("6"),
       "多头 × 正费率 = +6（支出）")
    eq(funding_amount(D("-2"), D("30000"), D("0.0001")), D("-6"),
       "空头 × 正费率 = -6（收入）")
    eq(funding_amount(D("2"), D("30000"), D("-0.0001")), D("-6"),
       "多头 × 负费率 = -6（反过来收钱）")

    # --- 结算边界 --- #
    now = datetime(2026, 10, 26, 13, 50, tzinfo=UTC)
    win = window_of(now, 8)
    eq(win.start.hour, 8, "13:50 UTC 落在 08:00 起的窗口")
    eq(win.end.hour, 16, "  └ 16:00 结算")
    eq(next_settlement(now, 8).hour, 16, "下次结算 16:00")
    eq(window_of(datetime(2026, 10, 26, 23, 0, tzinfo=UTC), 8).start.hour, 16,
       "23:00 落在 16:00 起的窗口（跨日也正确）")

    stamps = day_boundaries(date(2026, 10, 26), 8)
    eq(len(stamps), 3, "8h 周期一天 3 次结算")
    eq([s.hour for s in stamps], [0, 8, 16], "  └ 分别是 00:00 / 08:00 / 16:00")

    between = boundaries_between(datetime(2026, 10, 26, 7, 0, tzinfo=UTC),
                                 datetime(2026, 10, 26, 17, 0, tzinfo=UTC), 8)
    eq([b.hour for b in between], [8, 16], "区间 (07:00, 17:00] 内有 08:00 和 16:00")

    # --- 计提 --- #
    w = window_of(datetime(2026, 10, 26, 2, 0, tzinfo=UTC), 8)   # 00:00-08:00
    accrual = FundingAccrual(SYMBOL, w, D("2"), D("30000"), D("0.0001"))
    eq(accrual.full_amount, D("6"), "整窗资金费 6")
    eq(accrual.accrued(datetime(2026, 10, 26, 4, 0, tzinfo=UTC)), D("3"),
       "过了 4 小时 → 计提一半 = 3")
    eq(accrual.accrued(datetime(2026, 10, 26, 8, 0, tzinfo=UTC)), D("6"),
       "窗口结束 → 计提等于全额")
    eq(accrual.accrued(datetime(2026, 10, 26, 1, 0, tzinfo=UTC)), D("0.75"),
       "刚过 1 小时 → 计提 0.75（8 小时窗口的 1/8）")

    # --- 计提绝不进账本 --- #
    ledger = Ledger(base_currency="USD")
    ledger.deposit("ACC", "USDT", D("100000"), ts=now)
    before = ledger.entry_count
    _ = accrual.accrued(now)
    eq(ledger.entry_count, before, "计提只算不记，账本条目数不变")

    # --- 入账与幂等 --- #
    book = FundingBook(ledger, "ACC")
    payment = FundingPayment(VENUE, SYMBOL, D("6"), "USDT", D("0.0001"),
                             datetime(2026, 10, 26, 8, 0, tzinfo=UTC), "pmt-1")
    first = book.settle(payment)
    truth(first.wrote_ledger, "首次结算 → 写入账本")
    falsy(first.duplicate, "  └ 不是重复")
    eq(ledger.entry_count, before + 1, "  └ 账本多了 1 条分录")
    eq(book.total, D("6"), "  └ 资金费合计 6（支出）")

    again = book.settle(payment)
    truth(again.duplicate, "同一笔结算再记一次 → 判定为重复")
    falsy(again.wrote_ledger, "  └ **不写账本**")
    eq(ledger.entry_count, before + 1, "  └ 账本条目数没变")
    eq(book.total, D("6"), "  └ 合计没变")

    # 重跑 100 次日终
    for _ in range(100):
        book.settle(payment)
    eq(ledger.entry_count, before + 1, "重跑 100 次 → 账本仍然只有 1 条")
    eq(book.total, D("6"), "  └ 合计仍然是 6")

    # --- 结算金额被交易所修正 → 只补差额 --- #
    amended = FundingPayment(VENUE, SYMBOL, D("6.5"), "USDT", D("0.0001"),
                             datetime(2026, 10, 26, 8, 0, tzinfo=UTC), "pmt-1")
    result = book.settle(amended)
    truth(result.amended, "结算金额 6 → 6.5 → 判定为修正")
    eq(result.posted_amount, D("0.5"), "  └ 只补差额 0.5")
    eq(book.total, D("6.5"), "  └ 合计变成 6.5")
    eq(len(book.entries_for(result.key)), 2, "  └ 同一结算边界有 2 条分录（原记 + 补差）")

    # --- 资金费进归因 --- #
    report = ledger.performance({}, fx={"USDT": D("1")})
    eq(report.funding, D("-6.5"), "归因的 funding 项 = -6.5（支出记为负）")

    # --- 外币结算的汇率折算 --- #
    ledger2 = Ledger(base_currency="USD")
    ledger2.deposit("ACC", "USDT", D("100000"), ts=now)
    book2 = FundingBook(ledger2, "ACC")
    book2.settle(payment, fx_to_base=D("0.98"))
    report2 = ledger2.performance({}, fx={"USDT": D("0.98")})
    eq(report2.funding, D("-5.88"), "USDT 结算按 0.98 折算：6 × 0.98 = 5.88")

    # --- 对账 --- #
    reconciler = FundingReconciler(book)
    ok = reconciler.reconcile([amended])
    truth(ok.clean, "账本与交易所一致 → 对账干净")
    eq(len(ok.diffs), 0, "  └ 无差异")

    missing = FundingPayment(VENUE, SYMBOL, D("-2"), "USDT", D("-0.0000333"),
                             datetime(2026, 10, 26, 16, 0, tzinfo=UTC), "pmt-2")
    verdict = reconciler.reconcile([amended, missing])
    falsy(verdict.clean, "交易所多一笔、账本没记 → 不干净")
    eq(len(verdict.missed), 1, "  └ 识别为 missed（钱已经扣了，账本不知道）")
    eq(verdict.net_delta, D("-2"), "  └ 净差 -2（账本偏高，NAV 虚高）")

    stray = FundingPayment(VENUE, "ETH/USDT:USDT", D("1"), "USDT", D("0.0001"),
                           datetime(2026, 10, 27, 0, 0, tzinfo=UTC), "pmt-3")
    book.settle(stray)
    verdict = reconciler.reconcile([amended, missing])
    truth(any(d.kind == "extra" for d in verdict.diffs),
          "账本记了、交易所没有 → 识别为 extra")

    no_statement = reconciler.reconcile(None)
    falsy(no_statement.clean, "拿不到资金费流水 → 判不干净（与日终同口径）")
    truth("无法判定" in no_statement.note, "  └ 说明为什么判不了")

    # --- 计提 vs 实际：抓费率来源接错 --- #
    bps = estimate_error_bps(accrual, amended)
    eq(bps, D("833.33"), "计提 6 vs 实际结算 6.5 → 偏差 833.33 bp（费率来源可能接错）")
    zero = estimate_error_bps(accrual, FundingPayment(
        VENUE, SYMBOL, D("6"), "USDT", D("0.0001"),
        datetime(2026, 10, 26, 8, 0, tzinfo=UTC), "pmt-x"))
    eq(zero, D("0"), "计提与实际一致 → 偏差 0 bp")


# --------------------------------------------------------------------------- #
#  F. 24/7 调度
# --------------------------------------------------------------------------- #

def part_f() -> None:
    section("F. 调度：股票休市不拖累币，开盘不补跑")

    eq(load_tz("America/New_York").key, "America/New_York", "时区可加载")
    raises(RuntimeError, lambda: load_tz("Mars/Olympus"),
           "未知时区报错（绝不静默退化成 UTC）")

    eq(nyse_session().is_open(datetime(2026, 10, 24, 15, 0, tzinfo=UTC)), False,
       "周六 11:00 ET 休市")
    eq(nyse_session().is_open(datetime(2026, 10, 26, 13, 35, tzinfo=UTC)), True,
       "周一 09:35 ET 开市")
    eq(nyse_session().is_open(datetime(2026, 10, 26, 13, 0, tzinfo=UTC)), False,
       "周一 09:00 ET 未开盘")

    # 夏令时：10-26 是 EDT(UTC-4)，开盘 13:30 UTC；11 月转 EST 后是 14:30 UTC
    eq(nyse_session().open_dt(date(2026, 11, 2)).astimezone(UTC).hour, 14,
       "11 月 2 日转冬令时后，开盘时刻变成 14:30 UTC（时区库自动处理）")

    holiday = nyse_session(holidays=[date(2026, 11, 26)])
    eq(holiday.is_open(datetime(2026, 11, 26, 16, 0, tzinfo=UTC)), False,
       "感恩节休市")
    eq(holiday.next_open(datetime(2026, 11, 25, 21, 0, tzinfo=UTC)).date(),
       date(2026, 11, 27), "  └ 下次开盘跳到周五 11-27")

    early = nyse_session(early_closes={date(2026, 11, 27): time(13, 0)})
    eq(early.is_open(datetime(2026, 11, 27, 18, 30, tzinfo=UTC)), False,
       "提前收盘日 13:00 后休市（18:30 UTC = 13:30 EST）")
    eq(early.close_on(date(2026, 11, 27)), time(13, 0), "  └ close_on 返回提前的时刻")

    friday_close = datetime(2026, 10, 30, 21, 0, tzinfo=UTC)   # 周五 17:00 ET
    nxt = nyse_session().next_open(friday_close)
    eq(nxt.date(), date(2026, 11, 2), "周五收盘后，下次开盘跳过周末到周一")

    # --- 维护窗口 ---
    crypto = CryptoSession()
    eq(crypto.is_open(datetime(2026, 10, 26, 3, 0, tzinfo=UTC)), True,
       "加密 03:00 UTC 正常交易")
    crypto.add_maintenance(datetime(2026, 10, 26, 3, 0, tzinfo=UTC),
                           datetime(2026, 10, 26, 3, 40, tzinfo=UTC))
    eq(crypto.is_open(datetime(2026, 10, 26, 3, 0, tzinfo=UTC)), False,
       "维护窗口内不交易")

    # --- lane 行为 ---
    calls = []

    def handler(tag):
        def run_at(now):
            calls.append((tag, now))
        return run_at

    eq_lane = Lane("equity", nyse_session(), handler("equity"), 60)
    crypto_lane = Lane("crypto", CryptoSession(), handler("crypto"), 15)
    sched = Scheduler([eq_lane, crypto_lane])

    r = sched.tick(datetime(2026, 10, 26, 13, 0, tzinfo=UTC))     # 09:00 ET 未开盘
    eq(r["equity"].state, LaneState.SKIPPED_CLOSED, "开盘前 equity 跳过")
    eq(r["crypto"].state, LaneState.RAN, "  └ 同时 crypto 照常运行")

    r = sched.tick(datetime(2026, 10, 26, 13, 35, tzinfo=UTC))     # 09:35 ET
    eq(r["equity"].state, LaneState.RAN, "开盘后 equity 运行")
    eq(r["crypto"].state, LaneState.RAN, "  └ crypto 也运行")

    r = sched.tick(datetime(2026, 10, 26, 21, 0, tzinfo=UTC))      # 17:00 ET 收盘
    eq(r["equity"].state, LaneState.SKIPPED_CLOSED, "收盘后 equity 跳过")
    eq(r["crypto"].state, LaneState.RAN, "  └ crypto 不受美股收盘影响")

    eq(sum(1 for tag, _ in calls if tag == "equity"), 1, "整个过程 equity 只跑了 1 次")
    eq(sum(1 for tag, _ in calls if tag == "crypto"), 3, "  └ crypto 跑了 3 次")

    # --- 节拍 ---
    eq_lane2 = Lane("equity2", nyse_session(), handler("e2"), 60)
    sched2 = Scheduler([eq_lane2])
    sched2.tick(datetime(2026, 10, 26, 13, 35, tzinfo=UTC))
    r = sched2.tick(datetime(2026, 10, 26, 13, 35, 30, tzinfo=UTC))
    eq(r["equity2"].state, LaneState.SKIPPED_NOT_DUE, "30 秒后未到 60s 节拍 → 跳过")
    r = sched2.tick(datetime(2026, 10, 26, 13, 36, 30, tzinfo=UTC))
    eq(r["equity2"].state, LaneState.RAN, "61 秒后到点 → 运行")
    truth(r["equity2"].missed_intervals >= 0, "  └ 上报漏掉的节拍数")

    # --- 不补跑：休市三天后开盘只跑一次 ---
    eq_lane3 = Lane("equity3", nyse_session(), handler("e3"), 60)
    sched3 = Scheduler([eq_lane3])
    sched3.tick(datetime(2026, 10, 30, 21, 0, tzinfo=UTC))     # 周五 17:00 ET 收盘
    r = sched3.tick(datetime(2026, 11, 2, 14, 35, tzinfo=UTC))  # 周一 09:35 EST
    eq(r["equity3"].state, LaneState.RAN, "周末后开盘：跑了一次")
    eq(eq_lane3.stats.runs, 1, "  └ **核心**：只跑了 1 次，没有补出三千次")
    r = sched3.tick(datetime(2026, 11, 2, 14, 35, 20, tzinfo=UTC))
    eq(r["equity3"].state, LaneState.SKIPPED_NOT_DUE, "  └ 20 秒后不重复跑")

    # --- 异常隔离 --- #
    def boom(now):
        raise RuntimeError("策略在这条 lane 里炸了")

    bad = Lane("bad", CryptoSession(), boom, 15)
    good = Lane("good", CryptoSession(), handler("good"), 15)
    sched4 = Scheduler([bad, good])
    r = sched4.tick(datetime(2026, 10, 26, 3, 0, tzinfo=UTC))
    eq(r["bad"].state, LaneState.FAILED, "一条 lane 抛异常 → 该 lane 标 FAILED")
    eq(r["good"].state, LaneState.RAN, "  └ **另一条 lane 照常运行**")
    truth("策略在这条 lane 里炸了" in bad.stats.last_error, "  └ 错误原文被记录")

    # BaseException 也要兜住（SystemExit 不该带走别的 lane）
    def suicide(now):
        raise SystemExit(1)

    sched5 = Scheduler([Lane("suicide", CryptoSession(), suicide, 15),
                        Lane("alive", CryptoSession(), handler("alive"), 15)])
    r = sched5.tick(datetime(2026, 10, 26, 3, 0, tzinfo=UTC))
    eq(r["suicide"].state, LaneState.FAILED, "SystemExit 也被兜住")
    eq(r["alive"].state, LaneState.RAN, "  └ 兄弟 lane 不受影响")

    # --- 健康度 --- #
    flaky = Lane("flaky", CryptoSession(), boom, 15)
    s = Scheduler([flaky])
    for i in range(3):
        s.tick(datetime(2026, 10, 26, 3, i, tzinfo=UTC))
    eq(flaky.stats.consecutive_failures, 3, "连续失败 3 次")
    falsy(flaky.stats.healthy, "  └ 判定为不健康")

    ok_once = Lane("recoverable", CryptoSession(),
                   lambda now: None, 15)
    s6 = Scheduler([ok_once])
    s6.tick(datetime(2026, 10, 26, 3, 0, tzinfo=UTC))
    truth(ok_once.stats.healthy, "成功运行 → 健康")
    eq(ok_once.stats.consecutive_failures, 0, "  └ 连续失败计数为 0")

    # --- 参数校验 --- #
    raises(ValueError, lambda: Lane("x", CryptoSession(), lambda n: None, 0),
           "节拍为 0 的 lane 必须报错")
    raises(ValueError, lambda: Scheduler([
        Lane("dup", CryptoSession(), lambda n: None, 15),
        Lane("dup", CryptoSession(), lambda n: None, 15)]),
        "lane 名字重复必须报错")

    # --- 快照 --- #
    snap = Scheduler([Lane("eq", nyse_session(), lambda n: None, 60),
                      Lane("cx", CryptoSession(), lambda n: None, 15)]
                     ).snapshot(datetime(2026, 10, 26, 13, 0, tzinfo=UTC))
    eq(snap["lane_count"], 2, "快照列出 2 条 lane")
    eq(snap["open_lanes"], ["cx"], "  └ 只有 crypto 处于开放状态")
    eq(snap["unhealthy_lanes"], [], "  └ 无异常 lane")

    # --- 跨睡过结算边界：交易节拍不补，账必须补 ---
    crossed = boundaries_crossed(datetime(2026, 10, 26, 7, 0, tzinfo=UTC),
                                 datetime(2026, 10, 26, 17, 0, tzinfo=UTC), 8)
    eq(len(crossed), 2, "睡了 10 小时跨过 2 个结算边界")
    eq([c.hour for c in crossed], [8, 16], "  └ 分别是 08:00 和 16:00")
    eq(len(boundaries_crossed(datetime(2026, 10, 26, 9, 0, tzinfo=UTC),
                              datetime(2026, 10, 26, 9, 0, tzinfo=UTC), 8)), 0,
       "同一时刻不产生边界（区间是左开右闭）")


# --------------------------------------------------------------------------- #
#  入口
# --------------------------------------------------------------------------- #

def main() -> int:
    def body() -> None:
        for part in (part_a, part_b, part_c, part_d, part_e, part_f):
            guard(f"{part.__name__} 未预期中断", part)

    return run("B5 自检（网关/CCXT/能力矩阵/资金费/调度）", body)


if __name__ == "__main__":
    raise SystemExit(main())
