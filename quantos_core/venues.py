"""
QuantOS 2.0 — 交易所能力矩阵与准入（T-502）
===========================================

一句话：**能挂单，不等于能自动交易。**

币圈场所的差异不在"有没有 API"，而在几个要命的细节上：

  · 幂等键能不能发出去（clientOrderId / newClientOrderId / clOrdID / orderLinkId）
  · 发出去之后，能不能**按这个键把它查回来**
  · 下单超时后，多久才能在查询接口里看到这笔单
  · 数量步长、最小名义金额、价格最小变动单位

第二条最容易被忽略。很多场所支持传客户端单号，但查询接口只认场所自己的
orderId。这意味着超时那一刻，你既没拿到 orderId，也查不回单子 —— 这笔单
到底下没下出去，**永远无法确定**。这种场所不能承担自动交易，不管它多主流。

所以准入判定里，「能发 clientOrderId」和「能按 clientOrderId 查」缺一不可。
只有前者，等于给自己造了一个"看起来有幂等键、实际上救不回来"的错觉。

本模块是纯策略层：不联网、不抛网关错误，只回答"这笔单在这个场所合不合法"。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .contracts import D, Side

__all__ = [
    "CapabilityError", "SymbolRules", "Capabilities", "Admission",
    "VENUE_PROFILES", "profile", "floor_to_step", "ceil_to_step",
    "quantize_qty", "quantize_price", "OrderCheck",
]

_ZERO = Decimal(0)
# 标准资金费周期（小时）。不在这个集合里不代表错，但结算边界必须实测。
_STANDARD_FUNDING_HOURS = (1, 2, 4, 8)


class CapabilityError(RuntimeError):
    """
    能力不足以执行这笔单。

    刻意**不**继承网关错误：这类问题在本地就能判定，一次网络请求都不该发出去。
    区分"本地就知道不行"和"场所说不行"，能让排障时少查一层。

    `ambiguous` 恒为 False —— 这类错误发生时代码还没碰过网络，
    所以订单**确定**不存在，可以安全地不当它发生过。
    """

    ambiguous: bool = False


@dataclass(frozen=True)
class SymbolRules:
    """单个标的的下单约束。交易所对这些值是按标的分档的，不是一个场所一套。"""

    qty_step: Decimal = Decimal("0.00001")
    price_tick: Decimal = Decimal("0.01")
    min_qty: Decimal = Decimal("0.00001")
    min_notional: Decimal = Decimal("5")

    def __post_init__(self):
        for name in ("qty_step", "price_tick", "min_qty", "min_notional"):
            if D(getattr(self, name)) <= 0:
                raise CapabilityError(f"SymbolRules.{name} 必须为正，收到 {getattr(self, name)}")


@dataclass(frozen=True)
class Capabilities:
    """
    一家场所的能力声明。

    `verified_at` 为空表示**这是抄文档的，没实测过**。带着这种声明的场所
    可以进模拟盘，进实盘前必须先跑 `probe()` 把声明值变成实测值。
    这不是形式主义：文档写的"支持 clientOrderId"，和"超时后真的能查回来"，
    中间经常隔着一个 bug。
    """

    venue: str
    supports_client_order_id: bool
    client_order_id_param: str
    supports_query_by_client_id: bool
    order_visibility_lag_ms: int = 2000
    supports_reduce_only: bool = True
    supports_ws: bool = True
    funding_interval_hours: int = 0            # 0 = 现货，无资金费
    default_rules: SymbolRules = field(default_factory=SymbolRules)
    symbol_rules: Mapping[str, SymbolRules] = field(default_factory=dict)
    declared_from: str = ""
    verified_at: Optional[datetime] = None

    def __post_init__(self):
        if not self.venue:
            raise CapabilityError("venue 不可为空")
        if self.supports_client_order_id and not self.client_order_id_param:
            raise CapabilityError(
                f"{self.venue} 声称支持客户端单号，但没给参数名 —— 发不出去")
        if self.order_visibility_lag_ms < 0:
            raise CapabilityError("order_visibility_lag_ms 不能为负")
        if self.funding_interval_hours < 0:
            raise CapabilityError("funding_interval_hours 不能为负")

    # -- 标的级约束 ------------------------------------------------------ #

    def rules_for(self, symbol: str) -> SymbolRules:
        return self.symbol_rules.get(symbol, self.default_rules)

    def with_rules(self, symbol: str, rules: SymbolRules) -> "Capabilities":
        merged: Dict[str, SymbolRules] = dict(self.symbol_rules)
        merged[symbol] = rules
        return replace(self, symbol_rules=merged)

    def with_rules_many(self, table: Mapping[str, SymbolRules]) -> "Capabilities":
        merged: Dict[str, SymbolRules] = dict(self.symbol_rules)
        merged.update(table)
        return replace(self, symbol_rules=merged)

    @property
    def verified(self) -> bool:
        return self.verified_at is not None

    def mark_verified(self, at: datetime) -> "Capabilities":
        return replace(self, verified_at=at)

    # -- 下单前量化 ------------------------------------------------------ #

    def validate_order(self, symbol: str, side: Side, qty, price=None, *,
                       notional: Optional[Decimal] = None,
                       max_order_notional: Optional[Decimal] = None) -> "OrderCheck":
        """
        把"想下的单"对齐到场所允许的样子，并判定它能不能下。

        取整方向是这里唯一的铁律：**只能往"更小、更不容易成交"的方向取。**

          · 数量一律向下取整。向上取整 = 实际敞口比你算的大，风控数字全废。
          · 买入限价向下取整（限价是你能接受的**上限**，向上就越过了它）。
          · 卖出限价向上取整（限价是你能接受的**下限**，向下就越过了它）。

        两种取整都会降低成交概率，但都不会让账户承担超出意图的风险。
        宁可这一单不成交，也不能成交了才发现成交在预算之外。
        """
        rules = self.rules_for(symbol)
        raw_qty = D(qty)
        if raw_qty <= 0:
            raise CapabilityError(f"数量必须为正，收到 {qty}")

        q = quantize_qty(raw_qty, rules.qty_step)
        notes: List[str] = []
        if q != raw_qty:
            notes.append(f"数量 {raw_qty} 按步长 {rules.qty_step} 向下取整为 {q}")
        if q < rules.min_qty:
            raise CapabilityError(
                f"{symbol} 数量 {q} 低于最小下单量 {rules.min_qty}（原始 {raw_qty}）")

        px: Optional[Decimal] = None
        if price is not None:
            raw_px = D(price)
            if raw_px <= 0:
                raise CapabilityError(f"价格必须为正，收到 {price}")
            px = quantize_price(raw_px, rules.price_tick, side)
            if px != raw_px:
                notes.append(
                    f"价格 {raw_px} 按最小变动 {rules.price_tick} 调整为 {px}"
                    f"（{'买入向下、不越过限价上限' if side is Side.BUY else '卖出向上、不越过限价下限'}）")

        # 名义金额口径：有价格用 数量×价格，没有价格就用调用方给的名义（市价单）
        if px is not None:
            effective = q * px
        elif notional is not None:
            effective = D(notional)
        else:
            effective = q * rules.min_notional      # 仅用于最小额校验的保守下界

        if effective < rules.min_notional:
            raise CapabilityError(
                f"{symbol} 有效名义 {effective} 低于最小名义 {rules.min_notional}"
                f"（数量 {q} × 价格 {px}）")
        if max_order_notional is not None and effective > D(max_order_notional):
            raise CapabilityError(
                f"{symbol} 有效名义 {effective} 超过单笔上限 {max_order_notional}")

        return OrderCheck(symbol=symbol, side=side, qty=q, price=px,
                          notional=effective, notes=tuple(notes), rules=rules)


@dataclass(frozen=True)
class OrderCheck:
    """量化后的下单参数 + 做了什么调整（要进审计，否则事后说不清为什么和意图不一样）。"""

    symbol: str
    side: Side
    qty: Decimal
    price: Optional[Decimal]
    notional: Decimal
    notes: Tuple[str, ...] = ()
    rules: SymbolRules = field(default_factory=SymbolRules)


@dataclass(frozen=True)
class Admission:
    """准入结论。blockers 非空即不许自动交易，warnings 只提示。"""

    venue: str
    allowed: bool
    blockers: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()

    def to_dict(self) -> Dict:
        return {"venue": self.venue, "allowed": self.allowed,
                "blockers": list(self.blockers), "warnings": list(self.warnings)}


# --------------------------------------------------------------------------- #
#  量化工具
# --------------------------------------------------------------------------- #

def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    step = D(step)
    if step <= 0:
        raise CapabilityError("步长必须为正")
    return (D(value) / step).to_integral_value(rounding=ROUND_FLOOR) * step


def ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    step = D(step)
    if step <= 0:
        raise CapabilityError("步长必须为正")
    return (D(value) / step).to_integral_value(rounding=ROUND_CEILING) * step


def quantize_qty(qty: Decimal, step: Decimal) -> Decimal:
    """数量永远向下。向上取整会让实际敞口大于风控算的敞口。"""
    return floor_to_step(qty, step)


def quantize_price(price: Decimal, tick: Decimal, side: Side) -> Decimal:
    """买向下、卖向上 —— 都朝"不越限价"的方向，宁可少成交。"""
    return floor_to_step(price, tick) if side is Side.BUY else ceil_to_step(price, tick)


# --------------------------------------------------------------------------- #
#  准入判定
# --------------------------------------------------------------------------- #

def admit(caps: Capabilities, *,
          max_order_notional: Optional[Decimal] = None,
          reconcile_budget_ms: int = 15000,
          require_ws: bool = False) -> Admission:
    """
    判定这家场所能不能承担自动交易。

    blockers 里的每一条都是"不解决就一定出事故"，不是风格偏好：

      · 无客户端单号      → 超时重发就是重复下单
      · 无法按单号回查    → 超时后永远不知道那笔单是否存在
      · 缺数量步长        → 只能瞎猜小数位，猜多的那一次就是超敞口
      · 最小名义 > 单笔上限 → 任何合法订单都下不出去，等于假接通
      · 可见延迟 > 对账预算 → 查不到就判 UNKNOWN，订单永远卡在冻结态
    """
    blockers: List[str] = []
    warnings: List[str] = []

    if not caps.supports_client_order_id:
        blockers.append(
            f"{caps.venue} 不支持客户端订单号：下单超时后无法去重，重发即重复下单")
    if not caps.supports_query_by_client_id:
        blockers.append(
            f"{caps.venue} 无法按客户端订单号回查：超时那一刻既没有 orderId 也查不回，"
            f"订单状态将永远无法确定")
    if caps.default_rules.qty_step <= 0:
        blockers.append(f"{caps.venue} 缺少数量步长")
    if caps.default_rules.price_tick <= 0:
        blockers.append(f"{caps.venue} 缺少价格最小变动单位")

    # 逐标的检查，而不是只查默认值。交易所的最小名义是按标的定的：
    # 默认值 5 USDT、但你要交易的 BTC 是 100 USDT 的情况很常见。
    # 只看默认值会让准入通过、然后每一单都被最小额拒绝 —— 这叫"假接通"，
    # 比直接不接更糟，因为监控上是绿的。
    bad_step = sorted(s for s, r in caps.symbol_rules.items() if r.qty_step <= 0)
    if bad_step:
        blockers.append(f"{caps.venue} 以下标的缺少数量步长：{', '.join(bad_step)}")

    if max_order_notional is not None:
        cap = D(max_order_notional)
        table = [("<默认>", caps.default_rules)] + sorted(caps.symbol_rules.items())
        offenders = [(sym, rules.min_notional) for sym, rules in table
                     if rules.min_notional > cap]
        if offenders:
            worst_sym, worst_min = max(offenders, key=lambda item: item[1])
            where = "（默认规则）" if worst_sym == "<默认>" else f"（{worst_sym}）"
            blockers.append(
                f"{caps.venue} 最小名义 {worst_min}{where} 高于单笔上限 "
                f"{max_order_notional}：任何订单都会被最小额拒绝"
                f"{'，另有 %d 个标的同样超限' % (len(offenders) - 1) if len(offenders) > 1 else ''}")
    if caps.order_visibility_lag_ms > reconcile_budget_ms:
        blockers.append(
            f"{caps.venue} 下单后需 {caps.order_visibility_lag_ms}ms 才能查到，"
            f"超出对账预算 {reconcile_budget_ms}ms")

    if not caps.supports_ws:
        warnings.append(f"{caps.venue} 无 WebSocket：行情只能轮询，陈旧判定需放宽")
    if not caps.supports_reduce_only:
        warnings.append(
            f"{caps.venue} 不支持 reduce-only：减仓单退化为普通单，"
            f"风控必须自行保证不反向加仓")
    if caps.funding_interval_hours and caps.funding_interval_hours not in _STANDARD_FUNDING_HOURS:
        warnings.append(
            f"{caps.venue} 资金费周期 {caps.funding_interval_hours}h 非标准，"
            f"结算边界必须实测后再启用资金费记账")
    if not caps.verified:
        warnings.append(
            f"{caps.venue} 能力声明尚未实测（verified_at 为空）：进实盘前必须跑 probe()")

    return Admission(venue=caps.venue, allowed=not blockers,
                     blockers=tuple(blockers), warnings=tuple(warnings))


def assert_admitted(caps: Capabilities, **kwargs) -> Admission:
    """准入不通过就抛错。给"接入即生效"的路径用，避免调用方忘了看返回值。"""
    verdict = admit(caps, **kwargs)
    if not verdict.allowed:
        raise CapabilityError(
            f"{caps.venue} 不允许自动交易：" + "；".join(verdict.blockers))
    return verdict


# --------------------------------------------------------------------------- #
#  声明式矩阵
# --------------------------------------------------------------------------- #
#  ⚠️ 下面这些值抄自各场所公开文档与 CCXT 的 market 元数据，**没有实测**。
#     用途是给新接入的场所一个起点，不是给实盘背书。用之前必须：
#       1. cap = profile("binanceusdm").with_rules("BTC/USDT:USDT", SymbolRules(...))
#       2. 跑 probe() 用真实响应覆盖 lag / 单号参数名 / 步长
#       3. verified_at 才有值，admit() 才会停止告警
#
#  这里刻意只列"结构上确实支持按客户端单号回查"的场所。不满足的那一类
#  不是"配置问题"，是"设计上就不该接自动交易"，列进来只会误导。

VENUE_PROFILES: Dict[str, Capabilities] = {
    "binance": Capabilities(
        venue="binance",
        supports_client_order_id=True,
        client_order_id_param="newClientOrderId",   # 现货
        supports_query_by_client_id=True,           # GET /api/v3/order?origClientOrderId=
        order_visibility_lag_ms=1500,
        supports_reduce_only=False,                 # 现货没有这个概念
        supports_ws=True,
        funding_interval_hours=0,
        default_rules=SymbolRules(qty_step=Decimal("0.00001"),
                                  price_tick=Decimal("0.01"),
                                  min_qty=Decimal("0.00001"),
                                  min_notional=Decimal("5")),
        declared_from="binance spot api docs / ccxt markets",
    ),
    "binanceusdm": Capabilities(
        venue="binanceusdm",
        supports_client_order_id=True,
        client_order_id_param="newClientOrderId",   # 合约也叫这个
        supports_query_by_client_id=True,
        order_visibility_lag_ms=1500,
        supports_reduce_only=True,
        supports_ws=True,
        funding_interval_hours=8,
        default_rules=SymbolRules(qty_step=Decimal("0.001"),
                                  price_tick=Decimal("0.1"),
                                  min_qty=Decimal("0.001"),
                                  min_notional=Decimal("5")),
        declared_from="binance usdm futures docs / ccxt markets",
    ),
    "okx": Capabilities(
        venue="okx",
        supports_client_order_id=True,
        client_order_id_param="clOrdId",
        supports_query_by_client_id=True,           # GET /api/v5/trade/order?clOrdId=
        order_visibility_lag_ms=2000,
        supports_reduce_only=True,
        supports_ws=True,
        funding_interval_hours=8,
        default_rules=SymbolRules(qty_step=Decimal("0.0001"),
                                  price_tick=Decimal("0.1"),
                                  min_qty=Decimal("0.0001"),
                                  min_notional=Decimal("1")),
        declared_from="okx v5 api docs / ccxt markets",
    ),
    "bybit": Capabilities(
        venue="bybit",
        supports_client_order_id=True,
        client_order_id_param="orderLinkId",
        supports_query_by_client_id=True,           # /v5/order/realtime?orderLinkId=
        order_visibility_lag_ms=2500,
        supports_reduce_only=True,
        supports_ws=True,
        funding_interval_hours=8,
        default_rules=SymbolRules(qty_step=Decimal("0.001"),
                                  price_tick=Decimal("0.1"),
                                  min_qty=Decimal("0.001"),
                                  min_notional=Decimal("1")),
        declared_from="bybit v5 api docs / ccxt markets",
    ),
    "coinbase": Capabilities(
        venue="coinbase",
        supports_client_order_id=True,
        client_order_id_param="client_order_id",
        supports_query_by_client_id=True,
        order_visibility_lag_ms=3000,
        supports_reduce_only=False,
        supports_ws=True,
        funding_interval_hours=0,
        default_rules=SymbolRules(qty_step=Decimal("0.00000001"),
                                  price_tick=Decimal("0.01"),
                                  min_qty=Decimal("0.00000001"),
                                  min_notional=Decimal("1")),
        declared_from="coinbase advanced trade api docs",
    ),
}


def profile(venue: str) -> Capabilities:
    """取声明式档案。未知场所直接报错，不返回一个"什么都支持"的默认值。"""
    key = venue.strip().lower()
    caps = VENUE_PROFILES.get(key)
    if caps is None:
        raise CapabilityError(
            f"未知场所 {venue}。已声明：{', '.join(sorted(VENUE_PROFILES))}；"
            f"新场所必须先补能力矩阵，不能靠默认值上线")
    return caps


def all_admissions(max_order_notional: Optional[Decimal] = None) -> List[Admission]:
    """一眼看清"哪些场所现在就能自动交易"。运维面板和文档都从这儿取数。"""
    return [admit(c, max_order_notional=max_order_notional)
            for _, c in sorted(VENUE_PROFILES.items())]
