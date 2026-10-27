"""
QuantOS 2.0 — 收益序列指标（B12 / T-1201）
=========================================

把一条 NAV 曲线变成一堆数字。**纯函数，无状态，不读时钟，不读行情。**

为什么单独一层
--------------
账本（`ledger.py`）给的是"此刻的归因"：交易盈亏、滑点、费用、分红各是多少。
它不给"年化多少、回撤多深"—— 因为那需要**时间序列**，而账本是点值。

把这件事拆出来还有第二个理由：**指标口径必须能被单独测**。
回测和实盘日报如果用两份代码算 Sharpe，两边的数字永远对不上，
而没人会去查是谁把 `sqrt(252)` 写成了 `sqrt(365)`。

四条口径声明（写死在这里，不留给调用方"顺手调一下"）
------------------------------------------------------
1. **年化用日历天数**（365.25），不是"交易日数 / 252"。
   后者在跨越不同年份时会把长假算成收益日，7 年以上误差可以到 0.3pp。
2. **波动与夏普用交易日年化**（252），因为它们来自日收益的统计量。
   收益用日历、风险用交易日，看起来不一致，但这是各自正确的口径：
   收益是累计的事实，波动是采样的密度。
3. **夏普的无风险利率默认 0，且必须显式传**。不传就是 0 是一个危险默认，
   所以 `sharpe` 字段旁边永远带着 `rf_annual`，让读的人知道这是几。
4. **样本不足返回 None，绝不返回 0**。0 会被读成"没有波动"，
   而真相是"算不出来"。这条与全项目的 fail-closed 一致。

回撤怎么算
----------
用 NAV 的历史峰值（running max），不是"期初"。期初口径会把
"先赚 50% 再跌回原点"记成 0 回撤 —— 那种曲线是真实存在的。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from quantos_core.contracts import D, q

TRADING_DAYS = 252
CALENDAR_DAYS = Decimal("365.25")
CALENDAR_DAYS_F = 365.25


# --------------------------------------------------------------------------- #
#  NAV 曲线
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class NavPoint:
    """一个估值点。`nav` 是该日**收盘后**的总权益（含现金）。"""

    day: date
    nav: Decimal

    def __post_init__(self) -> None:
        if D(self.nav) <= 0:
            raise ValueError(f"NAV 必须为正，{self.day} 收到 {self.nav}")


def _as_points(series: Sequence[Any]) -> List[NavPoint]:
    """
    接受 `NavPoint` / `(date, nav)` 两种形态。日期必须严格升序且唯一 ——
    重复日期会让"日收益"变成两天的混合，指标静默失真。
    """
    out: List[NavPoint] = []
    for item in series:
        if isinstance(item, NavPoint):
            pt = item
        else:
            day, nav = item
            pt = NavPoint(day=day, nav=D(nav))
        if out and pt.day <= out[-1].day:
            raise ValueError(
                f"NAV 序列日期必须严格升序（{out[-1].day} -> {pt.day}）")
        out.append(pt)
    return out


def daily_returns(series: Sequence[Any]) -> List[float]:
    """日简单收益率。少于 2 个点返回空列表（不是 [0.0]）。"""
    pts = _as_points(series)
    if len(pts) < 2:
        return []
    out: List[float] = []
    for prev, cur in zip(pts, pts[1:]):
        out.append(float(D(cur.nav) / D(prev.nav)) - 1.0)
    return out


# --------------------------------------------------------------------------- #
#  单点统计
# --------------------------------------------------------------------------- #

def _mean(xs: Sequence[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def _std(xs: Sequence[float]) -> Optional[float]:
    """样本标准差（n-1）。n<2 返回 None —— 单点没有"波动"这回事。"""
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return math.sqrt(var)


def max_drawdown(series: Sequence[Any]) -> Optional[Tuple[float, Optional[date], Optional[date]]]:
    """
    返回 `(mdd, 峰值日, 谷底日)`，`mdd` 是负数（-0.261 = -26.1%）。

    用 running max 而不是期初净值：期初口径会把"先翻倍再跌回原点"算成 0。
    """
    pts = _as_points(series)
    if len(pts) < 2:
        return None
    peak = D(pts[0].nav)
    peak_day: date = pts[0].day
    worst = Decimal(0)
    worst_peak: Optional[date] = None
    worst_trough: Optional[date] = None
    for pt in pts[1:]:
        nav = D(pt.nav)
        if nav > peak:
            peak, peak_day = nav, pt.day
            continue
        dd = nav / peak - Decimal(1)
        if dd < worst:
            worst, worst_peak, worst_trough = dd, peak_day, pt.day
    if worst_peak is None:
        # 一路新高：确实没有回撤，但这是"算出来的 0"，不是"算不出来"。
        return 0.0, None, None
    return float(worst), worst_peak, worst_trough


# --------------------------------------------------------------------------- #
#  报告
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class PerformanceStats:
    """
    一条曲线的完整画像。所有字段都可能为 None（样本不足），
    调用方**必须**处理 None，不许 `or 0`。
    """

    start: Optional[date] = None
    end: Optional[date] = None
    bars: int = 0
    years: Optional[float] = None
    initial: Optional[Decimal] = None
    final: Optional[Decimal] = None
    total_return: Optional[float] = None
    annual_return: Optional[float] = None
    annual_vol: Optional[float] = None
    sharpe: Optional[float] = None
    sortino: Optional[float] = None
    calmar: Optional[float] = None
    max_drawdown: Optional[float] = None
    mdd_peak: Optional[date] = None
    mdd_trough: Optional[date] = None
    best_day: Optional[float] = None
    worst_day: Optional[float] = None
    positive_days: int = 0
    negative_days: int = 0
    rf_annual: float = 0.0
    #: 不足以计算的项。让"没算"变成可见的事实，而不是一个 0。
    gaps: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, Any]:
        def n(v, nd=6):
            return None if v is None else round(float(v), nd)
        return {
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "bars": self.bars,
            "years": n(self.years, 3),
            "initial": str(q(self.initial, 2)) if self.initial is not None else None,
            "final": str(q(self.final, 2)) if self.final is not None else None,
            "total_return_pct": None if self.total_return is None else round(self.total_return * 100, 2),
            "annual_return_pct": None if self.annual_return is None else round(self.annual_return * 100, 2),
            "annual_vol_pct": None if self.annual_vol is None else round(self.annual_vol * 100, 2),
            "sharpe": n(self.sharpe, 3),
            "sortino": n(self.sortino, 3),
            "calmar": n(self.calmar, 3),
            "max_drawdown_pct": None if self.max_drawdown is None else round(self.max_drawdown * 100, 2),
            "mdd_peak": self.mdd_peak.isoformat() if self.mdd_peak else None,
            "mdd_trough": self.mdd_trough.isoformat() if self.mdd_trough else None,
            "best_day_pct": None if self.best_day is None else round(self.best_day * 100, 2),
            "worst_day_pct": None if self.worst_day is None else round(self.worst_day * 100, 2),
            "positive_days": self.positive_days,
            "negative_days": self.negative_days,
            "rf_annual": self.rf_annual,
            "gaps": list(self.gaps),
        }


def annualize(total_return: float, years: float) -> Optional[float]:
    """
    几何年化。**不内置"最短区间"判断** —— 那是调用方（`compute_stats`）的事。

    这里只做一件事：把区间收益换算成"每年多少钱"。区间长度由调用方负责保证，
    因为只有调用方知道这条曲线是回测、是实盘、还是某一段压力测试。
    """
    if years <= 0:
        return None
    base = 1.0 + total_return
    if base <= 0:
        return None
    return base ** (1.0 / years) - 1.0


def compute_stats(series: Sequence[Any], *,
                  rf_annual: float = 0.0,
                  min_years_to_annualize: float = 60.0 / 365.25) -> PerformanceStats:
    """
    主入口。`min_years_to_annualize` 默认约两个月：比这短的年化没有意义。
    """
    pts = _as_points(series)
    gaps: List[str] = []

    if not pts:
        return PerformanceStats(gaps=("没有任何 NAV 点",))

    initial, final = D(pts[0].nav), D(pts[-1].nav)
    bars = len(pts)
    days = (pts[-1].day - pts[0].day).days
    years: Optional[float] = (days / CALENDAR_DAYS_F) if days > 0 else None

    rets = daily_returns(pts)
    stats = {
        "start": pts[0].day, "end": pts[-1].day, "bars": bars,
        "initial": initial, "final": final, "rf_annual": float(rf_annual),
    }

    if bars < 2:
        gaps.append("只有 1 个点：收益率/波动/回撤都算不出来")
        return PerformanceStats(**stats, gaps=tuple(gaps))

    total_return = float(final / initial) - 1.0
    stats["total_return"] = total_return

    if years is None or years < min_years_to_annualize:
        gaps.append(f"区间 {days} 天太短，不做年化")
        annual = None
    else:
        annual = annualize(total_return, years)
    stats["years"] = years
    stats["annual_return"] = annual

    vol = _std(rets)
    annual_vol = vol * math.sqrt(TRADING_DAYS) if vol is not None else None
    stats["annual_vol"] = annual_vol

    mean_ret = _mean(rets)
    if mean_ret is None or annual is None:
        gaps.append("收益样本不足，夏普/索提诺算不出来")
        sharpe = None
    elif not annual_vol or annual_vol == 0.0:
        gaps.append("波动为 0，夏普无定义（不是无穷大）")
        sharpe = None
    else:
        sharpe = (mean_ret * TRADING_DAYS - float(rf_annual)) / annual_vol
    stats["sharpe"] = sharpe

    downs = [r for r in rets if r < 0]
    if not downs or annual is None:
        if annual is not None:
            gaps.append("没有下跌日，索提诺算不出来")
        sortino = None
    else:
        # 下行偏差取**全部样本**的负半方差（分母是 n，不是下跌日数）。
        # 用"下跌日收益的标准差"是常见误写：它把下跌日的均值当成基准、
        # 再除以下跌日数，结果既不是半方差也不是波动率，会让索提诺比夏普还小。
        semi = math.sqrt(sum(min(r, 0.0) ** 2 for r in rets) / len(rets))
        sortino = ((mean_ret * TRADING_DAYS - float(rf_annual)) / (semi * math.sqrt(TRADING_DAYS))
                   if semi > 0 else None)
        if sortino is None:
            gaps.append("下行偏差为 0，索提诺无定义")
    stats["sortino"] = sortino

    mdd = max_drawdown(pts)
    if mdd is not None:
        stats["max_drawdown"], stats["mdd_peak"], stats["mdd_trough"] = mdd
        if annual is not None and mdd[0] < 0:
            stats["calmar"] = annual / abs(mdd[0])
        elif annual is not None:
            gaps.append("没有回撤，Calmar 无定义")

    stats["best_day"] = max(rets)
    stats["worst_day"] = min(rets)
    stats["positive_days"] = sum(1 for r in rets if r > 0)
    stats["negative_days"] = sum(1 for r in rets if r < 0)

    return PerformanceStats(**stats, gaps=tuple(gaps))


def yearly_returns(series: Sequence[Any]) -> List[Dict[str, Any]]:
    """
    自然年收益。跨年那段用"上年最后一个 NAV 到本年最后一个 NAV"，
    所以每年首个交易日的大幅跳空会正确地落在该年（而不是被丢掉）。

    只包含**两个点以上的年份**：某年只有一个估值点（比如曲线起点落在年尾），
    那一年没有"收益"可言。记成 0% 是在编一个不存在的事实。
    """
    pts = _as_points(series)
    if len(pts) < 2:
        return []

    def _span(first: NavPoint, last: NavPoint) -> Optional[Dict[str, Any]]:
        if first.day == last.day:
            return None
        return {
            "year": last.day.year,
            "start": first.day.isoformat(),
            "end": last.day.isoformat(),
            "return_pct": round(float(D(last.nav) / D(first.nav) - 1) * 100, 2),
        }

    out: List[Dict[str, Any]] = []
    year_start = pts[0]
    prev = pts[0]
    for pt in pts[1:]:
        if pt.day.year != prev.day.year:
            row = _span(year_start, prev)
            if row:
                out.append(row)
            year_start = prev
        prev = pt
    row = _span(year_start, prev)
    if row:
        out.append(row)
    return out


def cost_drag(fees: Decimal, average_nav: Decimal,
              years: Optional[float]) -> Optional[float]:
    """
    成本拖累：年化口径的费用占比。

    分母用**平均 NAV**而不是期末 NAV：期末 NAV 已经含了复利，
    拿它当分母会把早期的高费率摊薄，看起来成本比实际低。
    """
    fees, avg = D(fees), D(average_nav)
    if avg <= 0:
        return None
    if not years or years <= 0:
        return None
    return float(fees / avg) / years


def average_nav(series: Sequence[Any]) -> Optional[Decimal]:
    pts = _as_points(series)
    if not pts:
        return None
    total = sum((D(p.nav) for p in pts), Decimal(0))
    return q(total / Decimal(len(pts)), 2)


def exposure_stats(series: Sequence[Tuple[date, Decimal, Decimal]]
                   ) -> Dict[str, Optional[float]]:
    """
    `series` 是 `(day, gross_exposure, nav)` 三元组。

    返回平均/最大敞口占比。**平均**比最大更能说明"钱是不是一直在场"，
    但最大能抓到"某一天加了 3 倍杠杆"这种事故，两个都要。
    """
    ratios: List[float] = []
    for _day, gross, nav in series:
        n = D(nav)
        if n > 0:
            ratios.append(float(D(gross) / n))
    if not ratios:
        return {"avg_exposure_pct": None, "max_exposure_pct": None,
                "avg_cash_pct": None}
    avg = sum(ratios) / len(ratios)
    return {
        "avg_exposure_pct": round(avg * 100, 2),
        "max_exposure_pct": round(max(ratios) * 100, 2),
        "avg_cash_pct": round((1.0 - avg) * 100, 2),
    }


__all__ = [
    "TRADING_DAYS", "NavPoint", "PerformanceStats", "annualize",
    "compute_stats", "daily_returns", "max_drawdown", "yearly_returns",
    "cost_drag", "average_nav", "exposure_stats",
]
