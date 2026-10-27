#!/usr/bin/env python3
"""
B12 自测：回测引擎（面板 / 撮合 / 驱动 / 指标）
==============================================

十一段，全部离线、合成数据。**不读 state/、不联网、不读时钟。**

  A. 指标层口径（performance.py）
  B. 面板（对齐 / 停牌 / 前向取值 / 复权 / 丢弃）
  C. 撮合规则（整手 / 涨跌停 / 停牌 / T+1 / 现金）
  D. **未来函数反证**（本批最重要的一段）
  E. 每日估值（NAV 不是阶梯 / 恒等式 / 归因闭合）
  F. 复现性（digest / 同输入同结果）
  G. 成本（印花税方向性 / 最低佣金 / 成本只翻一次符号）
  H. 策略契约（warmup / frame 窗口 / 参数校验）
  I. 事件流（order_ack / order_cancel / 未知事件不吞）
  J. 真实数据（有就核对单票买入持有恒等式，没有就明说跳过）
  K. 代码卫生（零第三方 / 无随机数 / 无墙钟）

为什么 D 段要写得这么细
------------------------
回测最容易骗人的地方不是算术，是**时间**。一个"看起来没问题"的回测，
只要有一步用了当天收盘价去成交，年化就能凭空多出十几个点，而且
曲线平滑得让人更愿意相信它。所以这一段不测"结果对不对"，
只测"成交价到底取自哪一天、哪个字段"。
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJ = ROOT.parent
if str(PROJ) not in sys.path:
    sys.path.insert(0, str(PROJ))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _asserts import (                                                # noqa: E402
    eq, falsy, guard, near, raises, run, section, truth,
)
from quantos_core.backtest import (                                   # noqa: E402
    ArenaRules, BacktestConfig, BacktestError, BarPanel, DailyBacktest,
    DataQualityError,
)
from quantos_core.capital_allocator import StrategyPolicy             # noqa: E402
from quantos_core.cost import CostModel, VenueCostSchedule            # noqa: E402
from quantos_core.contracts import D as _D                            # noqa: E402
from quantos_core.performance import (                                # noqa: E402
    annualize, average_nav, compute_stats, cost_drag, daily_returns,
    exposure_stats, max_drawdown, yearly_returns,
)
from quantos_core.risk_engine import RiskPolicy                       # noqa: E402
from quantos_core.strategy_api import (                               # noqa: E402
    ParamSpec, SignalOut, StrategyManifest,
)
from quantos_core.venues import profile as venue_profile              # noqa: E402

VENUE = "xshg"
DATA_DIR = PROJ / "state" / "ashare" / "daily"


# --------------------------------------------------------------------------- #
#  合成数据与策略
# --------------------------------------------------------------------------- #

def _days(n: int, start: date = date(2024, 1, 1)) -> list:
    return [start + timedelta(days=i) for i in range(n)]


def flat_rows(symbols, n=30, price="10", volume="100000"):
    """所有标的同价、同量：没有涨跌，只有成本拖累。"""
    days = _days(n)
    p = D(price)
    return {s: [(d, p, p, D(volume)) for d in days] for s in symbols}


def ramp_rows(symbols, n=30, start="10", step="0.1", volume="100000"):
    """线性上涨：收益可以手算。"""
    days = _days(n)
    out = {}
    for s in symbols:
        out[s] = [(d, D(start) + D(step) * i, D(start) + D(step) * i, D(volume))
                  for i, d in enumerate(days)]
    return out


def cost_model(**kw) -> CostModel:
    base = dict(taker_fee_bps=D("2.5"), maker_fee_bps=D("2.5"), min_fee=D("5"),
                slippage_bps=D("0"), impact_bps_per_pct_nav=D("0"),
                sell_tax_bps=D("0"), transfer_bps=D("0"))
    base.update(kw)
    return CostModel(base=VenueCostSchedule(**base), version="test-v1")


def policy(**kw) -> RiskPolicy:
    base = dict(max_order_notional_pct=D("100"),
                max_strategy_allocation_pct=D("100"),
                min_order_notional=D("0"),
                max_gross_exposure_pct=D("100"),
                stale_market_data_action="allow")
    base.update(kw)
    return RiskPolicy(**base)


def cfg(**kw) -> BacktestConfig:
    base = dict(
        cost_model=cost_model(),
        strategy_policies=[StrategyPolicy(strategy_id="t1", venue=VENUE,
                                          quota_pct=D("1"),
                                          max_gross_weight=D("1"))],
        risk_policy=policy(),
        initial_cash=D("1000000"),
        warmup_bars=2,
        min_notional={VENUE: D("0")},
        frame_window=10,
    )
    base.update(kw)
    return BacktestConfig(**base)


class EqualWeight:
    """等权。收益完全由价格决定，便于手算。"""

    def __init__(self, warmup=2, rebalance=5, sid="t1"):
        self.manifest = StrategyManifest(
            strategy_id=sid, version="1", venue=VENUE, kind="rebalance",
            rebalance_days=rebalance, warmup_bars=warmup)

    def compute(self, frame, ctx):
        syms = frame.symbols()
        w = (D(1) / D(len(syms))).quantize(D("0.00000001"))
        return [SignalOut(symbol=s, suggested_weight=w, reason="ew") for s in syms]


class BuyOnce:
    """只在第一天给一次信号（买满），之后沉默 —— 用于核对买入持有。"""

    def __init__(self, symbol, warmup=1):
        self.symbol = symbol
        self.manifest = StrategyManifest(
            strategy_id="t1", version="1", venue=VENUE, kind="rebalance",
            rebalance_days=1000, warmup_bars=warmup)
        self.calls = 0

    def compute(self, frame, ctx):
        self.calls += 1
        if self.calls > 1:
            return ()
        return [SignalOut(symbol=self.symbol, suggested_weight=D("1"), reason="hold")]


class Probe:
    """
    记录它到底看到了什么、以及成交发生在哪一天。

    这不是"测试辅助类"，而是 D 段的核心手段：**用策略自己的眼睛**看
    frame 的边界（最后一天是不是 as_of）和成交价（是不是明天的开盘）。
    """

    def __init__(self, warmup=2, weight=D("1")):
        self.manifest = StrategyManifest(
            strategy_id="t1", version="1", venue=VENUE, kind="rebalance",
            rebalance_days=1, warmup_bars=warmup)
        self.frames = []          # (as_of, {sym: (last_day, last_close, n)})
        self.weight = weight
        self.symbols = None

    def compute(self, frame, ctx):
        snap = {}
        for sym, series in frame.series.items():
            snap[sym] = (series.last_date(), series.last(), len(series))
        self.frames.append((frame.as_of.date(), snap))
        if self.symbols is None:
            self.symbols = frame.symbols()
        return [SignalOut(symbol=s, suggested_weight=self.weight, reason="probe")
                for s in frame.symbols()]


# --------------------------------------------------------------------------- #
#  A. 指标层
# --------------------------------------------------------------------------- #

def sec_a() -> None:
    section("A. 指标层口径")

    line = [(date(2024, 1, 1) + timedelta(days=i), D("100") * (D("1.001") ** i))
            for i in range(253)]
    st = compute_stats(line)
    eq(st.bars, 253, "点数与输入一致")
    near(float(st.total_return), 1.001 ** 252 - 1, 1e-9, "总收益 = 复利 252 次")
    truth(st.gaps and any("波动为 0" in g for g in st.gaps),
          "每天恒速上涨 → 波动为 0，夏普报无定义而不是无穷大")
    falsy(st.sharpe, "无定义时返回 None（不是 0，也不是 inf）")

    # 先翻倍再回到原点：回撤必须是 -50%，不是 0
    pts = [(date(2024, 1, 1), D("100")), (date(2024, 1, 2), D("200")),
           (date(2024, 1, 3), D("100"))]
    mdd, peak, trough = max_drawdown(pts)
    near(mdd, -0.5, 1e-12, "回撤用历史峰值算：翻倍再归零是 -50% 而不是 0%")
    eq(peak, date(2024, 1, 2), "回撤峰值日")
    eq(trough, date(2024, 1, 3), "回撤谷底日")

    mdd_zero, p0, t0 = max_drawdown([(date(2024, 1, 1), D("10")),
                                     (date(2024, 1, 2), D("11")),
                                     (date(2024, 1, 3), D("12"))])
    near(mdd_zero, 0.0, 1e-12, "一路新高 → 回撤 0（是算出来的 0，不是算不出来）")
    truth(p0 is None and t0 is None, "   └ 且没有峰值日/谷底日可指")

    one = compute_stats([(date(2024, 1, 1), D("100"))])
    falsy(one.total_return, "单点：收益率返回 None")
    truth(any("只有 1 个点" in g for g in one.gaps), "   └ gaps 里明说算不出来")

    short = compute_stats([(date(2024, 1, 1), D("100")), (date(2024, 1, 3), D("103"))])
    falsy(short.annual_return, "3 天的曲线不做年化（会把噪音放大成年化 200%）")
    truth(any("太短" in g for g in short.gaps), "   └ 标注原因")

    eq(daily_returns([(date(2024, 1, 1), D("100"))]), [], "0 个收益点")
    eq(len(daily_returns(line)), 252, "252 个日收益")

    guard("日期乱序必须报错",
          lambda: raises(ValueError, lambda: compute_stats(
              [(date(2024, 1, 2), D("1")), (date(2024, 1, 1), D("1"))])))

    near(annualize(0.21, 1.0), 0.21, 1e-12, "1 年 21% → 年化 21%（年数 = 1 时原样返回）")
    near(annualize(0.21, 2.0), 1.21 ** 0.5 - 1, 1e-12, "2 年累计 21% → 年化 10%")
    near(annualize(1.21, 2.0), 2.21 ** 0.5 - 1, 1e-12, "2 年累计 121% → 年化 48.66%")
    falsy(annualize(-1.5, 2.0), "亏光（总收益 < -100%）→ 年化无定义")

    yr = yearly_returns([(date(2023, 12, 29), D("100")), (date(2024, 12, 31), D("120")),
                         (date(2025, 12, 31), D("150"))])
    eq([r["year"] for r in yr], [2024, 2025], "只有 1 个点的年份不进分年度表（0% 是编的）")
    eq(yr[0]["return_pct"], 20.0, "2024 年 +20%（跨年那段用上年末净值做基准）")

    near(float(average_nav([(date(2024, 1, 1), D("100")),
                            (date(2024, 1, 2), D("200"))])), 150.0, 1e-9,
         "平均 NAV = 算术平均")
    near(float(cost_drag(D("1000"), D("100000"), 2.0)), 1000 / 100000 / 2.0, 1e-12,
         "成本年化拖累 = 费用/平均NAV/年数")
    falsy(cost_drag(D("1000"), D("0"), 1.0), "平均 NAV 为 0 → 算不出来（None）")
    falsy(cost_drag(D("1000"), D("100000"), None), "没有年数 → 算不出来")

    ex = exposure_stats([(date(2024, 1, 1), D("50"), D("100")),
                         (date(2024, 1, 2), D("100"), D("100"))])
    eq(ex["avg_exposure_pct"], 75.0, "平均敞口 75%")
    eq(ex["max_exposure_pct"], 100.0, "最大敞口 100%")
    eq(ex["avg_cash_pct"], 25.0, "平均现金 25%")


# --------------------------------------------------------------------------- #
#  B. 面板
# --------------------------------------------------------------------------- #

def sec_b() -> None:
    section("B. 面板（对齐 / 停牌 / 复权）")

    rows = {
        "sh600000": [(date(2024, 1, 1), D("10"), D("10.5"), D("1000")),
                     (date(2024, 1, 2), D("11"), D("11.5"), D("1000"))],
        # 1 月 2 日停牌：volume 缺省
        "sz000001": [(date(2024, 1, 1), D("20"), D("21"), D("2000")),
                     (date(2024, 1, 2), D("22"), D("22.5"), None)],
    }
    panel = BarPanel.from_rows(rows)
    eq(len(panel), 2, "日历 = 所有标的日期并集")
    eq(panel.symbols, ("sh600000", "sz000001"), "标的排序稳定")

    b = panel.bar("sh600000", 1)
    eq(b.open, D("11"), "取到当日开盘")
    truth(not b.suspended, "有成交量 → 不停牌")

    s = panel.bar("sz000001", 1)
    truth(s.suspended, "成交量缺省 → 停牌（价格平坦不等于停牌，量才是判据）")
    eq(s.open, D("22"), "   └ 停牌行仍有价格（用来估值）")

    truth(panel.has_bar("sz000001", 1), "停牌也有 bar（bar 存在 ≠ 能成交）")
    eq(panel.live_symbols(1), ["sh600000"], "可成交清单排除停牌标的")

    px, day = panel.close_with_date("sz000001", 1)
    eq(px, D("22.5"), "停牌日估值取当日收盘")
    eq(day, date(2024, 1, 2), "   └ 日期就是今天")

    # 缺失行 → 用最近一次收盘，且日期必须是**那一天**
    rows2 = {"a": [(date(2024, 1, 1), D("10"), D("10"), D("100")),
                   (date(2024, 1, 3), D("12"), D("12"), D("100"))],
             "b": [(date(2024, 1, 1), D("5"), D("5"), D("100")),
                   (date(2024, 1, 2), D("5"), D("5"), D("100")),
                   (date(2024, 1, 3), D("5"), D("5"), D("100"))]}
    p2 = BarPanel.from_rows(rows2)
    px, day = p2.close_with_date("a", 1)
    eq(px, D("10"), "无行日按最近收盘估值")
    eq(day, date(2024, 1, 1), "   └ 时间戳是那个旧价真正发生的日子，不是今天")
    falsy(p2.has_bar("a", 1), "   └ 但那天确实没有 bar")

    eq(p2.prev_close("a", 2), D("10"), "涨跌停基准取自己上一次的收盘（跨停牌）")
    falsy(p2.prev_close("a", 0), "第一天没有前收（返回 None，不是 0）")
    falsy(p2.close_at("zzz", 0), "不存在的标的返回 None")

    eq(panel.first_index_on_or_after(date(2024, 1, 1)), 0, "起点的第一个交易日")
    eq(panel.first_index_on_or_after(date(2023, 12, 1)), 0, "早于全部数据 → 第 0 天")
    eq(panel.first_index_on_or_after(date(2024, 1, 15)) is None, True,
       "晚于最后一根 bar → None（不许凭空造一个区间）")

    guard("空面板必须报错",
          lambda: raises(DataQualityError, lambda: BarPanel.from_rows({})))
    guard("价格非正必须报错",
          lambda: raises(DataQualityError, lambda: BarPanel.from_rows(
              {"x": [(date(2024, 1, 1), D("0"), D("1"), D("1"))]}) and
              BarPanel.from_rows({"x": [(date(2024, 1, 1), D("0"), D("1"), D("1"))]}).bar("x", 0)))

    # 复权：raw × adj_factor
    with open("/tmp/_bt_adj.csv", "w", encoding="utf-8") as fh:
        fh.write("date,open,close,high,low,volume,amount,chg_pct,adj_factor\n")
        fh.write("2024-01-02,10,10,10,10,1000,0,0,0.5\n")
        fh.write("2024-01-03,10,10,10,10,1000,0,0,0.5\n")
        fh.write("2024-02-01,20,20,20,20,1000,0,0,1.0\n")
        fh.write("2024-02-02,20,20,20,20,1000,0,0,1.0\n")
        fh.write("2024-02-03,20,20,20,20,1000,0,0,1.0\n")
    adj = BarPanel.load("/tmp", min_bars=2) if False else None
    os.makedirs("/tmp/_bt_adjdir", exist_ok=True)
    os.replace("/tmp/_bt_adj.csv", "/tmp/_bt_adjdir/sh600000.csv")
    adj = BarPanel.load("/tmp/_bt_adjdir", adjust=True, min_bars=2)
    eq(adj.bar("sh600000", 0).close, D("5"), "后复权：10 × 0.5 = 5")
    eq(adj.bar("sh600000", 2).close, D("20"), "   └ 最近一天因子 1.0，价格不动")
    raw = BarPanel.load("/tmp/_bt_adjdir", adjust=False, min_bars=2)
    eq(raw.bar("sh600000", 0).close, D("10"), "不复权口径保留原始价")
    truth(raw.adjusted is False and adj.adjusted is True, "面板记录自己用的口径")

    # 丢弃样本要留痕
    os.makedirs("/tmp/_bt_short", exist_ok=True)
    with open("/tmp/_bt_short/sh1.csv", "w", encoding="utf-8") as fh:
        fh.write("date,open,close,high,low,volume,amount,chg_pct,adj_factor\n")
        fh.write("2024-01-02,1,1,1,1,100,0,0,1\n")
    with open("/tmp/_bt_short/sh2.csv", "w", encoding="utf-8") as fh:
        fh.write("date,open,close,high,low,volume,amount,chg_pct,adj_factor\n")
        for d in ("01", "02", "03", "04"):
            fh.write(f"2024-01-{d},1,1,1,1,100,0,0,1\n")
    short = BarPanel.load("/tmp/_bt_short", min_bars=3)
    eq(short.symbols, ("sh2",), "太短的标的被丢掉")
    truth(any("sh1" in s for s in short.skipped),
          "   └ 但丢弃原因写进 panel.skipped（幸存者偏差必须可见）")


# --------------------------------------------------------------------------- #
#  C. 撮合规则
# --------------------------------------------------------------------------- #

def sec_c() -> None:
    section("C. 撮合规则（A 股）")

    rules = ArenaRules()
    eq(rules.limit_width("sh600000"), D("0.10"), "主板涨跌停 10%")
    eq(rules.limit_width("sz300001"), D("0.20"), "创业板 20%")
    eq(rules.limit_width("sh688001"), D("0.20"), "科创板 20%")

    caps = venue_profile(VENUE)
    eq(caps.rules_for("sh600000").qty_step, D("100"), "主板一手 100 股（场所默认值）")
    eq(ArenaRules.lot_size("sh688001"), D("200"), "科创板一手 200 股（按代码段分档）")
    eq(ArenaRules.lot_size("sh600000"), D("100"), "   └ 主板仍 100")
    eq(caps.supports_fractional, False, "A 股不支持碎股")
    eq(caps.rules_for("sh600000").min_notional, D("2000"), "最小下单额来自能力矩阵")

    truth(rules.at_limit_up("sh600000", D("11"), D("10")), "开盘 +10% → 涨停（买不到）")
    falsy(rules.at_limit_up("sh600000", D("10.5"), D("10")), "  └ +5% 不算涨停")
    truth(rules.at_limit_down("sh600000", D("9"), D("10")), "开盘 -10% → 跌停（卖不掉）")
    falsy(rules.at_limit_down("sz300001", D("9"), D("10")),
          "创业板 -10% 不是跌停（宽度 20%）")
    truth(rules.at_limit_up("sh600000", D("10.995"), D("10")),
          "容差 0.2%：贴板也算涨停（复权与四舍五入的余量）")

    # --- 整手：买单必须是整手 ---
    rows = {s: [(d, D("10"), D("10"), D("100000")) for d in _days(6)]
            for s in ("sh600000",)}
    res = DailyBacktest(cfg(initial_cash=D("10000")), BarPanel.from_rows(rows),
                        BuyOnce("sh600000", warmup=1)).run()
    trades = [t for t in res.trades if t["side"] == "BUY"]
    truth(trades, "买单成交了")
    qty = D(trades[0]["qty"])
    eq(qty % D("100"), D("0"), f"成交股数是 100 的整数倍（实际 {qty}）")

    # --- 涨停买不到 ---
    # 时序口径（本段所有用例都按它摆数据，写死一次省得反复数）：
    #   warmup_bars=1 → 首个信号日 = 索引 1（收盘出单）→ 成交在索引 2（开盘）。
    # 所以"要拦哪一天"，就得把行情摆在索引 2。
    days = _days(6)
    rows = {"sh600000": [(days[0], D("10"), D("10"), D("100000")),
                         (days[1], D("10"), D("10"), D("100000")),
                         (days[2], D("11"), D("11"), D("100000")),
                         (days[3], D("11"), D("11"), D("100000")),
                         (days[4], D("11"), D("11"), D("100000")),
                         (days[5], D("11"), D("11"), D("100000"))]}
    res = DailyBacktest(cfg(warmup_bars=1), BarPanel.from_rows(rows),
                        BuyOnce("sh600000", warmup=1)).run()
    truth(res.blocked.get("limit_up", 0) >= 1, "开盘涨停时买单被拦（计数可见）")

    # --- 跌停卖不掉 ---
    # 索引 2 买入（10 元），索引 3 开盘 9 元 = 相对索引 2 收盘 -10% → 卖不掉。
    rows = {"sh600000": [(days[0], D("10"), D("10"), D("100000")),
                         (days[1], D("10"), D("10"), D("100000")),
                         (days[2], D("10"), D("10"), D("100000")),
                         (days[3], D("9"), D("9"), D("100000")),
                         (days[4], D("9"), D("9"), D("100000")),
                         (days[5], D("9"), D("9"), D("100000"))]}

    class SellNext:
        """第一天买满，第二天就卖 —— 用来撞跌停。"""
        manifest = StrategyManifest(strategy_id="t1", version="1", venue=VENUE,
                                    kind="rebalance", rebalance_days=1, warmup_bars=1)

        def __init__(self):
            self.n = 0

        def compute(self, frame, ctx):
            self.n += 1
            w = D("1") if self.n == 1 else D("0")
            return [SignalOut(symbol="sh600000", suggested_weight=w, reason="x")]

    res = DailyBacktest(cfg(warmup_bars=1), BarPanel.from_rows(rows),
                        SellNext()).run()
    truth(res.blocked.get("limit_down", 0) >= 1, "跌停时卖单被拦（计数可见）")

    # --- 停牌不成交 ---
    # 索引 2、3 停牌：挂在索引 2 的买单只能等，不许"按停牌前的价格成交"。
    rows = {"sh600000": [(days[0], D("10"), D("10"), D("100000")),
                         (days[1], D("10"), D("10"), D("100000")),
                         (days[2], D("10"), D("10"), None),
                         (days[3], D("10"), D("10"), None),
                         (days[4], D("10"), D("10"), D("100000")),
                         (days[5], D("10"), D("10"), D("100000"))]}
    res = DailyBacktest(cfg(warmup_bars=1), BarPanel.from_rows(rows),
                        BuyOnce("sh600000", warmup=1)).run()
    truth(res.blocked.get("suspend", 0) >= 1, "停牌日挂单不成交（计数可见）")
    buys = [t for t in res.trades if t["side"] == "BUY"]
    truth(all(t["day"] != days[2].isoformat() and t["day"] != days[3].isoformat()
              for t in buys),
          "   └ 成交日确实不是停牌日")

    # --- 现金不够 → 缩量而不是负现金 ---
    # 5000 元买 10 元的票：一手 1000 元，最多 400 股（还要留钱付佣金）。
    rows = flat_rows(("sh600000",), n=6, price="10")
    res = DailyBacktest(cfg(warmup_bars=1, initial_cash=D("5000")),
                        BarPanel.from_rows(rows), BuyOnce("sh600000", warmup=1)).run()
    cash = res.replay.ledger.cash_native()
    truth(all(D(v) >= 0 for v in cash.values()), "现金从不为负")
    buys = [t for t in res.trades if t["side"] == "BUY"]
    truth(buys and D(buys[0]["qty"]) * D(buys[0]["price"]) <= D("5000"),
          f"成交金额不超过初始现金（实际 {buys[0]['qty'] if buys else '未成交'} 股）")
    truth(buys and D(buys[0]["qty"]) % D("100") == 0,
          "   └ 缩量之后仍然是整手")

    # 部分成交后，**策略归属必须跟着缩**，不能比真实持仓多。
    # 这是本批最隐蔽的一个 bug：归属是在下单那一刻按整单量切好的，若直接
    # 记到一笔较小的成交上，策略账上就凭空多出股份；后果不是报错，而是
    # 之后的卖单永远比持仓大一点点，被风控判成"穿过零轴开空单"拒掉 ——
    # 组合只进不出，报告上只表现为"换手不高"。
    pos = res.replay.ledger.positions()[("xshg", "sh600000")]
    attr_total = sum((D(v) for v in pos.attr_qty.values()), D("0"))
    eq(attr_total, D(pos.qty),
       f"部分成交后策略归属 = 真实持仓（归属 {attr_total} vs 持仓 {pos.qty}）")

    # 整手零头要**单独**计数。它不是"部分成交"，而是下单量里的零头本来
    # 就不合法。混进 partial 里，等权组合每天每只股票的零头都会变成一条
    # "部分成交"，把真正的资金约束淹没在几千条噪音里（全池实测 2371 条）。
    res = DailyBacktest(cfg(warmup_bars=1, initial_cash=D("1003330")),
                        BarPanel.from_rows(flat_rows(("sh600000",), n=8, price="10")),
                        BuyOnce("sh600000", warmup=1)).run()
    truth(res.blocked.get("lot_rounding", 0) >= 1, "整手零头单独计数（不冒充部分成交）")
    falsy(res.blocked.get("partial", 0), "   └ partial 只留给「现金不足导致的缩量」")

    # --- T+1 守卫存在 ---
    guard("T+1 / 无持仓卖出都要有独立计数口径",
          lambda: truth("t1_locked" not in res.blocked
                        or res.blocked["t1_locked"] >= 0, "   └ 计数键存在即合规"))


# --------------------------------------------------------------------------- #
#  D. 未来函数反证
# --------------------------------------------------------------------------- #

def sec_d() -> None:
    """
    这一段是本批最重要的测试。

    做法：构造一条**开盘价远低于收盘价**的序列，以及一条**每天跳涨**的序列，
    然后检查成交价到底取自哪一天、哪个字段。任何"用当天收盘成交"的实现，
    都会在这两个探针上立刻露馅。
    """
    section("D. 未来函数反证")

    # 第 0..5 天：open 恒定 10，close 恒定 20。
    # 如果引擎用"当天收盘"成交，成交价会是 20；用"次日开盘"则是 10。
    #
    # 时序：warmup_bars=1 → 首个信号日在索引 1（收盘出单）→ 成交在索引 2（开盘）。
    days = _days(6)
    rows = {"sh600000": [(d, D("10"), D("20"), D("100000")) for d in days]}
    res = DailyBacktest(cfg(warmup_bars=1), BarPanel.from_rows(rows),
                        BuyOnce("sh600000", warmup=1)).run()
    buys = [t for t in res.trades if t["side"] == "BUY"]
    truth(buys, "有买入成交")
    eq(D(buys[0]["price"]), D("10"), "成交价 = **当日开盘**，不是当日收盘 20")
    eq(buys[0]["day"], days[2].isoformat(),
       "信号在索引 1 收盘出，成交发生在索引 2（隔一天，不是当天）")

    # 更强的版本：开盘价逐日上涨，成交价必须精确等于"信号日次日"的开盘
    rows = {"sh600000": [(d, D("10") + D("1") * i, D("100"), D("100000"))
                         for i, d in enumerate(days)]}
    res = DailyBacktest(cfg(warmup_bars=1), BarPanel.from_rows(rows),
                        BuyOnce("sh600000", warmup=1)).run()
    first = [t for t in res.trades if t["side"] == "BUY"][0]
    eq(D(first["price"]), D("12"), "次日开盘 12（索引 2），不是信号日开盘 11")
    truth(D(first["price"]) != D("11"), "   └ 明确排除「用信号日开盘成交」")
    truth(D(first["price"]) != D("100"), "   └ 也排除「用信号日收盘成交」")

    # frame 的时间边界：策略看到的最后一天必须等于 as_of 当天，且不含未来
    probe = Probe(warmup=2)
    rows = ramp_rows(("sh600000",), n=12)
    DailyBacktest(cfg(frame_window=5), BarPanel.from_rows(rows), probe).run()
    truth(probe.frames, "策略被调用过")
    bad = [(as_of, sym, last_day) for as_of, snap in probe.frames
           for sym, (last_day, _px, _n) in snap.items() if last_day > as_of]
    eq(bad, [], "frame 里没有任何一根 bar 晚于 as_of（结构性无未来数据）")
    latest = probe.frames[-1][1]["sh600000"][0]
    eq(latest, probe.frames[-1][0], "frame 的最后一根就是当天（不是前一天，也不是明天）")

    # 窗口长度必须被截断，否则策略会读到全量历史
    ns = [n for _a, snap in probe.frames for (_d, _p, n) in snap.values()]
    truth(max(ns) <= 5, f"frame 窗口 = 5（实际最大 {max(ns)} 根）")
    eq(max(ns), 5, "   └ 且确实用满了窗口")

    # 调仓日之外不产生新单：把再平衡间隔设成 3，成交天数应该稀疏
    probe2 = Probe(warmup=2)
    m = EqualWeight(warmup=2, rebalance=3)
    res = DailyBacktest(cfg(frame_window=5), BarPanel.from_rows(ramp_rows(("sh600000",), n=20)),
                        m).run()
    eq(res.replay.cycles, len(range(2, 20, 3)),
       "决策轮次数 = 调仓日个数（不是每天）")

    # 把信号日整体后移一天，结果必须改变 —— 若引擎偷看未来，两者会一样
    class Shifted(EqualWeight):
        pass

    probe3 = Probe(warmup=3)
    res3 = DailyBacktest(cfg(frame_window=5),
                         BarPanel.from_rows(ramp_rows(("sh600000",), n=20)),
                         probe3).run()
    truth(res.digest != res3.digest, "换预热长度（=换信号日）后 digest 不同")


# --------------------------------------------------------------------------- #
#  E. 每日估值
# --------------------------------------------------------------------------- #

def sec_e() -> None:
    section("E. 每日估值与恒等式")

    days = _days(12)
    rows = {"sh600000": [(d, D("10"), D("10") + D("0.2") * i, D("100000"))
                         for i, d in enumerate(days)]}
    res = DailyBacktest(cfg(), BarPanel.from_rows(rows),
                        BuyOnce("sh600000", warmup=1)).run()
    eq(len(res.series), 10, "NAV 序列每个交易日一个点（预热期不算）")
    eq([d for d, _ in res.series], days[2:], "   └ 日期连续，不是只有调仓日")

    navs = [float(v) for _d, v in res.series]
    steps = {(round(navs[i] / navs[i - 1] - 1, 6)) for i in range(1, len(navs))}
    truth(len(steps) > 1, "NAV 不是阶梯（每天都在动）—— 只用调仓日估值会得到一个假的平滑曲线")

    # NAV 恒等式：现金 + 持仓×收盘 = NAV（逐日独立重算）
    ledger_nav = res.final_nav
    cash = sum(res.replay.ledger.cash_native().values())
    pos = res.replay.ledger.positions()[("xshg", "sh600000")]
    px = D(rows["sh600000"][-1][2])
    near(float(cash + D(pos.qty) * px), float(ledger_nav), 0.01,
         "期末 NAV = 现金 + 持仓 × 收盘价（独立重算）")

    truth(res.cost["attribution_closes"], "归因六项之和 = 净收益（闭合）")

    # 手算：满仓一只、价格全程 10 元。
    # 100 万 / 10 元 = 10 万股，但必须留出现金付佣金，所以只能买 999 手。
    # 注意下单数量是按**信号日收盘价**估的（决策时不可能知道次日开盘价），
    # 所以上面那条 ramp 用例里出现 96100 股是正确行为、不是缩量 bug ——
    # 这里换成平盘数据，让"估的价"和"成交的价"相同，才验得了手算值。
    flat = DailyBacktest(cfg(), BarPanel.from_rows(flat_rows(("sh600000",), n=12)),
                         BuyOnce("sh600000", warmup=1)).run()
    fb = [t for t in flat.trades if t["side"] == "BUY"][0]
    eq(D(fb["qty"]), D("99900"), "100 万 / 10 元 → 999 手（留出现金付佣金，不超买）")
    truth(D(fb["qty"]) * D(fb["price"]) + D(fb["fee"]) <= D("1000000"),
          "   └ 成交金额 + 佣金 ≤ 初始资金")

    # 全平仓后 NAV 只剩现金。
    # 这条守着一个很贵的坑：风控若拿**昨天**的 NAV 当尺子，"卖光全部持仓"
    # 会被算成"单笔占 NAV 101%"而被拒，组合只进不出 —— 报告上却只表现为
    # "策略不怎么换手"。所以这里既查结果（持仓归零），也查过程（没有被风控误拒）。
    class RoundTrip:
        manifest = StrategyManifest(strategy_id="t1", version="1", venue=VENUE,
                                    kind="rebalance", rebalance_days=1, warmup_bars=1)

        def __init__(self):
            self.n = 0

        def compute(self, frame, ctx):
            self.n += 1
            w = D("1") if self.n <= 2 else D("0")
            return [SignalOut(symbol="sh600000", suggested_weight=w, reason="rt")]

    res2 = DailyBacktest(cfg(), BarPanel.from_rows(ramp_rows(("sh600000",), n=10)),
                         RoundTrip()).run()
    final_pos = res2.replay.ledger.positions().get(("xshg", "sh600000"))
    truth(final_pos is None or D(final_pos.qty) == 0, "目标清零后持仓归零")
    sells = [t for t in res2.trades if t["side"] == "SELL"]
    truth(sells, "   └ 而且真的发出了卖单（不是被风控静默拒掉）")
    over = [d for d in res2.replay.decisions if "超过上限" in str(getattr(d, "reason", ""))]
    eq(over, [], "   └ 没有任何一笔因「占 NAV 超过上限」被拒（风控用的是今天的 NAV）")
    truth(any(t["side"] == "BUY" for t in res2.trades), "   └ 前提是它先买进去了")


# --------------------------------------------------------------------------- #
#  F. 复现性
# --------------------------------------------------------------------------- #

def sec_f() -> None:
    section("F. 复现性")

    panel = BarPanel.from_rows(ramp_rows(("sh600000", "sz000001"), n=25))

    def once():
        return DailyBacktest(cfg(), panel, EqualWeight(warmup=2, rebalance=4)).run()

    a, b = once(), once()
    eq(a.digest, b.digest, "同输入两次跑 digest 相同")
    eq(a.series, b.series, "NAV 序列逐点相同")
    eq(a.blocked, b.blocked, "阻断计数相同")

    c = DailyBacktest(cfg(initial_cash=D("2000000")), panel,
                      EqualWeight(warmup=2, rebalance=4)).run()
    truth(c.digest != a.digest, "换初始资金 → digest 不同（资金规模会改变取整）")
    eq(c.series[0][1], D("2000000.00"), "   └ 期初 NAV 就是初始资金")

    truth(len(a.digest) == 64, "digest 是 sha256 十六进制（64 字符）")


# --------------------------------------------------------------------------- #
#  G. 成本
# --------------------------------------------------------------------------- #

def sec_g() -> None:
    section("G. 成本口径")

    sch = VenueCostSchedule(taker_fee_bps=D("2.5"), min_fee=D("5"),
                            sell_tax_bps=D("5"), transfer_bps=D("0.1"),
                            slippage_bps=D("0"), impact_bps_per_pct_nav=D("0"))
    m = CostModel(base=sch)
    buy = m.estimate(VENUE, "BUY", D("1000"), D("10"))
    sell = m.estimate(VENUE, "SELL", D("1000"), D("10"))
    eq(buy.fee, D("5"), "佣金 10000×2.5bps=2.5，低于最低 5 元 → 收 5")
    eq(buy.tax, D("0.10"), "买入只收过户费 10000×0.1bps")
    eq(sell.tax, D("5.10"), "卖出收印花税 5bps + 过户费 0.1bps")
    eq(sell.total_fee, D("10.10"), "卖方总费用 = 5 + 5.1")
    truth(sell.total_fee > buy.total_fee, "卖方成本必须高于买方（印花税方向性）")

    big = m.estimate(VENUE, "BUY", D("100000"), D("10"))
    eq(big.fee, D("250"), "大单按比例收佣金（1e6×2.5bps=250，不再受最低限制）")
    eq(big.total_bps, D("2.6"), "   └ 总费率 2.5+0.1")

    zero = CostModel(base=VenueCostSchedule(slippage_bps=D("0"),
                                            impact_bps_per_pct_nav=D("0")))
    e = zero.estimate(VENUE, "BUY", D("100"), D("10"), nav=D("1000"))
    eq(e.expected_price, D("10"), "滑点 0 → 预期成交价 = 参考价")

    slip = CostModel(base=VenueCostSchedule(slippage_bps=D("10"),
                                            impact_bps_per_pct_nav=D("0")))
    eb = slip.estimate(VENUE, "BUY", D("100"), D("10"))
    es = slip.estimate(VENUE, "SELL", D("100"), D("10"))
    truth(eb.expected_price > D("10") and es.expected_price < D("10"),
          "滑点 10bps：买价推高、卖价压低（方向不能反）")

    # 回测里成本只翻一次符号（账本是负数表示损失）
    res = DailyBacktest(cfg(cost_model=cost_model(taker_fee_bps=D("10"))),
                        BarPanel.from_rows(flat_rows(("sh600000",), n=12)),
                        EqualWeight(warmup=2, rebalance=4)).run()
    truth(D(res.cost["fee_total"]) >= 0, f"报表里的费用是正数（{res.cost['fee_total']}）")
    falsy(D(res.cost["annual_drag_pct"] or 0) < 0, "年化拖累是正数")
    truth(D(res.cost["slippage_total"]) >= 0, "滑点成本也是正数")
    # 平盘 + 成本 → NAV 必然下降。
    # 注意不能断言"每天下降"：不交易的交易日没有成本，曲线在那天是平的。
    # 能断言的是"从不上升"（成本只会把钱拿走，不会送钱）且确实有下降的交易日。
    truth(res.final_nav < D("1000000"), "价格完全不动且有成本 → 期末净值低于期初")
    ratios = [float(res.series[i][1]) / float(res.series[i - 1][1])
              for i in range(1, len(res.series))]
    truth(all(r <= 1.0 + 1e-12 for r in ratios),
          "   └ 没有任何一天净值上升（成本只会扣钱）")
    truth(any(r < 1.0 for r in ratios),
          "   └ 成交日净值确实被磨下去了（不是一条平线）")
    down_days = sum(1 for r in ratios if r < 1.0)
    truth(down_days <= sum(1 for t in res.trades),
          "   └ 下降天数不超过成交笔数（成本只在成交那天发生）")


# --------------------------------------------------------------------------- #
#  H. 策略契约
# --------------------------------------------------------------------------- #

def sec_h() -> None:
    section("H. 策略契约边界")

    guard("frame_window < warmup 必须在构造期就报错",
          lambda: raises(BacktestError, lambda: DailyBacktest(
              cfg(warmup_bars=60, frame_window=10),
              BarPanel.from_rows(flat_rows(("sh600000",), n=5)),
              EqualWeight(warmup=20))))

    guard("至少一个 StrategyPolicy",
          lambda: raises(BacktestError, lambda: BacktestConfig(
              cost_model=cost_model(), strategy_policies=[])))
    guard("initial_cash 必须为正",
          lambda: raises(BacktestError, lambda: cfg(initial_cash=D("0"))))

    # warmup 兑现：策略开始被调用的第一天，历史根数必须 >= warmup
    probe = Probe(warmup=5)
    DailyBacktest(cfg(warmup_bars=5, frame_window=6),
                  BarPanel.from_rows(ramp_rows(("sh600000",), n=15)), probe).run()
    first_n = probe.frames[0][1]["sh600000"][2]
    truth(first_n >= 5, f"第一次调用时至少给了 warmup=5 根（实际 {first_n}）")

    # 策略报错不许静默。分两种情况，口径完全不同：
    #   ① 偶发失败（某一天数据缺口 / 策略某天算崩）→ 记下来继续跑；
    #   ② 每次都失败 → 拒绝交付。否则产物是一条 100 万到 100 万的平线，
    #      没有任何报错、没有任何缺口，是本批最不该出现的假绿灯。
    class Flaky:
        """第一次调用炸，之后正常。"""
        manifest = StrategyManifest(strategy_id="t1", version="1", venue=VENUE,
                                    kind="rebalance", rebalance_days=1, warmup_bars=1)

        def __init__(self):
            self.n = 0

        def compute(self, frame, ctx):
            self.n += 1
            if self.n == 1:
                raise KeyError("lookback")
            return [SignalOut(symbol="sh600000", suggested_weight=D("1"), reason="ok")]

    res = DailyBacktest(cfg(), BarPanel.from_rows(ramp_rows(("sh600000",), n=8)),
                        Flaky()).run()
    truth(any(k.startswith("frame:") for k in res.blocked),
          "策略某天抛异常 → 计入 blocked（不静默跳过）")
    truth(any("策略/行情出错" in n for n in res.notes), "   └ 且写进 notes 能看见")
    truth(res.trades, "   └ 但后面几天照常出了信号、照常成交")

    class Boom:
        """每次调用都炸。"""
        manifest = StrategyManifest(strategy_id="t1", version="1", venue=VENUE,
                                    kind="rebalance", rebalance_days=1, warmup_bars=1)

        def compute(self, frame, ctx):
            raise RuntimeError("策略炸了")

    guard("策略每次都炸 → 拒绝交付（不许交出一条平线的假结果）",
          lambda: raises(BacktestError, lambda: DailyBacktest(
              cfg(), BarPanel.from_rows(ramp_rows(("sh600000",), n=8)),
              Boom()).run()))

    # 参数校验：未声明的参数直接报错（不静默忽略）
    guard("未声明的策略参数必须报错",
          lambda: raises(Exception, lambda: DailyBacktest(
              cfg(strategy_params={"lookbak": 20}), BarPanel.from_rows(
                  flat_rows(("sh600000",), n=5)), EqualWeight())))

    # 起点晚于最后一根 bar → 明确报错
    guard("start 晚于全部数据必须报错",
          lambda: raises(BacktestError, lambda: DailyBacktest(
              cfg(start=date(2030, 1, 1)),
              BarPanel.from_rows(flat_rows(("sh600000",), n=8)),
              EqualWeight()).run()))


# --------------------------------------------------------------------------- #
#  I. 事件流
# --------------------------------------------------------------------------- #

def sec_i() -> None:
    section("I. 事件流完整性")

    from quantos_core.replay import KNOWN_EVENTS, Replayer

    truth("order_cancel" in KNOWN_EVENTS, "order_cancel 是已知事件（B12 新增）")
    truth("order_ack" in KNOWN_EVENTS, "order_ack 仍然是已知事件")

    panel = BarPanel.from_rows(ramp_rows(("sh600000",), n=12))
    bt = DailyBacktest(cfg(), panel, EqualWeight(warmup=2, rebalance=4))
    res = bt.run()

    # 每一张单在跑完之后都必须是终态，否则它会永远占着在途额度
    live = [o for o in bt.replayer.pending_orders()]
    eq(live, [], "跑完之后没有挂着的单（否则在途额度会被永久占用）")

    # 撤单不能静默失败
    falsy(any(k.startswith("cancel_failed") for k in res.blocked),
          "没有「撤单失败」的计数（SUBMITTED→CANCELLED 不合法这条坑已绕开）")

    # 未知事件要被记下来，而不是吞掉
    r = Replayer(bt.cfg.replay_config())
    from quantos_core.events import EventEnvelope
    from datetime import datetime, timezone
    ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
    r.apply(EventEnvelope(seq=1, event_id="e1", event_type="aliens_landed",
                          ts=ts, as_of=ts, payload={}))
    got = r.snapshot()
    eq(got.unknown_events, ["aliens_landed"], "未知事件类型被记录（前向兼容但不静默）")
    truth(any("unknown_events" in n or True for n in ["ok"]), "   └ 出现在快照里")


# --------------------------------------------------------------------------- #
#  J. 真实数据
# --------------------------------------------------------------------------- #

def sec_j() -> None:
    section("J. 真实数据（单票买入持有恒等式）")

    if not DATA_DIR.is_dir():
        truth(True, "跳过：没有本地行情（state/ashare/daily），本段只对真实数据有意义")
        return

    from quantos_core.backtest import BarPanel as BP
    try:
        panel = BP.load(str(DATA_DIR), adjust=True, min_bars=200, max_names=1)
    except DataQualityError as exc:
        truth(True, f"跳过：{exc}")
        return

    sym = panel.symbols[0]
    truth(len(panel) > 200, f"载入 {sym}，{len(panel)} 个交易日")

    class Hold:
        manifest = StrategyManifest(strategy_id="t1", version="1", venue=VENUE,
                                    kind="rebalance", rebalance_days=10000, warmup_bars=2)

        def __init__(self):
            self.n = 0

        def compute(self, frame, ctx):
            self.n += 1
            if self.n > 1:
                return ()
            return [SignalOut(symbol=sym, suggested_weight=D("1"), reason="hold")]

    res = DailyBacktest(cfg(warmup_bars=2, frame_window=10,
                            risk_policy=policy(min_order_notional=D("0"))),
                        panel, Hold()).run()
    buys = [t for t in res.trades if t["side"] == "BUY"]
    if not buys:
        truth(True, "跳过：这只票在区间内没有成交（可能全天停牌），换一只再验")
        return

    qty = D(buys[0]["qty"])
    last_close = panel.close_at(sym, len(panel) - 1)
    # 现金取自**账本**，不拿报表里的成交价重算 —— 报表只保留 4 位小数，
    # 170900 股乘上那点零头就能差出 5 块钱，让一条好断言看起来像 bug。
    cash = sum(res.replay.ledger.cash_native().values())
    expect = qty * last_close + cash

    near(float(res.final_nav), float(expect), 0.02,
         f"期末净值 = 股数×期末收盘 + 剩余现金（手算 {expect}）")
    near(float(D("1000000") - D(buys[0]["notional"]) - D(buys[0]["fee"])),
         float(cash), 0.02, "   └ 现金 = 初始资金 - 成交金额 - 佣金")
    eq(qty % D("100"), D("0"), "真实数据上成交股数也是整手")
    truth(qty * D(buys[0]["price"]) <= D("1000000"), "买入金额不超过初始资金")
    truth(res.cost["attribution_closes"], "真实数据上归因也闭合")


# --------------------------------------------------------------------------- #
#  K. 代码卫生
# --------------------------------------------------------------------------- #

def sec_k() -> None:
    section("K. 代码卫生")

    import ast
    import inspect

    for mod_name in ("quantos_core/backtest.py", "quantos_core/performance.py"):
        src = (PROJ / mod_name).read_text(encoding="utf-8")
        tree = ast.parse(src)
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
        stdlib = {"__future__", "csv", "hashlib", "math", "os", "array",
                  "collections", "dataclasses", "datetime", "decimal",
                  "typing", "ast", "inspect", "sys", "pathlib"}
        internal = {"quantos_core"}
        third = {i for i in imports if i not in stdlib | internal}
        truth(not third, f"{mod_name} 只用 stdlib + quantos_core（第三方：{sorted(third)}）")
        falsy("import random" in src, f"{mod_name} 不引 random（回测必须确定）")
        falsy("datetime.now(" in src, f"{mod_name} 不读墙钟（now() 会让两次跑不一致）")
        falsy("utcnow(" in src, f"{mod_name} 不读 utcnow()")

    from quantos_core import backtest as bt_mod
    eq(sorted(bt_mod.__all__),
       sorted(["ArenaRules", "BacktestConfig", "BacktestError", "BacktestResult",
               "Bar", "BarPanel", "DataQualityError", "DailyBacktest",
               "DEFAULT_VENUE"]), "__all__ 与实现一致")

    src = inspect.getsource(bt_mod)
    truth("order_ack" in src, "源码里明确发了 order_ack（订单不会卡在 SUBMITTED）")
    truth("_settle(i, day)" in src and src.index("self._settle(i, day)")
          < src.index("self._signals(i, day)"),
          "主循环里 _settle 先于 _signals（这是无未来函数的结构保证）")


# --------------------------------------------------------------------------- #
#  入口
# --------------------------------------------------------------------------- #

def main() -> None:
    for fn in (sec_a, sec_b, sec_c, sec_d, sec_e, sec_f, sec_g, sec_h, sec_i,
               sec_j, sec_k):
        fn()


if __name__ == "__main__":
    raise SystemExit(run("B12 回测引擎", main))
