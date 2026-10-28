"""
策略池与影子盘只读归一化层（B14）。

为什么需要这一层
================
B12 之后回测能跑，B13 之后风险层能扩，但**「我到底有哪些策略、各自在什么阶段、
赚了多少、影子盘现在什么状况」这个问题在页面上答不出来** —— 因为答案散在
四种互不相同的结构里：

    research/*/outputs/*_summary.json    研究结论（字段名不统一、回撤符号不统一）
    research/strategy_packs/*.strat.py   可导入的策略包（只有契约，没有业绩）
    StrategyRegistry                     平台在册实例（只有生命周期，没有业绩）
    state/**/*_paper_book.json           影子盘账本（结构各不相同，两个盘两套字段）

这一层只做归一化：不计算、不补全、不预测。

三条硬规矩
==========
1. **口径不齐就写进数据，不让渲染层猜。** 六个 summary 里的 `max_drawdown_pct`
   本来就是两个口径（ew20 用负数、其余五个用正数）。归一化在这里做完，并把
   「原值是什么」记进 `normalized`，页面只负责显示。
2. **不可比就标不可比。** 初始资金 10 万~1000 万、区间 6.0~8.0 年、市场有
   china_a 与 us —— 收益率可以并排，绝对金额不行。这一条由 `warnings` 明说，
   而不是靠页面作者记得。
3. **缺就报缺（fail-closed）。** 文件不在、字段没有 → 进 `missing`，不填默认值、
   不填 0。因为「页面显示一条缺口」有价值，「页面显示一个编出来的 0」有害 ——
   后者看起来像「这个策略不赚钱」。

关于「未登记的 summary」
========================
策略登记表是显式的（文件名不统一：`ew20_summary.json` / `rev5_510300_summary.json`
/ `engine_replay_summary.json`，没法靠 glob 猜）。但 glob 仍然会跑，扫到没登记
的文件就进 `unregistered` 并给出原因 —— 新增研究报告不会被静默忽略，这是
R-O02 那类「看不见的缺失」最容易复发的地方。
"""

from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "CatalogError",
    "StrategyCard",
    "PaperBook",
    "CatalogPayload",
    "PaperPayload",
    "load_catalog",
    "load_paper",
    "REGISTERED",
]


class CatalogError(RuntimeError):
    """归一化层自己的错。出现它说明数据结构变了，不是数据缺了。"""


# --------------------------------------------------------------------------- #
#  极小的取值工具（宁可返回 None，也不返回 0）
# --------------------------------------------------------------------------- #

