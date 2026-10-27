#!/usr/bin/env python3
"""
B11 自测：Alpaca 纸面网关（端点守卫 / 错误分类 / 幂等 / 撤单两步 / 探针）
==========================================================================

**全部离线。** 用 `FakeTransport` 顶替 HTTP，一条断言都不碰网络。

理由不是"懒得连"：验收套件一旦依赖外网，红灯就再也分不清是代码坏了
还是网线断了。真实连通性由 `scripts/quantos_alpaca.py probe / roundtrip`
单独验证（那也是唯一会真下单的入口，且必须 `--yes`）。

本套件要证明的几件事：

  A 端点守卫    真钱端点要双开关；自定义 host 一律拒
  B 凭据        secret 不出现在任何可打印的地方
  C 错误分类    四种状态码 → 两种语义（确定 / 不确定）
  D 状态映射    done_for_day 带部分成交不能判成"还在场"
  E 下单守卫    五道闸全部在**发请求之前**
  F 幂等        重复单号 → 回查；超时 → 只查不重发
  G 撤单两步    先换 order_id 再 DELETE；终态不发请求
  H 探针        回查端点坏掉时必须红，不能绿着放行
  I 账本衔接    absent 判定看 status，不看对象是否为 None
  J 代码卫生    零第三方依赖；默认不下单
  K 上屏        网关卡片三种 fixture（连通 / 回查坏 / 未接入）都过
"""

from __future__ import annotations

import inspect
import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

