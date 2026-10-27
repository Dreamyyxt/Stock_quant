"""
QuantOS 2.0 — CCXT 适配器（T-501b）
====================================

CCXT 本身是一个外部依赖（`pip install ccxt`），而这一层刻意**不在核心包里
import 它**：`CcxtGateway` 只依赖一个"长得像 ccxt"的 transport 对象。

这么做的理由不是洁癖，是三条很实际的好处：

  1. 自测可以在完全离线的机器上跑（stub transport），CI 不会因为装不上
     ccxt 而变红，也不会因为某天网络抖动去查一次真实交易所而意外下单。
  2. 交易所 SDK 换版本、改字段、改异常类名是常态。变化被挡在 transport
     边界上，交易主链路不用跟着改。
  3. 想接非 CCXT 的场所（自建网关、券商 FIX）时，只要实现同样的 7 个方法。

真正 import ccxt 的地方只有 `build_ccxt_transport()` 一个函数，且是延迟导入。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from .contracts import D, OrderIntent, OrderState, OrderType, Side, utcnow
from .gateway import (
    AuthError, FundingPayment, GatewayError, RateLimited, TransportError,
    VenueBalance, VenueGateway, VenueOrder, VenuePosition, VenueRejected,
    normalize_state,
)
from .venues import Capabilities, CapabilityError, SymbolRules, profile

__all__ = [
    "CcxtGateway", "ProbeResult", "build_ccxt_transport",
    "gateway_for_venue", "translate_exception",
]

_ZERO = Decimal(0)

# --------------------------------------------------------------------------- #
#  异常翻译
# --------------------------------------------------------------------------- #
#  这里靠类名匹配，不 import ccxt —— ccxt 的异常层级随版本变动，而类名相对稳定。
#  若某天某个异常名改了，它会落到"未知异常"分支，被当成不确定处理：
#  表现为多查一次、拒绝重发。**这是刻意选择的失败方向。**

_AMBIGUOUS = frozenset({
    "NetworkError", "RequestTimeout", "ExchangeNotAvailable", "OnMaintenance",
    "DDoSProtection", "InvalidNonce", "TimeoutError", "ConnectionError",
    "ConnectionResetError", "RemoteDisconnected", "SSLError", "OSError",
    "ExchangeError",          # 场所回了无法解析的内容 —— 谁知道它做没做
})

_RATE_LIMITED = frozenset({"RateLimitExceeded", "TooManyRequests", "DDoSProtection"})

_REJECTED = frozenset({
    "InsufficientFunds", "InvalidOrder", "OrderImmediatelyFillable",
    "OrderNotFillable", "BadSymbol", "BadRequest", "BadResponse",
    "InvalidAddress", "ArgumentsRequired", "InsufficientMargin",
})

_AUTH = frozenset({
    "AuthenticationError", "PermissionDenied", "AccountSuspended",
    "InvalidApiKey", "SignatureError",
})

_UNSUPPORTED = frozenset({"NotSupported", "NotImplemented"})

#: 查询时表示"这笔单确实不存在"。与"查询失败"必须分开：
#: 前者是有效答案（回查未命中），后者是又一次"不知道"。
_NOT_FOUND = frozenset({"OrderNotFound", "OrderNotFoundByClientId", "OrderNotExist"})


def _exc_name(exc: BaseException) -> str:
    name = type(exc).__name__
    # ccxt 的异常都叫 XxxError，个别包装类会多套一层，取最像的那个
    for base in type(exc).__mro__:
        if base.__name__ in _AMBIGUOUS or base.__name__ in _REJECTED:
            return base.__name__
    return name


def translate_exception(exc: BaseException, *, during: str = "place") -> GatewayError:
    """
    把场所/SDK 异常翻译成本系统的错误分类。

    未知异常一律翻译成 `TransportError`（= 不确定）。
    这条规则听起来保守得过分，但反过来想：如果未知异常当"确定失败"，
    上层就会重发，而重发在交易所看来是一笔全新的订单。
    """
    name = _exc_name(exc)
    msg = f"{name}: {exc}"

    if name in _UNSUPPORTED:
        return CapabilityError(f"{during} 不受该场所支持：{msg}")
    if name in _AUTH:
        return AuthError(msg)
    if name in _RATE_LIMITED:
        return RateLimited(msg)
    if name in _REJECTED:
        return VenueRejected(msg)
    if name in _AMBIGUOUS:
        return TransportError(msg)
    return TransportError(f"未分类异常，按不确定处理：{msg}")


def _is_not_found(exc: BaseException) -> bool:
    return any(base.__name__ in _NOT_FOUND for base in type(exc).__mro__)


# --------------------------------------------------------------------------- #
#  实测结果
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ProbeResult:
    """
    `probe()` 的产出。

    刻意区分 `measured` 和 `unverified`：只实测了一部分就不算"已验证"。
    一个"半验证"的能力矩阵比没验证更危险，因为它看起来是绿的。
    """

    venue: str
    capabilities: Capabilities
    measured: Mapping[str, Any]
    unverified: Tuple[str, ...] = ()

    @property
    def fully_verified(self) -> bool:
        return not self.unverified

    def to_dict(self) -> Dict[str, Any]:
        return {
            "venue": self.venue,
            "fully_verified": self.fully_verified,
            "measured": dict(self.measured),
            "unverified": list(self.unverified),
        }


# --------------------------------------------------------------------------- #
#  网关
# --------------------------------------------------------------------------- #

class CcxtGateway(VenueGateway):
    """
    CCXT 风格场所网关。

    transport 需要提供（用不到的方法可以不实现，调用时才报错）：

        create_order(symbol, type, side, amount, price=None, params={}) -> dict
        fetch_order(id, symbol=None, params={})                          -> dict
        cancel_order(id, symbol=None, params={})                         -> dict
        fetch_open_orders(symbol=None, params={})                        -> list
        fetch_positions(symbols=None, params={})                         -> list
        fetch_balance(params={})                                         -> dict
        fetch_funding_history(symbol=None, since=None, limit=None)       -> list
        market(symbol)                                                   -> dict

    `amount` 用 float 传：CCXT 各场所对 str/float 的接受度不一，而数量已经按
    step 量化过，float 表示这些值不丢精度。
    """

    def __init__(self, transport: Any, capabilities: Capabilities, *,
                 max_order_notional: Optional[Decimal] = None,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self._t = transport
        self._caps = capabilities
        self._max_notional = D(max_order_notional) if max_order_notional is not None else None
        self._clock = clock
        self.place_calls = 0
        self.query_calls = 0

    # -- 元信息 ---------------------------------------------------------- #

    @property
    def name(self) -> str:
        return self._caps.venue

    @property
    def capabilities(self) -> Capabilities:
        return self._caps

    # -- 下单 ------------------------------------------------------------ #

    def place(self, intent: OrderIntent) -> VenueOrder:
        caps = self._caps
        if intent.venue != caps.venue:
            raise CapabilityError(
                f"意图场所 {intent.venue} 与网关 {caps.venue} 不一致")

        side_verb = "buy" if intent.side is Side.BUY else "sell"
        price = D(intent.limit_price) if intent.limit_price is not None else None

        # 本地量化 + 边界校验：不通过就在这里停住，一个字节都不发出去
        check = caps.validate_order(
            intent.symbol, intent.side, intent.quantity, price,
            notional=D(intent.notional), max_order_notional=self._max_notional)

        if intent.reduce_only and not caps.supports_reduce_only:
            raise CapabilityError(
                f"{caps.venue} 不支持 reduce-only，无法安全表达「只减不加」的意图")

        if not caps.supports_client_order_id:
            raise CapabilityError(f"{caps.venue} 不支持客户端订单号")

        params: Dict[str, Any] = {caps.client_order_id_param: intent.client_order_id}
        if intent.reduce_only:
            params["reduceOnly"] = True
        if intent.order_type is OrderType.LIMIT:
            params.setdefault("timeInForce", "GTC")

        order_type = "limit" if intent.order_type is OrderType.LIMIT else "market"
        self.place_calls += 1
        try:
            raw = self._t.create_order(
                intent.symbol, order_type, side_verb, float(check.qty),
                float(check.price) if check.price is not None else None, params)
        except BaseException as exc:                              # noqa: BLE001
            raise translate_exception(exc, during="下单") from exc

        if not isinstance(raw, Mapping):
            raise TransportError(f"下单响应不是对象：{type(raw).__name__}")
        venue_order_id = str(raw.get("id") or "")
        if not venue_order_id:
            # 场所没给 orderId。有幂等键还能回查，但此刻状态就是"不知道"。
            raise TransportError(
                f"{caps.venue} 下单响应缺少 order id（client_order_id "
                f"{intent.client_order_id} 已发出）—— 状态不确定，需回查")

        return self._to_order(raw, fallback_cid=intent.client_order_id)

    # -- 查询 ------------------------------------------------------------ #

    def fetch_order(self, symbol: str, client_order_id: str) -> Optional[VenueOrder]:
        """
        按幂等键回查。

        返回 None 与抛异常的区别很重要：
          · None      = 查过了，这个键在场所不存在（回查未命中，是有效答案）
          · 抛异常    = 这次查询本身失败（又一次"不知道"）
        """
        caps = self._caps
        if not caps.supports_query_by_client_id:
            raise CapabilityError(f"{caps.venue} 不支持按客户端订单号查询")
        self.query_calls += 1
        try:
            raw = self._t.fetch_order(
                client_order_id, symbol, {caps.client_order_id_param: client_order_id})
        except BaseException as exc:                              # noqa: BLE001
            if _is_not_found(exc):
                return None
            raise translate_exception(exc, during="回查") from exc
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise TransportError(f"回查响应不是对象：{type(raw).__name__}")
        return self._to_order(raw, fallback_cid=client_order_id)

    def cancel(self, symbol: str, client_order_id: str) -> VenueOrder:
        caps = self._caps
        self.query_calls += 1
        try:
            raw = self._t.cancel_order(
                client_order_id, symbol, {caps.client_order_id_param: client_order_id})
        except BaseException as exc:                              # noqa: BLE001
            raise translate_exception(exc, during="撤单") from exc
        if isinstance(raw, Mapping) and raw.get("id"):
            return self._to_order(raw, fallback_cid=client_order_id)
        # 撤单响应常常只有 {"status": "ok"}，状态以回查为准
        found = self.fetch_order(symbol, client_order_id)
        if found is None:
            raise TransportError(f"撤单后回查不到 {client_order_id}，状态不确定")
        return found

    def fetch_open_orders(self, symbol: Optional[str] = None) -> List[VenueOrder]:
        try:
            rows = self._t.fetch_open_orders(symbol, {}) or []
        except BaseException as exc:                              # noqa: BLE001
            raise translate_exception(exc, during="查询挂单") from exc
        return [self._to_order(r) for r in rows
                if isinstance(r, Mapping) and r.get("id")]

    # -- 账户 ------------------------------------------------------------ #

    def fetch_balance(self) -> List[VenueBalance]:
        try:
            raw = self._t.fetch_balance({}) or {}
        except BaseException as exc:                              # noqa: BLE001
            raise translate_exception(exc, during="查询余额") from exc
        total = raw.get("total") or {}
        free = raw.get("free") or {}
        used = raw.get("used") or {}
        out: List[VenueBalance] = []
        for ccy in sorted(set(total) | set(free) | set(used)):
            t = D(total.get(ccy) or 0)
            f = D(free.get(ccy) or 0)
            u = D(used.get(ccy) or 0)
            if t == 0 and f == 0 and u == 0:
                continue
            out.append(VenueBalance(venue=self._caps.venue, currency=str(ccy),
                                    free=f, used=u))
        return out

    def fetch_positions(self) -> List[VenuePosition]:
        try:
            rows = self._t.fetch_positions(None, {}) or []
        except BaseException as exc:                              # noqa: BLE001
            raise translate_exception(exc, during="查询持仓") from exc
        out: List[VenuePosition] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            contracts = D(row.get("contracts") or 0)
            if contracts == 0:
                continue
            side = str(row.get("side") or "").lower()
            signed = -contracts if side == "short" else contracts
            out.append(VenuePosition(
                venue=self._caps.venue, symbol=str(row.get("symbol") or ""),
                qty=signed,
                entry_price=_opt_dec(row.get("entryPrice")),
                mark_price=_opt_dec(row.get("markPrice")),
                realized=_opt_dec(row.get("realizedPnl")),
                raw=dict(row),
            ))
        return out

    def fetch_funding_payments(self, since: Optional[datetime] = None
                               ) -> List[FundingPayment]:
        """
        取已结算的资金费。

        ⚠️ **符号陷阱**：CCXT 的 `amount` 是现金流方向（付出为负），
        账本 `post_funding` 的约定是费用方向（支出为正）。必须取反。
        这个符号搞反的后果非常隐蔽：账本对得上券商账单总额（两笔都反了），
        但归因里的 funding 项和实际方向相反，等于把一个稳定的成本记成了收益。

        `since` 用毫秒时间戳传（CCXT 统一口径）。
        """
        caps = self._caps
        if not caps.funding_interval_hours:
            raise CapabilityError(f"{caps.venue} 无资金费（现货场所）")
        fn = getattr(self._t, "fetch_funding_history", None)
        if fn is None:
            raise CapabilityError(f"{caps.venue} 的 transport 未实现 fetch_funding_history")
        since_ms = int(since.timestamp() * 1000) if since else None
        try:
            rows = fn(None, since_ms, 200) or []
        except BaseException as exc:                              # noqa: BLE001
            raise translate_exception(exc, during="查询资金费") from exc

        out: List[FundingPayment] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            cash_flow = D(row.get("amount") or 0)      # 付出为负
            if cash_flow == 0:
                continue
            ts = _ts_of(row)
            info = row.get("info") or {}
            pid = str(row.get("id") or info.get("tranId") or info.get("id") or "")
            symbol = str(row.get("symbol") or info.get("symbol") or "")
            payment = FundingPayment(
                venue=caps.venue, symbol=symbol,
                amount=-cash_flow,                     # ← 现金流方向取反成费用方向
                currency=str(row.get("currency") or info.get("asset") or ""),
                rate=D(row.get("rate") or info.get("fundingRate") or 0),
                ts=ts, payment_id=pid or f"{symbol}-{int(ts.timestamp())}",
                interval_hours=caps.funding_interval_hours,
            )
            out.append(payment)
        return sorted(out, key=lambda p: p.ts)

    # -- 实测 ------------------------------------------------------------ #

    def probe(self, symbol: str) -> ProbeResult:
        """
        用真实元数据覆盖声明值。

        只测能测的：`market()` 里有步长、最小量、最小名义。
        `order_visibility_lag_ms` 和"能否按客户端单号回查"必须靠一次真实下单
        才能验证 —— 这里**不假装测过**，把没测的如实列进 `unverified`。
        """
        fn = getattr(self._t, "market", None)
        if fn is None:
            return ProbeResult(self.name, self._caps, {},
                               ("全部（transport 未实现 market()）",))
        try:
            mkt = fn(symbol) or {}
        except BaseException as exc:                              # noqa: BLE001
            raise translate_exception(exc, during="读取市场元数据") from exc

        measured: Dict[str, Any] = {}
        rules = self._caps.rules_for(symbol)
        limits = (mkt.get("limits") or {})
        precision = (mkt.get("precision") or {})

        amount_min = D(((limits.get("amount") or {}).get("min")) or 0)
        cost_min = D(((limits.get("cost") or {}).get("min")) or 0)
        step = D(precision.get("amount") or 0)
        tick = D(precision.get("price") or 0)

        new_rules = rules
        if step > 0:
            new_rules = replace(new_rules, qty_step=step, min_qty=amount_min or new_rules.min_qty)
            measured["qty_step"] = str(step)
        if tick > 0:
            new_rules = replace(new_rules, price_tick=tick)
            measured["price_tick"] = str(tick)
        if amount_min > 0:
            new_rules = replace(new_rules, min_qty=amount_min)
            measured["min_qty"] = str(amount_min)
        if cost_min > 0:
            new_rules = replace(new_rules, min_notional=cost_min)
            measured["min_notional"] = str(cost_min)

        unverified: List[str] = []
        if step <= 0:
            unverified.append("qty_step（market() 未给数量步长）")
        if tick <= 0:
            unverified.append("price_tick（market() 未给报价精度）")
        if cost_min <= 0:
            unverified.append("min_notional（market() 未给最小名义）")
        if self._caps.supports_query_by_client_id:
            unverified.append(
                "supports_query_by_client_id（需真实下单后超时回查才能验证）")
        unverified.append(
            f"order_visibility_lag_ms（声明 {self._caps.order_visibility_lag_ms}ms，需实测）")

        caps = self._caps.with_rules(symbol, new_rules)
        # 只有步长/精度/最小额都测到了，才允许标记 verified —— 半步验证更危险
        if not (step <= 0 or tick <= 0 or cost_min <= 0):
            caps = caps.mark_verified(self._clock())
        return ProbeResult(self.name, caps, measured, tuple(unverified))

    # -- 内部 ------------------------------------------------------------ #

    def _to_order(self, raw: Mapping[str, Any],
                  fallback_cid: str = "") -> VenueOrder:
        caps = self._caps
        quantity = D(raw.get("amount") or 0)
        filled = D(raw.get("filled") or 0)
        raw_state = str(raw.get("status") or (raw.get("info") or {}).get("status") or "")
        state = normalize_state(raw_state, filled, quantity if quantity > 0 else None)
        cid = str(raw.get("clientOrderId") or "")
        if not cid:
            params = (raw.get("info") or {})
            cid = str(params.get(caps.client_order_id_param)
                      or params.get("clientOrderId") or fallback_cid)
        return VenueOrder(
            venue=caps.venue,
            venue_order_id=str(raw.get("id") or ""),
            client_order_id=cid,
            symbol=str(raw.get("symbol") or ""),
            side=Side.BUY if str(raw.get("side") or "").lower() == "buy" else Side.SELL,
            quantity=quantity if quantity > 0 else Decimal("0.00000001"),
            filled_qty=filled,
            state=state,
            raw_state=raw_state,
            avg_price=_opt_dec(raw.get("average") or raw.get("price")),
            ts=_ts_of(raw),
        )


# --------------------------------------------------------------------------- #
#  小工具与装配
# --------------------------------------------------------------------------- #

def _opt_dec(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        d = D(value)
    except Exception:                                            # noqa: BLE001
        return None
    return d if d > 0 else None


def _ts_of(row: Mapping[str, Any]) -> datetime:
    ts = row.get("timestamp")
    if ts:
        return datetime.fromtimestamp(int(ts) / 1000.0, tz=utcnow().tzinfo)
    raw = (row.get("datetime") or "")
    if raw:
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            pass
    return utcnow()


def build_ccxt_transport(exchange_id: str, *, api_key: Optional[str] = None,
                         secret: Optional[str] = None,
                         password: Optional[str] = None,
                         sandbox: bool = False,
                         options: Optional[Mapping[str, Any]] = None) -> Any:
    """
    这里才是唯一 import ccxt 的地方，而且延迟导入。

    没装 ccxt 时给一句能照着做的报错，不静默降级成假网关 ——
    静默降级的后果是"以为接上了，其实在下模拟单"，这比直接报错危险得多。
    """
    try:
        import ccxt  # type: ignore  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "未安装 ccxt。核心包保持零依赖，接真实场所才需要它：\n"
            "    /path/to/.venv/bin/pip install ccxt\n"
            "（自测与回放不需要 ccxt，用 stub transport 即可）") from exc

    cls = getattr(ccxt, exchange_id, None)
    if cls is None:
        raise ImportError(f"ccxt 不认识交易所 {exchange_id}")

    conf: Dict[str, Any] = {"enableRateLimit": True, "timeout": 15000}
    if api_key:
        conf["apiKey"] = api_key
    if secret:
        conf["secret"] = secret
    if password:
        conf["password"] = password
    if options:
        conf["options"] = dict(options)
    transport = cls(conf)
    if sandbox and hasattr(transport, "set_sandbox_mode"):
        transport.set_sandbox_mode(True)
    return transport


def gateway_for_venue(transport: Any, venue: str, *,
                      max_order_notional: Optional[Decimal] = None) -> CcxtGateway:
    """按声明档案装配网关。准入不过就直接抛，不让"接上了但永远下不出单"的状态存在。"""
    caps = profile(venue)
    return CcxtGateway(transport, caps, max_order_notional=max_order_notional)
