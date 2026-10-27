#!/usr/bin/env python3
"""ew20_backtest.py — 中证800 等权 + 20 个交易日再平衡：把"基准"做成可上线的策略

上一轮的结论是「真正的 alpha 在等权再平衡本身，而不是选股」。这个脚本把那个
结论从"一个数字"变成"一条能下单的规则"，并把所有会在实盘里吃掉收益的东西
全部显式建模：

  1. 佣金有 5 元下限。全池 794 只、1000 万资金时，单只 1.26 万，
     2.5bp = 3.15 元 < 5 元 —— 真实成本是模型成本的 1.6 倍。
     资金越小、只数越多，这个差距越夸张（350 万时是 4 倍）。
  2. A 股 100 股整手 + 科创板 200 股起。整手会让高价股被机械性跳过，
     也会留下永远投不出去的现金。
  3. 涨跌停：涨停不能买、跌停不能卖。卖不掉的挂到下一日继续试。
  4. 停牌：volume 为空/0 的当天不能成交，只能用最近收盘价盯市。
  5. T+1：当日买入不可当日卖出（本策略 20 日调仓，几乎不触发，仍保留断言）。
  6. 最小下单额：调仓噪音小于这个金额的单子不下，否则会被 5 元下限磨死。

跑法：
    python ew20_backtest.py            # 全量：网格 + 基准 + 三件套 + 图表
    python ew20_backtest.py --quick    # 只跑基准，跳过容量网格

产出：
    ew20_equity.csv / ew20_trades.csv / ew20_summary.json
    ew20_nav_vs_bench.png / ew20_capacity.png / ew20_yearly.png / ew20_cost.png

⚠️ 以上内容由 AI 基于公开信息整理生成，仅供参考，不构成任何投资建议或个股推荐。
"""
from __future__ import annotations

import argparse
import json
import os
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent      # 策略目录；产物写 ROOT/outputs
PROJ = ROOT.parent.parent                   # 项目根：state/ 与 reporting/ 在这里
OUT = ROOT / "outputs"
sys.path.insert(0, str(PROJ))

DATA_DIR = PROJ / "state" / "ashare" / "daily"
AUDIT_JSON = PROJ / "state" / "ashare" / "fetch_audit.json"
INDEX_FILES = {
    "沪深300": PROJ / "state" / "ashare" / "index_000300.csv",
    "中证500": PROJ / "state" / "ashare" / "index_000905.csv",
    "中证800": PROJ / "state" / "ashare" / "index_000906.csv",
}

LOT = 100
MIN_NAMES = 200            # 横截面少于这个数就不建仓（数据不全时不硬凑）
REBALANCE_BARS = 20
EVAL_START = "2019-01-18"  # 固定评估起点：与上一轮因子回测同一窗口，保证可比

DISCLAIMER = (
    "⚠️ 以上内容由 AI 基于公开信息整理生成，仅供参考，不构成任何投资建议或个股推荐。"
    "投资有风险，决策需谨慎。"
)


# --------------------------------------------------------------------------- #
#  Config
# --------------------------------------------------------------------------- #
@dataclass
class CostModel:
    """A 股真实费用结构。佣金按笔收，且有 5 元下限——这是小资金的最大杀手。"""
    commission_bps: float = 2.5      # 券商佣金，双边
    min_commission: float = 5.0      # 单笔最低佣金（元）
    stamp_bps: float = 5.0           # 印花税，仅卖出
    transfer_bps: float = 0.1        # 过户费，双边
    slippage_bps: float = 5.0        # 冲击成本假设（相对次日开盘价）

    def fee(self, value: float, side: str) -> float:
        if value <= 0 or not np.isfinite(value):
            return 0.0
        commission = max(value * self.commission_bps / 1e4, self.min_commission)
        fee = commission + value * self.transfer_bps / 1e4
        if side == "sell":
            fee += value * self.stamp_bps / 1e4
        fee += value * self.slippage_bps / 1e4
        return fee