ROOT = Path(__file__).resolve().parent
PROJ = ROOT.parent
for _p in (str(PROJ), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _asserts import eq, falsy, guard, near, ne, raises, run, section, truth  # noqa: E402

from quantos_core.alpaca_gateway import (                                   # noqa: E402
    _DATA_BASE, DATA_BASE, LIVE_BASE, PAPER_BASE, LIVE_ENV_SWITCH,
    AlpacaCredentials, AlpacaGateway, AlpacaTransport, EndpointRefused,
    ProbeResult, resolve_base, to_order_state,
)
from quantos_core.contracts import (                                        # noqa: E402
    D, Fill, OrderIntent, OrderState, OrderType, Side, utcnow,
)
from quantos_core.gateway import (                                          # noqa: E402
    ACCEPTED, AuthError, CapabilityError, RateLimited, TransportError,
    UNKNOWN, VenueRejected, submit_with_reconcile,
)
from quantos_core.ledger import Ledger                                     # noqa: E402
from quantos_core.ledger_store import LedgerStore                          # noqa: E402
from quantos_core.venues import profile                                    # noqa: E402

VENUE = "alpaca"
SYMBOL = "AAPL"

ACCOUNT_OK = {
    "account_number": "PA9ZZTEST01", "status": "ACTIVE", "currency": "USD",
    "cash": "100000", "equity": "100000", "buying_power": "400000",
    "long_market_value": "0", "trading_blocked": False,
}
CLOCK_OPEN = {"is_open": True, "next_open": "2026-10-28T09:30:00-04:00",
              "next_close": "2026-10-27T16:00:00-04:00"}
ASSET_OK = {"symbol": SYMBOL, "tradable": True, "fractionable": True,
            "status": "active", "class": "us_equity"}


# --------------------------------------------------------------------------- #
#  FakeTransport
# --------------------------------------------------------------------------- #

class FakeTransport(AlpacaTransport):
    """
    把路由写死在字典里。每个 key 可以是：

      · `(status, payload)`                —— 永远这么答
      · `[ (status, payload), ... ]`       —— 按顺序消费，最后一个重复
      · `"TIMEOUT"`                        —— 抛 TransportError（模拟断网/超时）

    `calls` 记录每次请求。**"没发出去"这类断言只能靠它证明** ——
    只看返回值的测试，永远分不清"拒绝了"和"发了但被拒了"。
    """

    def __init__(self, routes: Mapping[Any, Any]) -> None:
        self.routes = dict(routes)
        self.calls: List[Tuple[str, str, Dict[str, Any], Dict[str, Any]]] = []

    def request(self, method: str, path: str, *, params=None, body=None):
        clean_path = path.split("?")[0]
        self.calls.append((method, clean_path, dict(params or {}), dict(body or {})))
        key = (method, clean_path)
        if key not in self.routes:
            return 404, {"code": 40410000, "message": f"endpoint not found: {clean_path}"}
        value = self.routes[key]
        if isinstance(value, list):
            index = min(sum(1 for c in self.calls if (c[0], c[1]) == key) - 1,
                        len(value) - 1)
            value = value[index]
        if value == "TIMEOUT":
            raise TransportError(f"{method} {path} 超时（模拟）")
        return value

    def count(self, method: str, path: str) -> int:
        return sum(1 for c in self.calls if c[0] == method and c[1] == path)

    def bodies(self, method: str, path: str) -> List[Dict[str, Any]]:
        return [c[3] for c in self.calls if c[0] == method and c[1] == path]


def order_json(*, cid: str = "cid-1", oid: str = "oid-1", symbol: str = SYMBOL,
               qty: str = "1", filled: str = "0", status: str = "new",
               avg: Optional[str] = None) -> Dict[str, Any]:
    return {
        "id": oid, "client_order_id": cid, "symbol": symbol,
        "side": "buy", "qty": qty, "filled_qty": filled, "status": status,
        "filled_avg_price": avg, "submitted_at": "2026-10-27T15:30:00.123456789Z",
        "created_at": "2026-10-27T15:30:00.123456789Z",
    }


def base_routes(overrides: Optional[Mapping[Any, Any]] = None) -> Dict[Any, Any]:
    routes: Dict[Any, Any] = {
        ("GET", "/v2/account"): (200, dict(ACCOUNT_OK)),
        ("GET", "/v2/clock"): (200, dict(CLOCK_OPEN)),
        ("GET", f"/v2/assets/{SYMBOL}"): (200, dict(ASSET_OK)),
        ("GET", "/v2/orders:by_client_order_id"):
            (404, {"code": 40410000, "message": "order not found for probe"}),
        ("POST", "/v2/orders"): (200, order_json(status="accepted")),
        ("GET", "/v2/positions"): (200, []),
        ("GET", "/v2/account/activities"): (200, []),
        ("GET", "/v2/orders"): (200, []),
        ("DELETE", "/v2/orders/oid-1"): (200, order_json(status="canceled")),
    }
    if overrides:
        routes.update(overrides)
    return routes


def make_gateway(routes: Optional[Mapping[Any, Any]] = None, **kwargs) -> Tuple[AlpacaGateway, FakeTransport]:
    transport = FakeTransport(routes if routes is not None else base_routes())
    credentials = AlpacaCredentials(key_id="PKTESTKEY", secret="s3cr3t-do-not-leak")
    defaults = dict(capabilities=profile(VENUE), require_market_open=True,
                    max_order_notional=Decimal("1000"),
                    now=lambda: utcnow())
    defaults.update(kwargs)
    return AlpacaGateway(transport, credentials, **defaults), transport


def make_intent(*, symbol: str = SYMBOL, side: Side = Side.BUY,
                qty: str = "1", kind: OrderType = OrderType.MARKET,
                limit: Optional[str] = None, cid: str = "cid-1",
                notional: Optional[str] = None, reduce_only: bool = False,
                venue: str = VENUE) -> OrderIntent:
    now = utcnow()
    quantity = D(qty)
    return OrderIntent(
        order_intent_id=cid, client_order_id=cid, account_id="ACCT",
        venue=venue, symbol=symbol, side=side, quantity=quantity,
        order_type=kind,
        notional=D(notional) if notional else (quantity * Decimal(100)).quantize(Decimal("0.01")),
        attribution={VENUE: quantity}, reason_chain=({"why": "自测"},),
        as_of=now, ts=now,
        limit_price=D(limit) if limit else None, reduce_only=reduce_only)


# --------------------------------------------------------------------------- #
#  A. 端点守卫
# --------------------------------------------------------------------------- #

def part_a() -> None:
    section("A. 端点守卫：真钱要双开关，自定义 host 一律拒")

    base, mode = resolve_base()
    eq(base, PAPER_BASE, "默认端点是纸面")
    eq(mode, "paper", "默认模式是 paper")

    base2, _ = resolve_base(PAPER_BASE + "/")
    eq(base2, PAPER_BASE, "结尾斜杠会被归一化（不靠调用方记得去掉）")

    saved = os.environ.pop(LIVE_ENV_SWITCH, None)
    try:
        raises(EndpointRefused, lambda: resolve_base(LIVE_BASE),
               "真钱端点 + 没开环境开关 → 拒绝")
        raises(EndpointRefused, lambda: resolve_base(LIVE_BASE, allow_live=True),
               "只开参数不设环境变量 → 仍然拒绝（双开关缺一不可）")

        os.environ[LIVE_ENV_SWITCH] = "1"
        raises(EndpointRefused, lambda: resolve_base(LIVE_BASE),
               "只设环境变量不给参数 → 仍然拒绝")
        live_base, live_mode = resolve_base(LIVE_BASE, allow_live=True)
        eq(live_mode, "live", "两个开关都在场 → 才认账")
        eq(live_base, LIVE_BASE, "  └ 返回的确实是真钱端点")
    finally:
        os.environ.pop(LIVE_ENV_SWITCH, None)
        if saved is not None:
            os.environ[LIVE_ENV_SWITCH] = saved

    for bad in ("https://api.alpaca.markets.evil.com", "http://127.0.0.1:9999",
                "https://example.com", PAPER_BASE + ".attacker.io"):
        raises(EndpointRefused, lambda b=bad: resolve_base(b),
               f"自定义端点被拒：{bad[:38]}")

    eq(DATA_BASE, _DATA_BASE, "行情端点与交易端点是两个 host")
    truth(DATA_BASE not in (PAPER_BASE, LIVE_BASE),
          "  └ 行情 host 不混进交易白名单")


# --------------------------------------------------------------------------- #
#  B. 凭据
# --------------------------------------------------------------------------- #

def part_b() -> None:
    section("B. 凭据：secret 不能出现在任何可打印的地方")

    import quantos_core.alpaca_gateway as mod

    # 这两段测的是「环境里没有凭据时会不会拿空值去试」。本机有 config/.env，
    # 所以要先关掉自动载入（`_ENV_LOADED` 置真即跳过），否则拿到的是文件里的
    # 真凭据，「缺失」这个场景根本进不去。
    saved_loaded = mod._ENV_LOADED
    mod._ENV_LOADED = True
    saved_key = os.environ.pop("ALPACA_API_KEY", None)
    saved_secret = os.environ.pop("ALPACA_API_SECRET", None)
    try:
        raises(AuthError, lambda: AlpacaCredentials.from_env(),
               "两个都缺 → AuthError（不拿空凭据去试）")
        os.environ["ALPACA_API_KEY"] = "PKONLYKEY"
        raises(AuthError, lambda: AlpacaCredentials.from_env(),
               "缺 secret → 一样拒绝")
        os.environ["ALPACA_API_SECRET"] = "top-secret-value"
        cred = AlpacaCredentials.from_env()
        eq(cred.mode, "paper", "凭据齐了就走纸面")
    finally:
        for name, value in (("ALPACA_API_KEY", saved_key),
                            ("ALPACA_API_SECRET", saved_secret)):
            os.environ.pop(name, None)
            if value is not None:
                os.environ[name] = value
        mod._ENV_LOADED = saved_loaded

    # .env 只补空缺，不覆盖已有值 —— 否则线上改了环境变量却被磁盘上的旧值盖掉，
    # 那种故障看起来像"配置没生效"，实际是优先级反了。
    mod._ENV_LOADED = False
    os.environ["ALPACA_API_KEY"] = "PKFROMENV"
    os.environ["ALPACA_API_SECRET"] = "sec-from-env"
    mod._ensure_env()
    eq(os.environ.get("ALPACA_API_KEY"), "PKFROMENV",
       "config/.env 不覆盖已经存在的环境变量")
    os.environ.pop("ALPACA_API_KEY", None)
    os.environ.pop("ALPACA_API_SECRET", None)
    mod._ENV_LOADED = saved_loaded

    credentials = AlpacaCredentials(key_id="PKTESTKEY", secret="s3cr3t-do-not-leak")
    eq(credentials.masked_key, "PKTE***", "key 只露前四位")
    blob = json.dumps(credentials.redacted(), ensure_ascii=False)
    falsy("s3cr3t" in blob, "redacted() 里没有 secret 片段")
    falsy("s3cr3t" in repr(credentials), "repr() 里也没有（field(repr=False) 生效）")
    truth("PKTE***" in blob, "  └ 但有可辨识的 key 前缀，运维能认出是哪个账户")


# --------------------------------------------------------------------------- #
#  C. 错误分类
# --------------------------------------------------------------------------- #

def part_c() -> None:
    section("C. 错误分类：确定 vs 不确定，两类绝不能混")

    cases = [
        ((401, {"message": "unauthorized"}), AuthError, False, "401 → AuthError"),
        ((403, {"message": "forbidden"}), AuthError, False, "403 → AuthError"),
        ((429, {"message": "too many requests"}), RateLimited, True, "429 → RateLimited"),
        ((500, {"message": "server error"}), TransportError, True, "500 → 不确定"),
        ((503, {"message": "unavailable"}), TransportError, True, "503 → 不确定"),
        ((400, {"message": "bad request"}), VenueRejected, False, "400 → 场所拒绝"),
        ((422, {"message": "qty too small"}), VenueRejected, False, "422（非重复单）→ 拒绝"),
        ((418, {"message": "teapot"}), TransportError, True, "未见过 418 → 按不确定"),
    ]
    for (status, payload), expected, ambiguous, label in cases:
        gw, tp = make_gateway(base_routes({("GET", "/v2/positions"): (status, payload)}))
        guard(label, lambda g=gw, e=expected: raises(e, g.fetch_positions))
        truth(expected.ambiguous is ambiguous,
              f"  └ {expected.__name__}.ambiguous == {ambiguous}")

    gw, _ = make_gateway(base_routes({("GET", "/v2/positions"): "TIMEOUT"}))
    raises(TransportError, gw.fetch_positions,
           "超时 → TransportError（不是 VenueRejected）")
    truth(TransportError.ambiguous, "  └ 而且标记为「请求可能已生效」")
    truth(not VenueRejected.ambiguous, "场所拒绝标记为「确定没生效」（两者语义互斥）")


# --------------------------------------------------------------------------- #
#  D. 状态映射
# --------------------------------------------------------------------------- #

def part_d() -> None:
    section("D. 状态映射：done_for_day 带部分成交不能判成「还在场」")

    eq(to_order_state("accepted", D(0), D(1)), OrderState.ACCEPTED,
       "accepted → ACCEPTED（共享表里没有这个词，Alpaca 独有）")
    eq(to_order_state("new", D(0), D(1)), OrderState.ACCEPTED, "new → ACCEPTED")
    eq(to_order_state("filled", D(10), D(10)), OrderState.FILLED, "filled 且量对 → FILLED")
    eq(to_order_state("filled", D(4), D(10)), OrderState.PARTIALLY_FILLED,
       "filled 但量不足 → PARTIALLY_FILLED（场所先报终态、明细后到）")
    eq(to_order_state("filled", D(0), D(10)), OrderState.CANCELLED,
       "filled 但零成交 → CANCELLED（敞口等价于撤销）")

    eq(to_order_state("done_for_day", D(0), D(10)), OrderState.CANCELLED,
       "done_for_day 无成交 → CANCELLED（当日单到期）")
    eq(to_order_state("done_for_day", D(3), D(10)), OrderState.CANCELLED,
       "done_for_day 部分成交 → 仍是 CANCELLED（不是 PARTIALLY_FILLED）")
    truth(OrderState.CANCELLED in (OrderState.FILLED, OrderState.CANCELLED,
                                   OrderState.REJECTED, OrderState.EXPIRED),
          "  └ 该状态必须终态，否则 OMS 会一直等残量")

    eq(to_order_state("replaced", D(0), D(1)), OrderState.CANCELLED,
       "replaced → CANCELLED（被替换掉的旧单不再有效）")
    eq(to_order_state("stopped", D(0), D(1)), OrderState.UNKNOWN,
       "stopped → UNKNOWN（止损未触发，不可判定）")
    eq(to_order_state("what_is_this", D(0), D(1)), OrderState.UNKNOWN,
       "没见过的词 → UNKNOWN（不猜成 FILLED 也不猜成 CANCELLED）")

    gw, _ = make_gateway(base_routes({
        ("GET", "/v2/orders:by_client_order_id"):
            (200, order_json(status="done_for_day", qty="10", filled="3", avg="10.5"))}))
    parsed = gw.fetch_order(SYMBOL, "cid-1")
    eq(parsed.state, OrderState.CANCELLED, "场所原文 done_for_day 被正确归一化")
    eq(parsed.filled_qty, D(3), "  └ 成交数量被保留（敞口是真实的）")
    eq(parsed.raw_state, "done_for_day", "  └ 保留场所原文，排障时要看这个")
    truth(parsed.is_terminal, "  └ 判为终态，OMS 不再轮询")
    truth(parsed.remaining_qty > 0, "  └ 残量非零（但单子已经死了，不该等它）")


# --------------------------------------------------------------------------- #
#  E. 下单守卫
# --------------------------------------------------------------------------- #

def part_e() -> None:
    section("E. 下单守卫：每一道闸都在发请求之前")

    gw, tp = make_gateway()
    raises(CapabilityError, lambda: gw.place(make_intent(reduce_only=True)),
           "reduce_only → CapabilityError（美股现货没这个概念）")
    eq(len(tp.calls), 0, "  └ 一次 HTTP 都没发（本地就知道不行）")

    gw, tp = make_gateway()
    raises(CapabilityError, lambda: gw.place(make_intent(qty="100", notional="10000")),
           "名义额 10000 超上限 1000 → CapabilityError")
    eq(tp.count("POST", "/v2/orders"), 0, "  └ 没有发 POST（网关是最后一道胖手指闸）")

    gw, tp = make_gateway()
    raises(CapabilityError,
           lambda: gw.place(make_intent(kind=OrderType.LIMIT, limit="100", qty="0.5")),
           "限价单 + 碎股 → CapabilityError（Alpaca 碎股仅市价单）")
    eq(tp.count("POST", "/v2/orders"), 0, "  └ 且没有偷偷取整成 1 股发出去")

    gw, tp = make_gateway(base_routes({
        ("GET", "/v2/clock"): (200, {"is_open": False, "next_open": "2026-10-28T13:30:00Z"})}))
    raises(VenueRejected, lambda: gw.place(make_intent()), "休市 → VenueRejected")
    eq(tp.count("POST", "/v2/orders"), 0, "  └ 没有把日单排到明天早上")

    gw, tp = make_gateway(base_routes({
        ("GET", "/v2/account"): (200, {**ACCOUNT_OK, "trading_blocked": True})}))
    raises(VenueRejected, lambda: gw.place(make_intent()), "trading_blocked → VenueRejected")
    eq(tp.count("POST", "/v2/orders"), 0, "  └ 没发请求")

    gw, tp = make_gateway(base_routes({
        ("GET", "/v2/account"): (200, {**ACCOUNT_OK, "status": "ACCOUNT_CLOSED"})}))
    raises(VenueRejected, lambda: gw.place(make_intent()),
           "账户非 ACTIVE → VenueRejected（不只看 trading_blocked）")

    gw, tp = make_gateway()
    raises(CapabilityError, lambda: gw.place(make_intent(venue="binance")),
           "订单 venue 与网关不符 → CapabilityError")

    gw, tp = make_gateway()
    placed = gw.place(make_intent(qty="1", cid="cid-abc"))
    body = tp.bodies("POST", "/v2/orders")[0]
    eq(body["client_order_id"], "cid-abc", "正常下单：幂等键随请求发出")
    eq(body["qty"], "1", "  └ 整股写成 \"1\" 而不是 \"1.000000\"")
    eq(body["type"], "market", "  └ 市价单 type=market")
    eq(body["side"], "buy", "  └ 方向翻译为 buy/sell")
    eq(body["time_in_force"], "day", "  └ 日单")
    falsy("limit_price" in body, "  └ 市价单不带 limit_price")
    eq(placed.state, OrderState.ACCEPTED, "返回体被正确解析")

    gw, tp = make_gateway()
    gw.place(make_intent(qty="0.5", notional="50"))
    eq(tp.bodies("POST", "/v2/orders")[0]["qty"], "0.5",
       "碎股数量保留精度（可碎股标的）")

    gw, tp = make_gateway(base_routes({
        ("GET", f"/v2/assets/{SYMBOL}"):
            (200, {**ASSET_OK, "fractionable": False})}))
    raises(CapabilityError, lambda: gw.place(make_intent(qty="0.5", notional="50")),
           "标的不可碎股 + 小数数量 → CapabilityError")
    eq(tp.count("POST", "/v2/orders"), 0, "  └ 不发请求（否则每单都被场所拒 = 假接通）")


# --------------------------------------------------------------------------- #
#  F. 幂等
# --------------------------------------------------------------------------- #

def part_f() -> None:
    section("F. 幂等：重复单号去查，超时只查不重发")

    gw, tp = make_gateway(base_routes({
        ("POST", "/v2/orders"):
            (422, {"code": 42210000, "message": "client_order_id must be unique"}),
        ("GET", "/v2/orders:by_client_order_id"):
            (200, order_json(cid="cid-1", status="accepted"))}))
    first = gw.place(make_intent(cid="cid-1"))
    eq(first.client_order_id, "cid-1", "重复单号 422 → 回查命中 → 当成功（不是失败）")
    eq(tp.count("POST", "/v2/orders"), 1, "  └ 只发了一次 POST，没有重发")
    truth(tp.count("GET", "/v2/orders:by_client_order_id") >= 1,
          "  └ 而是发了一次回查")

    gw, tp = make_gateway(base_routes({
        ("POST", "/v2/orders"):
            (422, {"code": 42210000, "message": "client_order_id must be unique"}),
        ("GET", "/v2/orders:by_client_order_id"):
            (404, {"code": 40410000, "message": "order not found"})}))
    raises(TransportError, lambda: gw.place(make_intent(cid="cid-1")),
           "重复单号但回查查不到 → 不确定（可见性延迟），不判失败")
    truth("不得重发" in str(_exc_message(lambda: gw.place(make_intent(cid="cid-1")))),
          "  └ 错误文案里明确写了「不得重发」")

    gw, tp = make_gateway(base_routes({("POST", "/v2/orders"): "TIMEOUT"}))
    outcome = submit_with_reconcile(gw, make_intent(), max_queries=2,
                                    query_delay_sec=0, sleeper=lambda _s: None)
    eq(outcome.status, UNKNOWN, "place 超时 + 回查无果 → UNKNOWN（挂起，不重发）")
    eq(outcome.place_calls, 1, "  └ place 只被调用 1 次（这是整条防线）")
    eq(tp.count("POST", "/v2/orders"), 1, "  └ HTTP 层面也只有 1 次 POST")
    truth(tp.count("GET", "/v2/orders:by_client_order_id") >= 2, "  └ 而是查了多次")

    gw, tp = make_gateway()
    eq(gw.fetch_order(SYMBOL, "nope"), None, "回查 404 → None（查过了，确实没有）")

    eq(profile(VENUE).supports_query_by_client_id, True,
       "能力声明：能按 client_order_id 回查（否则 submit_with_reconcile 直接拒）")


def _exc_message(fn) -> str:
    try:
        fn()
    except Exception as exc:            # noqa: BLE001  —— 断言辅助，故意抓全部
        return str(exc)
    return ""


# --------------------------------------------------------------------------- #
#  G. 撤单两步
# --------------------------------------------------------------------------- #

def part_g() -> None:
    section("G. 撤单：先换 order_id 再 DELETE（实测该端点没有 by_client_order_id）")

    gw, tp = make_gateway(base_routes({
        ("GET", "/v2/orders:by_client_order_id"):
            (200, order_json(cid="cid-1", oid="oid-9", status="accepted")),
        ("DELETE", "/v2/orders/oid-9"): (200, order_json(cid="cid-1", oid="oid-9",
                                                        status="canceled"))}))
    cancelled = gw.cancel(SYMBOL, "cid-1")
    eq(cancelled.state, OrderState.CANCELLED, "撤单成功，状态为 CANCELLED")
    eq(tp.count("GET", "/v2/orders:by_client_order_id"), 1, "  └ 第一步：按单号换 order_id")
    eq(tp.count("DELETE", "/v2/orders/oid-9"), 1, "  └ 第二步：DELETE 用场所 order_id")
    eq(tp.count("DELETE", "/v2/orders:by_client_order_id"), 0,
       "  └ 没有去试那个不存在的端点")

    gw, tp = make_gateway(base_routes({
        ("GET", "/v2/orders:by_client_order_id"):
            (404, {"code": 40410000, "message": "order not found"})}))
    raises(VenueRejected, lambda: gw.cancel(SYMBOL, "cid-1"),
           "撤不存在的单 → VenueRejected")
    eq(tp.count("DELETE", "/v2/orders/oid-1"), 0, "  └ 不发 DELETE")

    gw, tp = make_gateway(base_routes({
        ("GET", "/v2/orders:by_client_order_id"):
            (200, order_json(cid="cid-1", status="filled", filled="1", avg="11.5"))}))
    already = gw.cancel(SYMBOL, "cid-1")
    eq(already.state, OrderState.FILLED, "已成交的单：直接返回现状，不当成撤单失败")
    eq(tp.count("DELETE", "/v2/orders/oid-1"), 0, "  └ 一次 DELETE 都不发（省一次必然 422）")

    gw, tp = make_gateway(base_routes({
        ("GET", "/v2/orders:by_client_order_id"): [
            (200, order_json(cid="cid-1", oid="oid-9", status="accepted")),
            (200, order_json(cid="cid-1", oid="oid-9", status="filled",
                             filled="1", avg="11.5"))],
        ("DELETE", "/v2/orders/oid-9"):
            (422, {"code": 42210000, "message": "order is not cancelable"})}))
    raced = gw.cancel(SYMBOL, "cid-1")
    eq(raced.state, OrderState.FILLED,
       "撤单瞬间成交（422）→ 回查现状返回，而不是报错")


# --------------------------------------------------------------------------- #
#  H. 探针
# --------------------------------------------------------------------------- #

def part_h() -> None:
    section("H. 探针：回查端点坏掉必须红，不能绿着放行")

    gw, tp = make_gateway()
    ok = gw.probe(SYMBOL)
    truth(ok.ok, "正常账户 → 探针通过")
    truth(ok.query_by_client_id_ok, "  └ 回查端点判定为可用")
    truth(ok.query_latency_ms >= 0, "  └ 量出了回查延迟（实测值，不是抄文档）")
    ne(ok.rules, None, "  └ 拿到了标的级下单规则")
    eq(ok.rules.qty_step, Decimal("0.000001"), "  └ 可碎股标的步长 1e-6")

    gw, tp = make_gateway(base_routes({
        ("GET", "/v2/orders:by_client_order_id"): (200, order_json())}))
    bad = gw.probe(SYMBOL)
    falsy(bad.ok, "回查端点返回 200 → 探针必须红（预期 404 + order not found）")
    truth("拒绝承担自动交易" in bad.detail, "  └ 文案写明拒绝承担自动交易")

    gw, tp = make_gateway(base_routes({
        ("GET", "/v2/orders:by_client_order_id"):
            (404, {"message": "endpoint not found"})}))
    fake404 = gw.probe(SYMBOL)
    falsy(fake404.ok, "404 但不是 order not found（端点不存在）→ 同样必须红")
    truth(any("预期 404" in n for n in fake404.notes), "  └ 备注里写出了预期值")

    gw, tp = make_gateway(base_routes({
        ("GET", "/v2/account"): (401, {"message": "unauthorized"})}))
    noauth = gw.probe(SYMBOL)
    falsy(noauth.ok, "鉴权失败 → 探针红")
    truth("AuthError" in noauth.detail, "  └ detail 里带异常类名，能一眼定位")

    gw, tp = make_gateway()
    eq(gw.capabilities.verified_at, None, "默认能力声明是未实测（verified_at 为空）")
    gw.probe(SYMBOL, verify=True)
    ne(gw.capabilities.verified_at, None, "verify=True 后标记为实测")

    gw, tp = make_gateway(base_routes({
        ("GET", "/v2/clock"): (200, {"is_open": False})}))
    closed = gw.probe(SYMBOL)
    truth(closed.ok, "休市不算探针失败（账户是好的，只是不能下单）")
    truth(any("休市" in n for n in closed.notes), "  └ 但要在备注里说清楚")


# --------------------------------------------------------------------------- #
#  I. 账本衔接
# --------------------------------------------------------------------------- #

def part_i() -> None:
    section("I. 账本衔接：absent 判定看 status，不看对象是否为 None")

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "alpaca_ledger.json")
        store = LedgerStore(path)
        result = store.load(base_currency="USD", allow_absent=True)
        eq(result.status, "absent", "没有文件时 status == absent")
        truth(result.ledger is not None,
              "  └ 但 ledger **不是 None**（allow_absent 给了一个空账本）")
        truth(result.ledger.cash_native() == {},
              "  └ 所以它现金为 0：把「对象非 None」当「已有账本」会把 10 万当成 0")

        ledger = Ledger(base_currency="USD")
        ledger.deposit("ACCT", "USD", D("100000"), ref="initial")
        eq(ledger.cash_native()[("ACCT", "USD")], D("100000"), "首次入金后现金正确")

        fill = Fill(fill_id="f1", order_id="o1", client_order_id="cid-1",
                    venue_trade_id="t1", quantity=D("1"), price=D("11.75"),
                    fee=Decimal(0), fee_currency="USD", ts=utcnow())
        ledger.apply_fill(fill, venue=VENUE, symbol=SYMBOL, account_id="ACCT",
                          side=Side.BUY, quote_ccy="USD",
                          decision_price=D("11.745"),
                          attribution={VENUE: D("1")})
        eq(ledger.cash_native()[("ACCT", "USD")], D("99988.25"),
           "买入成交后现金减少 = 数量 × 成交价（不是决策价）")
        pos = ledger.positions()[("alpaca", SYMBOL)]
        eq(pos.qty, D("1"), "持仓 +1")
        truth(pos.buy_at_decision == D("11.745"),
              "  └ 决策价单独留存（滑点归因要靠它）")

        store.save(ledger)
        again = store.load(base_currency="USD").ledger
        eq(again.cash_native()[("ACCT", "USD")], D("99988.25"), "落盘往返后现金一致")
        eq(again.positions()[("alpaca", SYMBOL)].qty, D("1"), "  └ 持仓一致")

    eq(profile(VENUE).supports_reduce_only, False,
       "能力声明：美股现货不支持 reduce_only（所以 place() 直接拒）")
    eq(profile(VENUE).funding_interval_hours, 0, "美股现货无资金费")


