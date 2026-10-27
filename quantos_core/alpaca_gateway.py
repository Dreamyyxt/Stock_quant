"""
QuantOS 2.0 — Alpaca 纸面网关（T-1101 ~ T-1103，B11）
====================================================

B10 之后整条链路只有一处没被真实检验：**真下单**。

`PaperBroker` 是个诚实的假券商 —— 它会立刻成交、从不拒单、从不超时。
于是 B2 之后写的所有防线（幂等键、超时只查不重发、部分成交、对账不平）
在它面前全是"代码看起来对"。这个模块把同一套 `VenueGateway` 接口接到
一个**真会拒绝、真会排队、真会部分成交**的地方。

为什么是 Alpaca 纸面账户而不是直接上老虎/币安
------------------------------------------------
纸面账户有真撮合、真回报、真错误码，但**没有真钱**。它正好能验证
"网关这一层对不对"，而验证错一次的代价是 0。真钱那一步等这一层被证明
之后再说（B11 只做这一层，铁律：fail-closed 不等于 fail-brave）。

三条实测出来的事实（文档没写、但决定了这个适配器的形状）
----------------------------------------------------------
1. **`DELETE /v2/orders:by_client_order_id` 不存在。**
   文档里下单有 client_order_id 维度，撤单却只认场所自己的 order_id。
   所以 `cancel()` 必须先 `fetch_order()` 换出 order_id 再撤 ——
   这也意味着「撤单」天然是两步，中间可能落在"单子刚成交"的缝里。
2. **重复的 client_order_id 返回 422，不是 409。**
   和"数量非法"同一个状态码，只能靠 message 里的 `client_order_id`
   + `unique` 区分。把它当普通 422 拒绝掉，等于把「这笔单已经存在」
   误判成「这笔单没下出去」—— 然后上层就会重发。
3. **`done_for_day` 是当日单的终点，可能带着部分成交。**
   我们的 `OrderState` 没有"部分成交后被撤"这一档，所以映射成
   `CANCELLED` + 保留 `filled_qty`。映射成 `EXPIRED` 会让 OMS 以为
   还有残量要等，映射成 `PARTIALLY_FILLED` 会让它永远不终态。

三条都不在文档正中间，但三条都会在真钱上变成事故。这就是 B11 存在的理由。

设计边界
--------
- **零依赖**：Alpaca 是 REST + JSON，`urllib` 就够。不装 SDK，
  也不 import 任何第三方包 —— 和 `ccxt_gateway` 同一个理由：
  SDK 换版本、改字段是常态，变化必须被挡在 transport 边界上。
- **端点白名单硬编码**：只允许纸面。真实端点需要两个开关同时打开
  （环境变量 + 代码参数），少一个就抛。凭据躺在 `.env` 里，
  一个 URL 拼写错误就会打到真钱账户 —— 这种错误必须在下单前就断掉，
  不能靠"我记得改回来"。
- **单笔名义上限**：`place()` 在发请求之前就核对 `intent.notional`，
  超过上限直接 `CapabilityError`。网关是最后一个能拦住胖手指的地方。
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .contracts import D, OrderIntent, OrderState, OrderType, Side, utcnow
from .gateway import (
    AuthError, FundingPayment, GatewayError, RateLimited, TransportError,
    VenueBalance, VenueGateway, VenueOrder, VenuePosition, VenueRejected,
    normalize_state,
)
from .venues import Capabilities, CapabilityError, SymbolRules, profile

__all__ = [
    "PAPER_BASE", "LIVE_BASE", "DATA_BASE", "EndpointRefused", "AlpacaCredentials",
    "AlpacaTransport", "UrllibTransport", "AlpacaGateway", "ProbeResult",
    "resolve_base", "build_gateway_from_env",
]

_ZERO = Decimal(0)

#: 只认这两个 host，别的一律拒绝。故意用常量而不是配置项：
#: 一旦允许"自定义 base_url"，这台机器上任何一个拼错的 URL 都能把
#: 纸面凭据打到真钱端点，而两边的凭据前缀长得一模一样。
PAPER_BASE = "https://paper-api.alpaca.markets"
LIVE_BASE = "https://api.alpaca.markets"
_DATA_BASE = "https://data.alpaca.markets"

#: 行情与交易是两套 host。行情单独放行，但只用于 GET —— 它没有下单能力，
#: 泄不出去钱，而"下单前不知道价格"才是真的危险（名义额无从核对）。
DATA_BASE = _DATA_BASE
_ALLOWED_BASES = {PAPER_BASE: "paper", LIVE_BASE: "live"}

#: 真实端点需要同时满足的两个开关。缺一个就抛。
LIVE_ENV_SWITCH = "QUANTOS_ALPACA_LIVE"

_ENV_LOADED = False


def _ensure_env() -> None:
    """
    读一次 `config/.env`（KEY=VALUE，不覆盖已有环境变量）。

    为什么放在这里而不是"让调用方记得 import config.settings"：
    网关会被 cron、运维面、联调脚本三个入口分别构造，任何一个忘了载入
    就会得到"缺少凭据"。那种失败看起来像配置错误，实际是入口不一致 ——
    而入口不一致迟早会变成"运维面显示未接入，但 cron 其实在下单"。

    文件不存在时什么都不做（凭据也可能来自真实环境变量）。
    """
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "config", ".env")
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if key:
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


class EndpointRefused(CapabilityError):
    """端点不在白名单里。继承 CapabilityError：本地就能判定的错，不该发请求。"""


def resolve_base(base: str = "", *, allow_live: bool = False) -> Tuple[str, str]:
    """
    归一化端点，并判定这是纸面还是真钱。

    真钱需要 `QUANTOS_ALPACA_LIVE=1`（环境）**并且** `allow_live=True`（代码）。
    两个都在场才放行：只靠环境变量的话，某个 shell 里 export 过一次就会一直
    export；只靠代码参数的话，某次复制粘贴改错就把默认值改成了真钱。
    """
    raw = (base or PAPER_BASE).strip().rstrip("/")
    mode = _ALLOWED_BASES.get(raw)
    if mode is None:
        raise EndpointRefused(
            f"拒绝连接 {raw}：Alpaca 只允许 {PAPER_BASE}（纸面）"
            f"或 {LIVE_BASE}（真钱，需双开关）。"
            f"自定义端点一律拒绝 —— 拼错一个字母就是真钱账户")
    if mode == "live":
        env_on = os.getenv(LIVE_ENV_SWITCH, "").strip() in ("1", "true", "yes")
        if not (env_on and allow_live):
            raise EndpointRefused(
                f"拒绝连接真实端点 {LIVE_BASE}：需要同时满足 "
                f"{LIVE_ENV_SWITCH}=1 与 allow_live=True"
                f"（当前 env={env_on}，参数={allow_live}）")
    return raw, mode


# --------------------------------------------------------------------------- #
#  凭据
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class AlpacaCredentials:
    """
    纸面/真钱凭据。**不回显 secret** —— 它会被写进日志和运维快照。
    """

    key_id: str
    secret: str = field(repr=False)
    base: str = PAPER_BASE
    mode: str = "paper"

    @classmethod
    def from_env(cls, *, base: str = "", allow_live: bool = False) -> "AlpacaCredentials":
        _ensure_env()
        key = os.getenv("ALPACA_API_KEY", "").strip()
        secret = os.getenv("ALPACA_API_SECRET", "").strip()
        missing = [n for n, v in (("ALPACA_API_KEY", key),
                                  ("ALPACA_API_SECRET", secret)) if not v]
        if missing:
            raise AuthError(
                "缺少 Alpaca 凭据：" + ", ".join(missing) +
                "（放 config/.env 或环境变量；缺失时拒绝连接，不用空凭据试）")
        resolved, mode = resolve_base(base, allow_live=allow_live)
        return cls(key_id=key, secret=secret, base=resolved, mode=mode)

    @property
    def masked_key(self) -> str:
        return (self.key_id[:4] + "***") if self.key_id else "(空)"

    def redacted(self) -> Dict[str, Any]:
        return {"base": self.base, "mode": self.mode, "key": self.masked_key}


# --------------------------------------------------------------------------- #
#  Transport（唯一碰网络的地方）
# --------------------------------------------------------------------------- #

class AlpacaTransport:
    """
    HTTP 传输层协议。

    约定：**只对"网络本身出问题"抛异常**（`TransportError`）；
    HTTP 状态码一律原样返回 `(status, payload)`，由网关决定它是什么错误。

    这么切是因为"422 里哪一种是重复单"这种判定属于场所语义，
    而"超时算不算不确定"属于传输语义。混在一层里，自测就没法只测一半。
    """

    def request(self, method: str, path: str, *,
                params: Optional[Mapping[str, Any]] = None,
                body: Optional[Mapping[str, Any]] = None) -> Tuple[int, Any]:
        raise NotImplementedError


class UrllibTransport(AlpacaTransport):
    """
    纯 stdlib 实现。默认**直连**（不走环境代理）。

    为什么直连：这类进程跑在 cron 里，环境里可能挂着给别的服务用的代理；
    券商 API 走代理除了多一跳，还会让"超时"变得不可归因 ——
    而"超时"在本项目里等于"不知道下没下出去"，最不该有多种可能。
    需要代理时用 `QUANTOS_ALPACA_PROXY` 显式指定。
    """

    def __init__(self, base: str, credentials: AlpacaCredentials, *,
                 timeout: float = 15.0,
                 proxy: Optional[str] = None,
                 sleeper: Callable[[float], None] = time.sleep) -> None:
        self.base = base.rstrip("/")
        self.credentials = credentials
        self.timeout = timeout
        self._sleeper = sleeper
        proxy = proxy if proxy is not None else os.getenv("QUANTOS_ALPACA_PROXY", "")
        handlers: List[Any] = [urllib.request.ProxyHandler(
            {"http": proxy, "https": proxy} if proxy else {})]
        self._opener = urllib.request.build_opener(*handlers)

    def request(self, method: str, path: str, *,
                params: Optional[Mapping[str, Any]] = None,
                body: Optional[Mapping[str, Any]] = None) -> Tuple[int, Any]:
        url = self.base + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += ("&" if "?" in url else "?") + urllib.parse.urlencode(clean)
        data = None
        headers = {
            "APCA-API-KEY-ID": self.credentials.key_id,
            "APCA-API-SECRET-KEY": self.credentials.secret,
            "accept": "application/json",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["content-type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return resp.status, self._decode(resp.read(), resp.status)
        except urllib.error.HTTPError as exc:
            # 4xx/5xx 都是"场所说话了"，交给网关分类
            return exc.code, self._decode(exc.read(), exc.code)
        except (socket.timeout, TimeoutError) as exc:
            raise TransportError(
                f"{method} {path} 超时（{self.timeout}s）：请求可能已经到达场所 "
                f"—— 这是「不知道」，不是「失败」") from exc
        except urllib.error.URLError as exc:
            raise TransportError(f"{method} {path} 网络故障：{exc.reason}") from exc
        except (ConnectionError, OSError) as exc:
            raise TransportError(f"{method} {path} 连接故障：{exc}") from exc

    @staticmethod
    def _decode(raw: bytes, status: int) -> Any:
        text = (raw or b"").decode("utf-8", errors="replace").strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # 200 但返回非 JSON：场所改协议了，或者被中间件劫持
            if 200 <= status < 300:
                raise TransportError(
                    f"响应不是合法 JSON（HTTP {status}）：{text[:120]}")
            return {"message": text[:300]}


# --------------------------------------------------------------------------- #
#  错误分类
# --------------------------------------------------------------------------- #

def _message_of(payload: Any) -> str:
    if isinstance(payload, Mapping):
        msg = payload.get("message") or payload.get("error") or ""
        return str(msg)
    return str(payload or "")


def _is_duplicate_client_order_id(status: int, message: str) -> bool:
    """
    Alpaca 用 422 同时表达"重复单"和"参数非法"，只能看 message。

    这不是我们偷懒 —— 是场所没给可判定的错误码，而这两种情况的处理方式
    完全相反（一个继续查、一个立即放弃）。宁可多写这四行，也不能让
    一次重复提交被当成拒单，然后上层重发。
    """
    low = (message or "").lower()
    return status == 422 and "client_order_id" in low and "unique" in low


def _raise_for_status(status: int, payload: Any, context: str) -> None:
    """把 HTTP 状态码翻译成"确定/不确定"两类异常。未知状态一律按不确定。"""
    if 200 <= status < 300:
        return
    message = _message_of(payload)
    if status in (401, 403):
        # fail-closed：凭据问题不允许被当成"偶发"重试
        raise AuthError(f"{context} 鉴权失败（HTTP {status}）：{message}")
    if status == 429:
        raise RateLimited(f"{context} 被限流（HTTP 429）：{message}")
    if status >= 500:
        raise TransportError(f"{context} 场所 5xx（HTTP {status}）：{message}")
    if status in (400, 404, 405, 422):
        raise VenueRejected(f"{context} 场所拒绝（HTTP {status}）：{message}")
    # 其它状态码：没见过，就按"不确定"处理。多查一次，绝不重发。
    raise TransportError(f"{context} 未知 HTTP {status}：{message}")


# --------------------------------------------------------------------------- #
#  状态映射（场所方言 → 契约状态机）
# --------------------------------------------------------------------------- #

#: 只放 Alpaca 独有的词。其余仍走共享的 `normalize_state`，
#: 保证"共享表怎么变，其它场所跟着变，Alpaca 不会偷偷不一样"。
_ALPACA_STATE: Dict[str, OrderState] = {
    "accepted": OrderState.ACCEPTED,           # Alpaca 有，共享表没有
    "pending_new": OrderState.ACCEPTED,
    "accepted_for_bidding": OrderState.ACCEPTED,
    "held": OrderState.ACCEPTED,               # 部分成交后被 held，仍在场
    "pending_replace": OrderState.PENDING_CANCEL,
    "replaced": OrderState.CANCELLED,          # 被替换掉的旧单不再有效
    "stopped": OrderState.UNKNOWN,             # 止损单未触发：状态不可判定
    "suspended": OrderState.UNKNOWN,
    "calculated": OrderState.UNKNOWN,
}


def to_order_state(raw: Any, filled_qty: Decimal = _ZERO,
                   quantity: Optional[Decimal] = None) -> OrderState:
    """
    Alpaca 状态词 → 契约状态。两条场所特有的规矩：

      · `done_for_day`：当日单的终点，可能带着部分成交。映射 `CANCELLED`
        并保留 `filled_qty` —— 我们的枚举没有"部分成交后被撤"，
        映射 `PARTIALLY_FILLED` 会让 OMS 永远不终态，`EXPIRED` 会让它等残量。
      · `filled` 但成交量为 0 → 交给 `normalize_state` 的防御逻辑判 `CANCELLED`
        （场所先报终态、成交明细后到，是这个行业的常态）。
    """
    key = str(raw or "").strip().lower().replace("-", "_")
    if key == "done_for_day":
        return OrderState.CANCELLED
    mapped = _ALPACA_STATE.get(key)
    if mapped is OrderState.ACCEPTED:
        return OrderState.ACCEPTED
    if mapped is not None and mapped is not OrderState.ACCEPTED:
        if mapped is OrderState.CANCELLED and D(filled_qty) > 0 \
                and quantity is not None and D(filled_qty) < D(quantity):
            # 撤单但成交了一部分：敞口是真实的，状态也得是"已撤"
            return OrderState.CANCELLED
        return mapped
    return normalize_state(key, filled_qty, quantity)


def _parse_order(raw: Mapping[str, Any], venue: str) -> VenueOrder:
    qty = D(raw.get("qty") or 0)
    filled = D(raw.get("filled_qty") or 0)
    avg_raw = raw.get("filled_avg_price")
    avg = D(avg_raw) if avg_raw not in (None, "", "0", "0.0") else None
    side_raw = str(raw.get("side") or "").strip().lower()
    side = Side.BUY if side_raw == "buy" else Side.SELL
    status_raw = str(raw.get("status") or "")
    ts = _parse_ts(raw.get("submitted_at") or raw.get("created_at"))
    return VenueOrder(
        venue=venue,
        venue_order_id=str(raw.get("id") or ""),
        client_order_id=str(raw.get("client_order_id") or ""),
        symbol=str(raw.get("symbol") or ""),
        side=side,
        quantity=qty,
        filled_qty=filled,
        state=to_order_state(status_raw, filled, qty if qty > 0 else None),
        raw_state=status_raw,
        avg_price=avg,
        ts=ts,
        reason=str(raw.get("reject_reason") or ""),
    )


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_position(raw: Mapping[str, Any], venue: str) -> VenuePosition:
    # Alpaca 的 qty 本身就是带符号的字符串（空头为负），另给 side 字段。
    # 这里以 qty 为准，side 只用来做一致性校验 —— 有的账户 json 里
    # qty 是正数而 side 是 short，两边都信会算出多空相反的持仓。
    qty = D(raw.get("qty") or 0)
    return VenuePosition(
        venue=venue,
        symbol=str(raw.get("symbol") or ""),
        qty=qty,
        entry_price=_opt_dec(raw.get("avg_entry_price")),
        mark_price=_opt_dec(raw.get("current_price")),
        realized=None,
        raw=dict(raw),
    )


def _opt_dec(value: Any) -> Optional[Decimal]:
    if value in (None, "", "0", "0.0"):
        return None
    return D(value)


# --------------------------------------------------------------------------- #
#  探针结果
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ProbeResult:
    """一次只读探针的结论。`ok=False` 时 `detail` 必须能定位到哪一步断的。"""

    ok: bool
    mode: str
    detail: str
    account: Mapping[str, Any] = field(default_factory=dict)
    clock: Mapping[str, Any] = field(default_factory=dict)
    rules: Optional[SymbolRules] = None
    query_by_client_id_ok: bool = False
    query_latency_ms: int = 0
    notes: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok, "mode": self.mode, "detail": self.detail,
            "account": dict(self.account), "clock": dict(self.clock),
            "query_by_client_id_ok": self.query_by_client_id_ok,
            "query_latency_ms": self.query_latency_ms,
            "rules": None if self.rules is None else {
                "qty_step": str(self.rules.qty_step),
                "price_tick": str(self.rules.price_tick),
                "min_qty": str(self.rules.min_qty),
                "min_notional": str(self.rules.min_notional),
            },
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------- #
#  网关
# --------------------------------------------------------------------------- #

class AlpacaGateway(VenueGateway):
    """
    美股网关（Alpaca）。只做翻译与守卫，不做任何交易判断。

    几个刻意的默认值：

      · `require_market_open=True`：收盘后 `place()` 直接拒。
        Alpaca 会把 day 单排到下一个开盘，这在纸面上看不出问题，
        在真钱上就是"我说的 14:00 下单变成了明早 09:30"。
      · `max_order_notional`：发请求前的最后一道闸。默认 1000，
        可用 `QUANTOS_ALPACA_MAX_NOTIONAL` 覆盖。
      · `reduce_only` 一律拒：美股现货没有这个概念，静默忽略等于
        把"只许减仓"变成一个可以加仓的单。
    """

    VENUE = "alpaca"
    DEFAULT_MAX_NOTIONAL = Decimal("1000")

    def __init__(self, transport: AlpacaTransport, credentials: AlpacaCredentials, *,
                 capabilities: Optional[Capabilities] = None,
                 max_order_notional: Optional[Decimal] = None,
                 require_market_open: bool = True,
                 clock_ttl_sec: float = 20.0,
                 account_ttl_sec: float = 30.0,
                 data_transport: Optional[AlpacaTransport] = None,
                 feed: Optional[str] = None,
                 now: Callable[[], datetime] = utcnow) -> None:
        self.transport = transport
        self.credentials = credentials
        self._caps = capabilities or profile(self.VENUE)
        self._now = now
        self._clock_ttl = float(clock_ttl_sec)
        self._account_ttl = float(account_ttl_sec)
        self._clock_cache: Optional[Tuple[float, Mapping[str, Any]]] = None
        self._account_cache: Optional[Tuple[float, Mapping[str, Any]]] = None
        self._monotonic = time.monotonic
        self._data_transport = data_transport
        self._feed = feed if feed is not None else os.getenv("ALPACA_FEED", "iex").strip()
        env_cap = os.getenv("QUANTOS_ALPACA_MAX_NOTIONAL", "").strip()
        if max_order_notional is not None:
            self.max_order_notional = D(max_order_notional)
        elif env_cap:
            self.max_order_notional = D(env_cap)
        else:
            self.max_order_notional = self.DEFAULT_MAX_NOTIONAL
        self.require_market_open = bool(require_market_open)

    # -- 身份 ------------------------------------------------------------ #

    @property
    def name(self) -> str:
        return self.VENUE

    @property
    def capabilities(self) -> Capabilities:
        return self._caps

    @property
    def mode(self) -> str:
        return self.credentials.mode

    # -- 只读端点 -------------------------------------------------------- #

    def account(self, *, fresh: bool = False) -> Mapping[str, Any]:
        if not fresh and self._account_cache is not None:
            ts, payload = self._account_cache
            if self._monotonic() - ts < self._account_ttl:
                return payload
        status, payload = self.transport.request("GET", "/v2/account")
        _raise_for_status(status, payload, "读取账户")
        if not isinstance(payload, Mapping):
            raise TransportError(f"账户响应不是对象：{type(payload).__name__}")
        self._account_cache = (self._monotonic(), payload)
        return payload

    def clock(self, *, fresh: bool = False) -> Mapping[str, Any]:
        if not fresh and self._clock_cache is not None:
            ts, payload = self._clock_cache
            if self._monotonic() - ts < self._clock_ttl:
                return payload
        status, payload = self.transport.request("GET", "/v2/clock")
        _raise_for_status(status, payload, "读取交易时钟")
        if not isinstance(payload, Mapping):
            raise TransportError(f"时钟响应不是对象：{type(payload).__name__}")
        self._clock_cache = (self._monotonic(), payload)
        return payload

    def market_open(self) -> bool:
        """
        真实开市状态。

        取不到时钟时**抛异常**，不返回 False 也不返回 True：
        返回 False 会让今天的交易静默消失，返回 True 会往收盘的市场里下单。
        两种都是"看起来正常"的事故。
        """
        return bool(self.clock().get("is_open"))

    def asset(self, symbol: str) -> Mapping[str, Any]:
        status, payload = self.transport.request("GET", f"/v2/assets/{symbol}")
        _raise_for_status(status, payload, f"读取标的 {symbol}")
        if not isinstance(payload, Mapping):
            raise TransportError(f"标的信息不是对象：{type(payload).__name__}")
        return payload

    def rules_for(self, symbol: str) -> SymbolRules:
        """
        用**真实标的属性**生成下单约束，不用猜测值。

        整股还是可以碎股，直接决定 `qty_step`：拿默认的 0.00001 去下整股单，
        数量校验永远通过，然后每一单都被场所按"非整数股"拒掉 —— 这叫假接通。
        """
        info = self.asset(symbol)
        if not info.get("tradable", False):
            raise CapabilityError(
                f"{symbol} 不可交易（tradable={info.get('tradable')}，"
                f"status={info.get('status')}）")
        fractional = bool(info.get("fractionable")) and self._caps.supports_fractional
        if fractional:
            rules = SymbolRules(qty_step=Decimal("0.000001"),
                                price_tick=Decimal("0.01"),
                                min_qty=Decimal("0.000001"),
                                min_notional=Decimal("1"))
        else:
            rules = SymbolRules(qty_step=Decimal("1"),
                                price_tick=Decimal("0.01"),
                                min_qty=Decimal("1"),
                                min_notional=Decimal("1"))
        self._caps = self._caps.with_rules(symbol, rules)
        return rules

    # -- 行情（只读，用来核对名义额） ------------------------------------- #

    def _data(self) -> AlpacaTransport:
        if self._data_transport is None:
            self._data_transport = UrllibTransport(
                DATA_BASE, self.credentials, timeout=15.0)
        return self._data_transport

    def last_price(self, symbol: str) -> Optional[Decimal]:
        """
        最近一笔成交价。

        用途很具体：市价单没有限价，`OrderIntent.notional` 是上游算的，
        网关必须能自己核一遍这个名义额对不对。核不了就拒 —— 否则
        "单笔上限 1000"这道闸只是一个可以被上游写错的变量。

        取不到返回 None，由调用方拒单；**不返回 0 也不返回昨收**，
        用一个陈旧价格去放行一笔真实下单，比直接拒单危险得多。
        """
        status, payload = self._data().request(
            "GET", f"/v2/stocks/{symbol}/trades/latest",
            params={"feed": self._feed})
        if status == 404:
            return None
        _raise_for_status(status, payload, f"读取 {symbol} 最新成交价")
        if not isinstance(payload, Mapping):
            raise TransportError(f"行情响应不是对象：{type(payload).__name__}")
        trade = payload.get("trade")
        if isinstance(trade, Mapping) and trade.get("p") not in (None, ""):
            return D(trade.get("p"))
        return None

    def quote(self, symbol: str) -> Tuple[Optional[Decimal], Optional[Decimal]]:
        """(bid, ask)。市价单的真实成本是 ask 而不是 last —— 估计滑点要用它。"""
        status, payload = self._data().request(
            "GET", f"/v2/stocks/{symbol}/quotes/latest",
            params={"feed": self._feed})
        if status == 404:
            return None, None
        _raise_for_status(status, payload, f"读取 {symbol} 最新报价")
        if not isinstance(payload, Mapping):
            raise TransportError(f"报价响应不是对象：{type(payload).__name__}")
        q = payload.get("quote") or {}
        if not isinstance(q, Mapping):
            return None, None
        bid = D(q["bp"]) if q.get("bp") not in (None, "") else None
        ask = D(q["ap"]) if q.get("ap") not in (None, "") else None
        return bid, ask

    # -- 交易端点 -------------------------------------------------------- #

    def place(self, intent: OrderIntent) -> VenueOrder:
        if intent.venue and intent.venue.upper() not in (self.VENUE.upper(), "ALPACA"):
            raise CapabilityError(
                f"这笔单的 venue={intent.venue}，但网关是 {self.VENUE}")
        if intent.reduce_only:
            raise CapabilityError(
                "美股现货不支持 reduce_only：静默忽略会把'只许减仓'变成可以加仓。"
                "平仓请按持仓数量直接下反向单")
        if intent.order_type is OrderType.LIMIT and intent.limit_price is None:
            raise CapabilityError("LIMIT 单缺 limit_price")

        rules = self.rules_for(intent.symbol)
        check = self._caps.validate_order(
            intent.symbol, intent.side, intent.quantity,
            price=intent.limit_price if intent.order_type is OrderType.LIMIT else None,
            notional=intent.notional,
            max_order_notional=self.max_order_notional)
        if intent.order_type is OrderType.LIMIT and check.qty != check.qty.to_integral_value():
            # Alpaca 的碎股只支持市价单。限价 + 碎股会被场所拒，
            # 但更糟的是"取整成整股后照样发出去" —— 那等于偷偷改了数量。
            raise CapabilityError(
                f"{intent.symbol} 限价单只支持整股，数量 {check.qty} 含小数"
                f"（碎股仅限市价单）")

        if self.require_market_open and not self.market_open():
            raise VenueRejected(
                f"当前不在交易时段（Alpaca clock.is_open=false）：拒绝下单。"
                f"日单会被排到下一个开盘，那就不是'现在下这一单'了")
        account = self.account()
        if account.get("trading_blocked"):
            raise VenueRejected(
                f"账户 trading_blocked=true：拒绝下单（{account.get('status')}）")
        if str(account.get("status", "")).upper() != "ACTIVE":
            raise VenueRejected(
                f"账户状态 {account.get('status')} 不是 ACTIVE：拒绝下单")

        body: Dict[str, Any] = {
            "symbol": intent.symbol,
            "qty": _fmt_qty(check.qty),
            "side": "buy" if intent.side is Side.BUY else "sell",
            "type": "market" if intent.order_type is OrderType.MARKET else "limit",
            "time_in_force": "day",
            "client_order_id": intent.client_order_id,
        }
        if intent.order_type is OrderType.LIMIT:
            body["limit_price"] = str(check.price)

        status, payload = self.transport.request("POST", "/v2/orders", body=body)
        message = _message_of(payload)
        if _is_duplicate_client_order_id(status, message):
            # 场所认得这个 client_order_id → 这笔单一定存在，只是这次没返回体。
            # 回查一次；查不到就是可见性延迟，抛不确定让上层继续查，**绝不重发**。
            found = self.fetch_order(intent.symbol, intent.client_order_id)
            if found is not None:
                return found
            raise TransportError(
                f"client_order_id={intent.client_order_id} 已存在但回查不到"
                f"（可见性延迟）：状态未知，不得重发")
        _raise_for_status(status, payload, f"下单 {intent.client_order_id}")
        if not isinstance(payload, Mapping):
            raise TransportError(f"下单响应不是对象：{type(payload).__name__}")
        return _parse_order(payload, self.VENUE)

    def fetch_order(self, symbol: str, client_order_id: str) -> Optional[VenueOrder]:
        """按幂等键回查。404 = 查过了，确实没有这笔单（契约要求返回 None）。"""
        status, payload = self.transport.request(
            "GET", "/v2/orders:by_client_order_id",
            params={"client_order_id": client_order_id})
        if status == 404:
            return None
        _raise_for_status(status, payload, f"回查 {client_order_id}")
        if not isinstance(payload, Mapping):
            raise TransportError(f"订单响应不是对象：{type(payload).__name__}")
        return _parse_order(payload, self.VENUE)

    def cancel(self, symbol: str, client_order_id: str) -> VenueOrder:
        """
        撤单是**两步**：先换出 order_id，再 DELETE。

        实测 `DELETE /v2/orders:by_client_order_id` 是 404，所以第一步不能省。
        两步之间的窗口很小但真实存在：换出 id 之后单子可能刚成交，
        那时 DELETE 会返回 422 —— 我们按"已终结"处理（读到什么就报什么），
        而不是报错让上以为是网络问题。
        """
        order = self.fetch_order(symbol, client_order_id)
        if order is None:
            raise VenueRejected(f"撤单失败：找不到 client_order_id={client_order_id}")
        if order.is_terminal:
            return order
        status, payload = self.transport.request(
            "DELETE", f"/v2/orders/{order.venue_order_id}")
        if status == 422:
            # 撤不掉通常是因为刚好成交/已撤 —— 回查现状，按现状返回
            current = self.fetch_order(symbol, client_order_id)
            if current is not None:
                return current
            _raise_for_status(status, payload, f"撤单 {order.venue_order_id}")
        _raise_for_status(status, payload, f"撤单 {order.venue_order_id}")
        if isinstance(payload, Mapping) and payload.get("id"):
            return _parse_order(payload, self.VENUE)
        current = self.fetch_order(symbol, client_order_id)
        return current if current is not None else order

    def fetch_open_orders(self, symbol: Optional[str] = None) -> List[VenueOrder]:
        params: Dict[str, Any] = {"status": "open", "limit": 100, "direction": "desc"}
        if symbol:
            params["symbols"] = symbol
        status, payload = self.transport.request("GET", "/v2/orders", params=params)
        _raise_for_status(status, payload, "读取未结订单")
        if not isinstance(payload, list):
            raise TransportError(f"订单列表不是数组：{type(payload).__name__}")
        return [_parse_order(item, self.VENUE) for item in payload
                if isinstance(item, Mapping)]

    def fetch_recent_orders(self, limit: int = 50,
                            status_filter: str = "all") -> List[VenueOrder]:
        status, payload = self.transport.request(
            "GET", "/v2/orders",
            params={"status": status_filter, "limit": limit, "direction": "desc"})
        _raise_for_status(status, payload, "读取订单历史")
        if not isinstance(payload, list):
            raise TransportError(f"订单列表不是数组：{type(payload).__name__}")
        return [_parse_order(item, self.VENUE) for item in payload
                if isinstance(item, Mapping)]

    def fetch_positions(self) -> List[VenuePosition]:
        status, payload = self.transport.request("GET", "/v2/positions")
        _raise_for_status(status, payload, "读取场所持仓")
        if not isinstance(payload, list):
            raise TransportError(f"持仓列表不是数组：{type(payload).__name__}")
        return [_parse_position(item, self.VENUE) for item in payload
                if isinstance(item, Mapping)]

    def fetch_balance(self) -> List[VenueBalance]:
        account = self.account()
        cash = D(account.get("cash") or 0)
        # used 的口径：占用在保证金里的部分。美股现金账户基本为 0，
        # 但日内 4 倍杠杆下 buying_power 会远大于 cash —— 那个数字是
        # 额度不是余额，绝不能当 free 用。
        equity = D(account.get("equity") or 0)
        used = max(_ZERO, equity - cash) if D(account.get("long_market_value") or 0) == 0 else _ZERO
        return [VenueBalance(venue=self.VENUE, currency=str(account.get("currency") or "USD"),
                             free=cash, used=used)]

    def fetch_fills(self, since: Optional[datetime] = None,
                    page_size: int = 100) -> List[Mapping[str, Any]]:
        """
        真实成交回捞（activities 接口）。

        这一条是给对账用的：账本漏记一笔成交，账面上看不出任何异常 ——
        现金和持仓都自洽，只是比真实账户少了一笔。只有场所的成交流水
        能发现它。返回原始字典，由调用方决定怎么比对。
        """
        params: Dict[str, Any] = {"activity_types": "FILL", "page_size": page_size,
                                  "direction": "desc"}
        if since is not None:
            params["after"] = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        status, payload = self.transport.request(
            "GET", "/v2/account/activities", params=params)
        _raise_for_status(status, payload, "读取成交流水")
        if not isinstance(payload, list):
            raise TransportError(f"成交流水不是数组：{type(payload).__name__}")
        return [item for item in payload if isinstance(item, Mapping)]

    def fetch_funding_payments(self, since: Optional[datetime] = None) -> List[FundingPayment]:
        raise CapabilityError(
            f"{self.VENUE} 是美股现货，没有资金费 —— 不返回空列表，"
            f"因为'没有资金费'和'查不到资金费'在账上不是一回事")

    # -- 探针 ------------------------------------------------------------ #

    def probe(self, symbol: str = "AAPL", *, verify: bool = False) -> ProbeResult:
        """
        只读探针：确认"能用"，并且**实测**声明的能力。

        为什么要专门验一次 `orders:by_client_order_id`：
        这个端点是整条超时救援链的唯一入口。它要是坏了，`fetch_order()`
        会在每一笔超时之后返回"没有这笔单"，然后上层就会重发。
        而它坏掉的样子是 404 —— 和"确实没有这笔单"一模一样。

        所以探针拿一个**必然不存在**的随机 client_order_id 去查：
        拿到 404 说明端点活着（能区分"没有"而不是"没这个接口"）；
        拿到 404 且 message 里含 `order not found` 才算真的通。
        """
        notes: List[str] = []
        try:
            account = self.account(fresh=True)
            clock = self.clock(fresh=True)
        except GatewayError as exc:
            return ProbeResult(ok=False, mode=self.mode,
                               detail=f"{type(exc).__name__}: {exc}")

        probe_id = f"qos-probe-{int(time.time() * 1000)}"
        started = time.monotonic()
        try:
            status, payload = self.transport.request(
                "GET", "/v2/orders:by_client_order_id",
                params={"client_order_id": probe_id})
            elapsed_ms = int((time.monotonic() - started) * 1000)
        except GatewayError as exc:
            return ProbeResult(
                ok=False, mode=self.mode, account=_summary(account), clock=dict(clock),
                detail=f"回查端点不可用：{type(exc).__name__}: {exc}",
                notes=("回查端点坏了，等于超时之后永远救不回来",))

        message = _message_of(payload)
        query_ok = status == 404 and "order not found" in message.lower()
        if not query_ok:
            notes.append(
                f"回查端点返回 HTTP {status}（{message[:80]}）："
                f"预期 404 + 'order not found'")
            return ProbeResult(
                ok=False, mode=self.mode, account=_summary(account),
                clock=dict(clock), query_latency_ms=elapsed_ms,
                detail="无法按客户端单号回查 —— 拒绝承担自动交易",
                notes=tuple(notes))

        try:
            rules = self.rules_for(symbol)
        except GatewayError as exc:
            return ProbeResult(
                ok=False, mode=self.mode, account=_summary(account), clock=dict(clock),
                query_by_client_id_ok=True, query_latency_ms=elapsed_ms,
                detail=f"读取标的 {symbol} 失败：{type(exc).__name__}: {exc}")

        if verify:
            self._caps = self._caps.mark_verified(self._now())
            notes.append("已把能力声明标记为实测（verified_at 已写入）")

        open_now = bool(clock.get("is_open"))
        notes.append("市场当前开盘" if open_now else
                     "市场当前休市：place() 会被 require_market_open 拒绝")
        if str(account.get("status", "")).upper() != "ACTIVE":
            notes.append(f"账户状态 {account.get('status')} 不是 ACTIVE")
        if account.get("trading_blocked"):
            notes.append("trading_blocked=true：无法下单")

        return ProbeResult(
            ok=True, mode=self.mode,
            detail=f"纸面账户可用（{account.get('currency')} "
                   f"{account.get('cash')}，{account.get('status')}）",
            account=_summary(account), clock=dict(clock), rules=rules,
            query_by_client_id_ok=True, query_latency_ms=elapsed_ms,
            notes=tuple(notes))

    # -- 诊断 ------------------------------------------------------------ #

    def health(self) -> Dict[str, Any]:
        base = super().health()
        base.update({
            "mode": self.mode,
            "base": self.credentials.base,
            "key": self.credentials.masked_key,
            "max_order_notional": str(self.max_order_notional),
            "require_market_open": self.require_market_open,
        })
        return base

    def invalidate(self) -> None:
        """清缓存。自测与运维面手动刷新用。"""
        self._clock_cache = None
        self._account_cache = None


def _summary(account: Mapping[str, Any]) -> Dict[str, Any]:
    keep = ("account_number", "status", "currency", "cash", "equity",
            "buying_power", "long_market_value", "trading_blocked",
            "pattern_day_trader", "daytrade_count")
    out = {k: account.get(k) for k in keep if k in account}
    # 账户号只留后四位：运维面是公网可看的（见隐私边界），
    # 但它确实需要"这是哪个账户"的区分度。
    number = str(out.get("account_number") or "")
    if number:
        out["account_number"] = "***" + number[-4:]
    return out


def _fmt_qty(qty: Decimal) -> str:
    """Alpaca 只收字符串数量。整数股不能写成 `1.000000`，碎股要保留精度。"""
    q = D(qty)
    if q == q.to_integral_value():
        return str(int(q))
    return format(q.normalize(), "f")


def build_gateway_from_env(*, base: str = "", allow_live: bool = False,
                           require_market_open: bool = True,
                           transport: Optional[AlpacaTransport] = None,
                           timeout: float = 15.0) -> AlpacaGateway:
    """按环境造一个网关。凭据/端点任何一项不合格都在这里抛，不返回半成品。"""
    credentials = AlpacaCredentials.from_env(base=base, allow_live=allow_live)
    tp = transport or UrllibTransport(credentials.base, credentials, timeout=timeout)
    return AlpacaGateway(tp, credentials, require_market_open=require_market_open)