def _num(raw: Any) -> Optional[float]:
    """
    严格取数：只有真正的数值才返回 float，其余一律 None。

    `True` 是 `int` 的子类，所以必须先排除 bool —— 否则 JSON 里的 `"complete": true`
    会被当成 1.0 混进收益率里，而且不报错。
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        v = float(raw)
        # NaN / inf 不能进 JSON（裸 NaN 是非法 JSON，前端会白屏）
        if v != v or v in (float("inf"), float("-inf")):
            return None
        return v
    return None


def _text(raw: Any) -> Optional[str]:
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _int(raw: Any) -> Optional[int]:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float) and raw == int(raw):
        return int(raw)
    return None


def _day(raw: Any) -> Optional[date]:
    t = _text(raw)
    if not t:
        return None
    try:
        return date.fromisoformat(t[:10])
    except ValueError:
        return None


def _years(start: Optional[date], end: Optional[date]) -> Optional[float]:
    if not start or not end or end <= start:
        return None
    return round((end - start).days / 365.25, 3)


# --------------------------------------------------------------------------- #
#  策略登记表
# --------------------------------------------------------------------------- #

#: `summary` 相对路径 → 元数据。key 用相对路径而不是 glob 模式，因为文件名不统一。
#: `stage` 的判定依据写在每个条目的 `stage_why` 里 —— 阶段是**声明**，不是猜的：
#: 有影子盘账本且账本非空才算 paper，否则就是 research。
REGISTERED: Dict[str, Dict[str, Any]] = {
    "research/ashare_ew20/outputs/ew20_summary.json": {
        "strategy_id": "ashare_ew20",
        "name": "中证800 等权 + 20 日再平衡",
        "venue": "china_a",
        "universe": "中证800 成分股（当前成分，含幸存者偏差）",
        "paper_book": "ew20",
        "stage_why": "影子盘账本已建（state/ashare/ew20_paper_book.json）",
    },
    "research/ashare_xsec/outputs/ashare_rev20_summary.json": {
        "strategy_id": "ashare_rev20",
        "name": "A股横截面 · 20 日反转",
        "venue": "china_a",
        "universe": "中证800 成分股",
        "stage_why": "只有研究结论，未建影子盘",
    },
    "research/ashare_xsec/outputs/ashare_mom120_summary.json": {
        "strategy_id": "ashare_mom120",
        "name": "A股横截面 · 120 日动量",
        "venue": "china_a",
        "universe": "中证800 成分股",
        "stage_why": "只有研究结论，未建影子盘",
    },
    "research/ashare_etf_510300/outputs/rev5_510300_summary.json": {
        "strategy_id": "rev5_510300",
        "name": "沪深300ETF · 逆势 5 日",
        "venue": "china_a",
        "universe": "510300.SH 单标的",
        "stage_why": "只有研究结论，且结论为不可用（见 notes）",
    },
    "research/tesla_orb/outputs/tsla_breakout_summary.json": {
        "strategy_id": "tsla_orb",
        "name": "TSLA 开盘区间突破 · 手工回测",
        "venue": "us",
        "universe": "TSLA 单标的",
        "peer": "tsla_orb_engine",
        "stage_why": "手工回测口径，用于与引擎回放对账",
    },
    "research/tesla_orb/outputs/engine_replay_summary.json": {
        "strategy_id": "tsla_orb_engine",
        "name": "TSLA 开盘区间突破 · 引擎回放",
        "venue": "us",
        "universe": "TSLA 单标的",
        "paper_book": "tsla_orb",
        "peer": "tsla_orb",
        "stage_why": "影子盘账本已建（state/tsla_paper/tsla_paper_book.json）",
    },
}

#: 影子盘账本登记表。两套账本字段完全不同，所以各自带一个读取器。
_PAPER_BOOKS: Dict[str, Dict[str, Any]] = {
    "ew20": {
        "strategy_id": "ashare_ew20",
        "name": "中证800 等权 20 日 · 影子盘",
        "venue": "china_a",
        "path": "state/ashare/ew20_paper_book.json",
        "kind": "rebalance",   # 批量调仓型：现金 + 持仓 + 待成交批次
    },
    "tsla_orb": {
        "strategy_id": "tsla_orb_engine",
        "name": "TSLA ORB · 影子会话",
        "venue": "us",
        "path": "state/tsla_paper/tsla_paper_book.json",
        "kind": "session",     # 会话型：每场一行，日内进出
    },
}


# --------------------------------------------------------------------------- #
#  策略卡
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class StrategyCard:
    """一个策略的业绩快照。全字段可为 None —— None 的含义是「没有」，不是「零」。"""

    strategy_id: str
    name: str
    venue: str
    stage: str                          # research / paper
    stage_why: str
    source: str                         # 数据来源（相对路径）
    universe: Optional[str] = None
    peer: Optional[str] = None

    start: Optional[str] = None
    end: Optional[str] = None
    years: Optional[float] = None
    initial_cash: Optional[float] = None
    final_value: Optional[float] = None

    annual_return_pct: Optional[float] = None
    total_return_pct: Optional[float] = None
    max_drawdown_pct: Optional[float] = None    # 归一到 <= 0（损失用负号）
    sharpe: Optional[float] = None
    sortino: Optional[float] = None
    calmar: Optional[float] = None
    annual_vol_pct: Optional[float] = None
    win_rate_pct: Optional[float] = None
    total_trades: Optional[int] = None

    generated_at: Optional[str] = None
    normalized: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()
    missing: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "strategy_id": self.strategy_id,
            "name": self.name,
            "venue": self.venue,
            "stage": self.stage,
            "stage_why": self.stage_why,
            "source": self.source,
            "normalized": list(self.normalized),
            "notes": list(self.notes),
            "missing": list(self.missing),
        }
        for k in ("universe", "peer", "start", "end", "generated_at",
                  "years", "initial_cash", "final_value",
                  "annual_return_pct", "total_return_pct", "max_drawdown_pct",
                  "sharpe", "sortino", "calmar", "annual_vol_pct",
                  "win_rate_pct", "total_trades"):
            out[k] = getattr(self, k)
        return out


def _read_json(path: str) -> Tuple[Optional[Mapping[str, Any]], Optional[str]]:
    """返回 `(内容, 错误说明)`。读不到就返回 None + 原因，绝不返回空 dict。"""
    if not os.path.exists(path):
        return None, "文件不存在"
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"读取失败：{type(exc).__name__}: {exc}"
    if not isinstance(raw, Mapping):
        return None, f"顶层不是对象（是 {type(raw).__name__}）"
    return raw, None


def _card_from_summary(root: str, rel: str, meta: Mapping[str, Any],
                       has_book: bool) -> Tuple[StrategyCard, List[str]]:
    """
    把一个 summary 归一化成卡片。

    返回 `(卡片, 全局告警)`；全局告警是「这一条本身没错，但并排看会误导」的那种。
    """
    normalized: List[str] = []
    notes: List[str] = []
    missing: List[str] = []
    warn: List[str] = []

    raw, err = _read_json(os.path.join(root, rel))
    if raw is None:
        return StrategyCard(
            strategy_id=meta["strategy_id"],
            name=meta["name"],
            venue=meta["venue"],
            stage="paper" if has_book else "research",
            stage_why=meta.get("stage_why", ""),
            source=rel,
            universe=meta.get("universe"),
            peer=meta.get("peer"),
            missing=(f"summary {err}",),
        ), warn

    summary = raw.get("summary")
    smeta = raw.get("meta")
    if not isinstance(summary, Mapping):
        missing.append("summary 段缺失或不是对象")
        summary = {}
    if not isinstance(smeta, Mapping):
        missing.append("meta 段缺失或不是对象")
        smeta = {}

    # ---- 区间：ew20 把 start/end 放在 summary 里，其余放在 meta 里 ---- #
    start = _day(summary.get("start")) or _day(smeta.get("start"))
    end = _day(summary.get("end")) or _day(smeta.get("end"))
    if start is None or end is None:
        missing.append("区间（start / end）")
    if summary.get("start") is not None and smeta.get("start") is not None:
        if _day(summary.get("start")) != _day(smeta.get("start")):
            notes.append("summary 与 meta 各写了一份 start，取值不同；本层用 summary 的")
    years = _num(summary.get("years")) or _years(start, end)

    # ---- 回撤：符号归一 ---- #
    mdd_raw = _num(summary.get("max_drawdown_pct"))
    mdd: Optional[float] = None
    if mdd_raw is None:
        missing.append("max_drawdown_pct")
    elif mdd_raw > 0:
        # 六个 summary 里五个用正数表示回撤幅度，只有 ew20 用负数。
        # 语义上回撤是损失，本层统一成负数，并把原值写下来。
        mdd = -mdd_raw
        normalized.append(
            f"max_drawdown_pct 原值 {mdd_raw:.2f}（正数口径）→ 归一为 {mdd:.2f}")
    else:
        mdd = mdd_raw

    # ---- 业绩字段：缺就是缺 ---- #
    def _take(key: str) -> Optional[float]:
        v = _num(summary.get(key))
        if v is None:
            missing.append(key)
            return None
        return v

    annual = _take("annual_return_pct")
    total = _take("total_return_pct")
    sharpe = _num(summary.get("sharpe"))
    if sharpe is None:
        missing.append("sharpe")
    sortino = _num(summary.get("sortino"))
    calmar = _num(summary.get("calmar"))
    vol = _num(summary.get("annual_vol_pct"))
    win = _num(summary.get("win_rate_pct"))
    trades = _int(summary.get("total_trades"))

    initial_cash = _num(smeta.get("initial_cash"))
    if initial_cash is None:
        missing.append("meta.initial_cash")
    final_value = _num(summary.get("final_value")) or _num(smeta.get("final_value"))
    if final_value is None:
        missing.append("final_value")

    # ---- 口径说明（不是错误，是「并排看会误导」的边界） ---- #
    survivor = _text(smeta.get("survivorship"))
    if survivor:
        notes.append(f"幸存者偏差：{survivor}")
    exec_note = _text(smeta.get("execution"))
    if exec_note:
        notes.append(f"成交口径：{exec_note}")
    universe = _text(smeta.get("universe")) or meta.get("universe")
    if _num(summary.get("years")) is None and years is not None:
        normalized.append(f"years 由区间反算 = {years}（summary 未给）")
    if str(meta.get("venue")) == "us":
        notes.append("美股口径：无涨跌停、无 T+1、可碎股，与 A 股结论不可直接类推")
    if meta["strategy_id"] == "rev5_510300":
        notes.append(
            "该结论为**不可用**：年化约 -0.06%、夏普 0.06，扣成本后基本打平。"
            "留在池子里是为了不让「试过的方向」消失。")
    if meta.get("peer"):
        notes.append(f"同一策略另有口径：{meta['peer']}（并列比较，不合并）")

    card = StrategyCard(
        strategy_id=meta["strategy_id"],
        name=meta["name"],
        venue=str(meta["venue"]),
        stage="paper" if has_book else "research",
        stage_why=meta.get("stage_why", ""),
        source=rel,
        universe=universe,
        peer=meta.get("peer"),
        start=start.isoformat() if start else None,
        end=end.isoformat() if end else None,
        years=years,
        initial_cash=initial_cash,
        final_value=final_value,
        annual_return_pct=annual,
        total_return_pct=total,
        max_drawdown_pct=mdd,
        sharpe=sharpe,
        sortino=sortino,
        calmar=calmar,
        annual_vol_pct=vol,
        win_rate_pct=win,
        total_trades=trades,
        generated_at=_text(smeta.get("generated_at")),
        normalized=tuple(normalized),
        notes=tuple(notes),
        missing=tuple(missing),
    )
    return card, warn


# --------------------------------------------------------------------------- #
#  影子盘
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class PaperBook:
    """一个影子盘的快照。两个盘（批量调仓 / 日内会话）共用这一个形状。"""

    book_id: str
    strategy_id: str
    name: str
    venue: str
    kind: str                       # rebalance / session
    source: str

    created_at: Optional[str] = None
    as_of: Optional[str] = None
    capital: Optional[float] = None
    cash: Optional[float] = None
    equity: Optional[float] = None
    n_positions: Optional[int] = None
    pending_orders: Optional[int] = None
    fills: Optional[int] = None
    sessions: Optional[int] = None
    pending_signal_date: Optional[str] = None
    latest_bar: Optional[str] = None
    stale_note: Optional[str] = None
    seed_note: Optional[str] = None
    positions: Tuple[Dict[str, Any], ...] = ()
    recent_trades: Tuple[Dict[str, Any], ...] = ()
    notes: Tuple[str, ...] = ()
    missing: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "book_id": self.book_id,
            "strategy_id": self.strategy_id,
            "name": self.name,
            "venue": self.venue,
            "kind": self.kind,
            "source": self.source,
            "positions": [dict(p) for p in self.positions],
            "recent_trades": [dict(t) for t in self.recent_trades],
            "notes": list(self.notes),
            "missing": list(self.missing),
        }
        for k in ("created_at", "as_of", "capital", "cash", "equity",
                  "n_positions", "pending_orders", "fills", "sessions",
                  "pending_signal_date", "latest_bar", "stale_note", "seed_note"):
            out[k] = getattr(self, k)
        return out


def _tail_line(path: str, nbytes: int = 512) -> Optional[str]:
    """只读文件尾部，取最后一个非空行。个股日线单文件可达几百 KB，整读 794 份不划算。"""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - nbytes))
            chunk = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    lines = [ln for ln in chunk.splitlines() if ln.strip()]
    return lines[-1] if lines else None


def _latest_bar_day(root: str) -> Optional[date]:
    """
    最新一根 A 股日线的日期。

    **不能用指数 CSV 判断**：`state/ashare/index_000300.csv` 只在抓指数时更新，
    实测比个股日线晚 4 天（10-23 vs 10-27）。拿它当「今天」，会把一批已经过期
    一个月的委托判成「没过期」—— 而这个判断的全部价值就在于及时报出过期。

    所以抽样读个股日线的末行，取**最大值**：停牌股末行会停在几天前，
    取第一条会误判成「数据不新鲜」。
    """
    days: List[date] = []
    ddir = os.path.join(root, "state", "ashare", "daily")
    if os.path.isdir(ddir):
        names = sorted(n for n in os.listdir(ddir) if n.endswith(".csv"))
        if len(names) > 8:
            sample = names[:4] + names[-4:]
        else:
            sample = names
        for name in sample:
            line = _tail_line(os.path.join(ddir, name))
            if not line:
                continue
            d = _day(line.split(",")[0])
            if d:
                days.append(d)
    if days:
        return max(days)
    return None


def _book_ew20(root: str, rel: str, meta: Mapping[str, Any]) -> PaperBook:
    notes: List[str] = []
    missing: List[str] = []
    raw, err = _read_json(os.path.join(root, rel))
    if raw is None:
        return PaperBook(book_id="ew20", strategy_id=meta["strategy_id"],
                         name=meta["name"], venue=meta["venue"],
                         kind="rebalance", source=rel,
                         missing=(f"账本{err}",))

    holdings = raw.get("holdings")
    if not isinstance(holdings, Mapping):
        missing.append("holdings 段缺失或不是对象")
        holdings = {}
    pend = raw.get("pending_orders")
    if not isinstance(pend, list):
        missing.append("pending_orders 段缺失或不是数组")
        pend = []

    cash = _num(raw.get("cash"))
    equity = _num(raw.get("equity"))
    capital = _num(raw.get("capital"))
    if cash is None:
        missing.append("cash")
    if equity is None:
        missing.append("equity")

    # 持仓明细：字段名在两个盘里不一致，这里统一成 symbol / qty / cost。
    positions: List[Dict[str, Any]] = []
    for sym, pos in list(holdings.items())[:20]:
        if not isinstance(pos, Mapping):
            positions.append({"symbol": sym, "qty": None, "cost": None})
            continue
        positions.append({
            "symbol": sym,
            "qty": _num(pos.get("shares")) or _num(pos.get("qty")),
            "cost": _num(pos.get("cost")) or _num(pos.get("avg_cost")),
        })

    pending_signal = _text(raw.get("pending_signal_date"))
    latest = _latest_bar_day(root)
    stale_note = None
    if pending_signal and latest:
        gap = (latest - _day(pending_signal)).days if _day(pending_signal) else None
        if gap is not None and gap > 0:
            stale_note = (f"账本里这批委托的计划基准日是 {pending_signal}，"
                          f"而最新日线已到 {latest.isoformat()}（相隔 {gap} 个自然日）"
                          "—— 批次可能已过期，需重新 plan。")
    elif pending_signal and not latest:
        stale_note = (f"账本里这批委托的计划基准日是 {pending_signal}，"
                      "但读不到最新日线日期，无法判断是否过期（不猜）。")

    fills = _int(raw.get("fills"))
    if fills == 0:
        notes.append("从未结算：账本里 0 笔成交，净值还是初始资金。"
                     "这不是「不赚钱」，是「还没开始记」。")
    n_pos = len(holdings)
    if n_pos == 0:
        notes.append("当前无持仓。")

    cfg = raw.get("config") if isinstance(raw.get("config"), Mapping) else {}
    if cfg:
        notes.append("调仓配置：" + " / ".join(f"{k}={v}" for k, v in cfg.items()))

    return PaperBook(
        book_id="ew20",
        strategy_id=meta["strategy_id"],
        name=meta["name"],
        venue=meta["venue"],
        kind="rebalance",
        source=rel,
        created_at=_text(raw.get("created_at")),
        as_of=_text(raw.get("as_of")),
        capital=capital,
        cash=cash,
        equity=equity,
        n_positions=n_pos,
        pending_orders=len(pend),
        fills=fills,
        pending_signal_date=pending_signal,
        latest_bar=latest.isoformat() if latest else None,
        stale_note=stale_note,
        positions=tuple(positions),
        notes=tuple(notes),
        missing=tuple(missing),
    )


def _book_tsla(root: str, rel: str, meta: Mapping[str, Any]) -> PaperBook:
    notes: List[str] = []
    missing: List[str] = []
    raw, err = _read_json(os.path.join(root, rel))
    if raw is None:
        return PaperBook(book_id="tsla_orb", strategy_id=meta["strategy_id"],
                         name=meta["name"], venue=meta["venue"],
                         kind="session", source=rel,
                         missing=(f"账本{err}",))

    sessions = raw.get("sessions")
    if not isinstance(sessions, list):
        missing.append("sessions 段缺失或不是数组")
        sessions = []

    equity = _num(raw.get("equity"))
    if equity is None:
        missing.append("equity")

    seed = _text(raw.get("seed_note"))
    if seed:
        notes.append(
            f"净值起点不是初始本金，而是历史回放终点（{seed}）—— "
            "所以「净值 / 初始资金」不等于这段影子期的收益率，别这么算。")

    recent: List[Dict[str, Any]] = []
    for s in sessions[-5:]:
        if not isinstance(s, Mapping):
            continue
        recent.append({
            "date": _text(s.get("date")),
            "start_equity": _num(s.get("start_equity")),
            "end_equity": _num(s.get("end_equity")),
            "return_pct": _num(s.get("return_pct")),
            "bars": _int(s.get("bars")),
            "complete": bool(s.get("complete")) if isinstance(s.get("complete"), bool) else None,
            "halted": bool(s.get("halted")) if isinstance(s.get("halted"), bool) else None,
            "trades": len(s.get("trades") or []) if isinstance(s.get("trades"), list) else None,
        })

    n_trades = 0
    for s in sessions:
        if isinstance(s, Mapping) and isinstance(s.get("trades"), list):
            n_trades += len(s["trades"])

    last = raw.get("last_run") if isinstance(raw.get("last_run"), Mapping) else {}
    as_of = _text(last.get("date")) if last else None
    if len(sessions) == 1:
        notes.append("只有 1 个会话 —— 单日结果没有统计意义，只能证明链路通了。")

    return PaperBook(
        book_id="tsla_orb",
        strategy_id=meta["strategy_id"],
        name=meta["name"],
        venue=meta["venue"],
        kind="session",
        source=rel,
        created_at=_text(raw.get("created_at")),
        as_of=as_of,
        capital=_num(raw.get("capital")),
        cash=None,
        equity=equity,
        n_positions=0,      # 日内策略收盘平掉，仓位恒为 0 —— 这是事实不是缺失
        pending_orders=0,
        fills=n_trades,
        sessions=len(sessions),
        seed_note=seed,
        recent_trades=tuple(recent),
        notes=tuple(notes),
        missing=tuple(missing),
    )


# --------------------------------------------------------------------------- #
#  对外入口
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class CatalogPayload:
    generated_at: str
    cards: Tuple[StrategyCard, ...] = ()
    unregistered: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "cards": [c.to_dict() for c in self.cards],
            "unregistered": list(self.unregistered),
            "warnings": list(self.warnings),
            "counts": {
                "total": len(self.cards),
                "paper": sum(1 for c in self.cards if c.stage == "paper"),
                "research": sum(1 for c in self.cards if c.stage == "research"),
                "with_missing": sum(1 for c in self.cards if c.missing),
            },
        }


@dataclass(frozen=True)
class PaperPayload:
    generated_at: str
    books: Tuple[PaperBook, ...] = ()
    warnings: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "books": [b.to_dict() for b in self.books],
            "warnings": list(self.warnings),
            "counts": {
                "total": len(self.books),
                "settled": sum(1 for b in self.books
                               if (b.fills or 0) > 0),
                "pending": sum((b.pending_orders or 0) for b in self.books),
            },
        }


def load_catalog(root: str) -> CatalogPayload:
    """扫描已登记的 summary 并归一化。`root` 是项目根目录。"""
    cards: List[StrategyCard] = []
    warnings: List[str] = []
    for rel, meta in REGISTERED.items():
        has_book = bool(meta.get("paper_book"))
        if has_book:
            book_meta = _PAPER_BOOKS.get(meta["paper_book"], {})
            has_book = os.path.exists(os.path.join(root, book_meta.get("path", "")))
        card, _ = _card_from_summary(root, rel, meta, has_book)
        cards.append(card)

    # 扫到没登记的 summary 就说出来 —— 新研究不该被静默忽略。
    found = set()
    for path in glob.glob(os.path.join(root, "research", "*", "outputs", "*summary*.json")):
        found.add(os.path.relpath(path, root).replace(os.sep, "/"))
    unregistered = sorted(found - set(REGISTERED))

    # ---- 并排比较前必须说清的三件事 ---- #
    valid = [c for c in cards if c.missing == ()]
    if len(valid) >= 2:
        years = [c.years for c in valid if c.years]
        cashes = [c.initial_cash for c in valid if c.initial_cash]
        venues = {c.venue for c in valid}
        if years and max(years) - min(years) > 1.0:
            warnings.append(
                f"区间长度不同（{min(years):.2f}~{max(years):.2f} 年），"
                "年化可以并排，总收益不能。")
        if cashes and max(cashes) / min(cashes) > 2:
            warnings.append(
                f"初始资金不同（{min(cashes):,.0f}~{max(cashes):,.0f}），"
                "绝对盈亏金额不可横向比较。")
        if len(venues) > 1:
            warnings.append(
                f"跨市场（{'/'.join(sorted(venues))}）：交易规则与成本结构不同，"
                "排名只在同市场内成立。")
    if len(valid) < len(cards):
        warnings.append(
            f"{len(cards) - len(valid)} 个策略有字段缺口（见各自 missing），"
            "它们不参与任何对比结论。")

    return CatalogPayload(
        generated_at=datetime.now().isoformat(timespec="seconds"),
        cards=tuple(cards),
        unregistered=tuple(unregistered),
        warnings=tuple(warnings),
    )


def load_paper(root: str) -> PaperPayload:
    """读全部影子盘账本并归一化。"""
    books: List[PaperBook] = []
    warnings: List[str] = []
    for book_id, meta in _PAPER_BOOKS.items():
        rel = meta["path"]
        if book_id == "ew20":
            book = _book_ew20(root, rel, meta)
        elif book_id == "tsla_orb":
            book = _book_tsla(root, rel, meta)
        else:                                  # pragma: no cover - 登记了就必有分支
            raise CatalogError(f"影子盘 {book_id} 没有登记读取器")
        books.append(book)
        if book.stale_note:
            warnings.append(f"{book.name}：{book.stale_note}")
        if book.missing:
            warnings.append(f"{book.name}：缺 {', '.join(book.missing)}")

    settled = [b for b in books if (b.fills or 0) > 0]
    if not settled:
        warnings.append(
            "两个影子盘都没有成交记录 —— 页面上的净值全等于初始资金。"
            "这不是「不亏不赚」，是「还没开始记」。")

    return PaperPayload(
        generated_at=datetime.now().isoformat(timespec="seconds"),
        books=tuple(books),
        warnings=tuple(warnings),
    )
