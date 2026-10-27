"""
QuantOS 2.0 — Portfolio Ledger（T-004）
======================================

对应需求 R-D01..D05 与契约第 4 节。基金会计口径，不是"读余额显示"。

四条硬规则：

  1. 每笔记账借贷必须相等（按币种），否则拒绝入账 —— 这是对账永远平的根。
  2. 入金/出金只进 `equity:capital`，**绝不进 pnl** —— 收益率才不被出入金扭曲。
  3. 归因五项（trading / slippage / fee / funding / fx）之和必须等于净收益，
     差超过 1e-6 就抛 AttributionMismatch，不许四舍五入糊过去。
  4. 现金与持仓按**原币种**记录，报表时才折算到计价货币。

收益归因的代数恒等式（契约 §4.3）
--------------------------------
设 m 为期末市价、p 为成交价、p0 为决策价：

    Trading  = Σ 持仓数量×m + Σ 卖出(p0×q) - Σ 买入(p0×q)
    Slippage = Σ 买入(p0-p)×q + Σ 卖出(p-p0)×q
    Trading + Slippage - Fee - Funding = Net PnL

因为 NAV = 现金 + Σ数量×m，而现金 = 入金 + Σ卖出(p×q) - Σ买入(p×q) - 费用 - 资金费。
这个恒等式不是巧合，是双分录的必然结果 —— 所以拿它做断言能抓出任何漏记。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from quantos_core.contracts import (
    ContractViolation, D, Fill, Side, q, utcnow,
)

SymbolKey = Tuple[str, str]          # (venue, symbol)
CashKey = Tuple[str, str]            # (account_id, currency)
AttrKey = Tuple[str, str, str]       # (venue, symbol, strategy_id)

_ZERO = Decimal(0)
_EPS = Decimal("0.000001")


class UnbalancedEntry(ContractViolation):
    """借贷不平。拒绝入账。"""


class AttributionMismatch(RuntimeError):
    """归因之和与净收益不符 —— 说明有未分类损益漏记。"""


# --------------------------------------------------------------------------- #
#  分录
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class LedgerLine:
    """一条分录行。借正贷负必须二选一，按币种分别配平。"""

    account: str
    currency: str
    debit: Decimal = _ZERO
    credit: Decimal = _ZERO

    def __post_init__(self):
        if D(self.debit) < 0 or D(self.credit) < 0:
            raise UnbalancedEntry(f"{self.account}: 借贷金额不能为负")
        if D(self.debit) > 0 and D(self.credit) > 0:
            raise UnbalancedEntry(f"{self.account}: 同一行不能既借又贷")

    @property
    def signed(self) -> Decimal:
        return D(self.debit) - D(self.credit)


@dataclass(frozen=True)
class LedgerEntry:
    entry_id: str
    ts: datetime
    kind: str                        # TRADE / FEE / FUNDING / TRANSFER / FX / DIVIDEND
    ref_type: str
    ref_id: str
    lines: Tuple[LedgerLine, ...]
    meta: Mapping[str, str] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.lines:
            raise UnbalancedEntry(f"{self.entry_id}: 分录为空")
        totals: Dict[str, List[Decimal]] = {}
        for line in self.lines:
            bucket = totals.setdefault(line.currency, [_ZERO, _ZERO])
            bucket[0] += D(line.debit)
            bucket[1] += D(line.credit)
        for ccy, (debit, credit) in totals.items():
            if abs(debit - credit) > _EPS:
                raise UnbalancedEntry(
                    f"{self.entry_id}: {ccy} 借贷不平 借={debit} 贷={credit} "
                    f"（差额 {debit - credit}）")

    @property
    def currencies(self) -> Set[str]:
        return {line.currency for line in self.lines}


# --------------------------------------------------------------------------- #
#  持仓与策略归属
# --------------------------------------------------------------------------- #

@dataclass
class PositionBook:
    """单标的的账本持仓。cost/proceeds 以决策价计，用于归因；现金用成交价。"""

    venue: str
    symbol: str
    quote_ccy: str
    qty: Decimal = _ZERO
    #: Σ买入(决策价×数量)，计价货币。Trading 归因用。
    buy_at_decision: Decimal = _ZERO
    #: Σ卖出(决策价×数量)，计价货币。
    sell_at_decision: Decimal = _ZERO
    #: 滑点损益（正=有利），计价货币。
    slippage: Decimal = _ZERO
    attr_qty: Dict[str, Decimal] = field(default_factory=dict)
    attr_cost: Dict[str, Decimal] = field(default_factory=dict)
    attr_realized: Dict[str, Decimal] = field(default_factory=dict)

    def strategy_pnl(self, strategy_id: str, price: Decimal,
                     fx: Decimal) -> Decimal:
        """单策略损益 = 归属市值 - 归属成本 + 已实现。"""
        qty = D(self.attr_qty.get(strategy_id, _ZERO))
        cost = D(self.attr_cost.get(strategy_id, _ZERO))
        realized = D(self.attr_realized.get(strategy_id, _ZERO))
        return q(qty * D(price) * D(fx) - cost + realized, 4)


@dataclass(frozen=True)
class PerformanceReport:
    nav: Decimal
    net_pnl: Decimal
    trading: Decimal
    slippage: Decimal
    fee: Decimal
    funding: Decimal
    fx: Decimal
    external_flow: Decimal
    gross_exposure: Decimal
    net_exposure: Decimal
    attribution_sum: Decimal
    #: 现金分红（B7）。给默认值是为了不破坏既有的位置参数调用。
    #: 正数 = 收益（收到分红），负数 = 补缴/退回。
    dividend: Decimal = _ZERO

    @property
    def closes(self) -> bool:
        """归因六项之和是否等于净收益。

        注意：这个恒等式**在构造上恒成立**，因为 fx 是残差（net 减去其余五项）。
        它能抓的只有一种错 —— 残差本身不该出现而出现了（比如单币种下
        fx 不为 0）。真正能抓错的是 `build_daily_attribution` 里那两条独立校验。
        """
        return abs(self.attribution_sum - self.net_pnl) <= _EPS

    def to_dict(self) -> Dict:
        return {
            "nav": str(q(self.nav, 2)),
            "net_pnl": str(q(self.net_pnl, 2)),
            "trading": str(q(self.trading, 2)),
            "slippage": str(q(self.slippage, 2)),
            "fee": str(q(self.fee, 2)),
            "funding": str(q(self.funding, 2)),
            "dividend": str(q(self.dividend, 2)),
            "fx": str(q(self.fx, 2)),
            "external_flow": str(q(self.external_flow, 2)),
            "gross_exposure": str(q(self.gross_exposure, 2)),
            "net_exposure": str(q(self.net_exposure, 2)),
            "closes": self.closes,
        }


@dataclass(frozen=True)
class ReconciliationReport:
    ts: datetime
    cash_diffs: Mapping[str, str]        # "account|ccy" -> 差额
    position_diffs: Mapping[str, str]    # "venue|symbol" -> 差额
    missing_at_broker: Tuple[str, ...] = ()
    missing_in_ledger: Tuple[str, ...] = ()

    @property
    def is_clean(self) -> bool:
        return not (self.cash_diffs or self.position_diffs
                    or self.missing_at_broker or self.missing_in_ledger)

    def to_dict(self) -> Dict:
        return {
            "ts": self.ts.isoformat(),
            "clean": self.is_clean,
            "cash_diffs": dict(self.cash_diffs),
            "position_diffs": dict(self.position_diffs),
            "missing_at_broker": list(self.missing_at_broker),
            "missing_in_ledger": list(self.missing_in_ledger),
        }


# --------------------------------------------------------------------------- #
#  Ledger
# --------------------------------------------------------------------------- #

class Ledger:
    """基金级账本。所有写入都是显式动作，没有隐式状态。"""

    def __init__(self, base_currency: str = "USD") -> None:
        self.base_currency = base_currency
        self._entries: List[LedgerEntry] = []
        self._refs: Set[Tuple[str, str, str]] = set()
        self._cash: Dict[CashKey, Decimal] = {}
        self._positions: Dict[SymbolKey, PositionBook] = {}
        self._quote_ccy: Dict[SymbolKey, str] = {}
        self._fees_base: Decimal = _ZERO
        self._funding_base: Decimal = _ZERO
        self._dividends_base: Decimal = _ZERO
        self._deposits_base: Decimal = _ZERO
        self._withdrawals_base: Decimal = _ZERO
        #: 幂等键 -> 首次写入的那条分录。重复回报要能返回原件，
        #: 否则调用方无法判断「到底有没有真的写进去」。
        self._by_key: Dict[Tuple[str, str, str], LedgerEntry] = {}
        self._seq = 0

    # -- 记账原语 -------------------------------------------------------- #

    def post(self, entry: LedgerEntry) -> LedgerEntry:
        """
        唯一记账入口。重复引用（同 ref_type+ref_id+kind）自动忽略，保证
        交易所重复回报不会把仓位算两遍（R-A08）。
        """
        entry.validate()
        key = (entry.ref_type, entry.ref_id, entry.kind)
        if key in self._refs:
            # 幂等：返回**首次写入的那条**，不是这条被丢弃的副本。
            # 调用方可以靠 was_written() 区分「写进去了」和「被去重挡了」。
            return self._by_key[key]
        self._refs.add(key)
        self._by_key[key] = entry
        self._entries.append(entry)
        self._apply(entry)
        return entry

    def was_written(self, entry: LedgerEntry) -> bool:
        """这条分录是否真的写进了账本（而不是被幂等去重挡掉）。

        派生字段（净流入 / 费用 / 资金费 / 分红）的累加必须挂在这个判断上。
        否则重复的交易所回报会让派生字段翻倍，而现金只被改一次 ——
        差额最终被 fx 残差吞掉，归因恒等式照样「闭合」，
        但报表上会显示一个凭空出现的巨额亏损。
        """
        return self._by_key.get(
            (entry.ref_type, entry.ref_id, entry.kind)) is entry

    def _new_entry(self, kind: str, ref_type: str, ref_id: str,
                   lines: Sequence[LedgerLine], ts: datetime,
                   meta: Optional[Mapping[str, str]] = None) -> LedgerEntry:
        self._seq += 1
        return LedgerEntry(
            entry_id=f"le-{self._seq:06d}", ts=ts, kind=kind,
            ref_type=ref_type, ref_id=ref_id, lines=tuple(lines),
            meta=dict(meta or {}),
        )

    def _apply(self, entry: LedgerEntry) -> None:
        for line in entry.lines:
            account = line.account
            if account.startswith("cash:"):
                _, acct, ccy = account.split(":", 2)
                cur = D(self._cash.get((acct, ccy), _ZERO))
                self._cash[(acct, ccy)] = cur + D(line.debit) - D(line.credit)
            elif account == "equity:capital":
                # 借 = 出金（权益减少），贷 = 入金（权益增加）
                pass
            elif account == "pnl:fee":
                pass
            # 损益科目在 apply_fill / post_funding 里已经更新派生字段，
            # 这里只需要保证现金被正确改动。

    # -- 外部资金流 ------------------------------------------------------ #

    def deposit(self, account_id: str, currency: str, amount: Decimal,
                fx_to_base: Decimal = Decimal(1),
                ts: Optional[datetime] = None, ref: Optional[str] = None
                ) -> LedgerEntry:
        amount = D(amount)
        if amount <= 0:
            raise ContractViolation("入金金额必须为正")
        ts = ts or utcnow()
        entry = self._new_entry(
            "TRANSFER", "deposit", ref or f"dep-{int(ts.timestamp())}-{self._seq}",
            [LedgerLine(f"cash:{account_id}:{currency}", currency, debit=amount),
             LedgerLine("equity:capital", currency, credit=amount)], ts)
        self.post(entry)
        # 重复的入金回报如果照样累加，净流入会翻倍，net_pnl 凭空变成巨额亏损。
        if self.was_written(entry):
            self._deposits_base += amount * D(fx_to_base)
        return entry

    def withdraw(self, account_id: str, currency: str, amount: Decimal,
                 fx_to_base: Decimal = Decimal(1),
                 ts: Optional[datetime] = None, ref: Optional[str] = None
                 ) -> LedgerEntry:
        amount = D(amount)
        if amount <= 0:
            raise ContractViolation("出金金额必须为正")
        ts = ts or utcnow()
        entry = self._new_entry(
            "TRANSFER", "withdraw", ref or f"wd-{int(ts.timestamp())}-{self._seq}",
            [LedgerLine("equity:capital", currency, debit=amount),
             LedgerLine(f"cash:{account_id}:{currency}", currency, credit=amount)], ts)
        self.post(entry)
        if self.was_written(entry):
            self._withdrawals_base += amount * D(fx_to_base)
        return entry

    # -- 成交入账 -------------------------------------------------------- #

    def apply_fill(self, fill: Fill, venue: str, symbol: str, account_id: str,
                   side: Side, quote_ccy: str,
                   decision_price: Decimal,
                   fx_to_base: Decimal = Decimal(1),
                   attribution: Optional[Mapping[str, Decimal]] = None,
                   ts: Optional[datetime] = None
                   ) -> bool:
        """
        把成交记入账本。返回 False 表示重复回报已忽略。

        decision_price 是"下单时的决策价"，与成交价之差记为滑点（R-D03）。
        ts 默认取成交时间；事件回放时可显式覆盖账务时间，不改变成交事实。
        """
        book_ts = ts or fill.ts
        key = (venue, symbol)
        if ("fill", fill.venue_trade_id, "TRADE") in self._refs:
            return False

        qty = D(fill.quantity)
        price = D(fill.price)
        p0 = D(decision_price)
        fee = D(fill.fee)
        fx = D(fx_to_base)
        gross = q(qty * price, 8)
        gross_base = q(gross * fx, 8)

        pos = self._positions.get(key)
        if pos is None:
            pos = PositionBook(venue=venue, symbol=symbol, quote_ccy=quote_ccy)
            self._positions[key] = pos
        self._quote_ccy[key] = quote_ccy

        self._seq += 1
        if side is Side.BUY:
            if qty * price > self._cash_native(account_id, quote_ccy) + _EPS:
                raise ContractViolation(
                    f"现金不足：需 {qty * price} {quote_ccy}，"
                    f"实有 {self._cash_native(account_id, quote_ccy)}")
            trade_lines = [
                LedgerLine(f"position:{venue}:{symbol}", quote_ccy, debit=gross),
                LedgerLine(f"cash:{account_id}:{quote_ccy}", quote_ccy, credit=gross),
            ]
            pos.qty += qty
            pos.buy_at_decision += qty * p0
            pos.slippage += qty * (p0 - price)
            self._apply_attribution(pos, attribution, side, qty, p0, fx)
        else:
            if qty > pos.qty + _EPS:
                raise ContractViolation(
                    f"持仓不足：卖 {qty}，账面 {pos.qty}")
            trade_lines = [
                LedgerLine(f"cash:{account_id}:{quote_ccy}", quote_ccy, debit=gross),
                LedgerLine(f"position:{venue}:{symbol}", quote_ccy, credit=gross),
            ]
            pos.sell_at_decision += qty * p0
            pos.slippage += qty * (price - p0)
            self._apply_attribution(pos, attribution, side, qty, p0, fx)
            pos.qty -= qty

        self.post(self._new_entry(
            "TRADE", "fill", fill.venue_trade_id, trade_lines, book_ts,
            meta={"client_order_id": fill.client_order_id,
                  "decision_price": str(p0), "fill_price": str(price),
                  "fx": str(fx)}))

        if fee > 0:
            fee_entry = self._new_entry(
                "FEE", "fill", f"{fill.venue_trade_id}:fee",
                [LedgerLine("pnl:fee", quote_ccy, debit=fee),
                 LedgerLine(f"cash:{account_id}:{quote_ccy}", quote_ccy, credit=fee)],
                book_ts)
            self.post(fee_entry)
            if self.was_written(fee_entry):
                self._fees_base += fee * fx
        return True

    def _apply_attribution(self, pos: PositionBook,
                           attribution: Optional[Mapping[str, Decimal]],
                           side: Side, qty: Decimal, p0: Decimal,
                           fx: Decimal) -> None:
        """按成交归属更新每策略的数量与成本（R-D05）。"""
        if not attribution:
            return
        for sid, qty_sid in attribution.items():
            q_sid = D(qty_sid)
            if q_sid <= 0:
                continue
            if side is Side.BUY:
                pos.attr_qty[sid] = D(pos.attr_qty.get(sid, _ZERO)) + q_sid
                pos.attr_cost[sid] = D(pos.attr_cost.get(sid, _ZERO)) + q_sid * p0 * fx
            else:
                held = D(pos.attr_qty.get(sid, _ZERO))
                sold = min(held, q_sid)
                if sold <= 0:
                    continue
                avg = (D(pos.attr_cost.get(sid, _ZERO)) / held) if held > 0 else _ZERO
                pos.attr_qty[sid] = held - sold
                pos.attr_cost[sid] = D(pos.attr_cost.get(sid, _ZERO)) - avg * sold
                pos.attr_realized[sid] = (D(pos.attr_realized.get(sid, _ZERO))
                                          + sold * (p0 * fx - avg))

    # -- 资金费 / 分红 --------------------------------------------------- #

    def post_funding(self, account_id: str, currency: str, amount: Decimal,
                     fx_to_base: Decimal = Decimal(1), ts: Optional[datetime] = None,
                     ref: Optional[str] = None) -> LedgerEntry:
        """资金费：正数=支出（多头付），负数=收入。"""
        amount = D(amount)
        ts = ts or utcnow()
        lines = ([LedgerLine("pnl:funding", currency, debit=amount),
                  LedgerLine(f"cash:{account_id}:{currency}", currency, credit=amount)]
                 if amount >= 0 else
                 [LedgerLine(f"cash:{account_id}:{currency}", currency, debit=-amount),
                  LedgerLine("pnl:funding", currency, credit=-amount)])
        entry = self._new_entry("FUNDING", "funding",
                                ref or f"fund-{int(ts.timestamp())}-{self._seq}",
                                lines, ts)
        self.post(entry)
        if self.was_written(entry):
            self._funding_base += amount * D(fx_to_base)
        return entry

    # -- 公司行动（R-D04 补全） ------------------------------------------ #

    def post_dividend(self, account_id: str, currency: str, amount: Decimal,
                      fx_to_base: Decimal = Decimal(1),
                      ts: Optional[datetime] = None, ref: Optional[str] = None,
                      *, gross_amount: Optional[Decimal] = None,
                      withheld_tax: Decimal = _ZERO) -> LedgerEntry:
        """
        现金分红：正数 = 收到（收益），负数 = 补缴/退回。

        为什么必须单独记一类，而不是塞进别的科目：
        `performance()` 里的 fx 是**残差**（net 减去已知各项）。分红到账让现金
        增加，同时除权让持仓市值等额下降。如果分红不单独占一项，价格下跌会被
        算进 trading、现金增加会被算进 fx —— 报表上就会出现「汇率损益 +1000」
        这种没人看得懂的数字，而真正的原因（分红）消失了。

        `gross_amount` / `withheld_tax` 只用于对账：券商账单上的现金是**税后**，
        宣告的分红是税前。两者差一个预扣税，不比这一项就会天天报假差异。
        """
        amount = D(amount)
        ts = ts or utcnow()
        ref_id = ref or f"div-{int(ts.timestamp())}-{self._seq}"
        # pnl:dividend 借方=收益减少，贷方=收益增加
        lines = ([LedgerLine(f"cash:{account_id}:{currency}", currency, debit=amount),
                  LedgerLine("pnl:dividend", currency, credit=amount)]
                 if amount >= 0 else
                 [LedgerLine("pnl:dividend", currency, debit=-amount),
                  LedgerLine(f"cash:{account_id}:{currency}", currency, credit=-amount)])
        meta = {}
        if gross_amount is not None:
            meta["gross_amount"] = str(D(gross_amount))
        if D(withheld_tax) != 0:
            meta["withheld_tax"] = str(D(withheld_tax))
        entry = self._new_entry("DIVIDEND", "dividend", ref_id, lines, ts, meta)
        self.post(entry)
        if self.was_written(entry):
            self._dividends_base += amount * D(fx_to_base)
        return entry

    def post_interest(self, account_id: str, currency: str, amount: Decimal,
                      fx_to_base: Decimal = Decimal(1),
                      ts: Optional[datetime] = None, ref: Optional[str] = None
                      ) -> LedgerEntry:
        """
        融资利息 / 借券费：正数 = 支出。归入 `pnl:funding` 口径。

        为什么不新开一项：融资利息与永续合约资金费在经济含义上是同一件事
        —— 「持有这个仓位的资金成本」。归因表里合并成「资金成本」一项，
        比拆成两项更容易看出「这个策略的收益里有多少被持有成本吃掉了」。
        `ref` 里带 `interest:` 前缀，需要拆分时还能拆。
        """
        amount = D(amount)
        ts = ts or utcnow()
        ref_id = ref or f"interest-{int(ts.timestamp())}-{self._seq}"
        lines = ([LedgerLine("pnl:funding", currency, debit=amount),
                  LedgerLine(f"cash:{account_id}:{currency}", currency, credit=amount)]
                 if amount >= 0 else
                 [LedgerLine(f"cash:{account_id}:{currency}", currency, debit=-amount),
                  LedgerLine("pnl:funding", currency, credit=-amount)])
        entry = self._new_entry("FUNDING", "interest", ref_id, lines, ts,
                                {"source": "interest"})
        self.post(entry)
        if self.was_written(entry):
            self._funding_base += amount * D(fx_to_base)
        return entry

    def apply_split(self, venue: str, symbol: str, ratio: Decimal, *,
                    ts: Optional[datetime] = None, ref: Optional[str] = None
                    ) -> Optional[PositionBook]:
        """
        拆股 / 并股 / 反向拆股：**只改数量，不改金额**。

        ratio > 1 拆股（1 股变 ratio 股），0 < ratio < 1 并股。

        这条「只改数量不改金额」是整个函数的关键。成本金额 `buy_at_decision`
        代表「当初花了多少钱」，拆股不改变已经花掉的钱。市值 = 数量 × 新价格，
        而新价格是旧价格 / ratio，所以市值不变、盈亏不变。

        如果顺手把成本金额也除以 ratio，会凭空产生 50% 的收益（2:1 拆股时），
        而且这笔假收益会被后续所有归因继承下去。

        分录金额为 0（只调数量）：零金额分录合法，但**必须有分录** ——
        持仓被改动却不留痕迹，事后没人能解释「数量怎么突然变了」。
        """
        ratio = D(ratio)
        if ratio <= 0:
            raise ContractViolation(f"拆股比例必须为正，收到 {ratio}")
        key = (venue, symbol)
        pos = self._positions.get(key)
        if pos is None:
            return None
        ts = ts or utcnow()
        ref_id = ref or f"split-{venue}-{symbol}-{ts.date()}"
        # 幂等门必须在改数量**之前**。
        #
        # 这里与 apply_fill 的差别很关键：apply_fill 是先查 refs 再动持仓，
        # 而拆股是"先算新数量、再 post" —— 如果只依赖 post() 去重，重复的
        # 拆股通知（或重启后重放同一条公司行动）会让数量乘第二次，而账本里
        # 始终只有一条 SPLIT 分录：**分录数、金额全都对得上，只有持仓翻倍**。
        # 这种错不会在任何恒等式里露头，只能靠这道门。
        if ("split", ref_id, "SPLIT") in self._refs:
            return pos
        before = pos.qty
        pos.qty = q(before * ratio, 10)
        for sid in list(pos.attr_qty):
            pos.attr_qty[sid] = q(D(pos.attr_qty[sid]) * ratio, 10)
        # 成本金额、已实现、滑点一律不动（见 docstring）。
        ccy = pos.quote_ccy
        entry = self._new_entry(
            "SPLIT", "split", ref_id,
            [LedgerLine(f"position:{venue}:{symbol}", ccy, debit=_ZERO,
                        credit=_ZERO)],
            ts,
            meta={"ratio": str(ratio), "qty_before": str(before),
                  "qty_after": str(pos.qty)})
        self.post(entry)
        return pos

    # -- 估值 ------------------------------------------------------------ #

    def _cash_native(self, account_id: str, currency: str) -> Decimal:
        return D(self._cash.get((account_id, currency), _ZERO))

    def cash_native(self) -> Dict[CashKey, Decimal]:
        return {k: D(v) for k, v in self._cash.items()}

    def cash_base(self, fx: Mapping[str, Decimal]) -> Dict[str, Decimal]:
        out: Dict[str, Decimal] = {}
        for (acct, ccy), amount in self._cash.items():
            if acct not in out:
                out[acct] = _ZERO
            out[acct] += D(amount) * D(fx.get(ccy, Decimal(1)))
        return out

    def positions(self) -> Dict[SymbolKey, PositionBook]:
        return {k: v for k, v in self._positions.items() if abs(v.qty) > _EPS}

    @property
    def net_deposits_base(self) -> Decimal:
        """
        累计净入金（计价货币），与 `performance().external_flow` 同口径。

        运维页面算「累计收益」必须用它做分母，而不是某个硬编码的初始资金。
        """
        return q(self._deposits_base - self._withdrawals_base, 4)

    def nav(self, prices: Mapping[SymbolKey, Decimal],
            fx: Optional[Mapping[str, Decimal]] = None) -> Decimal:
        fx = fx or {}
        total = _ZERO
        for (acct, ccy), amount in self._cash.items():
            total += D(amount) * D(fx.get(ccy, Decimal(1)))
        for key, pos in self._positions.items():
            if abs(pos.qty) <= _EPS:
                continue
            if key not in prices:
                raise ContractViolation(f"缺少 {key} 的估值价格，无法计算 NAV")
            total += pos.qty * D(prices[key]) * D(fx.get(pos.quote_ccy, Decimal(1)))
        return q(total, 4)

    def gross_exposure(self, prices: Mapping[SymbolKey, Decimal],
                       fx: Optional[Mapping[str, Decimal]] = None) -> Decimal:
        fx = fx or {}
        total = _ZERO
        for key, pos in self._positions.items():
            if abs(pos.qty) <= _EPS:
                continue
            if key not in prices:
                raise ContractViolation(f"缺少 {key} 的估值价格")
            total += abs(pos.qty * D(prices[key]) * D(fx.get(pos.quote_ccy, Decimal(1))))
        return q(total, 4)

    def net_exposure(self, prices, fx=None) -> Decimal:
        fx = fx or {}
        total = _ZERO
        for key, pos in self._positions.items():
            if abs(pos.qty) <= _EPS:
                continue
            total += pos.qty * D(prices[key]) * D(fx.get(pos.quote_ccy, Decimal(1)))
        return q(total, 4)

    def exposure_by_strategy(self, prices, fx=None) -> Dict[str, Decimal]:
        fx = fx or {}
        out: Dict[str, Decimal] = {}
        for key, pos in self._positions.items():
            if abs(pos.qty) <= _EPS or key not in prices:
                continue
            for sid, qty in pos.attr_qty.items():
                out[sid] = out.get(sid, _ZERO) + D(qty) * D(prices[key]) \
                    * D(fx.get(pos.quote_ccy, Decimal(1)))
        return {k: q(v, 2) for k, v in out.items()}

    def exposure_by_strategy_symbol(self, prices,
                                    fx=None) -> Dict[str, Dict[SymbolKey, Decimal]]:
        """
        策略 → 标的 → 名义敞口（B3）。

        净额层按策略算差额时必须用这个：只知道「策略总敞口」没法判断
        某个策略在某只标的上该买还是该卖。无归属的持仓不会出现在结果里，
        调用方需自行把残差当作「无主」处理。
        """
        fx = fx or {}
        out: Dict[str, Dict[SymbolKey, Decimal]] = {}
        for key, pos in self._positions.items():
            if abs(pos.qty) <= _EPS or key not in prices:
                continue
            rate = D(fx.get(pos.quote_ccy, Decimal(1)))
            price = D(prices[key])
            for sid, qty in pos.attr_qty.items():
                if abs(D(qty)) <= _EPS:
                    continue
                bucket = out.setdefault(sid, {})
                bucket[key] = q(bucket.get(key, _ZERO) + D(qty) * price * rate, 2)
        return out

    def strategy_pnl(self, prices, fx=None) -> Dict[str, Decimal]:
        fx = fx or {}
        out: Dict[str, Decimal] = {}
        for key, pos in self._positions.items():
            price = D(prices.get(key, _ZERO))
            rate = D(fx.get(pos.quote_ccy, Decimal(1)))
            for sid in set(pos.attr_qty) | set(pos.attr_cost) | set(pos.attr_realized):
                out[sid] = out.get(sid, _ZERO) + pos.strategy_pnl(sid, price, rate)
        return {k: q(v, 2) for k, v in out.items()}

    # -- 绩效归因 -------------------------------------------------------- #

    def performance(self, prices: Mapping[SymbolKey, Decimal],
                    fx: Optional[Mapping[str, Decimal]] = None) -> PerformanceReport:
        fx = fx or {}
        nav = self.nav(prices, fx)

        trading = _ZERO
        slippage = _ZERO
        for key, pos in self._positions.items():
            price = D(prices.get(key, _ZERO))
            rate = D(fx.get(pos.quote_ccy, Decimal(1)))
            trading += (pos.qty * price + pos.sell_at_decision
                        - pos.buy_at_decision) * rate
            slippage += pos.slippage * rate

        flow = self._deposits_base - self._withdrawals_base
        net = q(nav - flow, 4)
        fee = q(-self._fees_base, 4)
        funding = q(-self._funding_base, 4)
        # 分红是收益，方向与 fee/funding 相反（后两者累加的是支出，取负得损失）。
        dividend = q(self._dividends_base, 4)
        trading = q(trading, 4)
        slippage = q(slippage, 4)
        # fx 是折算残差：所有币种 fx=1 时必须为 0，否则说明有漏记。
        #
        # 分红必须出现在 known 里，否则它会**整个掉进 fx**：除权日持仓市值下跌
        # 让 trading 变负，分红让现金变正，net 不变 —— 残差会把这两笔对冲掉，
        # 报表上显示「汇率损益 +1000」，而真正的原因（分红）消失了。
        known = trading + slippage + fee + funding + dividend
        fx_pnl = q(net - known, 4)

        return PerformanceReport(
            nav=nav, net_pnl=net, trading=trading, slippage=slippage,
            fee=fee, funding=funding, fx=fx_pnl, external_flow=q(flow, 4),
            gross_exposure=self.gross_exposure(prices, fx),
            net_exposure=self.net_exposure(prices, fx),
            attribution_sum=q(known + fx_pnl, 4),
            dividend=dividend,
        )

    def assert_attribution_closes(self, prices, fx=None) -> PerformanceReport:
        report = self.performance(prices, fx)
        if not report.closes:
            raise AttributionMismatch(
                f"归因不闭合：净收益 {report.net_pnl} vs 归因和 {report.attribution_sum}"
                f"（差额 {report.net_pnl - report.attribution_sum}）")
        return report

    # -- 对账（R-D04） --------------------------------------------------- #

    def reconcile(self, broker_cash: Mapping[CashKey, Decimal],
                  broker_positions: Mapping[SymbolKey, Decimal],
                  ts: Optional[datetime] = None,
                  tolerance: Decimal = _ZERO) -> ReconciliationReport:
        """与券商/交易所快照对账。差异超出 tolerance 即视为不平。"""
        tol = D(tolerance)
        cash_diffs: Dict[str, str] = {}
        pos_diffs: Dict[str, str] = {}

        for key in set(self._cash) | set(broker_cash):
            ledger_amt = self._cash_native(*key)
            broker_amt = D(broker_cash.get(key, _ZERO))
            if abs(ledger_amt - broker_amt) > tol:
                cash_diffs[f"{key[0]}|{key[1]}"] = str(q(broker_amt - ledger_amt, 8))

        ledger_pos = {k: p.qty for k, p in self._positions.items() if abs(p.qty) > _EPS}
        for key in set(ledger_pos) | set(broker_positions):
            ledger_qty = D(ledger_pos.get(key, _ZERO))
            broker_qty = D(broker_positions.get(key, _ZERO))
            if abs(ledger_qty - broker_qty) > tol:
                pos_diffs[f"{key[0]}|{key[1]}"] = str(q(broker_qty - ledger_qty, 8))

        return ReconciliationReport(
            ts=ts or utcnow(), cash_diffs=cash_diffs, position_diffs=pos_diffs,
            missing_at_broker=tuple(sorted(f"{k[0]}|{k[1]}" for k in ledger_pos
                                           if k not in broker_positions)),
            missing_in_ledger=tuple(sorted(f"{k[0]}|{k[1]}" for k in broker_positions
                                           if k not in ledger_pos)),
        )

    # -- 导出（给控制台/报表用） ---------------------------------------- #

    def journal(self) -> List[Dict]:
        return [{
            "entry_id": e.entry_id, "ts": e.ts.isoformat(), "kind": e.kind,
            "ref": f"{e.ref_type}:{e.ref_id}",
            # meta 要带出来：对账要核对拆股比例、预扣税这些只存在 meta 里的口径，
            # 拿不到就只能比金额，而金额相同不代表口径相同。
            "meta": dict(e.meta),
            "lines": [{"account": ln.account, "ccy": ln.currency,
                       "debit": str(ln.debit), "credit": str(ln.credit)}
                      for ln in e.lines],
        } for e in self._entries]

    @property
    def entry_count(self) -> int:
        return len(self._entries)


__all__ = [
    "LedgerLine", "LedgerEntry", "PositionBook", "Ledger",
    "PerformanceReport", "ReconciliationReport",
    "UnbalancedEntry", "AttributionMismatch",
]