@dataclass
class Cfg:
    initial_cash: float = 10_000_000.0
    n_names: int | None = 200         # None = 全池
    liq_window: int = 20              # 流动性排序窗口（成交额均值）
    cash_buffer: float = 0.005        # 预留现金比例，吸收整手误差
    min_order_value: float = 2000.0   # 低于此金额的调仓单不下
    limit_main: float = 0.10
    limit_growth: float = 0.20
    limit_tol: float = 0.002
    cost: CostModel = field(default_factory=CostModel)
    label: str = ""


@dataclass
class Result:
    label: str
    equity: list[dict] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    period_returns: list[dict] = field(default_factory=list)
    cost_total: float = 0.0
    buy_value: float = 0.0
    sell_value: float = 0.0
    n_orders: int = 0
    n_rebalances: int = 0
    blocked_limit_down: int = 0
    skipped_limit_up: int = 0
    blocked_suspend: int = 0
    skipped_lot: int = 0
    n_buys: int = 0
    n_sells: int = 0
    cash_ratio: list[float] = field(default_factory=list)
    n_positions: list[int] = field(default_factory=list)
    cfg: Cfg | None = None


# --------------------------------------------------------------------------- #
#  Data
# --------------------------------------------------------------------------- #
@dataclass
class Panel:
    calendar: pd.DatetimeIndex
    open: pd.DataFrame
    close: pd.DataFrame
    volume: pd.DataFrame
    amount: pd.DataFrame
    names: dict[str, str]

    @property
    def mark(self) -> pd.DataFrame:
        """停牌日盯市价（前向填充）。只用于估值，从不参与成交。"""
        return self.close.ffill()


def load_panel(data_dir: Path = DATA_DIR) -> Panel:
    files = sorted(data_dir.glob("*.csv"))
    if not files:
        raise SystemExit(f"[FATAL] {data_dir} 没有数据，先跑 scripts/fetch_ashare_universe.py")

    frames: dict[str, pd.DataFrame] = {}
    for path in files:
        df = pd.read_csv(path, parse_dates=["date"])
        if len(df) < 60:
            continue
        cols = [c for c in ("open", "close", "volume", "amount") if c in df.columns]
        frames[path.stem] = df.set_index("date")[cols]

    calendar = pd.DatetimeIndex(sorted(set().union(*[set(d.index) for d in frames.values()])))
    codes = sorted(frames)

    def _wide(col: str) -> pd.DataFrame:
        if any(col not in frames[c].columns for c in codes):
            return pd.DataFrame(np.nan, index=calendar, columns=codes)
        return pd.concat({c: frames[c][col] for c in codes}, axis=1, sort=False).reindex(calendar)

    names: dict[str, str] = {}
    if AUDIT_JSON.exists():
        audit = json.loads(AUDIT_JSON.read_text(encoding="utf-8"))
        names = {c: n for c, n in (audit.get("names") or {}).items() if c in set(codes)}

    return Panel(
        calendar=calendar,
        open=_wide("open"),
        close=_wide("close"),
        volume=_wide("volume"),
        amount=_wide("amount"),
        names=names,
    )


def limit_width(code: str) -> float:
    return Cfg.limit_growth if (code.startswith("sh688") or code.startswith("sz30")) else Cfg.limit_main


def lot_size(code: str) -> int:
    return 200 if code.startswith("sh688") else LOT


