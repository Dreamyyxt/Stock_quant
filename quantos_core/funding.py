"""
QuantOS 2.0 — 资金费记账（T-503）
==================================

永续合约没有到期日，靠资金费把合约价格锚在现货上。对做多的人，正费率意味着
每 8 小时被收一次钱；做空的人收钱。这笔钱既不是手续费也不是滑点，**必须单列**,
否则归因会把"持仓成本"错记成"交易亏损"，策略评估整体偏移。

三个特别容易错的地方，本模块每一条都做成断言：

  1. **符号**。正费率时多头支出。账本 `post_funding` 的约定是"正数 = 支出"，
     所以 `多头 × 正费率 = 正数`。搞反了，一个稳定亏损的策略会显示成在赚钱，
     而且账本总额还能对上 —— 因为两处同时反了。这是最难自查的一类错误。

  2. **应计 ≠ 已结算**。盘中估算"我大概要被收 2 块钱"不能进账本：
     进了，真结算时再记一次就双记；不进，NAV 又少算了这笔负债。
     正确做法是计提只进展示口径（`FundingAccrual`），账本只记 `FundingPayment`。

  3. **重复入账**。8 小时一次、一天 3 次、一个月约 90 次。任何一次重跑日终、
     任何一次补数据，都可能把同一笔资金费记两遍。幂等键必须是
     `(场所, 标的, 结算边界)`，不是"跑了几次"。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .contracts import D, utcnow
from .gateway import FundingPayment
from .ledger import Ledger, LedgerEntry

__all__ = [
    "FundingWindow", "FundingAccrual", "FundingBook", "SettleResult",
    "FundingDiff", "FundingReconciliation", "FundingReconciler",
    "funding_amount", "boundaries_between", "next_settlement", "window_of",
    "DEFAULT_ANCHOR",
]

_ZERO = Decimal(0)
_EPS = Decimal("0.000001")

#: 结算边界的对齐锚点。UTC 零点起算，8 小时周期即 00/08/16 UTC。
DEFAULT_ANCHOR = datetime(2020, 1, 1, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
#  结算边界
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class FundingWindow:
    """一个资金费区间 [start, end)。"""

    start: datetime
    end: datetime
    interval_hours: int

    def __post_init__(self):
        if self.end <= self.start:
            raise ValueError(f"资金费区间非法：{self.start} → {self.end}")
        if self.interval_hours <= 0:
            raise ValueError("interval_hours 必须为正")

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def is_open(self, now: datetime) -> bool:
        return self.start <= now < self.end

    def progress(self, now: datetime) -> Decimal:
        """已过去的比例，[0, 1]。用于线性计提。"""
        if now <= self.start:
            return _ZERO
        if now >= self.end:
            return Decimal(1)
        return D((now - self.start).total_seconds()) / D(self.duration.total_seconds())

    def boundary_key(self, symbol: str, venue: str = "") -> str:
        """结算边界键：资金费入账的幂等键。到点时间本身，与执行次数无关。"""
        stamp = self.end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M")
        return f"{venue}|{symbol}|{stamp}" if venue else f"{symbol}|{stamp}"


def _floor_boundary(ts: datetime, interval_hours: int,
                    anchor: datetime = DEFAULT_ANCHOR) -> datetime:
    step = timedelta(hours=interval_hours)
    delta = ts - anchor
    # timedelta // timedelta 是向下取整，早于锚点的时间也能正确对齐
    return anchor + step * int(delta // step)


def window_of(ts: datetime, interval_hours: int,
              anchor: datetime = DEFAULT_ANCHOR) -> FundingWindow:
    """`ts` 落在哪个资金费区间里。"""
    if interval_hours <= 0:
        raise ValueError("interval_hours 必须为正")
    start = _floor_boundary(ts, interval_hours, anchor)
    return FundingWindow(start=start, end=start + timedelta(hours=interval_hours),
                         interval_hours=interval_hours)


def next_settlement(ts: datetime, interval_hours: int,
                    anchor: datetime = DEFAULT_ANCHOR) -> datetime:
    """下一次结算时刻（严格大于 ts）。"""
    return window_of(ts, interval_hours, anchor).end


def boundaries_between(start: datetime, end: datetime, interval_hours: int,
                       anchor: datetime = DEFAULT_ANCHOR) -> List[datetime]:
    """(start, end] 之间的所有结算时刻。用于补历史、算应补笔数。"""
    if end <= start:
        return []
    out: List[datetime] = []
    cursor = window_of(start, interval_hours, anchor).end
    guard = 0
    while cursor <= end:
        out.append(cursor)
        cursor += timedelta(hours=interval_hours)
        guard += 1
        if guard > 100000:
            raise RuntimeError("boundaries_between 迭代失控，检查参数")
    return out


# --------------------------------------------------------------------------- #
#  金额
# --------------------------------------------------------------------------- #

def funding_amount(signed_qty: Decimal, mark_price: Decimal, rate: Decimal) -> Decimal:
    """
    单次结算的资金费金额，**正数 = 账户支出**。

        多头(+2 BTC) × 30000 × 0.0001 = +6   → 支出 6，正常（多头付钱）
        空头(-2 BTC) × 30000 × 0.0001 = -6   → 收入 6，正常（空头收钱）

    这就是全部的符号逻辑。它简单到不值得写一个函数 —— 但它也是最容易
    在别处被"顺手简化"成 `abs(qty) * price * rate` 的地方，那一简化，
    空头就从收钱变成付钱。
    """
    return (D(signed_qty) * D(mark_price) * D(rate)).quantize(Decimal("0.00000001"))


@dataclass(frozen=True)
class FundingAccrual:
    """
    盘中计提（**不进账本**）。

    为什么要它：如果只在结算那一刻记账，两次结算之间 NAV 会莫名其妙地"少一块"，
    而那一块是已经发生、只是还没扣的钱。计提把它摊到时间上，让曲线连续。

    为什么不能进账本：账本是"已发生事实"的记录。计提是估算，估算一旦入账，
    真结算时就会记第二笔，而且没人会在三个月后去对哪一笔是估算。
    """

    symbol: str
    window: FundingWindow
    signed_qty: Decimal
    mark_price: Decimal
    rate: Decimal

    @property
    def full_amount(self) -> Decimal:
        """整个区间按当前速率会收/付多少。"""
        return funding_amount(self.signed_qty, self.mark_price, self.rate)

    def accrued(self, now: datetime) -> Decimal:
        """按已过时间线性计提。区间结束后等于 full_amount。"""
        return (self.full_amount * self.window.progress(now)).quantize(
            Decimal("0.00000001"))

    def at_boundary(self) -> Decimal:
        return self.full_amount

    def to_dict(self, now: Optional[datetime] = None) -> Dict[str, str]:
        now = now or utcnow()
        return {
            "symbol": self.symbol,
            "window_start": self.window.start.isoformat(),
            "window_end": self.window.end.isoformat(),
            "rate": str(self.rate),
            "full_amount": str(self.full_amount),
            "accrued": str(self.accrued(now)),
            "settled": "false",
        }


# --------------------------------------------------------------------------- #
#  入账
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SettleResult:
    """一次结算入账的结果。`duplicate` 为 True 时 `entry` 是**原来那笔**。"""

    key: str
    posted_amount: Decimal
    duplicate: bool = False
    amended: bool = False
    entry: Optional[LedgerEntry] = None
    note: str = ""

    @property
    def wrote_ledger(self) -> bool:
        return self.entry is not None and (not self.duplicate or self.amended)


class FundingBook:
    """
    资金费入账簿。

    幂等键是 `(场所, 标的, 结算边界)`。重跑一百次日终，账本里也只有那三笔。

    修正结算（交易所事后改金额）走"补差额"路径：同一个键第二次出现且金额不同，
    只记差额，不重记全额。这样审计时能看出"当时记了 6，后来补了 0.5"，
    而不是看到两笔 6 和 6.5 去猜哪笔作废。
    """

    def __init__(self, ledger: Ledger, account_id: str = "ACC") -> None:
        self._ledger = ledger
        self._account = account_id
        #: key -> 已入账累计金额
        self._posted: Dict[str, Decimal] = {}
        self._entries: Dict[str, List[LedgerEntry]] = {}
        self._amend_seq = 0

    # -- 查询 ------------------------------------------------------------ #

    def posted_amount(self, key: str) -> Decimal:
        return D(self._posted.get(key, _ZERO))

    def keys(self) -> List[str]:
        return sorted(self._posted)

    @property
    def total(self) -> Decimal:
        """已入账资金费合计（正=净支出）。"""
        return sum(self._posted.values(), _ZERO)

    def entries_for(self, key: str) -> List[LedgerEntry]:
        return list(self._entries.get(key, ()))

    # -- 入账 ------------------------------------------------------------ #

    def settle(self, payment: FundingPayment, *,
               fx_to_base: Decimal = Decimal(1),
               account_id: Optional[str] = None) -> SettleResult:
        """
        记一笔已结算资金费。同一笔重复调用不会重复入账。

        `fx_to_base` 用于把结算币种折算到记账本位币。资金费常常是 USDT 结算而
        账本是 USD —— 这中间有汇率，归因的 fx 项就靠它。
        """
        key = payment.boundary_key
        amount = D(payment.amount)
        acct = account_id or self._account
        already = self.posted_amount(key)

        if key in self._posted:
            delta = amount - already
            if abs(delta) <= _EPS:
                return SettleResult(key=key, posted_amount=_ZERO, duplicate=True,
                                    entry=self._entries[key][-1],
                                    note="重复结算已忽略，账本未变")
            self._amend_seq += 1
            entry = self._ledger.post_funding(
                account_id=acct, currency=payment.currency, amount=delta,
                fx_to_base=D(fx_to_base),
                ts=payment.ts,
                ref=f"{key}#amend{self._amend_seq}")
            self._posted[key] = already + delta
            self._entries[key].append(entry)
            return SettleResult(key=key, posted_amount=delta, duplicate=True,
                                amended=True, entry=entry,
                                note=f"结算金额修正 {already} → {amount}，只补差额 {delta}")

        entry = self._ledger.post_funding(
            account_id=acct, currency=payment.currency, amount=amount,
            fx_to_base=D(fx_to_base), ts=payment.ts, ref=key)
        self._posted[key] = amount
        self._entries[key] = [entry]
        return SettleResult(key=key, posted_amount=amount, entry=entry,
                            note=f"资金费入账 {amount}（{'支出' if amount > 0 else '收入'}）")

    def settle_many(self, payments: Iterable[FundingPayment], **kwargs) -> List[SettleResult]:
        results = [self.settle(p, **kwargs) for p in payments]
        return sorted(results, key=lambda r: r.key)


# --------------------------------------------------------------------------- #
#  与交易所账单对账
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class FundingDiff:
    """
    一笔资金费差异。

    `MISSED` 是这里最危险的一类：交易所已经把钱扣了，账本不知道。
    表现为"账本 NAV 比实际高"，而 NAV 偏高会让风控以为还有余量继续下单。
    """

    kind: str          # missed | extra | amount
    key: str
    symbol: str
    local: Decimal
    venue: Decimal

    @property
    def delta(self) -> Decimal:
        return D(self.venue) - D(self.local)

    def to_dict(self) -> Dict[str, str]:
        return {"kind": self.kind, "key": self.key, "symbol": self.symbol,
                "local": str(self.local), "venue": str(self.venue),
                "delta": str(self.delta)}


@dataclass(frozen=True)
class FundingReconciliation:
    """资金费对账结论。`clean` 与日终对账同口径：拿不到账单就不算干净。"""

    checked_keys: int
    diffs: Tuple[FundingDiff, ...] = ()
    statement_ok: bool = True
    note: str = ""

    @property
    def clean(self) -> bool:
        return self.statement_ok and not self.diffs

    @property
    def missed(self) -> Tuple[FundingDiff, ...]:
        return tuple(d for d in self.diffs if d.kind == "missed")

    @property
    def net_delta(self) -> Decimal:
        return sum((d.delta for d in self.diffs), _ZERO)

    def to_dict(self) -> Dict:
        return {
            "clean": self.clean,
            "statement_ok": self.statement_ok,
            "checked_keys": self.checked_keys,
            "diff_count": len(self.diffs),
            "net_delta": str(self.net_delta),
            "note": self.note,
            "diffs": [d.to_dict() for d in self.diffs],
        }


class FundingReconciler:
    """
    账本资金费 vs 交易所资金费流水。

    只比"已结算"的部分。用计提数去比账单，必然会天天报差异 ——
    因为计提是估算，账单是事实，两者本来就不该相等。
    """

    def __init__(self, book: FundingBook) -> None:
        self._book = book

    def reconcile(self, payments: Optional[Sequence[FundingPayment]], *,
                  statement_ok: bool = True,
                  note: str = "") -> FundingReconciliation:
        if payments is None or not statement_ok:
            return FundingReconciliation(
                checked_keys=0, diffs=(), statement_ok=False,
                note=note or "拿不到交易所资金费流水，无法判定")

        keys = set(self._book.keys())
        diffs: List[FundingDiff] = []
        for p in payments:
            key = p.boundary_key
            local = self._book.posted_amount(key)
            if key not in keys:
                diffs.append(FundingDiff("missed", key, p.symbol, _ZERO, D(p.amount)))
                continue
            if abs(local - D(p.amount)) > _EPS:
                diffs.append(FundingDiff("amount", key, p.symbol, local, D(p.amount)))

        seen = {p.boundary_key for p in payments}
        for key in sorted(keys - seen):
            diffs.append(FundingDiff("extra", key, "", self._book.posted_amount(key), _ZERO))

        diffs.sort(key=lambda d: ({"missed": 0, "amount": 1, "extra": 2}[d.kind], d.key))
        return FundingReconciliation(checked_keys=len(seen), diffs=tuple(diffs),
                                     statement_ok=True, note=note)


def estimate_error_bps(accrual: FundingAccrual, actual: FundingPayment) -> Decimal:
    """
    计提 vs 实际的偏差（万分之几）。

    这不是"对账"，是**校验费率来源有没有接错**。如果连续几个结算周期的偏差
    都稳定在同一个方向且量级相同，多半是用了错误的费率（比如把 8h 费率当 1h
    用了，或者取了 mark price 而交易所用的是 premium index）。
    """
    expected = accrual.at_boundary()
    if abs(expected) <= _EPS:
        return _ZERO
    return ((D(actual.amount) - expected) / abs(expected) * Decimal(10000)
            ).quantize(Decimal("0.01"))


def day_boundaries(day: date, interval_hours: int,
                   anchor: datetime = DEFAULT_ANCHOR) -> List[datetime]:
    """
    某一天（UTC）内的全部结算时刻。

    8 小时周期下是 00:00 / 08:00 / 16:00 三个 —— 注意 00:00 那个是**当天**的，
    它结算的是前一天 16:00-24:00 那个区间。运维面板显示"今天还要结算几次"
    时用它，所以锚在当天 00:00 而不是"从现在起"。
    """
    start = datetime.combine(day, time(0, 0), tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    prev = start - timedelta(seconds=1)
    return [b for b in boundaries_between(prev, end, interval_hours, anchor) if b < end]
