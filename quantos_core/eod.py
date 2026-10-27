"""
T-402 · 日终四项对账  +  T-403 · 归因日报  +  净值时序

三个东西放一个模块，因为它们本来就是同一件事的三个面：
没有净值时序就无法算"当日"归因，没有日终对账就不敢相信净值。

T-402 四项对账
==============
B3 之前只对现金和持仓（B0 的 `Ledger.reconcile`）。真钱场景下漏掉的两项
恰恰是最容易出事的：

    挂单  本地以为还挂着、券商那边已经结束了 —— 成交回报丢了；
          或者券商那边还挂着、本地没记录 —— 本地根本不知道有这么笔单。
    成交  券商有、本地没有 —— 账本少记一笔真实成交（虚高净值）；
          本地有、券商没有 —— 本地记了券商不认的成交（多为 B2 的 synthetic 补录）。

**拿不到券商账单 ≠ 对上了。** `StatementUnavailable` 一律判为不干净，
因为"没对成"和"对平了"在净值上看起来一样，但在风险上完全相反。

T-403 归因日报
==============
五项：策略收益 / 汇率 / 手续费 / 滑点 / 资金费。加起来必须等于当日净收益。

关于"五项之和等于净收益"这件事，必须说清楚：
    汇率项是**按残差算出来的**，所以这个等式在构造上恒成立 —— 它证明不了什么。
    真正能证明"没有漏记"的是另外两条独立校验：
      · 单一计价货币（所有汇率 = 1）时，汇率项必须**精确为 0**
      · 手续费项必须 ≤ 0（费用只会是负贡献）
    这两条如果破了，说明有一笔钱记到了不该记的地方。
报表里 `warnings` 会把这些不成立的项列出来，而不是假装闭合就完事。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Callable, Dict, List, Mapping, Optional, Protocol, Tuple

from .contracts import D, OrderState, q, utcnow
from .ledger import Ledger

__all__ = [
    "DiffKind", "Diff", "BrokerStatement", "BrokerStatementGateway",
    "StatementUnavailable", "EodReport", "EodReconciler",
    "NavPoint", "NavSeries", "DailyAttribution", "EodRun", "EodService",
    "build_daily_attribution", "ledger_trade_ids",
]

CashKey = Tuple[str, str]        # (account_id, currency)
SymbolKey = Tuple[str, str]      # (venue, symbol)

#: 浮点噪声容差。与 `ledger` 保持同一口径（两边判断"是否相等"必须一致，
#: 否则会出现账本认为闭合、日报认为不闭合的分裂）。
_EPS = Decimal("0.000001")


# --------------------------------------------------------------------------- #
#  差异
# --------------------------------------------------------------------------- #

class DiffKind(str, Enum):
    CASH = "cash"
    POSITION = "position"
    OPEN_ORDER = "open_order"
    TRADE = "trade"
    CORPORATE_ACTION = "corporate_action"


#: 每类差异「谁更危险」。用来排序，让运维先看最要命的。
#
#: 公司行动排在持仓/现金**之前**，因为它是根因：未入账的拆股会让持仓数量
#: 和成本价一起错，未入账的分红会让现金差异显得毫无来由。先修根因，
#: 剩下的差异往往自己就消失了 —— 反过来先改数字只会把根因埋掉。
_SEVERITY = {
    DiffKind.TRADE: 0,              # 漏记成交会直接让净值失真
    DiffKind.OPEN_ORDER: 1,         # 幽灵挂单可能随时成交
    DiffKind.CORPORATE_ACTION: 2,   # 根因：会让持仓与现金一起错
    DiffKind.POSITION: 3,
    DiffKind.CASH: 4,
}


@dataclass(frozen=True)
class Diff:
    kind: DiffKind
    key: str
    local: str
    broker: str
    delta: str = ""
    note: str = ""

    def to_dict(self) -> Dict:
        return {"kind": self.kind.value, "key": self.key, "local": self.local,
                "broker": self.broker, "delta": self.delta, "note": self.note}


# --------------------------------------------------------------------------- #
#  券商账单
# --------------------------------------------------------------------------- #

class StatementUnavailable(RuntimeError):
    """拿不到账单（网络/权限/券商维护）。此时必须判不干净，不许猜。"""


@dataclass(frozen=True)
class DeclaredAction:
    """券商账单里声明的公司行动。

    只放"券商说是这样"的事实，不放"账本记成怎样"。两边的比对在
    `EodReconciler._corporate_actions` 里做。
    """

    kind: str                        # dividend / split / interest
    venue: str = ""
    symbol: str = ""
    #: split 用：1 股变 ratio 股（并股给 0.5）
    ratio: Decimal = Decimal(0)
    #: dividend / interest 用：金额（币种见 currency）
    amount: Decimal = Decimal(0)
    currency: str = ""
    ref: str = ""
    #: 税前宣告额（分红才有）。账单现金是税后，比总额会天天报假差异。
    gross_amount: Decimal = Decimal(0)
    withheld_tax: Decimal = Decimal(0)

    def key(self) -> str:
        if self.kind == "split":
            return f"{self.venue}:{self.symbol}"
        return f"{self.currency or self.venue}:{self.symbol or self.kind}"


@dataclass(frozen=True)
class BrokerStatement:
    """券商侧的日终事实。这是对账的唯一外部真相，本地账本只能向它靠。"""

    account_id: str
    day: date
    as_of: datetime
    cash: Mapping[CashKey, Decimal] = field(default_factory=dict)
    positions: Mapping[SymbolKey, Decimal] = field(default_factory=dict)
    open_client_order_ids: Tuple[str, ...] = ()
    trade_ids: Tuple[str, ...] = ()
    #: 当日公司行动（分红 / 拆股 / 融资利息）。空元组 = 券商声明"今天没有"。
    corporate_actions: Tuple[DeclaredAction, ...] = ()


class BrokerStatementGateway(Protocol):
    """生产实现是券商适配器；测试里是 FakeStatementGateway。"""

    def fetch_statement(self, account_id: str, day: date) -> BrokerStatement: ...


# --------------------------------------------------------------------------- #
#  T-402 报告与执行
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class EodReport:
    account_id: str
    day: date
    ts: datetime
    diffs: Tuple[Diff, ...] = ()
    statement_ok: bool = True
    statement_error: str = ""
    #: 只在 statement_ok 时才有意义的五项计数
    counts: Mapping[str, int] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        """
        干净 = 账单拿到了 **且** 五项差异为 0。

        `statement_ok=False` 时恒不干净 —— 这是本模块最重要的一行。
        """
        return self.statement_ok and not self.diffs

    def by_kind(self, kind: DiffKind) -> Tuple[Diff, ...]:
        return tuple(d for d in self.diffs if d.kind is kind)

    def to_dict(self) -> Dict:
        return {
            "account_id": self.account_id,
            "day": self.day.isoformat(),
            "ts": self.ts.isoformat(),
            "clean": self.clean,
            "statement_ok": self.statement_ok,
            "statement_error": self.statement_error,
            "counts": dict(self.counts),
            "diffs": [d.to_dict() for d in self.diffs],
        }


class EodReconciler:
    """
    四项对账：现金 / 持仓 / 挂单 / 成交。

    只读：不改账本、不改订单。差异要人去判断该怎么修 —— 自动"抹平差异"
    是对账系统最危险的功能。
    """

    def __init__(self, ledger: Ledger, registry,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self.ledger = ledger
        self.registry = registry
        self.clock = clock

    # -- 主流程 ---------------------------------------------------------- #

    def run(self, account_id: str, day: date,
            gateway: BrokerStatementGateway, *,
            cash_tolerance: Decimal = Decimal("0.01"),
            position_tolerance: Decimal = Decimal("0"),
            now: Optional[datetime] = None) -> EodReport:
        now = now or self.clock()
        try:
            stmt = gateway.fetch_statement(account_id, day)
        except Exception as exc:                      # noqa: BLE001
            # 任何取数失败都归为"没对成"。不区分网络还是权限，因为结果一样：
            # 我们现在不知道券商那边是什么状态。
            return EodReport(
                account_id=account_id, day=day, ts=now,
                statement_ok=False, statement_error=f"{type(exc).__name__}: {exc}")

        diffs: List[Diff] = []
        # 公司行动放第一位算：它是现金差异和持仓差异的常见根因，
        # 先算出来，后面两项的 note 才有上下文可指。
        diffs += self._corporate_actions(day, stmt)
        diffs += self._cash(account_id, stmt, D(cash_tolerance))
        diffs += self._positions(stmt, D(position_tolerance))
        diffs += self._open_orders(stmt)
        diffs += self._trades(day, stmt)

        diffs.sort(key=lambda d: (_SEVERITY[d.kind], d.key))
        counts = {
            "cash": len([d for d in diffs if d.kind is DiffKind.CASH]),
            "position": len([d for d in diffs if d.kind is DiffKind.POSITION]),
            "open_order": len([d for d in diffs if d.kind is DiffKind.OPEN_ORDER]),
            "trade": len([d for d in diffs if d.kind is DiffKind.TRADE]),
            "corporate_action": len([d for d in diffs
                                     if d.kind is DiffKind.CORPORATE_ACTION]),
        }
        return EodReport(account_id=account_id, day=day, ts=now, diffs=tuple(diffs),
                         statement_ok=True, counts=counts)

    # -- 公司行动（第五项） ---------------------------------------------- #

    def _corporate_actions(self, day: date, stmt: BrokerStatement) -> List[Diff]:
        """
        券商声明的公司行动，本地是否都入账了。

        为什么要单独一项，而不是让它以「现金差异 + 持仓差异」的形式暴露：
        拆股未入账时，持仓项会报「本地 100 / 券商 200」，现金项会报
        「券商比账面多 1000」—— 看起来像两个互不相关的错误，
        而真实原因只有一个。拆开报能让人一次修对，也不会误改账本去"凑数"。

        只读：发现未入账只报告，**绝不自动补记**。补记需要知道正确的成本
        分摊方式（尤其是按策略分摊），那必须人来定。
        """
        declared = tuple(getattr(stmt, "corporate_actions", ()) or ())
        if not declared:
            return []

        todays = [e for e in self.ledger.journal()
                  if str(e["ts"]).startswith(day.isoformat())]
        out: List[Diff] = []

        for act in declared:
            matched, note = self._match_action(act, todays)
            if matched is None:
                out.append(Diff(
                    DiffKind.CORPORATE_ACTION, f"{act.kind}:{act.key()}",
                    "未入账", self._describe_action(act), "",
                    f"券商声明了 {act.kind} 但账本当日没有对应分录。{note}"))
                continue
            mismatch = self._action_mismatch(act, matched)
            if mismatch:
                out.append(Diff(
                    DiffKind.CORPORATE_ACTION, f"{act.kind}:{act.key()}",
                    "已入账但口径不同", self._describe_action(act), "",
                    mismatch))
        return out

    @staticmethod
    def _describe_action(act: DeclaredAction) -> str:
        if act.kind == "split":
            return f"比例 {act.ratio}"
        tail = f"税后 {act.amount} {act.currency}"
        if D(act.withheld_tax) != 0:
            tail += f"（税前 {act.gross_amount}，预扣税 {act.withheld_tax}）"
        return f"{act.kind} {tail}"

    @staticmethod
    def _match_action(act: DeclaredAction, entries: List[Dict]):
        """
        找对应的本地分录。

        匹配顺序：ref 精确 → 类型 + 关键字段。**不按金额单独匹配**：
        当日两笔金额相同的分红会被认成同一笔，于是"漏了一笔"永远查不出来。
        """
        want_kind = {"dividend": "DIVIDEND", "split": "SPLIT",
                     "interest": "FUNDING"}.get(act.kind)
        if want_kind is None:
            return None, f"未知的公司行动类型 {act.kind!r}，无法判定是否已入账"
        candidates = [e for e in entries if e["kind"] == want_kind]
        if act.kind == "interest":
            # 融资利息与永续资金费共用 FUNDING 科目，靠 ref 前缀区分。
            candidates = [e for e in candidates if str(e["ref"]).startswith("interest:")]
        if not candidates:
            return None, ("拆股会让持仓数量与成本价同时失真，后续所有 PnL 都跟着错"
                          if act.kind == "split" else
                          "现金差异往往就是它造成的")
        if act.ref:
            for e in candidates:
                if act.ref in str(e["ref"]):
                    return e, ""
        if act.kind == "split":
            for e in candidates:
                if act.symbol and act.symbol in str(e["ref"]):
                    return e, ""
            return candidates[0], ""
        # dividend / interest：同币种且金额对上才算
        for e in candidates:
            hit = sum(abs(D(ln["debit"]) - D(ln["credit"])) for ln in e["lines"]
                      if ln["ccy"] == act.currency)
            if abs(hit - abs(D(act.amount))) <= Decimal("0.01"):
                return e, ""
        return None, "账本当日有同类分录，但金额对不上（可能只记了一部分）"

    @staticmethod
    def _action_mismatch(act: DeclaredAction, entry: Dict) -> str:
        """已匹配上，再核对口径（比例 / 金额 / 税）。"""
        if act.kind == "split":
            got = entry.get("meta", {}).get("ratio")
            if got is None:
                return "本地拆股分录缺少 ratio 记录，无法核对比例"
            if abs(D(got) - D(act.ratio)) > Decimal("1e-9"):
                return (f"拆股比例不一致：券商 {act.ratio}，本地 {got}。"
                        f"比例错会让持仓数量和成本价同时偏，务必先修这一条")
            return ""
        got_tax = entry.get("meta", {}).get("withheld_tax")
        if D(act.withheld_tax) != 0 and (got_tax is None
                                         or abs(D(got_tax) - D(act.withheld_tax)) > Decimal("0.01")):
            return (f"预扣税不一致：券商 {act.withheld_tax}，本地 {got_tax or '未记录'}。"
                    f"券商的现金是税后额，只比总额会把税差当成漏账")
        return ""

    # -- 五项 ------------------------------------------------------------ #

    def _cash(self, account_id: str, stmt: BrokerStatement,
              tol: Decimal) -> List[Diff]:
        local = {k: v for k, v in self.ledger.cash_native().items()
                 if k[0] == account_id}
        broker = {k: D(v) for k, v in stmt.cash.items() if k[0] == account_id}
        out: List[Diff] = []
        for key in sorted(set(local) | set(broker)):
            lv, bv = D(local.get(key, Decimal(0))), D(broker.get(key, Decimal(0)))
            if abs(lv - bv) > tol:
                out.append(Diff(DiffKind.CASH, f"{key[1]}",
                                str(q(lv, 8)), str(q(bv, 8)),
                                str(q(bv - lv, 8)),
                                "券商比账面多" if bv > lv else "账面比券商多"))
        return out

    def _positions(self, stmt: BrokerStatement, tol: Decimal) -> List[Diff]:
        local = {k: p.qty for k, p in self.ledger.positions().items()
                 if abs(p.qty) > _EPS}
        broker = {k: D(v) for k, v in stmt.positions.items()
                  if abs(D(v)) > _EPS}
        out: List[Diff] = []
        for key in sorted(set(local) | set(broker)):
            lv, bv = D(local.get(key, Decimal(0))), D(broker.get(key, Decimal(0)))
            if abs(lv - bv) > tol:
                note = "本地无此持仓" if key not in local else (
                    "券商无此持仓" if key not in broker else "数量不一致")
                out.append(Diff(DiffKind.POSITION, f"{key[0]}:{key[1]}",
                                str(q(lv, 8)), str(q(bv, 8)),
                                str(q(bv - lv, 8)), note))
        return out

    def _open_orders(self, stmt: BrokerStatement) -> List[Diff]:
        local = {o.client_order_id: o for o in self.registry.active_orders()}
        broker = set(stmt.open_client_order_ids)
        out: List[Diff] = []
        for cid in sorted(set(local) - broker):
            o = local[cid]
            out.append(Diff(DiffKind.OPEN_ORDER, cid,
                            f"{o.state.value} {o.symbol}",
                            "券商侧无此挂单", "",
                            "本地仍在等，券商已结束：成交/撤单回报可能丢失"))
        for cid in sorted(broker - set(local)):
            out.append(Diff(DiffKind.OPEN_ORDER, cid,
                            "本地无记录", "券商侧挂单中", "",
                            "券商挂了本地不知道的单，必须查明来源"))
        # 状态不同（本地 UNKNOWN，券商已终结）在上面的集合差里已经覆盖；
        # 两边都「在途」但本地是 UNKNOWN 的，照样要报 —— 持仓到底多少没人知道。
        for cid in sorted(broker & set(local)):
            o = local[cid]
            if o.state is OrderState.UNKNOWN:
                out.append(Diff(DiffKind.OPEN_ORDER, cid,
                                "UNKNOWN", "券商侧挂单中", "",
                                "两边都认为在途，但本地状态未知"))
        return out

    def _trades(self, day: date, stmt: BrokerStatement) -> List[Diff]:
        local_ids = ledger_trade_ids(self.ledger, day)
        broker_ids = {str(t) for t in stmt.trade_ids}
        out: List[Diff] = []
        for tid in sorted(local_ids - broker_ids):
            out.append(Diff(DiffKind.TRADE, tid, "已入账", "券商账单无此成交", "",
                            "本地记了券商不认的成交（多为 synthetic 补录，需人工确认）"))
        for tid in sorted(broker_ids - local_ids):
            out.append(Diff(DiffKind.TRADE, tid, "未入账", "券商有成交", "",
                            "账本漏记真实成交，净值会虚高"))
        return out


# --------------------------------------------------------------------------- #
#  净值时序
# --------------------------------------------------------------------------- #

def ledger_trade_ids(ledger: Ledger, day: date) -> set:
    """
    从账本分录里取当日已入账的成交号。

    用 `journal()` 而不是新开一张表：账本已经是唯一事实来源，
    再维护一份「成交号清单」就多了个会不同步的副本。

    公开成模块函数是因为券商账单的模拟端（测试/演示）也要用它 ——
    两边用同一个取数口径，否则"对账"会变成"两套口径互相校验"。
    """
    out = set()
    for entry in ledger.journal():
        if entry["kind"] != "TRADE":
            continue
        # ts 是 ISO 字符串，前缀即日期；避免再解析一次时区。
        if not str(entry["ts"]).startswith(day.isoformat()):
            continue
        ref = str(entry["ref"])
        if ref.startswith("fill:"):
            out.add(ref[len("fill:"):])
    return out


@dataclass(frozen=True)
class NavPoint:
    """一天的收盘快照。既是净值曲线数据源，也是次日归因的基线。"""

    day: str
    ts: datetime
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
    by_strategy: Mapping[str, Decimal] = field(default_factory=dict)
    #: 现金分红（B7）。放在末尾并给默认值，老快照读回来时按 0 处理。
    dividend: Decimal = Decimal(0)

    def to_dict(self) -> Dict:
        return {
            "day": self.day, "ts": self.ts.isoformat(),
            "nav": str(q(self.nav, 2)), "net_pnl": str(q(self.net_pnl, 4)),
            "trading": str(q(self.trading, 4)), "slippage": str(q(self.slippage, 4)),
            "fee": str(q(self.fee, 4)), "funding": str(q(self.funding, 4)),
            "dividend": str(q(self.dividend, 4)),
            "fx": str(q(self.fx, 4)), "external_flow": str(q(self.external_flow, 4)),
            "gross_exposure": str(q(self.gross_exposure, 2)),
            "net_exposure": str(q(self.net_exposure, 2)),
            "by_strategy": {k: str(q(v, 2)) for k, v in self.by_strategy.items()},
        }

    @classmethod
    def from_dict(cls, d: Mapping) -> "NavPoint":
        return cls(
            day=str(d["day"]), ts=datetime.fromisoformat(d["ts"]),
            nav=D(d["nav"]), net_pnl=D(d["net_pnl"]), trading=D(d["trading"]),
            slippage=D(d["slippage"]), fee=D(d["fee"]), funding=D(d["funding"]),
            fx=D(d["fx"]), external_flow=D(d["external_flow"]),
            # 老快照没有 dividend 字段（B7 之前写的），按 0 读。
            # 不能用 d["dividend"] 硬取：那会让所有历史净值文件读不出来。
            dividend=D(d.get("dividend") or 0),
            gross_exposure=D(d["gross_exposure"]),
            net_exposure=D(d["net_exposure"]),
            by_strategy={k: D(v) for k, v in (d.get("by_strategy") or {}).items()},
        )


class NavSeries:
    """
    净值时序。按日 upsert，整天重写后原子替换。

    为什么整天重写而不是追加：追加两次同一天会产生两个点，净值曲线就会
    "抖"一下；而这正是"重跑了一次日终"会发生的正常情况。幂等比性能重要。
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._points: Dict[str, NavPoint] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.isfile(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        for item in data:
            p = NavPoint.from_dict(item)
            self._points[p.day] = p

    def _flush(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        payload = [self._points[k].to_dict() for k in sorted(self._points)]
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path) or ".",
                                   prefix=".nav-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def record(self, point: NavPoint) -> NavPoint:
        self._points[point.day] = point
        self._flush()
        return point

    def points(self, limit: int = 0) -> List[NavPoint]:
        out = [self._points[k] for k in sorted(self._points)]
        return out[-limit:] if limit else out

    def previous(self, day: str) -> Optional[NavPoint]:
        """严格早于 day 的最后一个点。次日归因的基线。"""
        earlier = [k for k in self._points if k < day]
        return self._points[max(earlier)] if earlier else None

    def latest(self) -> Optional[NavPoint]:
        return self.points()[-1] if self._points else None

    def __len__(self) -> int:
        return len(self._points)


# --------------------------------------------------------------------------- #
#  T-403 归因日报
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class DailyAttribution:
    day: str
    baseline_day: str
    nav_open: Decimal
    nav_close: Decimal
    external_flow: Decimal
    net_return: Decimal
    trading: Decimal
    slippage: Decimal
    fee: Decimal
    funding: Decimal
    fx: Decimal
    by_strategy: Mapping[str, Decimal] = field(default_factory=dict)
    warnings: Tuple[str, ...] = ()
    first_day: bool = False
    #: 现金分红（B7）。给默认值以兼容按位置构造的旧调用。
    dividend: Decimal = Decimal(0)

    @property
    def attribution_sum(self) -> Decimal:
        """六项之和。分红必须在这里单独占一项，不能被 fx 残差吸收。"""
        return q(self.trading + self.slippage + self.fee + self.funding
                 + self.dividend + self.fx, 4)

    @property
    def closes(self) -> bool:
        return abs(self.attribution_sum - self.net_return) <= _EPS

    def to_dict(self) -> Dict:
        return {
            "day": self.day,
            "baseline_day": self.baseline_day,
            "first_day": self.first_day,
            "nav_open": str(q(self.nav_open, 2)),
            "nav_close": str(q(self.nav_close, 2)),
            "external_flow": str(q(self.external_flow, 4)),
            "net_return": str(q(self.net_return, 4)),
            "trading": str(q(self.trading, 4)),
            "slippage": str(q(self.slippage, 4)),
            "fee": str(q(self.fee, 4)),
            "funding": str(q(self.funding, 4)),
            "dividend": str(q(self.dividend, 4)),
            "fx": str(q(self.fx, 4)),
            "attribution_sum": str(self.attribution_sum),
            "closes": self.closes,
            "by_strategy": {k: str(q(v, 4)) for k, v in self.by_strategy.items()},
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------- #
#  EOD 服务：跑一次日终
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class EodRun:
    report: EodReport
    attribution: DailyAttribution
    point: NavPoint

    def to_dict(self) -> Dict:
        return {"report": self.report.to_dict(),
                "attribution": self.attribution.to_dict(),
                "nav_point": self.point.to_dict()}


class EodService:
    """把对账、归因、净值落库串成一次"日终"。"""

    def __init__(self, ledger: Ledger, registry, *, series: NavSeries,
                 report_dir: str = "state/eod",
                 gate=None,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self.ledger = ledger
        self.registry = registry
        self.series = series
        self.report_dir = report_dir
        self.gate = gate
        self.clock = clock
        self.reconciler = EodReconciler(ledger, registry, clock=clock)

    def run(self, account_id: str, day: date, gateway: BrokerStatementGateway,
            prices: Mapping[SymbolKey, Decimal], *,
            fx: Optional[Mapping[str, Decimal]] = None,
            cash_tolerance: Decimal = Decimal("0.01"),
            now: Optional[datetime] = None) -> EodRun:
        fx = fx or {}
        now = now or self.clock()

        report = self.reconciler.run(
            account_id, day, gateway, cash_tolerance=cash_tolerance, now=now)

        # 对账结果直接写回实盘开关：不干净就不许再武装（T-401 × T-402 的耦合点）。
        if self.gate is not None:
            self.gate.set_reconciliation(
                account_id, report.clean,
                "四项差异 0" if report.clean
                else f"{len(report.diffs)} 项差异待处理")

        perf = self.ledger.performance(prices, fx)
        by_strategy = self.ledger.strategy_pnl(prices, fx)
        point = NavPoint(
            day=day.isoformat(), ts=now, nav=perf.nav, net_pnl=perf.net_pnl,
            trading=perf.trading, slippage=perf.slippage, fee=perf.fee,
            funding=perf.funding, fx=perf.fx, external_flow=perf.external_flow,
            gross_exposure=perf.gross_exposure, net_exposure=perf.net_exposure,
            by_strategy=by_strategy,
            dividend=getattr(perf, "dividend", Decimal(0)),
        )
        prev = self.series.previous(point.day)
        attribution = build_daily_attribution(point, prev, fx)
        self.series.record(point)
        self._persist(report)

        return EodRun(report=report, attribution=attribution, point=point)

    def _persist(self, report: EodReport) -> str:
        os.makedirs(self.report_dir, exist_ok=True)
        path = os.path.join(self.report_dir, f"{report.day.isoformat()}.json")
        fd, tmp = tempfile.mkstemp(dir=self.report_dir, prefix=".eod-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(report.to_dict(), fh, ensure_ascii=False, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return path


def build_daily_attribution(point: NavPoint,
                            prev: Optional[NavPoint],
                            fx: Optional[Mapping[str, Decimal]] = None
                            ) -> DailyAttribution:
    """
    用「今日累计 - 昨日累计」算当日归因。

    没有昨日基线时（第一个交易日）当日就等于累计，并在 `warnings` 里说明：
    这时候"当日"其实是"自开户以来"，别把它当一天的收益。
    """
    fx = fx or {}
    z = Decimal("0")
    base = prev or NavPoint(
        day="", ts=point.ts, nav=z, net_pnl=z, trading=z, slippage=z,
        fee=z, funding=z, fx=z, external_flow=z, gross_exposure=z,
        net_exposure=z)

    trading = q(point.trading - base.trading, 4)
    slippage = q(point.slippage - base.slippage, 4)
    fee = q(point.fee - base.fee, 4)
    funding = q(point.funding - base.funding, 4)
    dividend = q(D(point.dividend) - D(base.dividend), 4)
    fx_pnl = q(point.fx - base.fx, 4)
    flow = q(point.external_flow - base.external_flow, 4)
    net_return = q((point.nav - base.nav) - flow, 4)

    by_strategy: Dict[str, Decimal] = {}
    for sid in set(point.by_strategy) | set(base.by_strategy):
        by_strategy[sid] = q(D(point.by_strategy.get(sid, z))
                             - D(base.by_strategy.get(sid, z)), 4)

    warnings: List[str] = []
    if prev is None:
        warnings.append(
            "无上一交易日基线：期初按「账户尚未开立」计（NAV 0、净流入 0），"
            "本行是自开户以来的累计值，不是单日收益")

    # ---- 两条**独立**校验（不是恒等式，是真能抓错的） ---- #
    single_currency = all(D(v) == Decimal(1) for v in fx.values()) if fx else True
    if single_currency and fx_pnl != 0:
        warnings.append(
            f"单一计价货币下汇率项应为 0，实际 {fx_pnl} —— 有一笔钱记到了残差里")
    if fee > 0:
        warnings.append(f"手续费项为正（{fee}）—— 费用不可能带来收益，记账方向可能反了")
    if dividend != 0 and trading == 0 and slippage == 0:
        warnings.append(
            f"当日只有分红（{dividend}）没有任何交易损益。若当天确实是除权日且"
            f"未动仓位，这是正常的；否则请核对分红与除权是否记在同一天 —— "
            f"跨日记会让交易项凭空出现一笔与分红等额的亏损")

    # 策略级是近似分摊（B6 前没有真实分账），对不上只提示，不当断言。
    # 口径对齐：`strategy_pnl` 用「现价 vs 决策成本」，对应的是 `trading` 这一项，
    # 不含滑点与费用（否则会拿两个不同口径的数去比，永远对不上）。
    strat_total = q(sum(by_strategy.values(), z), 4)
    if by_strategy and abs(strat_total - trading) > Decimal("0.02"):
        warnings.append(
            f"策略分摊合计 {strat_total} 与交易收益 {trading} 相差 "
            f"{q(strat_total - trading, 4)}（无归属持仓或分摊取整所致）")

    return DailyAttribution(
        day=point.day, baseline_day=base.day or "(无)", nav_open=base.nav,
        nav_close=point.nav, external_flow=flow, net_return=net_return,
        trading=trading, slippage=slippage, fee=fee, funding=funding,
        fx=fx_pnl, by_strategy=by_strategy, warnings=tuple(warnings),
        first_day=prev is None, dividend=dividend,
    )