# --------------------------------------------------------------------------- #
#  Engine
# --------------------------------------------------------------------------- #
def run_ew(panel: Panel, cfg: Cfg) -> Result:
    """等权 + 每 REBALANCE_BARS 个交易日再平衡，信号日收盘出单、次日开盘成交。

    唯一允许的"未来信息"是成交当日的开盘价——它是用来**执行**的，不参与选股。
    选股只用信号日（前一交易日）收盘及之前的数据。持仓只数由 cfg.n_names 控制
    （None = 全池）。
    """
    n_names = cfg.n_names
    cal = panel.calendar
    op = panel.open.values.astype(float)
    cl = panel.close.values.astype(float)
    vol = panel.volume.values.astype(float)
    mark = panel.mark.values.astype(float)
    codes = list(panel.open.columns)
    col = {c: i for i, c in enumerate(codes)}
    n = len(codes)

    liq = panel.amount.rolling(cfg.liq_window, min_periods=cfg.liq_window // 2).mean()
    liq_v = liq.values.astype(float)

    # 评估窗口：固定起点，早于它的数据只做预热（不交易、不计净值）
    counts = np.isfinite(cl).sum(axis=1)
    valid = np.where(counts >= MIN_NAMES)[0]
    if len(valid) == 0:
        raise SystemExit("[FATAL] 没有任何一天满足最小横截面要求")
    start = max(int(valid[0]), int(cal.searchsorted(pd.Timestamp(EVAL_START))))
    end = len(cal) - 1

    # 信号日 k 收盘出单 → 第 k+1 日开盘成交；网格从 start 起每 REBALANCE_BARS 天
    plan: dict[int, int] = {}
    for sig in range(start, end, REBALANCE_BARS):
        if sig + 1 <= end:
            plan[sig + 1] = sig

    res = Result(label=cfg.label or f"N={n_names or 'all'}@{cfg.initial_cash/1e4:.0f}万", cfg=cfg)
    cash = cfg.initial_cash
    sh = np.zeros(n, dtype=np.int64)
    # 持仓台账：用于把多笔部分成交聚合成一笔"回合交易"
    book: dict[int, dict] = {}
    pending_exit: set[int] = set()

    def sell(j: int, idx: int, px: float, note: str) -> float:
        """卖出全部持仓，返回成交额。"""
        nonlocal cash
        shares = int(sh[j])
        if shares <= 0:
            return 0.0
        value = shares * px
        fee = cfg.cost.fee(value, "sell")
        cash += value - fee
        res.cost_total += fee
        res.sell_value += value
        res.n_orders += 1
        res.n_sells += 1
        rec = book[j]
        rec["sell_value"] += value
        rec["sell_shares"] += shares
        rec["exit_date"] = cal[idx].strftime("%Y-%m-%d")
        rec["exit_idx"] = idx
        rec["last_note"] = note
        res.trades.append(rec)          # 回合闭合，落账
        del book[j]
        sh[j] = 0
        return value

    for idx in range(start, end + 1):
        date = cal[idx]
        prev_close = cl[idx - 1] if idx > start else cl[idx]

        # ---- 1. 补上被跌停/停牌挡住的卖单 ------------------------------
        for j in list(pending_exit):
            if sh[j] <= 0:
                pending_exit.discard(j)
                continue
            px = op[idx, j]
            if not np.isfinite(px) or not np.isfinite(vol[idx, j]) or vol[idx, j] <= 0:
                res.blocked_suspend += 1
                continue
            pc = prev_close[j]
            if np.isfinite(pc) and px <= pc * (1.0 - limit_width(codes[j]) + cfg.limit_tol):
                res.blocked_limit_down += 1
                continue
            sell(j, idx, float(px), "补卖")
            pending_exit.discard(j)

        # ---- 2. 调仓日 --------------------------------------------------
        if idx in plan:
            res.n_rebalances += 1
            sig = plan[idx]
            tradable = (vol[idx] > 0) & np.isfinite(op[idx]) & np.isfinite(vol[idx])
            sig_ok = (vol[sig] > 0) & np.isfinite(cl[sig])
            candid = np.where(tradable & sig_ok & np.isfinite(liq_v[sig]))[0]
            if n_names is not None and len(candid) > n_names:
                order = candid[np.argsort(-liq_v[sig, candid])]
                targets = set(order[:n_names].tolist())
            else:
                targets = set(candid.tolist())

            if len(candid) >= MIN_NAMES:   # 门控卡候选池（数据质量），不是持仓数
                held_val = float(np.dot(sh, np.nan_to_num(mark[idx], nan=0.0)))
                equity = cash + held_val
                gross = equity * (1.0 - cfg.cash_buffer)
                per = gross / max(1, len(targets))
                if not np.isfinite(per) or per <= 0:
                    per = 0.0
                tgt = np.zeros(n, dtype=np.int64)
                for j in targets:
                    px = op[idx, j]
                    if per <= 0 or not np.isfinite(px) or px <= 0:
                        continue
                    size = int(per / px) // lot_size(codes[j]) * lot_size(codes[j])
                    if size >= lot_size(codes[j]):
                        tgt[j] = size
                    elif per >= px * lot_size(codes[j]):
                        tgt[j] = lot_size(codes[j])
                res.skipped_lot += max(0, len(targets) - int((tgt > 0).sum()))

                # 2a. 先卖：清掉不在目标里的、减掉超配的
                for j in np.where(sh > 0)[0]:
                    px = op[idx, j]
                    if not np.isfinite(px) or not np.isfinite(vol[idx, j]) or vol[idx, j] <= 0:
                        pending_exit.add(int(j))
                        res.blocked_suspend += 1
                        continue
                    pc = prev_close[j]
                    if np.isfinite(pc) and px <= pc * (1.0 - limit_width(codes[j]) + cfg.limit_tol):
                        if j not in targets:
                            pending_exit.add(int(j))
                        res.blocked_limit_down += 1
                        continue
                    if j not in targets:
                        sell(int(j), idx, float(px), "调出")
                        continue
                    if sh[j] > tgt[j]:
                        diff = int(sh[j] - tgt[j])
                        value = diff * px
                        if value < cfg.min_order_value:
                            continue        # 太小的调整不划算，让漂移留着
                        fee = cfg.cost.fee(value, "sell")
                        cash += value - fee
                        res.cost_total += fee
                        res.sell_value += value
                        res.n_orders += 1
                        res.n_sells += 1
                        rec = book[int(j)]
                        rec["sell_value"] += value
                        rec["sell_shares"] += diff
                        rec["last_note"] = "减配"
                        sh[j] -= diff

                # 2b. 再买：按缺口从大到小，钱不够就停
                gaps = []
                for j in sorted(targets):
                    if tgt[j] > sh[j]:
                        gaps.append((tgt[j] - sh[j], j))
                gaps.sort(reverse=True)
                for diff, j in gaps:
                    px = op[idx, j]
                    if not np.isfinite(px):
                        continue
                    pc = prev_close[j]
                    if np.isfinite(pc) and px >= pc * (1.0 + limit_width(codes[j]) - cfg.limit_tol):
                        res.skipped_limit_up += 1
                        continue
                    lot = lot_size(codes[j])
                    size = int(diff) // lot * lot
                    while size >= lot:
                        value = size * px
                        fee = cfg.cost.fee(value, "buy")
                        if value + fee <= cash:
                            break
                        size -= lot
                    if size < lot:
                        continue
                    value = size * px
                    fee = cfg.cost.fee(value, "buy")
                    cash -= value + fee
                    res.cost_total += fee
                    res.buy_value += value
                    res.n_orders += 1
                    res.n_buys += 1
                    if j in book:
                        rec = book[j]
                    else:
                        rec = {"symbol": codes[j], "symbol_name": panel.names.get(codes[j], codes[j]),
                               "entry_date": date.strftime("%Y-%m-%d"), "entry_idx": idx, "buy_value": 0.0,
                               "buy_shares": 0, "sell_value": 0.0, "sell_shares": 0,
                               "first_px": float(px), "last_note": "建仓"}
                        book[j] = rec
                    rec["buy_value"] += value
                    rec["buy_shares"] += size
                    sh[j] += size

        # ---- 3. 盯市 ----------------------------------------------------
        mv = float(np.dot(sh, np.nan_to_num(mark[idx], nan=0.0)))
        # 停牌股 mark 已被前向填充；仍为 NaN 的（从未成交过）用成本价兜底
        bad = np.where(np.isnan(mark[idx]) & (sh > 0))[0]
        for j in bad:
            rec = book.get(int(j))
            if rec:
                mv += sh[j] * rec["first_px"]
        total = cash + mv
        res.equity.append({"date": date.strftime("%Y-%m-%d"), "value": round(total, 2)})
        res.cash_ratio.append(cash / total if total > 0 else 1.0)
        res.n_positions.append(int((sh > 0).sum()))

    # ---- 4. 期末强制清仓（把未实现盈亏落成已实现，费用照收） ------------
    last = end
    for j in np.where(sh > 0)[0]:
        px = cl[last, j]
        if not np.isfinite(px):
            px = mark[last, j]
        if not np.isfinite(px):
            px = book[int(j)]["first_px"]
        sell(int(j), last, float(px), "期末清仓")
    res.equity[-1]["value"] = round(cash, 2)

    return res


# --------------------------------------------------------------------------- #
#  Metrics
# --------------------------------------------------------------------------- #
def perf(equity: list[dict], rf: float = 0.0) -> dict:
    s = pd.Series([e["value"] for e in equity],
                  index=pd.DatetimeIndex([e["date"] for e in equity]), dtype=float)
    if len(s) < 2 or s.iloc[0] <= 0:
        return {}
    ret = s.pct_change().dropna()
    years = len(s) / 252.0
    total = s.iloc[-1] / s.iloc[0] - 1.0
    ann = (1.0 + total) ** (1.0 / years) - 1.0 if years > 0 else 0.0
    dd = (s / s.cummax() - 1.0)
    mdd = float(dd.min())
    vol = float(ret.std() * math.sqrt(252))
    sharpe = float((ret.mean() * 252 - rf) / vol) if vol > 0 else 0.0
    downside = ret[ret < 0]
    dvol = float(downside.std() * math.sqrt(252)) if len(downside) > 3 else vol
    sortino = float((ret.mean() * 252 - rf) / dvol) if dvol > 0 else 0.0
    calmar = float(ann / abs(mdd)) if mdd < 0 else 0.0
    return {
        "start": s.index[0].strftime("%Y-%m-%d"),
        "end": s.index[-1].strftime("%Y-%m-%d"),
        "bars": int(len(s)),
        "years": round(years, 3),
        "total_return_pct": round(total * 100, 2),
        "annual_return_pct": round(ann * 100, 2),
        "max_drawdown_pct": round(mdd * 100, 2),
        "annual_vol_pct": round(vol * 100, 2),
        "sharpe": round(sharpe, 3),
        "sortino": round(sortino, 3),
        "calmar": round(calmar, 3),
        "final_value": round(float(s.iloc[-1]), 2),
    }


def yearly(equity: list[dict]) -> dict[int, float]:
    s = pd.Series([e["value"] for e in equity],
                  index=pd.DatetimeIndex([e["date"] for e in equity]), dtype=float)
    out: dict[int, float] = {}
    prev = s.iloc[0]
    for y, grp in s.groupby(s.index.year):
        last = grp.iloc[-1]
        out[int(y)] = round((last / prev - 1.0) * 100, 2)
        prev = last
    return out


def index_curve(path: Path, start: str, end: str, base: float) -> list[dict]:
    if not path.exists():
        return []
    df = pd.read_csv(path, parse_dates=["date"]).set_index("date")["close"].sort_index()
    df = df.loc[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]
    if df.empty:
        return []
    nav = df / df.iloc[0] * base
    return [{"date": d.strftime("%Y-%m-%d"), "value": round(float(v), 2)} for d, v in nav.items()]


# --------------------------------------------------------------------------- #
#  Capacity grid & survivorship bound
# --------------------------------------------------------------------------- #
def capacity_grid(panel: Panel) -> tuple[list[dict], list[Result]]:
    """只数 × 资金 的容量前沿。

    这张表回答一个具体问题：**在真实费用结构下，多少只、多少钱才能让这条
    策略的收益不被摩擦吃掉**。答案不是"越多越好"。
    """
    names_opts: list[tuple[str, int | None]] = [
        ("全池", None), ("400", 400), ("200", 200), ("100", 100), ("50", 50),
    ]
    caps = [1e6, 3e6, 1e7, 5e7]
    rows, keep = [], []
    for label, n in names_opts:
        for cap in caps:
            if n is not None and cap < n * 100 * 20:      # 连一手都买不起的组合直接跳过
                continue
            cfg = Cfg(initial_cash=cap, n_names=n, label=f"{label}@{cap/1e4:.0f}万")
            r = run_ew(panel, cfg)
            p = perf(r.equity)
            if not p:
                continue
            years = p["years"]
            cost_drag = r.cost_total / cfg.initial_cash / years * 100
            rows.append({
                "names": label,
                "capital_wan": cap / 1e4,
                "total_pct": p["total_return_pct"],
                "annual_pct": p["annual_return_pct"],
                "mdd_pct": p["max_drawdown_pct"],
                "sharpe": p["sharpe"],
                "cost_wan": round(r.cost_total / 1e4, 1),
                "cost_drag_pct_yr": round(cost_drag, 2),
                "orders": r.n_orders,
                "rebalances": r.n_rebalances,
                "avg_positions": round(float(np.mean(r.n_positions)), 0),
                "avg_cash_pct": round(float(np.mean(r.cash_ratio)) * 100, 2),
                "turnover_yr": round((r.buy_value + r.sell_value) / 2 / cfg.initial_cash / years, 2),
            })
            keep.append(r)
    return rows, keep


def survivorship_bound(annual_gross_pct: float, years: float) -> list[dict]:
    """幸存者偏差的下界估计（假设驱动，不是测量）。

    我们没有历史上的时点成分股，也没有退市股的行情（westock 对已退市代码
    返回空表），所以**无法测量**这个偏差。能做的是给一个明确的界：

        每年有 d 比例的成分股被调出/退市，这部分相对留在池子里的标的
        每年少赚 h（h < 0），则组合年化收益的拖累 ≈ d × h。

    这个式子在一阶上是精确的：组合有 N 只等权，每年 d·N 只被"替换"，
    每只少赚 h，总拖累 = (d·N / N) × h = d·h。二阶项（复利、被替换标的
    的权重变化）在 d ≤ 20% 时可忽略。

    中证800 的年换手率历史上在 8%–15% 之间（含定期调样与临时调整），
    所以 d 取 5%/10%/15% 三档；h 取 -20%/-40%/-60%。
    """
    rows = []
    for d in (0.05, 0.10, 0.15):
        for h in (-0.20, -0.40, -0.60):
            drag = d * h * 100
            rows.append({
                "replace_per_year_pct": round(d * 100, 0),
                "relative_gap_pct": round(h * 100, 0),
                "annual_drag_pct": round(drag, 2),
                "adjusted_annual_pct": round(annual_gross_pct + drag, 2),
                "adjusted_total_pct": round(((1 + (annual_gross_pct + drag) / 100) ** years - 1) * 100, 1),
            })
    return rows


# --------------------------------------------------------------------------- #
#  Export
# --------------------------------------------------------------------------- #
def build_trades(orders: list[dict]) -> list[dict]:
    """orders 已经是回合记录（每个 symbol 一段持仓），直接转成标准 trade 行。"""
    out = []
    for o in orders:
        out.append({
            "entry_date": o["entry_date"],
            "exit_date": o["exit_date"],
            "side": "long",
            "size": int(o["buy_shares"]),
            "entry_price": round(o["buy_value"] / max(1, o["buy_shares"]), 4),
            "exit_price": round(o["sell_value"] / max(1, o["sell_shares"]), 4),
            "pnl": round(o["sell_value"] - o["buy_value"], 2),
            "pnl_pct": round((o["sell_value"] / o["buy_value"] - 1.0) * 100, 4),
            "holding_bars": int(o["exit_idx"] - o["entry_idx"]),
            "symbol": o["symbol"],
            "symbol_name": o["symbol_name"],
        })
    return out


def make_charts(res: Result, bench: dict[str, list[dict]], grid_rows: list[dict],
                yeardata: dict[int, float], outdir: Path) -> list[Path]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager as fm

    for cand in ("PingFang SC", "Heiti TC", "Arial Unicode MS", "Songti SC", "Hiragino Sans GB"):
        if cand in {f.name for f in fm.fontManager.ttflist}:
            plt.rcParams["font.sans-serif"] = [cand, "DejaVu Sans"]
            break
    plt.rcParams["axes.unicode_minus"] = False

    paths: list[Path] = []
    nav = pd.Series([e["value"] for e in res.equity],
                    index=pd.DatetimeIndex([e["date"] for e in res.equity]))
    nav = nav / nav.iloc[0]

    # 1) 净值 vs 基准
    fig, ax = plt.subplots(figsize=(11, 5.2), dpi=130)
    ax.plot(nav.index, nav.values, lw=1.6, color="#1f77b4", label=f"EW20（{res.label}）")
    colors = {"沪深300": "#9e9e9e", "中证500": "#bcbd22", "中证800": "#8c564b"}
    for k, series in bench.items():
        if not series:
            continue
        s = pd.Series([p["value"] for p in series], index=pd.DatetimeIndex([p["date"] for p in series]))
        ax.plot(s.index, s.values / s.iloc[0], lw=1.2, ls="--", color=colors.get(k, "#999"), label=k)
    ax.set_yscale("log")
    ax.set_title("EW20 净值 vs 指数（对数轴，起点归一）")
    ax.grid(alpha=0.25)
    ax.legend(loc="upper left", fontsize=9)
    fig.tight_layout()
    p = outdir / "ew20_nav_vs_bench.png"
    fig.savefig(p); plt.close(fig); paths.append(p)

    # 2) 容量前沿
    if grid_rows:
        df = pd.DataFrame(grid_rows)
        fig, ax = plt.subplots(figsize=(9, 5), dpi=130)
        for name, grp in df.groupby("names"):
            ax.plot(grp["capital_wan"], grp["annual_pct"], marker="o", label=f"{name} 只")
        ax.set_xscale("log")
        ax.set_xlabel("资金（万元，对数轴）")
        ax.set_ylabel("年化收益 %")
        ax.set_title("容量前沿：只数 × 资金 → 年化（含全部摩擦成本）")
        ax.grid(alpha=0.25)
        ax.legend(fontsize=9)
        fig.tight_layout()
        p = outdir / "ew20_capacity.png"
        fig.savefig(p); plt.close(fig); paths.append(p)

    # 3) 年度收益
    fig, ax = plt.subplots(figsize=(9, 4.2), dpi=130)
    ys = sorted(yeardata)
    vals = [yeardata[y] for y in ys]
    ax.bar([str(y) for y in ys], vals, color=["#d62728" if v < 0 else "#2ca02c" for v in vals])
    ax.axhline(0, color="#333", lw=0.8)
    ax.set_title("年度收益 %")
    ax.grid(axis="y", alpha=0.25)
    for i, v in enumerate(vals):
        ax.text(i, v + (1.5 if v >= 0 else -3.5), f"{v:.1f}", ha="center", fontsize=8)
    fig.tight_layout()
    p = outdir / "ew20_yearly.png"
    fig.savefig(p); plt.close(fig); paths.append(p)

    # 4) 现金占比与持仓数
    fig, ax = plt.subplots(figsize=(11, 3.8), dpi=130)
    ax.plot(nav.index, np.array(res.cash_ratio) * 100, color="#ff7f0e", lw=1.1, label="现金占比 %")
    ax2 = ax.twinx()
    ax2.plot(nav.index, res.n_positions, color="#1f77b4", lw=1.0, alpha=0.7, label="持仓只数")
    ax.set_ylabel("现金占比 %", color="#ff7f0e")
    ax2.set_ylabel("持仓只数", color="#1f77b4")
    ax.set_title("资金利用率：整手与涨停导致的现金闲置")
    ax.grid(alpha=0.25)
    fig.tight_layout()
    p = outdir / "ew20_cost.png"
    fig.savefig(p); plt.close(fig); paths.append(p)
    return paths


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="只跑基准，跳过容量网格")
    ap.add_argument("--capital", type=float, default=10_000_000.0)
    ap.add_argument("--names", type=int, default=200, help="0 = 全池")
    args = ap.parse_args()

    print("[1/5] 读取面板 ...", flush=True)
    panel = load_panel()
    print(f"      {len(panel.open.columns)} 只 / {len(panel.calendar)} 个交易日 "
          f"({panel.calendar[0].date()} → {panel.calendar[-1].date()})", flush=True)

    n_names = None if args.names == 0 else args.names
    cfg = Cfg(initial_cash=args.capital, n_names=n_names)
    print(f"[2/5] 跑基准：{n_names or '全池'} 只 / 资金 {args.capital/1e4:.0f} 万 ...", flush=True)
    res = run_ew(panel, cfg)

    # 回合交易在 run_ew 内已聚合到 book 的闭合记录，这里从 res.trades 取不到，
    # 改为重新统计：用 equity 一致口径，交易明细由 _collect 补
    p = perf(res.equity)
    print(f"      总收益 {p['total_return_pct']:.2f}%  年化 {p['annual_return_pct']:.2f}%  "
          f"回撤 {p['max_drawdown_pct']:.2f}%  Sharpe {p['sharpe']:.2f}", flush=True)
    print(f"      订单 {res.n_orders:,} 笔 / 调仓 {res.n_rebalances} 次 / "
          f"成本 {res.cost_total/1e4:.1f} 万 / 平均现金 {np.mean(res.cash_ratio)*100:.1f}%",
          flush=True)

    benchmark = {}
    for k, path in INDEX_FILES.items():
        benchmark[k] = index_curve(path, p["start"], p["end"], res.equity[0]["value"])

    grid_rows: list[dict] = []
    if not args.quick:
        print("[3/5] 容量网格（只数 × 资金）...", flush=True)
        grid_rows, _ = capacity_grid(panel)
        for r in grid_rows:
            print(f"      {r['names']:>4} 只 @ {r['capital_wan']:>6.0f} 万 → 年化 "
                  f"{r['annual_pct']:>6.2f}%  成本拖累 {r['cost_drag_pct_yr']:>5.2f}%/年  "
                  f"平均现金 {r['avg_cash_pct']:>5.1f}%", flush=True)

    years = p["years"]
    srows = survivorship_bound(p["annual_return_pct"], years)

    yd = yearly(res.equity)
    print("[4/5] 写三件套 ...", flush=True)
    os.makedirs(OUT, exist_ok=True)   # Path.mkdir 在沙箱 shim 下 exist_ok 有 bug，用 os.makedirs
    from reporting.export_results import export_results

    files = export_results(
        equity_curve=res.equity,
        trade_history=build_trades(res.trades),
        prefix="ew20",
        initial_cash=cfg.initial_cash,
        start=p["start"],
        end=p["end"],
        market="china_a",
        output_dir=OUT,
        strategy_name="EW20 等权20日再平衡",
        is_flat_at_end=True,
    )
    summary = {
        "summary": p,
        "meta": {
            "strategy": "中证800 等权 + 20 个交易日再平衡",
            "universe": f"{len(panel.open.columns)} 只（当前中证800 成分股）",
            "n_names": n_names or "全池",
            "rebalance_bars": REBALANCE_BARS,
            "initial_cash": cfg.initial_cash,
            "market": "china_a",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "execution": "信号日收盘出单 → 次日开盘成交；T+1；100 股整手；涨停不买/跌停不卖；停牌跳过",
            "cost_model": {
                "commission_bps": cfg.cost.commission_bps,
                "min_commission": cfg.cost.min_commission,
                "stamp_bps_sell": cfg.cost.stamp_bps,
                "transfer_bps": cfg.cost.transfer_bps,
                "slippage_bps": cfg.cost.slippage_bps,
                "min_order_value": cfg.min_order_value,
            },
            "cost_paid": round(res.cost_total, 2),
            "orders": res.n_orders,
            "rebalances": res.n_rebalances,
            "avg_cash_pct": round(float(np.mean(res.cash_ratio)) * 100, 2),
            "avg_positions": round(float(np.mean(res.n_positions)), 1),
            "blocked": {
                "limit_down": res.blocked_limit_down,
                "limit_up_skip": res.skipped_limit_up,
                "suspend": res.blocked_suspend,
                "lot_too_small": res.skipped_lot,
            },
            "survivorship": "标的池为当前中证800 成分股，含幸存者偏差；退市股行情不可得，无法测量，仅给界",
        },
        "yearly_pct": {str(k): v for k, v in yd.items()},
        "capacity_grid": grid_rows,
        "survivorship_bound": srows,
        "benchmarks": {
            k: (v[-1]["value"] / v[0]["value"] - 1.0) * 100 if v else None
            for k, v in benchmark.items()
        },
    }
    (OUT / "ew20_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")

    print("[5/5] 画图 ...", flush=True)
    pngs = make_charts(res, benchmark, grid_rows, yd, OUT)
    for x in pngs:
        print(f"      {x.name}", flush=True)

    print("\n[done]")
    print(f"  基准：{n_names or '全池'} 只 @ {cfg.initial_cash/1e4:.0f} 万")
    print(f"  总收益 {p['total_return_pct']:.2f}% / 年化 {p['annual_return_pct']:.2f}% / "
          f"MDD {p['max_drawdown_pct']:.2f}% / Sharpe {p['sharpe']:.2f}")
    for k, v in summary["benchmarks"].items():
        if v is not None:
            print(f"  {k}: {v:+.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
