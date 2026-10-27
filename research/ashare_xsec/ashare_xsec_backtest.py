#!/usr/bin/env python3
"""A 股横截面选股回测（预注册双假设：20 日反转 / 120 日动量）。

为什么是这个脚本
----------------
上一轮把「510300 单标的择时」这条路走到了死胡同（105 个检验里唯一存活的因子，
落地成策略后 -0.44%，而买入持有 +32.80%）。结论是：猜指数方向在 A 股没有立足点。
剩下的唯一一条 A 股路径是「横截面选股」——不猜大盘涨跌，只赌同一时点横截面上
相对强弱的分化。

预注册（写代码之前就定死，不给事后调参留口子）
--------------------------------------------
1. 假设 H1：20 日反转——过去 20 个交易日跌得最惨的 10%，未来 20 个交易日跑赢中位数。
2. 假设 H2：120 日动量——过去 120 个交易日涨得最好的 10%，未来 20 个交易日跑赢中位数。
   H2 是经典对照，不是第二个猜想：如果连这个都没效应，说明这套数据/流程本身没有
   横截面信息，H1 的结果就更不可信。
   两个假设，就两个检验。上一轮 105 个检验的教训是：检验越多，活下来的越可能是噪声。
3. 调仓 20 个交易日一次、持有 20 个交易日、等权、只做多（A 股个股不能裸空）。
   **持有期与调仓期完全相同 → 样本不重叠**，这是本轮相对上轮最大的方法论改进：
   上轮 10 倍放大就是重叠样本造成的。
4. 成本照实收：佣金 2.5bp 双边 + 印花税 5bp（仅卖出）+ 过户费 0.1bp 双边 = 单次往返 10.2bp。
5. 最后 20% 的日期封存，只在最后看一眼。

执行模型
--------
* 信号在第 t 日收盘后算出，第 t+1 日开盘价成交（不用产生信号的当根 bar 的价格）；
* T+1：当日买入的标的当日不可卖（用 entry_date 显式拦截）；
* 100 股整手；科创板（sh688）最低 200 股；
* 涨跌停拒单：开盘价已在涨停 → 不买；已在跌停 → 不卖，顺延到下一日；
* 停牌（当日无成交）不能交易，顺延；
* 样本末尾强制平仓，用最后一根 bar 的收盘价。

数据口径
--------
标的池 = 当前中证 800 成分股（沪深300 + 中证500）。**这是幸存者偏差的来源**：
今天还在指数里的股票，天生就是活下来并且做大的那批。结论方向偏乐观，不能当预期收益。
日线为前复权，接口单标的返回上限 2000 根（2018-07-24 起），覆盖约 8.2 年。

Run:
    python ashare_xsec_backtest.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent      # 策略目录；产物写 ROOT/outputs
PROJ = ROOT.parent.parent                   # 项目根：state/ 与 reporting/ 在这里
OUT = ROOT / "outputs"
from reporting.export_results import export_results  # noqa: E402

# --------------------------------------------------------------------------- #
#  Pre-registered configuration
# --------------------------------------------------------------------------- #
DATA_DIR = PROJ / "state" / "ashare" / "daily"
INDEX_CSV = PROJ / "state" / "ashare" / "index_000300.csv"
AUDIT_JSON = PROJ / "state" / "ashare" / "fetch_audit.json"

INITIAL_CASH = 2_000_000.0

LOOKBACK = {"rev20": 20, "mom120": 120}
# 因子方向：+1 = 买分数最高的十分位；-1 = 买分数最低的十分位
DIRECTION = {"rev20": -1, "mom120": +1}

REBALANCE_BARS = 20          # 每 20 个交易日调仓一次
QUANTILE = 0.10              # 取十分位
MIN_CROSS_SECTION = 200      # 横截面可用标的少于此数则跳过该期

COMMISSION = 0.00025         # 佣金 2.5bp，双边
STAMP_TAX = 0.0005           # 印花税 5bp，仅卖出
TRANSFER_FEE = 0.00001       # 过户费 0.1bp，双边
LOT = 100

LIMIT_MAIN = 0.10            # 主板 ±10%
LIMIT_GROWTH = 0.20          # 创业板 / 科创板 ±20%
LIMIT_TOLERANCE = 0.002      # 距涨跌停 0.2% 以内视为封板，不假设能成交

HOLDOUT_FRAC = 0.20          # 末尾 20% 封存

BUY_COST = COMMISSION + TRANSFER_FEE
SELL_COST = COMMISSION + STAMP_TAX + TRANSFER_FEE   # 合计单次往返 10.2bp

DISCLAIMER = (
    "⚠️ 以上内容由 AI 基于公开信息整理生成，仅供参考，不构成任何投资建议或个股推荐。"
    "投资有风险，决策需谨慎。"
)


# --------------------------------------------------------------------------- #
#  Data layer
# --------------------------------------------------------------------------- #
@dataclass
class Panel:
    calendar: pd.DatetimeIndex
    open: pd.DataFrame
    close: pd.DataFrame
    volume: pd.DataFrame
    names: dict[str, str]
    fetched: int
    requested: int
    mark: pd.DataFrame = None  # type: ignore[assignment]  # 停牌日盯市价（前向填充），只用于估值

    def __post_init__(self) -> None:
        if self.mark is None:
            self.mark = self.close.ffill()


def load_panel(data_dir: Path = DATA_DIR) -> Panel:
    files = sorted(data_dir.glob("*.csv"))
    if not files:
        raise SystemExit(
            f"[FATAL] {data_dir} 里没有数据。先跑：python scripts/fetch_ashare_universe.py"
        )

    frames: dict[str, pd.DataFrame] = {}
    for path in files:
        code = path.stem
        df = pd.read_csv(path, parse_dates=["date"])
        if len(df) < 60:
            continue
        frames[code] = df.set_index("date")[["open", "close", "volume"]]

    if not frames:
        raise SystemExit("[FATAL] 没有一只标的有足够历史")

    calendar = pd.DatetimeIndex(sorted(set().union(*[set(df.index) for df in frames.values()])))
    codes = sorted(frames)

    def _wide(col: str) -> pd.DataFrame:
        return pd.concat({c: frames[c][col] for c in codes}, axis=1).reindex(calendar)

    names: dict[str, str] = {}
    requested = len(codes)
    if AUDIT_JSON.exists():
        audit = json.loads(AUDIT_JSON.read_text(encoding="utf-8"))
        names = {c: n for c, n in (audit.get("names") or {}).items() if c in set(codes)}
        requested = int(audit.get("requested") or len(codes))

    return Panel(
        calendar=calendar,
        open=_wide("open"),
        close=_wide("close"),
        volume=_wide("volume"),
        names=names,
        fetched=len(codes),
        requested=requested,
    )


def limit_width(code: str) -> float:
    """涨跌幅限制宽度。ST 无法从行情数据判断，统一按 10%（对 ST 偏宽松，已披露）。"""
    if code.startswith("sh688") or code.startswith("sz30"):
        return LIMIT_GROWTH
    return LIMIT_MAIN


def min_shares(code: str) -> int:
    return 200 if code.startswith("sh688") else LOT


# --------------------------------------------------------------------------- #
#  Factor layer — only trailing data, no shift(-n)
# --------------------------------------------------------------------------- #
def build_factor(close: pd.DataFrame, volume: pd.DataFrame, name: str) -> pd.DataFrame:
    n = LOOKBACK[name]
    raw = close / close.shift(n) - 1.0
    # 信号日必须真的在交易（volume>0），否则停牌期的陈旧价格会变成假信号
    traded = volume > 0
    raw = raw.where(traded & traded.shift(n, fill_value=False))
    return raw


# --------------------------------------------------------------------------- #
#  Execution
# --------------------------------------------------------------------------- #
@dataclass
class Position:
    code: str
    size: int
    entry_px: float
    entry_date: pd.Timestamp
    entry_idx: int
    entry_cost: float


@dataclass
class Run:
    name: str
    equity: list[dict] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    plan_dates: list[tuple[int, int]] = field(default_factory=list)   # (signal_idx, exec_idx)
    skipped_price: int = 0
    skipped_limit_up: int = 0
    blocked_limit_down: int = 0
    blocked_suspend: int = 0
    period_returns: list[dict] = field(default_factory=list)


def allocate_cash(
    cash: float,
    targets: list[str],
    prices: dict[str, float],
) -> tuple[dict[str, int], float]:
    """等权分配，向上取整到整手；剩余现金再分配一轮，最多 3 轮。

    返回 (仓位字典, 实际占用现金)。已含买入端费用。
    """
    if not targets or cash <= 0:
        return {}, 0.0

    sizes: dict[str, int] = {}
    budget = cash / len(targets)
    for _ in range(3):
        sizes = {}
        for code in targets:
            px = prices.get(code)
            if not px or px <= 0 or not np.isfinite(px):
                continue
            size = int(budget / (px * (1.0 + BUY_COST))) // LOT * LOT
            if size < min_shares(code):
                continue
            sizes[code] = size
        spent = sum(s * prices[c] * (1.0 + BUY_COST) for c, s in sizes.items())
        if not sizes or spent >= cash * 0.995:
            break
        budget += (cash - spent) / len(sizes)

    spent = sum(s * prices[c] * (1.0 + BUY_COST) for c, s in sizes.items())
    # 保底：绝不超出现金（多轮再分配可能略微冲高）
    while spent > cash and sizes:
        worst = max(sizes, key=lambda c: sizes[c] * prices[c])
        del sizes[worst]
        spent = sum(s * prices[c] * (1.0 + BUY_COST) for c, s in sizes.items())
    return sizes, spent


def run_strategy(
    panel: Panel,
    factor: pd.DataFrame,
    name: str,
    direction: int,
    eval_start: int,
    eval_end: int,
) -> Run:
    cal = panel.calendar
    open_px = panel.open
    close_px = panel.close
    mark_px = panel.mark
    volume = panel.volume

    out = Run(name=name)
    cash = INITIAL_CASH
    positions: dict[str, Position] = {}
    pending_exit: set[str] = set()

    # ---- 调仓计划：信号日 i -> 执行日 i+1（下一交易日开盘） ----------------
    signal_idx = list(range(eval_start, eval_end, REBALANCE_BARS))
    plan: dict[int, int] = {}
    for i in signal_idx:
        if i + 1 <= eval_end:
            plan[i + 1] = i
    out.plan_dates = sorted((i, i + 1) for i in plan.values())

    def _sell(idx: int, pos: Position, px: float, date: pd.Timestamp) -> None:
        nonlocal cash
        proceeds = pos.size * px * (1.0 - SELL_COST)
        cash += proceeds
        pnl = proceeds - pos.entry_cost
        out.trades.append({
            "entry_date": pos.entry_date.strftime("%Y-%m-%d"),
            "exit_date": date.strftime("%Y-%m-%d"),
            "side": "long",
            "size": int(pos.size),
            "entry_price": round(float(pos.entry_px), 4),
            "exit_price": round(float(px), 4),
            "pnl": round(float(pnl), 2),
            "pnl_pct": round(float(pnl / pos.entry_cost * 100.0), 4),
            "holding_bars": int(idx - pos.entry_idx),
            "symbol": pos.code,
            "symbol_name": panel.names.get(pos.code, pos.code),
        })
        del positions[pos.code]

    for idx in range(eval_start, eval_end + 1):
        date = cal[idx]

        # ---- 1. 先把上一日被跌停/停牌拦下的卖单补上 --------------------
        for code in list(pending_exit):
            pos = positions.get(code)
            if pos is None:
                pending_exit.discard(code)
                continue
            px = open_px.at[date, code] if code in open_px.columns else np.nan
            if not np.isfinite(px) or not np.isfinite(volume.at[date, code]):
                out.blocked_suspend += 1
                continue
            prev_close = close_px.at[cal[idx - 1], code]
            if np.isfinite(prev_close) and px <= prev_close * (1.0 - limit_width(code) + LIMIT_TOLERANCE):
                out.blocked_limit_down += 1
                continue
            if pos.entry_date >= date:          # T+1
                continue
            _sell(idx, pos, float(px), date)
            pending_exit.discard(code)

        # ---- 2. 调仓日：先清后买 ---------------------------------------
        if idx in plan:
            sig_idx = plan[idx]
            for code in list(positions):
                pos = positions[code]
                px = open_px.at[date, code] if code in open_px.columns else np.nan
                if not np.isfinite(px) or not np.isfinite(volume.at[date, code]):
                    pending_exit.add(code)
                    out.blocked_suspend += 1
                    continue
                prev_close = close_px.at[cal[idx - 1], code]
                if np.isfinite(prev_close) and px <= prev_close * (1.0 - limit_width(code) + LIMIT_TOLERANCE):
                    pending_exit.add(code)
                    out.blocked_limit_down += 1
                    continue
                if pos.entry_date >= date:      # T+1
                    continue
                _sell(idx, pos, float(px), date)

            # 目标组合：只在信号日「真的在交易 + 有因子 + 有开盘价」的标的里挑
            scores = factor.iloc[sig_idx]
            valid = scores.where(scores.notna() & (volume.iloc[sig_idx] > 0)).dropna()
            if len(valid) >= MIN_CROSS_SECTION:
                ranked = valid.sort_values(ascending=(direction < 0))
                k = max(1, int(round(len(ranked) * QUANTILE)))
                targets = list(ranked.index[:k])
                prices = {}
                for code in targets:
                    px = open_px.at[date, code]
                    prices[code] = float(px) if np.isfinite(px) else np.nan

                # 涨停/停牌不买
                tradable = []
                for code in targets:
                    px = prices.get(code)
                    if not px or not np.isfinite(px) or not np.isfinite(volume.at[date, code]):
                        out.skipped_price += 1
                        continue
                    prev_close = close_px.at[cal[idx - 1], code]
                    if np.isfinite(prev_close) and px >= prev_close * (1.0 + limit_width(code) - LIMIT_TOLERANCE):
                        out.skipped_limit_up += 1
                        continue
                    if code in pending_exit:       # 还没卖掉，不重复买
                        continue
                    tradable.append(code)

                sizes, spent = allocate_cash(cash, tradable, prices)
                filled = 0
                for code, size in sizes.items():
                    px = prices[code]
                    cost = size * px * (1.0 + BUY_COST)
                    if cost > cash:
                        continue
                    cash -= cost
                    positions[code] = Position(
                        code=code, size=size, entry_px=px, entry_date=date,
                        entry_idx=idx, entry_cost=cost,
                    )
                    filled += 1
                out.skipped_price += len(tradable) - filled

        # ---- 3. 每日盯市 -------------------------------------------------
        value = cash
        for code, pos in positions.items():
            px = mark_px.at[date, code]
            if not np.isfinite(px):
                px = pos.entry_px
            value += pos.size * float(px)
        out.equity.append({"date": date.strftime("%Y-%m-%d"), "value": round(value, 2)})

    # ---- 4. 样本末尾强制平仓 --------------------------------------------
    last = cal[eval_end]
    for code in list(positions):
        pos = positions[code]
        px = close_px.at[last, code]
        if not np.isfinite(px):
            px = mark_px.at[last, code]
        if not np.isfinite(px):
            px = pos.entry_px
        _sell(eval_end, pos, float(px), last)

    assert not positions, "样本末尾未清空持仓"
    return out


# --------------------------------------------------------------------------- #
#  Post-hoc analysis (never feeds the trading loop)
# --------------------------------------------------------------------------- #
def forward_return_panel(panel: Panel, bars: int = REBALANCE_BARS) -> pd.DataFrame:
    """执行日开盘 -> 持有到期开盘的前瞻收益。

    这里出现 shift(-n) 是**研究统计**（IC 检验要用未来收益当标签），
    下面的交易循环从不调用这个函数——两者在代码上物理隔离。
    """
    fwd = panel.open.shift(-1 - bars) / panel.open.shift(-1) - 1.0
    return fwd


def ic_stats(
    factor: pd.DataFrame,
    fwd: pd.DataFrame,
    signal_idx: list[int],
    direction: int = 1,
    n_perm: int = 500,
    seed: int = 20261026,
) -> dict:
    """横截面 Rank IC。

    `direction` 把 IC 统一成「该策略方向是否有效」：IC > 0 = 按这个方向买是对的。
    反转因子原始 IC 天然为负，直接看原值容易读反。
    """
    rng = np.random.default_rng(seed)
    ics: list[float] = []
    used_dates: list[str] = []
    for i in signal_idx:
        f = factor.iloc[i]
        r = fwd.iloc[i]
        mask = f.notna() & r.notna()
        if mask.sum() < MIN_CROSS_SECTION:
            continue
        fv = f[mask].to_numpy(dtype=float)
        rv = r[mask].to_numpy(dtype=float)
        rf = pd.Series(fv).rank().to_numpy()
        rr = pd.Series(rv).rank().to_numpy()
        if rf.std() == 0 or rr.std() == 0:
            continue
        ics.append(float(np.corrcoef(rf, rr)[0, 1]) * direction)
        used_dates.append(str(factor.index[i].date()))

    if len(ics) < 10:
        return {"n": len(ics), "mean_ic": None, "t_stat": None, "p_perm": None}

    arr = np.asarray(ics)
    mean_ic = float(arr.mean())
    t_stat = float(mean_ic / (arr.std(ddof=1) / np.sqrt(len(arr)))) if arr.std(ddof=1) > 0 else None

    # 置换零假设：每个信号日内部打乱因子横截面顺序，再重算平均 IC
    null = np.empty(n_perm)
    for k in range(n_perm):
        sims = []
        for i in signal_idx:
            f = factor.iloc[i]
            r = fwd.iloc[i]
            mask = f.notna() & r.notna()
            if mask.sum() < MIN_CROSS_SECTION:
                continue
            fv = f[mask].to_numpy(dtype=float)
            rv = r[mask].to_numpy(dtype=float)
            perm = rng.permutation(fv)
            rf = pd.Series(perm).rank().to_numpy()
            rr = pd.Series(rv).rank().to_numpy()
            if rf.std() == 0 or rr.std() == 0:
                continue
            sims.append(float(np.corrcoef(rf, rr)[0, 1]))
        null[k] = float(np.mean(sims)) if sims else np.nan

    null = null[np.isfinite(null)]
    p_perm = float((np.abs(null) >= abs(mean_ic)).mean()) if len(null) else None

    return {
        "n": len(ics),
        "mean_ic": mean_ic,
        "ic_std": float(arr.std(ddof=1)),
        "ic_positive_share_pct": float((arr > 0).mean() * 100.0),
        "t_stat": t_stat,
        "p_perm": p_perm,
        "first_date": used_dates[0],
        "last_date": used_dates[-1],
    }


def bh_fdr(pvals: list[float]) -> list[float]:
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    out = [1.0] * m
    prev = 1.0
    for rank, i in enumerate(reversed(order), start=1):
        k = m - rank + 1
        val = min(prev, pvals[i] * m / k)
        out[i] = val
        prev = val
    return out


def equal_weight_benchmark(
    panel: Panel, signal_idx: list[int], bars: int = REBALANCE_BARS
) -> tuple[list[dict], list[dict]]:
    """等权全池基准：每个信号日等权买入**全部可交易标的**，持有 bars 个交易日后卖出。

    口径必须和策略完全一致，否则超额收益是假的：
    - 成交价：信号日 i 的因子来自当日收盘，所以买入发生在 i+1 的**开盘**（不是 i 的收盘，
      否则就是前视）；卖出发生在 i+1+bars 的开盘。
    - 频率：每一个信号日都调仓，不许跳期（旧实现每期跳过后一天，只调了 47/94 期，
      把基准低估了 3.5 倍，直接导致超额收益被虚高）。
    - 不计成本：基准就是"什么都不做"的那条线，成本在策略侧体现。
    """
    cal = panel.calendar
    open_px = panel.open
    volume = panel.volume

    equity: list[dict] = []
    periods: list[dict] = []
    nav = INITIAL_CASH
    start = None

    for i in signal_idx:
        entry, exit_ = i + 1, i + 1 + bars
        if exit_ >= len(cal):
            break
        live = volume.iloc[i] > 0
        px_in = open_px.iloc[entry][live]
        px_out = open_px.iloc[exit_][live]
        ok = px_in.notna() & px_out.notna() & (px_in > 0)
        if ok.sum() < MIN_CROSS_SECTION:
            continue
        if start is None:
            start = entry
            equity.append({"date": cal[start].strftime("%Y-%m-%d"), "value": round(nav, 2)})
        period_ret = float((px_out[ok] / px_in[ok] - 1.0).mean())
        periods.append({
            "entry": cal[entry].strftime("%Y-%m-%d"),
            "exit": cal[exit_].strftime("%Y-%m-%d"),
            "return_pct": period_ret * 100.0,
            "names": int(ok.sum()),
        })
        # 逐日盯市：用期初净值 × 篮子的收盘价相对涨幅，画出来的曲线才和策略可比
        idx_ok = ok.index[ok]
        base_close = panel.mark.iloc[entry][idx_ok]
        nav_before = nav
        nav *= 1.0 + period_ret
        for d in range(entry, exit_ + 1):
            ratio = (panel.mark.iloc[d][idx_ok] / base_close).dropna()
            if ratio.empty:
                continue
            equity.append({
                "date": cal[d].strftime("%Y-%m-%d"),
                "value": round(float(nav_before * ratio.mean()), 2),
            })
    return equity, periods


def index_benchmark(panel: Panel, start: str, end: str) -> list[dict]:
    if not INDEX_CSV.exists():
        return []
    df = pd.read_csv(INDEX_CSV, parse_dates=["date"]).set_index("date")
    df = df.loc[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]
    if df.empty:
        return []
    base = float(df["close"].iloc[0])
    return [
        {"date": d.strftime("%Y-%m-%d"), "value": round(INITIAL_CASH * float(c) / base, 2)}
        for d, c in df["close"].items()
    ]


# --------------------------------------------------------------------------- #
#  Reporting helpers
# --------------------------------------------------------------------------- #
def yearly_returns(equity: list[dict]) -> dict[int, float]:
    """按自然年计算收益，基期用上一年最后一个净值（链式），不用当年首根。"""
    if not equity:
        return {}
    s = pd.Series(
        [p["value"] for p in equity],
        index=pd.DatetimeIndex([p["date"] for p in equity]),
    )
    out: dict[int, float] = {}
    prev_year_end = float(s.iloc[0])
    for year, chunk in s.groupby(s.index.year):
        out[int(year)] = (float(chunk.iloc[-1]) / prev_year_end - 1.0) * 100.0
        prev_year_end = float(chunk.iloc[-1])
    return out


def summarize_holdout(equity: list[dict], start: str) -> dict:
    s = pd.Series(
        [p["value"] for p in equity],
        index=pd.DatetimeIndex([p["date"] for p in equity]),
    )
    seg = s[s.index >= pd.Timestamp(start)]
    if len(seg) < 2:
        return {"start": start, "return_pct": None}
    return {
        "start": start,
        "return_pct": (float(seg.iloc[-1]) / float(seg.iloc[0]) - 1.0) * 100.0,
        "value_start": float(seg.iloc[0]),
        "value_end": float(seg.iloc[-1]),
    }


# --------------------------------------------------------------------------- #
#  Charts
# --------------------------------------------------------------------------- #
def make_charts(
    curves: dict[str, list[dict]],
    trades_by_name: dict[str, list[dict]],
    yearly: dict[str, dict[int, float]],
    stats: dict[str, dict],
) -> list[Path]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["PingFang SC", "Arial Unicode MS", "Heiti TC", "sans-serif"]
    plt.rcParams["axes.unicode_minus"] = False

    made: list[Path] = []

    # 1) 净值对比
    fig, ax = plt.subplots(figsize=(11, 5.2))
    for label, curve, style in curves["plot"]:
        if not curve:
            continue
        s = pd.Series(
            [p["value"] for p in curve],
            index=pd.DatetimeIndex([p["date"] for p in curve]),
        )
        ax.plot(s.index, s.values / float(s.iloc[0]) * 100.0, label=label, **style)
    ax.set_title("净值对比（起点 = 100）")
    ax.set_ylabel("净值")
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(alpha=0.25)
    fig.tight_layout()
    plt.show()
    p = ROOT / "outputs" / "ashare_xsec_nav_vs_bench.png"
    fig.savefig(p, dpi=140)
    plt.close(fig)
    made.append(p)

    # 2) 回撤
    fig, ax = plt.subplots(figsize=(11, 3.8))
    for label, curve, style in curves["plot"]:
        if not curve or "rev20" not in label and "动量" not in label and "反转" not in label:
            continue
        s = pd.Series(
            [p["value"] for p in curve],
            index=pd.DatetimeIndex([p["date"] for p in curve]),
        )
        dd = (s / s.cummax() - 1.0) * 100.0
        ax.plot(dd.index, dd.values, label=label, **style)
    ax.set_title("回撤对比（%）")
    ax.set_ylabel("%")
    ax.legend(loc="lower left", fontsize=9)
    ax.grid(alpha=0.25)
    fig.tight_layout()
    plt.show()
    p = ROOT / "outputs" / "ashare_xsec_drawdown.png"
    fig.savefig(p, dpi=140)
    plt.close(fig)
    made.append(p)

    # 3) 年度收益
    years = sorted({y for d in yearly.values() for y in d})
    fig, ax = plt.subplots(figsize=(11, 4.2))
    width = 0.26
    labels = list(yearly)
    for j, name in enumerate(labels):
        vals = [yearly[name].get(y, np.nan) for y in years]
        ax.bar(np.arange(len(years)) + (j - (len(labels) - 1) / 2) * width, vals, width, label=name)
    ax.set_xticks(np.arange(len(years)))
    ax.set_xticklabels([str(y) for y in years], rotation=0)
    ax.axhline(0, color="#888", linewidth=0.8)
    ax.set_title("分年度收益（%，链式基期）")
    ax.set_ylabel("%")
    ax.legend(fontsize=9)
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    plt.show()
    p = ROOT / "outputs" / "ashare_xsec_yearly.png"
    fig.savefig(p, dpi=140)
    plt.close(fig)
    made.append(p)

    # 4) 单笔收益分布
    fig, axes = plt.subplots(1, len(trades_by_name), figsize=(11, 3.6), squeeze=False)
    for ax, (name, trades) in zip(axes[0], trades_by_name.items()):
        vals = [t["pnl_pct"] for t in trades]
        ax.hist(vals, bins=60, color="#4c78a8", alpha=0.85)
        ax.axvline(0, color="#888", linewidth=0.8)
        ax.set_title(f"{name} 单笔收益分布（n={len(vals)}）")
        ax.set_xlabel("%")
        ax.grid(alpha=0.2)
    fig.tight_layout()
    plt.show()
    p = ROOT / "outputs" / "ashare_xsec_trade_dist.png"
    fig.savefig(p, dpi=140)
    plt.close(fig)
    made.append(p)

    # 5) 因子 IC
    names = [n for n in stats if stats[n].get("mean_ic") is not None]
    if names:
        fig, ax = plt.subplots(figsize=(7.5, 3.6))
        vals = [stats[n]["mean_ic"] for n in names]
        errs = [stats[n]["ic_std"] / np.sqrt(stats[n]["n"]) for n in names]
        ax.bar(names, vals, yerr=errs, capsize=5, color=["#e45756" if v > 0 else "#4c78a8" for v in vals])
        ax.axhline(0, color="#888", linewidth=0.8)
        for i, n in enumerate(names):
            p_ = stats[n].get("p_perm")
            ax.annotate(f"p={p_:.3f}" if p_ is not None else "", (i, vals[i]),
                        textcoords="offset points", xytext=(0, 8), ha="center", fontsize=9)
        ax.set_title("横截面 Rank IC（均值 ± 标准误）")
        ax.set_ylabel("IC")
        ax.grid(axis="y", alpha=0.25)
        fig.tight_layout()
        plt.show()
        p = ROOT / "outputs" / "ashare_xsec_ic.png"
        fig.savefig(p, dpi=140)
        plt.close(fig)
        made.append(p)

    return made


# --------------------------------------------------------------------------- #
#  Main
# --------------------------------------------------------------------------- #
def main() -> int:
    t0 = time.time()
    OUT.mkdir(exist_ok=True)
    os.chdir(OUT)
    panel = load_panel()
    cal = panel.calendar

    print(f"[data] 标的 {panel.fetched}/{panel.requested} 只，"
          f"{cal[0].date()} → {cal[-1].date()}，共 {len(cal)} 个交易日")
    if panel.fetched < panel.requested:
        print(f"[warn] 有 {panel.requested - panel.fetched} 只未取到数据（接口失败），已如实记录")

    factors = {name: build_factor(panel.close, panel.volume, name) for name in LOOKBACK}
    max_look = max(LOOKBACK.values())

    # 评估起点：横截面第一次达到 MIN_CROSS_SECTION 的日子
    ready = (factors["mom120"].notna() & (panel.volume > 0)).sum(axis=1)
    ok_days = ready[ready >= MIN_CROSS_SECTION]
    if ok_days.empty:
        raise SystemExit("[FATAL] 从未形成足够的横截面，无法评估")
    eval_start = int(cal.get_loc(ok_days.index[0]))
    eval_end = len(cal) - 1
    holdout_start_idx = int(eval_start + (eval_end - eval_start) * (1 - HOLDOUT_FRAC))
    holdout_start = cal[holdout_start_idx].strftime("%Y-%m-%d")

    print(f"[window] 评估 {cal[eval_start].date()} → {cal[eval_end].date()}，"
          f"封存起点 {holdout_start}，warmup {max_look} 根")

    results: dict[str, Run] = {}
    for name in LOOKBACK:
        run = run_strategy(panel, factors[name], name, DIRECTION[name], eval_start, eval_end)
        results[name] = run
        print(f"[run] {name}: {len(run.trades)} 笔闭合交易，"
              f"期末 {run.equity[-1]['value']:,.0f}，"
              f"涨停跳过 {run.skipped_limit_up}，跌停顺延 {run.blocked_limit_down}，"
              f"停牌 {run.blocked_suspend}，整手跳过 {run.skipped_price}")

    # ---- 基准 -----------------------------------------------------------
    signal_idx = list(range(eval_start, eval_end, REBALANCE_BARS))
    ew_equity, ew_periods = equal_weight_benchmark(panel, signal_idx)
    idx_equity = index_benchmark(panel, cal[eval_start].strftime("%Y-%m-%d"),
                                 cal[eval_end].strftime("%Y-%m-%d"))
    ew_year = yearly_returns(ew_equity)
    idx_year = yearly_returns(idx_equity)

    # ---- IC（研究统计，与交易循环物理隔离） ------------------------------
    fwd = forward_return_panel(panel, REBALANCE_BARS)
    stats: dict[str, dict] = {}
    for name in LOOKBACK:
        stats[name] = ic_stats(factors[name], fwd, signal_idx, direction=DIRECTION[name])
        stats[name]["direction"] = DIRECTION[name]
    pvals = [stats[n]["p_perm"] if stats[n].get("p_perm") is not None else 1.0 for n in stats]
    adjusted = bh_fdr(pvals)
    for (name, s), q in zip(stats.items(), adjusted):
        s["p_bh"] = q

    # ---- 导出 -----------------------------------------------------------
    holdout: dict[str, dict] = {}
    for name, run in results.items():
        export_results(
            equity_curve=run.equity,
            trade_history=run.trades,
            prefix=f"ashare_{name}",
            initial_cash=INITIAL_CASH,
            start=cal[eval_start].strftime("%Y-%m-%d"),
            end=cal[eval_end].strftime("%Y-%m-%d"),
            market="china_a",
            strategy_name=f"A股横截面·{ '20日反转' if name == 'rev20' else '120日动量' }",
            symbol="中证800成分股",
            is_flat_at_end=True,
        )
        holdout[name] = summarize_holdout(run.equity, holdout_start)

    holdout["ew_bench"] = summarize_holdout(ew_equity, holdout_start)
    holdout["csi300"] = summarize_holdout(idx_equity, holdout_start) if idx_equity else {}

    # ---- 基准曲线落盘（给仪表盘画对比线） --------------------------------
    bench = pd.DataFrame({"date": [p["date"] for p in ew_equity],
                          "equal_weight": [p["value"] for p in ew_equity]})
    if idx_equity:
        idx = pd.DataFrame({"date": [p["date"] for p in idx_equity],
                            "csi300": [p["value"] for p in idx_equity]})
        bench = bench.merge(idx, on="date", how="outer").sort_values("date")
    # 外连接会在「某一边还没开始 / 已经结束」的日期留下空值。这些空值会被写进
    # 仪表盘的 report JSON，json.dumps 输出裸 NaN，前端 JSON.parse 直接抛异常，
    # 整页白屏（已踩过）。这里前向填充，两端再补，保证没有空值。
    bench = bench.ffill().bfill()
    bench.to_csv(ROOT / "outputs" / "ashare_xsec_benchmarks.csv", index=False, float_format="%.2f")

    # ---- 附加统计 -------------------------------------------------------
    extra = {
        "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
        "window": {"start": cal[eval_start].strftime("%Y-%m-%d"),
                   "end": cal[eval_end].strftime("%Y-%m-%d"),
                   "holdout_start": holdout_start,
                   "bars": int(eval_end - eval_start + 1)},
        "universe": {"fetched": panel.fetched, "requested": panel.requested,
                     "pool": "中证800（沪深300+中证500）当前成分股",
                     "survivorship_bias": True},
        "config": {
            "rebalance_bars": REBALANCE_BARS, "quantile": QUANTILE,
            "initial_cash": INITIAL_CASH, "round_trip_cost_bp": round((BUY_COST + SELL_COST) * 1e4, 2),
            "min_cross_section": MIN_CROSS_SECTION,
        },
        "ic": stats,
        "holdout": holdout,
        "yearly": {name: yearly_returns(run.equity) for name, run in results.items()},
        "yearly_ew_bench": ew_year,
        "yearly_csi300": idx_year,
        "frictions": {name: {
            "skipped_limit_up": run.skipped_limit_up,
            "blocked_limit_down": run.blocked_limit_down,
            "blocked_suspend": run.blocked_suspend,
            "skipped_lot_or_price": run.skipped_price,
        } for name, run in results.items()},
    }
    (ROOT / "outputs" / "ashare_xsec_factor_stats.json").write_text(
        json.dumps(extra, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 图表 -----------------------------------------------------------
    curves = {"plot": [
        ("20日反转", results["rev20"].equity, {"color": "#e45756", "linewidth": 1.6}),
        ("120日动量", results["mom120"].equity, {"color": "#4c78a8", "linewidth": 1.3, "alpha": 0.9}),
        ("等权全池基准", ew_equity, {"color": "#54a24b", "linewidth": 1.3, "linestyle": "--"}),
        ("沪深300", idx_equity, {"color": "#9e9e9e", "linewidth": 1.1, "linestyle": ":"}),
    ]}
    yearly = {
        "20日反转": extra["yearly"]["rev20"],
        "120日动量": extra["yearly"]["mom120"],
        "等权全池": ew_year,
        "沪深300": idx_year,
    }
    charts = make_charts(
        curves,
        {("20日反转" if k == "rev20" else "120日动量"): v.trades for k, v in results.items()},
        yearly,
        stats,
    )
    print(f"[charts] {len(charts)} 张，耗时 {time.time() - t0:.0f}s")

    # ---- 控制台小结 -----------------------------------------------------
    print("\n================ 结果 ================")
    for name, run in results.items():
        s = json.loads((ROOT / "outputs" / f"ashare_{name}_summary.json").read_text(encoding="utf-8"))["summary"]
        print(f"{name:8s} 总收益 {s['total_return_pct']:8.2f}%  "
              f"年化 {s['annual_return_pct']:7.2f}%  "
              f"MDD {s['max_drawdown_pct']:6.2f}%  "
              f"Sharpe {s['sharpe']:5.2f}  "
              f"胜率 {s['win_rate_pct']:5.1f}%  笔数 {s['total_trades']}")
        ic = stats[name]
        if ic.get("mean_ic") is not None:
            print(f"         IC {ic['mean_ic']:+.4f} (t={ic['t_stat']:.2f}, "
                  f"p_perm={ic['p_perm']:.3f}, p_bh={ic['p_bh']:.3f}, n={ic['n']})")
        print(f"         封存段 {holdout[name]['return_pct']:+.2f}%")
    print(f"等权全池基准 总收益 "
          f"{(ew_equity[-1]['value'] / ew_equity[0]['value'] - 1) * 100:8.2f}%  "
          f"封存段 {holdout['ew_bench']['return_pct']:+.2f}%")
    if idx_equity:
        print(f"沪深300   总收益 "
              f"{(idx_equity[-1]['value'] / idx_equity[0]['value'] - 1) * 100:8.2f}%  "
              f"封存段 {holdout['csi300']['return_pct']:+.2f}%")
    print("\n" + DISCLAIMER)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