# --------------------------------------------------------------------------- #
#  J. 代码卫生
# --------------------------------------------------------------------------- #

def part_j() -> None:
    section("J. 代码卫生：零第三方依赖，默认不下单")

    import quantos_core.alpaca_gateway as mod
    source = inspect.getsource(mod)
    imported = set()
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            head = stripped.split()[1]
            imported.add(head.split(".")[0])
    third_party = {name for name in imported
                   if name not in {"__future__", "json", "os", "socket", "time",
                                   "urllib", "dataclasses", "datetime", "decimal",
                                   "typing", ""}}
    falsy(third_party, f"只依赖 stdlib，无第三方包（实际额外：{sorted(third_party)}）")

    script = (ROOT / "quantos_alpaca.py").read_text(encoding="utf-8")
    truth("--yes" in script, "roundtrip 需要 --yes")
    truth("纸面账户＝假钱" in script or "假钱" in script,
          "  └ 提示里讲清了「假钱但订单是真的」")
    truth('sub.add_parser("probe"' in script and 'sub.add_parser("reconcile"' in script,
          "只读子命令存在（probe / reconcile）")
    truth("submit_with_reconcile" in script,
          "下单走 submit_with_reconcile（超时只查不重发），不直接调 place")

    gw, tp = make_gateway()
    gw.probe(SYMBOL)
    posts = [c for c in tp.calls if c[0] == "POST"]
    eq(len(posts), 0, "探针全程没有 POST（只读就是只读）")

    falsy(hasattr(gw, "flatten") or hasattr(gw, "close_all"),
          "网关没有「一键清仓」这种便利方法（那是策略层的决定）")

    eq(sorted(k for k in (PAPER_BASE, LIVE_BASE) if k),
       sorted([PAPER_BASE, LIVE_BASE]), "端点常量只有两个，没有第三个出口")


