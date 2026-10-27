"""
QuantOS 2.0 — 行情快照契约（T-102）
===================================

对应需求 R-D02（缺价必须显式缺失）、R-K03（陈旧行情阻断下单）。

三条不可协商的规则
------------------
1. **缺价一律 None，绝不用 0 填充**。0 会被下游当成真实价格算出 0 名义敞口，
   进而绕过风控（一笔 0 元订单永远不超限）。这里对 0 直接抛异常。
2. **as_of 与 received_at 分开记**。前者是行情源时间（判断是否陈旧），
   后者是本地收到时间（判断链路延迟）。两者都缺失时无法判断新鲜度。
3. **乱序拒收**。同一个 (venue, symbol) 收到比当前更旧的快照时丢弃，
   否则一次网络重排就能让系统拿几十秒前的价格下单。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from quantos_core.contracts import D, q

UTC = timezone.utc
SymbolKey = Tuple[str, str]

#: as_of 允许领先本机时钟的容差（交易所时钟漂移）。
MAX_CLOCK_SKEW = timedelta(seconds=5)


class MarketDataError(RuntimeError):
    """行情数据不可用。上层必须按 fail-closed 处理，不许兜底成 0 价。"""


class PriceUnavailable(MarketDataError):
    """该标的/方向当前没有可用价格 —— 禁止下单，不是"价格等于 0"。"""


def _aware(dt: datetime, name: str) -> datetime:
    if not isinstance(dt, datetime):
        raise TypeError(f"{name} 必须是 datetime，收到 {type(dt).__name__}")
    if dt.tzinfo is None:
        raise ValueError(f"{name} 必须带时区")
    return dt.astimezone(UTC)


@dataclass(frozen=True)
class MarketSnapshot:
    """单一标的某一时刻的行情快照。不可变，可安全放进事件日志。"""

    venue: str
    symbol: str
    as_of: datetime
    received_at: datetime
    bid: Optional[Decimal] = None
    ask: Optional[Decimal] = None
    last: Optional[Decimal] = None
    source: str = "unknown"
    sequence: Optional[int] = None

    def __post_init__(self):
        if not self.venue or not self.symbol:
            raise ValueError("MarketSnapshot 的 venue/symbol 不可为空")
        _aware(self.as_of, "as_of")
        _aware(self.received_at, "received_at")
        for name in ("bid", "ask", "last"):
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                value = D(value)
                object.__setattr__(self, name, value)
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} 必须是 Decimal 或 None，收到 {type(value).__name__}")
            if value == 0:
                # 显式拦截：0 价不是"没有价格"，是数据错误。
                raise ValueError(
                    f"{self.venue}:{self.symbol} 的 {name} 为 0 —— "
                    f"缺价必须传 None（R-D02），禁止用 0 占位")
            if value < 0:
                raise ValueError(f"{name} 不能为负，收到 {value}")

    # -- 新鲜度 ---------------------------------------------------------- #

    def age_ms(self, now: Optional[datetime] = None) -> int:
        """行情源时间到现在的毫秒数。"""
        now = _aware(now or datetime.now(UTC), "now")
        return int((now - self.as_of).total_seconds() * 1000)

    def staleness_ms(self, now: Optional[datetime] = None) -> int:
        """陈旧度 = 行情本身年龄（与 age_ms 同义，保留契约命名）。"""
        return self.age_ms(now)

    def latency_ms(self) -> int:
        """链路延迟：源时间 → 本地收到。"""
        return int((self.received_at - self.as_of).total_seconds() * 1000)

    def is_stale(self, now: Optional[datetime], limit_sec: Decimal | int | float) -> bool:
        return D(self.age_ms(now)) > D(limit_sec) * Decimal(1000)

    def is_future(self, now: Optional[datetime] = None) -> bool:
        now = _aware(now or datetime.now(UTC), "now")
        return self.as_of - now > MAX_CLOCK_SKEW

    # -- 价格 ------------------------------------------------------------ #

    @property
    def mid(self) -> Optional[Decimal]:
        if self.bid is None or self.ask is None:
            return None
        return q((self.bid + self.ask) / Decimal(2), 8)

    @property
    def spread_bps(self) -> Optional[Decimal]:
        mid = self.mid
        if mid is None or mid <= 0:
            return None
        return q((self.ask - self.bid) / mid * Decimal(10000), 4)

    def executable_price(self, side) -> Decimal:
        """
        实际成交参考价：买入看卖一，卖出看买一。

        拿不到就抛 PriceUnavailable —— 绝不回退到 last，更不回退到 0。
        """
        want = getattr(side, "value", str(side)).upper()
        price = self.ask if want == "BUY" else self.bid
        if price is None:
            raise PriceUnavailable(
                f"{self.venue}:{self.symbol} 缺少{'卖一' if want == 'BUY' else '买一'}报价，"
                f"无法为 {want} 定价（R-D02）")
        return price

    def reference_price(self) -> Decimal:
        """估值用价格：mid → last → 单边报价。全部缺失才抛异常。"""
        mid = self.mid
        if mid is not None:
            return mid
        for candidate in (self.last, self.bid, self.ask):
            if candidate is not None:
                return candidate
        raise PriceUnavailable(f"{self.venue}:{self.symbol} 无任何可用价格")

    @property
    def is_crossed(self) -> bool:
        """买一 > 卖一，说明行情错乱或撮合异常。"""
        return self.bid is not None and self.ask is not None and self.bid > self.ask

    def defects(self) -> List[str]:
        """数据质量缺陷清单。空列表 = 可直接用于交易决策。"""
        problems: List[str] = []
        if self.bid is None:
            problems.append("missing_bid")
        if self.ask is None:
            problems.append("missing_ask")
        if self.last is None:
            problems.append("missing_last")
        if self.is_crossed:
            problems.append("crossed_book")
        return problems

    @property
    def is_usable(self) -> bool:
        return not self.defects()

    def to_payload(self) -> Dict:
        return {
            "venue": self.venue, "symbol": self.symbol,
            "as_of": self.as_of, "received_at": self.received_at,
            "bid": self.bid, "ask": self.ask, "last": self.last,
            "source": self.source, "sequence": self.sequence,
        }


@dataclass
class MarketDataBook:
    """
    最新行情表。只负责"现在有什么价、有多旧"，不做任何交易决策。

    与 RiskEngine 的分工：这里给事实（年龄、缺失、交叉盘），RiskEngine 给结论。
    """

    _latest: Dict[SymbolKey, MarketSnapshot] = field(default_factory=dict)
    _rejected: List[Tuple[str, str, str]] = field(default_factory=list)

    # -- 写入 ------------------------------------------------------------ #

    def update(self, snap: MarketSnapshot, now: Optional[datetime] = None) -> bool:
        """
        更新快照。返回 False 表示被拒（乱序/时钟异常），当前价保持不变。

        拒收而不是覆盖：过期数据覆盖新数据会让风控误判行情新鲜。
        """
        key = (snap.venue, snap.symbol)
        now = _aware(now or datetime.now(UTC), "now")

        if snap.is_future(now):
            self._rejected.append((snap.venue, snap.symbol, "future_timestamp"))
            return False

        current = self._latest.get(key)
        if current is not None:
            if snap.as_of < current.as_of:
                self._rejected.append((snap.venue, snap.symbol, "out_of_order"))
                return False
            if (snap.as_of == current.as_of and snap.sequence is not None
                    and current.sequence is not None and snap.sequence <= current.sequence):
                self._rejected.append((snap.venue, snap.symbol, "duplicate_sequence"))
                return False

        self._latest[key] = snap
        return True

    # -- 读取 ------------------------------------------------------------ #

    def get(self, venue: str, symbol: str) -> Optional[MarketSnapshot]:
        return self._latest.get((venue, symbol))

    def require(self, venue: str, symbol: str) -> MarketSnapshot:
        snap = self.get(venue, symbol)
        if snap is None:
            raise PriceUnavailable(f"{venue}:{symbol} 从未收到行情（R-D02）")
        return snap

    def price_for(self, venue: str, symbol: str, side=None) -> Decimal:
        """下单参考价。side 为 None 时用估值价。"""
        snap = self.require(venue, symbol)
        return snap.reference_price() if side is None else snap.executable_price(side)

    def prices(self, keys: Optional[Iterable[SymbolKey]] = None
               ) -> Dict[SymbolKey, Decimal]:
        """估值价表。任何标的拿不到价就抛异常 —— 用 0 补齐会让 NAV 偏小。"""
        out: Dict[SymbolKey, Decimal] = {}
        for key in (keys if keys is not None else self._latest.keys()):
            out[tuple(key)] = self.require(key[0], key[1]).reference_price()
        return out

    def age_sec_by_venue(self, now: Optional[datetime] = None) -> Dict[str, Decimal]:
        """
        各场所「最新一条行情的年龄」。直接喂给 RiskContext.market_data_age_sec。

        取该场所最旧的那条：一个场所里只要有一个标的数据陈旧，就说明这条
        行情链路有问题，不该按"最健康的那条"自我安慰。
        """
        now = _aware(now or datetime.now(UTC), "now")
        worst: Dict[str, Decimal] = {}
        for (venue, _symbol), snap in self._latest.items():
            age = D(snap.age_ms(now)) / Decimal(1000)
            if venue not in worst or age > worst[venue]:
                worst[venue] = q(age, 3)
        return worst

    def staleness_report(self, limit_sec, now: Optional[datetime] = None) -> Dict[str, List[str]]:
        """陈旧/缺失清单，给运维中心用。"""
        now = _aware(now or datetime.now(UTC), "now")
        stale, defective = [], []
        for (venue, symbol), snap in self._latest.items():
            label = f"{venue}:{symbol}"
            if snap.is_stale(now, limit_sec):
                stale.append(label)
            if not snap.is_usable:
                defective.append(f"{label}({','.join(snap.defects())})")
        return {"stale": sorted(stale), "defective": sorted(defective)}

    def missing(self, venue: str, symbols: Sequence[str]) -> List[str]:
        return [s for s in symbols if (venue, s) not in self._latest]

    def crossed(self) -> List[str]:
        return sorted(f"{v}:{s}" for (v, s), snap in self._latest.items() if snap.is_crossed)

    def keys(self) -> Set[SymbolKey]:
        return set(self._latest.keys())

    @property
    def rejection_log(self) -> List[Tuple[str, str, str]]:
        return list(self._rejected)


__all__ = [
    "MarketDataError", "PriceUnavailable", "MarketSnapshot", "MarketDataBook",
    "SymbolKey", "MAX_CLOCK_SKEW",
]