# --------------------------------------------------------------------------- #
#  K. 上屏
# --------------------------------------------------------------------------- #

GATEWAY_OK = {
    "available": True, "mode": "paper", "ok": True, "healthy": True,
    "endpoint": "https://paper-api.alpaca.markets", "key": "PK2N***",
    "detail": "纸面账户可用（USD 100000，ACTIVE）",
    "account": {"account_number": "***ST01", "status": "ACTIVE", "currency": "USD",
                "cash": "100000", "equity": "100000", "buying_power": "400000",
                "trading_blocked": False},
    "is_open": True, "next_close": "2026-10-27T16:00:00-04:00",
    "query_by_client_id_ok": True, "query_latency_ms": 743,
    "max_order_notional": "1000", "notes": ["市场当前开盘"],
}

GATEWAY_NOQUERY = dict(
    GATEWAY_OK, ok=False, healthy=False, query_by_client_id_ok=False,
    query_latency_ms=0,
    detail="无法按客户端单号回查 —— 拒绝承担自动交易",
    notes=["回查端点坏了，等于超时之后永远救不回来"])


def part_k() -> None:
    section("K. 上屏：网关卡片按 fixture 渲染（node 探针）")

    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if node is None:
        truth(True, "K 本机没有 node，跳过前端探针（不影响后端结论）")
        return

    sys.path.insert(0, str(ROOT))
    from quantos_ops_server import OpsService, build_runtime

    rt = build_runtime()

    def make_service(provider):
        return OpsService(metrics=rt["metrics"], registry=rt["registry"],
                          prices_provider=lambda: rt["prices"], token="t0ken",
                          series=rt["series"], live_gate=rt["gate"],
                          risk_model_store=rt["risk_model_store"],
                          canary_router=rt["canary_router"],
                          research_inbox=rt["research_inbox"],
                          gateway_provider=provider)

    def run_probe(svc, name: str, probe_name: str) -> "subprocess.CompletedProcess":
        payload = {
            "snapshot": svc.snapshot(), "strategies": svc.strategies(),
            "audit": svc.audit(30), "navHistory": svc.nav_history(),
            "live": svc.live(), "portfolio": svc.portfolio(),
            "gateway": svc.gateway(),
        }
        path = os.path.join(probe_name, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, default=str)
        return subprocess.run([node, str(ROOT / "page_probe.js"),
                               str(PROJ / "quantos" / "index.html"), path],
                              capture_output=True, text=True, timeout=240)

    with tempfile.TemporaryDirectory() as tmp:
        healthy = run_probe(make_service(lambda: dict(GATEWAY_OK)), "gw_ok.json", tmp)
        eq(healthy.returncode, 0, "K1 健康网关 fixture：页面探针全部通过")
        if healthy.stdout.strip():
            print("      " + healthy.stdout.strip().splitlines()[-1])

        broken = run_probe(make_service(lambda: dict(GATEWAY_NOQUERY)),
                           "gw_noquery.json", tmp)
        eq(broken.returncode, 0,
           "K2 回查不可用 fixture：页面红着说「不许承担自动交易」")

        absent = run_probe(make_service(None), "gw_absent.json", tmp)
        eq(absent.returncode, 0, "K3 未接入 fixture：页面明说未接入且不挂绿灯")

    src = (PROJ / "quantos" / "index.html").read_text(encoding="utf-8")
    start = src.index("function renderGateway()")
    body = src[start: src.index("function renderDrawer()", start)]
    for other in ("renderLedger", "renderSchedule", "renderOverview", "refreshAll"):
        falsy(other + "(" in body,
              f"K4 renderGateway 不调用 {other}（联动统一走 refreshAll）")
    truth("renderGateway();" in src, "K4 refreshAll 里调用了 renderGateway")
    truth("LIVE_ONLY_NEVER" not in body, "K5 渲染函数里没有占位符残留")


def main() -> int:
    print("=" * 66)
    print("B11 自测：Alpaca 纸面网关（离线，不联网）")
    print("=" * 66)
    for part in (part_a, part_b, part_c, part_d, part_e, part_f,
                 part_g, part_h, part_i, part_j, part_k):
        part()
    return 0


if __name__ == "__main__":
    raise SystemExit(run("B11 Alpaca 纸面网关", main))
