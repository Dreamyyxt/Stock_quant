"""
B7 验收：协方差估计（T-701）、公司行动（T-702）、能力上屏（T-703）
====================================================================

三个部分，每条断言都对应一个"会安静出错"的具体做法：

  A 协方差估计：口径（年化/复权/对齐）、收缩、自由度、正定性、落盘
  B 公司行动：分红不被 fx 吞、拆股不动金额、第五项对账、幂等不污染派生字段
  C 上屏契约：/api/ops/portfolio 在「有数据」「没数据」两种状态下的结构

其中 B 的第一组断言（幂等）修的是一个**真实存在过**的 bug：
重复的交易所回报会让派生字段翻倍而现金只变一次，差额被 fx 残差吞掉，
归因恒等式照样显示"闭合"，但报表上是凭空出现的巨额亏损。
"""

from __future__ import annotations

import math
import json
import os
import random
import re
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quantos_core.ai_research import ResearchInbox, ResearchKind
from quantos_core.canary import CanaryRouter
from quantos_core.contracts import Fill, Side, utcnow
from quantos_core.covariance import (
    EstimationConfig, EstimationError, PriceSeries, RiskModelStore,
    TRADING_DAYS_CRYPTO, TRADING_DAYS_EQUITY, align_prices, detect_price_jumps,
    estimate, estimate_from_dict, ewma_weights, intersect_dates,
    simple_returns,
)
from quantos_core.eod import (
    BrokerStatement, DailyAttribution, DeclaredAction, DiffKind, EodReconciler,
    NavPoint, build_daily_attribution, ledger_trade_ids,
)
from quantos_core.ledger import Ledger
from quantos_core.metrics import OpsMetrics
from quantos_core.risk_budget import RiskModelIncomplete

from _asserts import eq, falsy, guard, near, ne, raises, run, section, truth

ROOT = str(Path(__file__).resolve().parent.parent)

D = lambda x: Decimal(str(x))
UTC = timezone.utc
TS = datetime(2026, 10, 26, 20, 0, tzinfo=UTC)
DAY = date(2026, 10, 26)
AAPL = ("NYSE", "AAPL")


# --------------------------------------------------------------------------- #
#  造数据
# --------------------------------------------------------------------------- #


def _standardize(xs):
    m = sum(xs) / len(xs)
    c = [x - m for x in xs]
    sd = math.sqrt(sum(v * v for v in c) / (len(c) - 1))
    return [v / sd for v in c]


def _series(symbol, rets, sigma_daily, start=date(2026, 1, 1)):
    """把标准化收益乘上 σ 变成价格序列 —— 样本标准差正好等于 σ。"""
    px = [100.0]
    for r in rets:
        px.append(px[-1] * (1.0 + sigma_daily * r))
    return PriceSeries(symbol, tuple(
        (start + timedelta(days=i), D(f"{p:.10f}")) for i, p in enumerate(px)))


def _returns(n, seed):
    rng = random.Random(seed)
    return _standardize([rng.gauss(0, 1) for _ in range(n)])


def _correlated(a, b, rho):
    """用 Cholesky 造指定相关的第二组收益。"""
    return _standardize([rho * x + math.sqrt(1 - rho * rho) * y
                         for x, y in zip(a, b)])


def _fill(tid, cid, qty, px, fee="0"):
    return Fill(fill_id="f-" + tid, order_id="o-" + cid, client_order_id=cid,
                venue_trade_id=tid, quantity=D(qty), price=D(px),
                fee=D(fee), fee_currency="USD", ts=TS)


def _book(div=None, split=None, interest=None, buy_at="100"):
    led = Ledger("USD")
    led.deposit("A", "USD", D("100000"), ts=TS, ref="dep")
    led.apply_fill(_fill("t1", "c1", "100", buy_at), "NYSE", "AAPL", "A",
                   Side.BUY, "USD", D(buy_at))
    if div is not None:
        led.post_dividend("A", "USD", D(div), ts=TS, ref="div-1")
    if interest is not None:
        led.post_interest("A", "USD", D(interest), ts=TS, ref="int-1")
    if split is not None:
        led.apply_split("NYSE", "AAPL", D(split), ts=TS)
    return led


class _Reg:
    def active_orders(self):
        return []


class _Gateway:
    def __init__(self, stmt):
        self.stmt = stmt

    def fetch_statement(self, account_id, day):
        return self.stmt


def _stmt_for(led, actions=(), cash_bias="0"):
    cash = {k: D(v) for k, v in led.cash_native().items()}
    cash[("A", "USD")] = cash.get(("A", "USD"), D("0")) + D(cash_bias)
    return BrokerStatement(
        account_id="A", day=DAY, as_of=TS, cash=cash,
        positions={k: p.qty for k, p in led.positions().items()},
        trade_ids=tuple(ledger_trade_ids(led, DAY)),
        corporate_actions=tuple(actions))


def _reconcile(led, actions=(), cash_bias="0"):
    return EodReconciler(led, _Reg()).run(
        "A", DAY, _Gateway(_stmt_for(led, actions, cash_bias)))


# --------------------------------------------------------------------------- #
#  A · 协方差估计
# --------------------------------------------------------------------------- #


def part_a() -> None:
    section("A1 已知答案回收（造数据时可精确断言）")
    z = _returns(120, 20261027)
    sig_d = 0.02
    s = _series("AAA", z, sig_d)
    est = estimate([s], EstimationConfig(annualization=252, min_obs=60))
    expect = D(f"{sig_d * math.sqrt(252):.8f}")
    near(est.vols["AAA"], expect, D("1e-6"), "σ 精确回收到 0.02×√252")
    eq(est.n_obs, 120, "观测数 = 价格数 - 1")
    eq(est.annualization, 252, "年化因子被记录进结果")

    section("A2 年化因子：只改口径不改数据")
    e365 = estimate([s], EstimationConfig(annualization=TRADING_DAYS_CRYPTO,
                                          min_obs=60))
    near(e365.vols["AAA"] / est.vols["AAA"], D(f"{math.sqrt(365/252):.8f}"),
         D("1e-7"), "365/252 的比值正好是 √(365/252)（用错差 20%）")
    truth(e365.vols["AAA"] > est.vols["AAA"],
          "  └ Crypto 口径必然高于股票口径")

    section("A3 已知 ρ 回收")
    z1 = _returns(120, 11)
    z2 = _correlated(z1, _returns(120, 22), 0.8)
    e2 = estimate([_series("AAA", z1, 0.02), _series("BBB", z2, 0.015)],
                  EstimationConfig(annualization=252, min_obs=60))
    # 容差 0.05：样本相关的标准误约 1/√120 ≈ 0.09，卡 0.01 是自欺欺人。
    near(e2.corr[("AAA", "BBB")], D("0.8"), D("0.05"),
         "ρ 回收到 0.8（容差取 ρ 的标准误量级，不是 0.001）")
    truth(e2.trustworthy, "  └ 120 观测 2 标的 → 判定可信")

    section("A4 等权结果必须等于手算的样本协方差")
    prices = [D("100"), D("102"), D("101"), D("105"), D("103"), D("107")]
    rets = simple_returns(prices)
    mean = sum(rets) / len(rets)
    var_hand = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    one = PriceSeries("H", tuple((date(2026, 1, 1) + timedelta(days=i), v)
                                 for i, v in enumerate(prices)))
    eh = estimate([one], EstimationConfig(annualization=252, min_obs=5))
    near(eh.vols["H"] / D(252).sqrt(),
         D(f"{math.sqrt(float(var_hand)):.8f}"), D("1e-7"),
         "等权权重下与样本标准差手算值一致（1-Σw² 修正正好还原 n-1）")
    w5 = ewma_weights(5, None)
    eq(sum(w5, Decimal(0)), D("1"), "等权权重和精确为 1")

    section("A5 对齐：必须取交集，绝不用前值填充")
    short = PriceSeries("S", tuple((date(2026, 1, 1) + timedelta(days=i), D("100"))
                                   for i in range(0, 40)))
    long_ = PriceSeries("L", tuple((date(2026, 1, 1) + timedelta(days=i), D("50"))
                                   for i in range(0, 120)))
    eq(len(intersect_dates([short, long_])), 40, "交集 = 短序列长度")
    raises(EstimationError, lambda: align_prices([short, long_], min_obs=60),
           "交集不足 → 抛错，而不是拿 40 天硬估")
    def _check_msg():
        try:
            align_prices([short, long_], min_obs=60)
        except EstimationError as exc:
            truth("40" in str(exc) and "61" in str(exc),
                  "  └ 报错里带上了实际天数（40）和所需天数（61）")
            return
        raise AssertionError("没有抛错")

    guard("交集不足的报错信息可诊断", _check_msg)

    section("A6 输入校验：坏输入不许静默通过")
    raises(EstimationError,
           lambda: PriceSeries("X", ((date(2026, 1, 2), D("1")),
                                     (date(2026, 1, 1), D("1")))),
           "日期乱序 → 拒绝")
    raises(EstimationError,
           lambda: PriceSeries("X", ((date(2026, 1, 1), D("1")),
                                     (date(2026, 1, 1), D("2")))),
           "日期重复 → 拒绝")
    raises(EstimationError,
           lambda: PriceSeries("X", ((date(2026, 1, 1), D("0")),
                                     (date(2026, 1, 2), D("1")))),
           "价格为 0 → 拒绝（用 0 表示停牌会算出 -100% 收益）")
    raises(EstimationError, lambda: PriceSeries("X", ()), "空序列 → 拒绝")

    section("A7 停牌检测：连续同价会低估 σ")
    flat = [D("100")] * 8 + [D("101"), D("102")] + [D("102")] * 30
    sf = PriceSeries("FLAT", tuple((date(2026, 1, 1) + timedelta(days=i), v)
                                   for i, v in enumerate(flat)))
    e7 = estimate([sf], EstimationConfig(annualization=252, min_obs=10))
    truth(any("连续" in w and "停牌" in w for w in e7.warnings),
          "连续 31 天同价 → 告警「疑似停牌或前值填充」")

    section("A8 未复权拆股检测：-50% 不是市场波动")
    sp = [D("200")] * 5 + [D("100")] + [D("101")] * 30
    ss = PriceSeries("SPLIT", tuple((date(2026, 1, 1) + timedelta(days=i), v)
                                    for i, v in enumerate(sp)))
    jumps = detect_price_jumps([p for _, p in ss.points],
                               [d for d, _ in ss.points])
    eq(len(jumps), 1, "检出 1 处跳变")
    near(jumps[0][1], D("-0.5"), D("1e-6"), "跳变幅度 -50%（正好是 2:1 拆股）")
    e8 = estimate([ss], EstimationConfig(annualization=252, min_obs=10))
    truth(any("拆股" in w for w in e8.warnings),
          "告警明确指出「若当天有拆股…价格必须复权」")

    section("A9 收缩：只动相关、不动波动率")
    zc = _returns(120, 33)
    trio = [_series("AAA", z1, 0.02), _series("BBB", z2, 0.018),
            _series("CCC", zc, 0.025)]
    base = estimate(trio, EstimationConfig(annualization=252, min_obs=60))
    shrunk = estimate(trio, EstimationConfig(annualization=252, min_obs=60,
                                             shrinkage=D("0.3")))
    eq(shrunk.vols, base.vols, "收缩后 σ 完全不变（目标矩阵对角线 = 1）")
    ne(shrunk.corr[("AAA", "BBB")], base.corr[("AAA", "BBB")],
       "收缩后 ρ 确实变了")
    eq(shrunk.raw_corr, base.corr, "收缩前的 ρ 被完整保留（raw_corr）")
    rho_bar = (base.corr[("AAA", "BBB")] + base.corr[("AAA", "CCC")]
               + base.corr[("BBB", "CCC")]) / 3
    near(shrunk.corr[("AAA", "BBB")],
         D("0.7") * base.corr[("AAA", "BBB")] + D("0.3") * rho_bar,
         D("1e-6"), "收缩公式 (1-δ)ρ + δρ̄ 精确成立")
    full = estimate(trio, EstimationConfig(annualization=252, min_obs=60,
                                           shrinkage=D("1")))
    eq(len(set(full.corr.values())), 1, "δ=1 时所有 ρ 都等于 ρ̄")

    section("A10 收缩的局限：只有 2 个标的时它什么也不做")
    only2 = estimate([_series("AAA", z1, 0.02), _series("BBB", z2, 0.015)],
                     EstimationConfig(annualization=252, min_obs=60,
                                      shrinkage=D("0.5")))
    truth(any("2 个标的" in w and "不起作用" in w for w in only2.warnings),
          "2 标的 + shrinkage → 明确告警「实际不起作用」")

    section("A11 自由度：分母是参数个数，不是标的个数")
    rng = random.Random(7)
    many = [_series(f"S{i}", _returns(40, 100 + i), 0.01 + 0.002 * i)
            for i in range(5)]
    e11 = estimate(many, EstimationConfig(annualization=252, min_obs=30))
    eq(e11.n_params, 15, "5 标的 = 5 个 σ + 10 个 ρ = 15 个待估参数")
    near(e11.dof_ratio, D("40") / D("15"), D("0.01"),
         "自由度比 = 40/15 = 2.67（按标的数算会是 8，差 3 倍）")
    falsy(e11.trustworthy, "自由度不足 → 判定不可信")
    truth(any("待估参数" in w for w in e11.warnings),
          "告警里说明了参数构成，不只是给个数字")

    section("A12 正定性：每个 ρ 都合法，矩阵仍可能非正定")
    from quantos_core.covariance import _corr_matrix, _psd_via_cholesky
    bad = _corr_matrix(["A", "B", "C"],
                       {("A", "B"): D("0.9"), ("B", "C"): D("0.9"),
                        ("A", "C"): D("-0.9")})
    ok, why = _psd_via_cholesky(bad)
    falsy(ok, "ρ_ab=ρ_bc=0.9 且 ρ_ac=-0.9 的矩阵非正定（三个 ρ 都在 [-1,1] 内）")
    truth("主元" in why, "  └ 报错指出是第几个主元出问题")
    good = _corr_matrix(["A", "B", "C"],
                        {("A", "B"): D("0.5"), ("B", "C"): D("0.3"),
                         ("A", "C"): D("0.2")})
    truth(_psd_via_cholesky(good)[0], "合理矩阵判为正定")

    section("A13 EWMA 权重形状")
    for hl in (5, 20, 60):
        w = ewma_weights(6, hl)
        eq(sum(w, Decimal(0)), D("1"), f"halflife={hl} 权重和为 1")
        truth(all(w[i] < w[i + 1] for i in range(len(w) - 1)),
              f"  └ halflife={hl} 权重单调递增（越近越大）")
    truth(ewma_weights(6, 5)[-1] > ewma_weights(6, 60)[-1],
          "短半衰期把更多权重压在近端")

    section("A14 转 RiskModel：故意不给 default_corr")
    rm = e2.to_risk_model()
    eq(rm.default_corr, None, "default_corr 必须是 None（不给「看起来合理」的 ρ）")
    eq(rm.vols["AAA"], e2.vols["AAA"], "σ 原样传入")
    raises(RiskModelIncomplete, lambda: rm.rho("AAA", "ZZZ"),
           "没估过的标的对 → 抛错，而不是静默取默认值")

    section("A15 落盘与读回")
    tmp = tempfile.mkdtemp(prefix="qos-cov-")
    store = RiskModelStore(os.path.join(tmp, "model.json"))
    eq(store.load(), None, "文件不存在 → load 返回 None（不抛错）")
    eq(store.age_days(), None, "  └ age_days 也返回 None")
    eq(store.stale(), None, "  └ stale 返回 None（区别于「有且新鲜」）")

    now = datetime(2026, 10, 27, 0, 0, tzinfo=UTC)
    store.save(est, estimated_at=now)
    back, at = store.load()
    eq(back.vols, est.vols, "读回 σ 一致")
    eq(back.corr, est.corr, "读回 ρ 一致")
    eq(at, now, "读回估计时刻一致")
    eq(store.age_days(now), 0.0, "刚估完年龄 0")
    falsy(store.stale(7, now), "刚估完不算过期")
    truth(store.stale(7, now + timedelta(days=10)),
          "10 天后判为过期（阈值 7 天）")

    with open(store.path, "w", encoding="utf-8") as fh:
        fh.write("{ broken json")
    eq(store.load(), None, "文件损坏 → 返回 None（坏模型比没模型更危险）")

    section("A16 落盘保留收缩前对比")
    store.save(shrunk, estimated_at=now)
    back2, _ = store.load()
    eq(back2.raw_corr, shrunk.raw_corr, "raw_corr 一起落盘")
    truth(back2.raw_corr != back2.corr,
          "  └ 收缩前后不同，读回能看出差了多少")
    eq(estimate_from_dict(shrunk.to_dict()).vols, shrunk.vols,
       "to_dict/from_dict 往返一致")


# --------------------------------------------------------------------------- #
#  B · 公司行动
# --------------------------------------------------------------------------- #


def part_b() -> None:
    section("B1 幂等：重复回报不许污染派生字段（真实修掉的 bug）")
    led = Ledger("USD")
    led.deposit("A", "USD", D("100000"), ts=TS, ref="dep-1")
    led.deposit("A", "USD", D("100000"), ts=TS, ref="dep-1")
    eq(led.cash_native()[("A", "USD")], D("100000"), "现金只加一次（幂等生效）")
    eq(led.net_deposits_base, D("100000"),
       "净流入也只加一次 —— 否则 net_pnl 会凭空变成 -10 万")
    led.post_funding("A", "USD", D("5"), ts=TS, ref="fund-1")
    led.post_funding("A", "USD", D("5"), ts=TS, ref="fund-1")
    p = led.performance({})
    eq(p.funding, D("-5"), "资金费项不重复累加")
    eq(p.fx, D("0"), "残差为 0（重复累加会让 fx 吞掉差额）")
    truth(p.closes, "  └ 注意：closes=True 在这时**说明不了**任何事，"
                    "旧代码下它也是 True")

    section("B2 分红自成一格，不被 fx 残差吞掉")
    led = _book()
    p_before = led.performance({AAPL: D("100")})
    eq(p_before.dividend, D("0"), "没分红时该格为 0")
    led.post_dividend("A", "USD", D("1000"), ts=TS, ref="div-1")
    # 除权：价格 100 → 90
    p_after = led.performance({AAPL: D("90")})
    eq(p_after.nav, p_before.nav, "除权日 NAV 不变（现金补回了持仓市值下跌）")
    eq(p_after.trading, D("-1000"), "交易项 -1000（持仓市值下跌，这是真实发生的）")
    eq(p_after.dividend, D("1000"), "分红项 +1000")
    eq(p_after.fx, D("0"), "fx 残差为 0 —— 分红没有被当成汇率损益")
    near(p_after.attribution_sum, p_after.net_pnl, D("1e-9"),
         "六项之和 = 净收益")

    section("B3 分红的符号与方向")
    led = _book()
    led.post_dividend("A", "USD", D("500"), ts=TS, ref="d+")
    led.post_dividend("A", "USD", D("-200"), ts=TS, ref="d-")   # 补缴/退回
    p = led.performance({AAPL: D("100")})
    eq(p.dividend, D("300"), "正负分红相抵 = 300")
    eq(led.cash_native()[("A", "USD")], D("90300"),
       "现金同步 +300（基线 90000 = 10 万入金 - 1 万买入）")

    section("B4 拆股：只改数量，不改金额")
    led = _book()
    pos = led.positions()[AAPL]
    eq(pos.qty, D("100"), "拆股前 100 股")
    eq(pos.buy_at_decision, D("10000"), "成本金额 10000")
    p_before = led.performance({AAPL: D("100")})
    eq(p_before.trading, D("0"), "拆股前不盈不亏")
    led.apply_split("NYSE", "AAPL", D("2"), ts=TS)
    pos = led.positions()[AAPL]
    eq(pos.qty, D("200"), "2:1 拆股后 200 股")
    eq(pos.buy_at_decision, D("10000"),
       "成本金额不变 —— 顺手除以 ratio 会凭空产生 50% 收益")
    p_after = led.performance({AAPL: D("50")})       # 价格同步腰斩
    eq(p_after.trading, D("0"), "拆股后仍不盈不亏（200×50 - 10000 = 0）")
    eq(p_after.nav, p_before.nav, "NAV 不变")

    section("B5 反向拆股（并股）同样成立")
    led = _book(buy_at="50")
    p0 = led.performance({AAPL: D("50")})
    led.apply_split("NYSE", "AAPL", D("0.5"), ts=TS)
    eq(led.positions()[AAPL].qty, D("50"), "2:1 并股后 50 股")
    p1 = led.performance({AAPL: D("100")})
    eq(p1.trading, p0.trading, "并股不产生盈亏")
    eq(p1.nav, p0.nav, "并股不改 NAV")

    section("B6 拆股必须留审计痕迹")
    led = _book()
    led.apply_split("NYSE", "AAPL", D("2"), ts=TS)
    splits = [e for e in led.journal() if e["kind"] == "SPLIT"]
    eq(len(splits), 1, "拆股留下一条 SPLIT 分录")
    eq(splits[0]["meta"].get("ratio"), "2", "分录里记了比例")
    eq(splits[0]["meta"].get("qty_before"), "100", "分录里记了拆前数量")
    eq(splits[0]["meta"].get("qty_after"), "200", "分录里记了拆后数量")
    eq(led.apply_split("NYSE", "ZZZ", D("2")), None,
       "没有持仓的标的 → 返回 None（不报错，但也不假装做了事）")

    section("B7 参数校验")
    led = _book()
    raises(Exception, lambda: led.apply_split("NYSE", "AAPL", D("0")),
           "拆股比例 0 → 拒绝")
    raises(Exception, lambda: led.apply_split("NYSE", "AAPL", D("-2")),
           "拆股比例负数 → 拒绝")

    section("B8 融资利息归入资金成本口径")
    led = Ledger("USD")
    led.deposit("A", "USD", D("10000"), ts=TS, ref="d")
    led.post_interest("A", "USD", D("12.5"), ts=TS, ref="int-1")
    led.post_interest("A", "USD", D("12.5"), ts=TS, ref="int-1")
    p = led.performance({})
    eq(p.funding, D("-12.5"), "利息记入 funding 项（与永续资金费同口径）")
    eq(p.fx, D("0"), "不污染 fx")
    ints = [e for e in led.journal() if str(e["ref"]).startswith("interest:")]
    eq(len(ints), 1, "ref_type=interest，与普通资金费在账本里可区分")

    section("B9 分红税：账单现金是税后额")
    led = _book()
    led.post_dividend("A", "USD", D("700"), ts=TS, ref="div-tax",
                      gross_amount=D("1000"), withheld_tax=D("300"))
    divs = [e for e in led.journal() if e["kind"] == "DIVIDEND"]
    eq(divs[0]["meta"].get("gross_amount"), "1000", "税前额进 meta")
    eq(divs[0]["meta"].get("withheld_tax"), "300", "预扣税进 meta")
    eq(led.performance({AAPL: D("100")}).dividend, D("700"),
       "归因按税后现金计（账本记录的是真实收到的钱）")

    section("B10 对账第五项：公司行动已入账 → 干净")
    led = _book(div="1000")
    r = _reconcile(led, [DeclaredAction("dividend", amount=D("1000"),
                                        currency="USD", ref="div-1")])
    truth(r.clean, "声明与入账一致 → clean")
    eq(r.counts.get("corporate_action"), 0, "第五项差异 0")
    eq(set(r.counts), {"cash", "position", "open_order", "trade",
                       "corporate_action"}, "counts 有五个键")

    section("B11 未入账的分红 → 报公司行动差异，且排在现金差异之前")
    led = _book()
    r = _reconcile(led, [DeclaredAction("dividend", amount=D("1000"),
                                        currency="USD", ref="div-1")],
                   cash_bias="1000")
    falsy(r.clean, "有差异 → 不干净")
    eq(r.counts.get("corporate_action"), 1, "第五项报 1 条")
    eq(r.counts.get("cash"), 1, "现金项也报 1 条")
    kinds = [d.kind for d in r.diffs]
    eq(kinds[0], DiffKind.CORPORATE_ACTION,
       "公司行动排在现金之前 —— 先看根因，不要让人去猜为什么现金多了")
    truth("现金差异往往就是它造成的" in r.diffs[0].note,
          "  └ note 把两件事关联起来")

    section("B12 未入账的拆股 → 排在持仓差异之前")
    led = _book()
    r = EodReconciler(led, _Reg()).run("A", DAY, _Gateway(BrokerStatement(
        account_id="A", day=DAY, as_of=TS,
        cash={("A", "USD"): led.cash_native()[("A", "USD")]},
        positions={AAPL: D("200")},          # 券商已按 2:1 拆股
        trade_ids=tuple(ledger_trade_ids(led, DAY)),
        corporate_actions=(DeclaredAction("split", venue="NYSE", symbol="AAPL",
                                          ratio=D("2")),))))
    kinds = [d.kind for d in r.diffs]
    eq(kinds[0], DiffKind.CORPORATE_ACTION, "公司行动排第一")
    eq(kinds[1], DiffKind.POSITION, "持仓差异排第二")
    truth("拆股会让持仓数量与成本价同时失真" in r.diffs[0].note,
          "  └ note 说清了为什么必须先修这一条")

    section("B13 拆股已入账 → 不误报")
    led = _book(split="2")
    r = EodReconciler(led, _Reg()).run("A", DAY, _Gateway(BrokerStatement(
        account_id="A", day=DAY, as_of=TS,
        cash={("A", "USD"): led.cash_native()[("A", "USD")]},
        positions={AAPL: D("200")},
        trade_ids=tuple(ledger_trade_ids(led, DAY)),
        corporate_actions=(DeclaredAction("split", venue="NYSE", symbol="AAPL",
                                          ratio=D("2")),))))
    truth(r.clean, "已按 2:1 调整过 → 对账干净（不制造假差异）")

    section("B14 拆股比例不一致 → 即使数量对上了也要报")
    led = _book(split="2")
    r = EodReconciler(led, _Reg()).run("A", DAY, _Gateway(BrokerStatement(
        account_id="A", day=DAY, as_of=TS,
        cash={("A", "USD"): led.cash_native()[("A", "USD")]},
        positions={AAPL: D("200")},
        trade_ids=tuple(ledger_trade_ids(led, DAY)),
        corporate_actions=(DeclaredAction("split", venue="NYSE", symbol="AAPL",
                                          ratio=D("3")),))))
    falsy(r.clean, "券商说 3:1、本地按 2:1 → 不干净")
    truth(any("比例不一致" in d.note for d in r.diffs),
          "  └ 明确指出是比例问题，而不是笼统「持仓不符」")

    section("B15 预扣税不符 → 总额相同也要报")
    led = _book(div="700")
    r = _reconcile(led, [DeclaredAction("dividend", amount=D("700"),
                                        currency="USD", ref="div-1",
                                        gross_amount=D("1000"),
                                        withheld_tax=D("300"))])
    falsy(r.clean, "本地没记预扣税 → 不干净")
    truth(any("预扣税" in d.note for d in r.diffs),
          "  └ 指出税差，而不是让人以为漏了一笔钱")

    section("B16 融资利息与资金费在账本里可区分")
    led = Ledger("USD")
    led.deposit("A", "USD", D("100000"), ts=TS, ref="dep")
    led.post_funding("A", "USD", D("12.5"), ts=TS, ref="fund-1")
    r = _reconcile(led, [DeclaredAction("interest", amount=D("12.5"),
                                        currency="USD")])
    falsy(r.clean, "券商说是利息、本地记成普通资金费 → 报差异")
    eq(r.counts.get("corporate_action"), 1, "第五项报 1 条")
    eq(r.counts.get("cash"), 0, "  └ 现金项干净（金额确实对上，只是科目不对）")

    section("B17 券商声明未知类型 → 不能默认它没问题")
    led = _book()
    r = _reconcile(led, [DeclaredAction("buyback", amount=D("1"),
                                        currency="USD")])
    falsy(r.clean, "未知公司行动类型 → 判不干净")
    truth(any("未知的公司行动类型" in d.note for d in r.diffs),
          "  └ 明说「无法判定是否已入账」")

    section("B18 归因六项：分红日的完整拆解")
    prev = NavPoint(day="2026-10-23", ts=TS, nav=D("100000"), net_pnl=D("0"),
                    trading=D("0"), slippage=D("0"), fee=D("0"),
                    funding=D("0"), fx=D("0"), external_flow=D("100000"),
                    gross_exposure=D("10000"), net_exposure=D("10000"))
    cur = NavPoint(day="2026-10-26", ts=TS, nav=D("100000"), net_pnl=D("0"),
                   trading=D("-1000"), slippage=D("0"), fee=D("0"),
                   funding=D("0"), fx=D("0"), external_flow=D("100000"),
                   gross_exposure=D("9000"), net_exposure=D("9000"),
                   dividend=D("1000"))
    attr = build_daily_attribution(cur, prev, {"USD": D("1")})
    eq(attr.dividend, D("1000"), "分红项 = 1000")
    eq(attr.trading, D("-1000"), "交易项 = -1000")
    eq(attr.net_return, D("0"), "当日净收益 = 0（除权日不赚不亏）")
    near(attr.attribution_sum, attr.net_return, D("1e-9"), "六项之和闭合")
    eq(len([attr.trading, attr.slippage, attr.fee, attr.funding,
            attr.dividend, attr.fx]), 6, "归因确实是六项")

    section("B19 老快照（B7 之前写的）仍能读")
    old = {"day": "2026-10-20", "ts": TS.isoformat(), "nav": "100000",
           "net_pnl": "0", "trading": "0", "slippage": "0", "fee": "0",
           "funding": "0", "fx": "0", "external_flow": "100000",
           "gross_exposure": "0", "net_exposure": "0"}
    p_old = NavPoint.from_dict(old)
    eq(p_old.dividend, D("0"), "缺 dividend 字段 → 按 0 读（不是报错）")
    attr_old = build_daily_attribution(p_old, None, {"USD": D("1")})
    truth(isinstance(attr_old, DailyAttribution), "老数据也能生成归因")

    section("B20 对账只读：绝不自动补账")
    led = _book()
    cash_before = dict(led.cash_native())
    entries_before = len(led.journal())
    _reconcile(led, [DeclaredAction("dividend", amount=D("1000"),
                                    currency="USD", ref="div-1")],
               cash_bias="1000")
    eq(dict(led.cash_native()), cash_before, "对账跑完后现金没被动过")
    eq(len(led.journal()), entries_before, "分录数没变（没有偷偷补记）")


# --------------------------------------------------------------------------- #
#  C · 上屏契约
# --------------------------------------------------------------------------- #


def part_c() -> None:
    from quantos_core.ops_server import OpsService
    from quantos_core.strategy_registry import (
        AuditLog, StrategyRegistry,
    )

    tmp = tempfile.mkdtemp(prefix="qos-b7-")
    registry = StrategyRegistry(audit=AuditLog(os.path.join(tmp, "audit.jsonl")))

    section("C1 什么都没接 → 三块都 available=false 且带原因")
    svc = OpsService(metrics=OpsMetrics(registry=registry),
                     registry=registry)
    out = svc.portfolio()
    for key in ("risk_model", "canary", "research"):
        truth(key in out, f"  └ 返回里有 {key}")
        falsy(out[key]["available"], f"{key}.available = False")
        truth(bool(out[key].get("reason")), f"  └ {key} 带了 reason（不是空白）")
    truth(all(m["severity"] == "not_configured" for m in out["metrics"]),
          "未接入时返回的都是 not_configured（不是 ok —— 那会假装健康）")
    eq(len(out["metrics"]), 2, "恰好 2 条（risk.model / canary；research 的 key 无点）")

    section("C2 接上风险模型 → 页面能读到诊断与新鲜度")
    z1 = _returns(120, 5)
    z2 = _correlated(z1, _returns(120, 6), 0.6)
    series = [_series("AAA", z1, 0.02), _series("BBB", z2, 0.015)]
    est = estimate(series, EstimationConfig(annualization=252, min_obs=60,
                                            shrinkage=D("0.3")))
    store = RiskModelStore(os.path.join(tmp, "m.json"))
    now = datetime(2026, 10, 27, 3, 0, tzinfo=UTC)
    store.save(est, estimated_at=now)

    metrics = OpsMetrics(registry=registry, risk_model_store=store,
                         clock=lambda: now)
    svc = OpsService(metrics=metrics, registry=registry)
    out = svc.portfolio()
    rm = out["risk_model"]
    truth(rm["available"], "risk_model.available = True")
    eq(rm["n_obs"], 120, "观测数透传到页面")
    eq(rm["n_assets"], 2, "标的数透传")
    truth("dof_ratio" in rm and "corr_stderr" in rm,
          "自由度比与 ρ 分辨率都在（页面要能显示「这个 ρ 有多准」）")
    truth(rm["trustworthy"], "样本充足 → 标记可信")
    eq(rm["age_days"], 0.0, "年龄 0 天")
    falsy(rm["stale"], "未过期")
    truth(bool(rm["corr"]), "相关系数透传")
    truth(bool(rm["vols"]), "波动率透传")

    section("C3 指标层能单独抓出「模型过期」")
    old_metrics = OpsMetrics(
        registry=registry, risk_model_store=store,
        clock=lambda: now + timedelta(days=45))
    snap = old_metrics.sample()
    by_key = snap.by_key()
    truth("risk.model.age_days" in by_key, "有 risk.model.age_days 指标")
    eq(by_key["risk.model.age_days"].severity.value, "critical",
       "45 天 → critical（不是 warn：这时候 σ 基本无效了）")
    long_metrics = OpsMetrics(
        registry=registry, risk_model_store=store,
        clock=lambda: now + timedelta(days=10))
    eq(long_metrics.sample().by_key()["risk.model.age_days"].severity.value,
       "warn", "10 天 → warn（过了 7 天阈值但还能用）")

    section("C4 接上灰度与研究收件箱")
    router = CanaryRouter(clock=lambda: now)
    router.register_stable("etf_momentum", "v1", actor="human:owner")
    router.open_canary("etf_momentum", "v2", weight=D("0.1"),
                       actor="human:owner", reason="小流量试跑")
    inbox = ResearchInbox(Path(tmp) / "inbox.jsonl", clock=lambda: now)
    inbox.submit(author="ai:r", kind=ResearchKind.REPORT, strategy_id="s1",
                 summary="这是一条足够长的研究结论摘要，用来通过长度校验。")

    full = OpsMetrics(registry=registry, risk_model_store=store,
                      canary_router=router, research_inbox=inbox,
                      clock=lambda: now)
    svc2 = OpsService(metrics=full, registry=registry)
    out2 = svc2.portfolio()
    truth(out2["canary"]["available"], "canary.available = True")
    eq(len(out2["canary"]["cohorts"]), 1, "一个灰度队列")
    c0 = out2["canary"]["cohorts"][0]
    eq(c0["stable_version"], "v1", "稳定版 v1")
    eq(c0["canary_version"], "v2", "灰度版 v2")
    eq(c0["weight"], "0.1000", "权重 0.1")
    eq(c0["state"], "canary", "状态 canary")
    truth(out2["research"]["available"], "research.available = True")
    eq(out2["research"]["pending"], 1, "1 条待复核")
    eq(len(out2["research"]["items"]), 1, "items 里有 1 条")
    truth("summary" in out2["research"]["items"][0], "带了摘要（人能看懂是什么）")

    keys = {m.key for m in full.sample().metrics}
    for k in ("risk.model.age_days", "risk.model.dof", "risk.model.corr_stderr",
              "canary.active", "canary.etf_momentum", "research.pending"):
        truth(k in keys, f"  指标 {k} 存在")

    section("C5 端点是只读的：没有任何写入口")
    for bad in ("arm", "set_weight", "promote", "apply", "submit"):
        falsy(hasattr(OpsService, bad), f"OpsService 没有 {bad} 方法"
              "（风险假设与灰度权重只能由人工通过 Python 接口动）")

    section("C6 指标与页面用同一份事实（不许两套口径）")
    snap_a = full.sample()
    pf_a = snap_a.facts["portfolio"]
    out3 = svc2.portfolio()
    eq(out3["risk_model"]["dof_ratio"], pf_a["risk_model"]["dof_ratio"],
       "portfolio 端点与 facts 里的 dof_ratio 一致")
    sev = {m.key: m.severity.value for m in snap_a.metrics}
    if not pf_a["risk_model"]["trustworthy"]:
        eq(sev.get("risk.model.dof"), "warn", "不可信时指标也报 warn（不会一边 green）")


# --------------------------------------------------------------------------- #
#  D · 端到端
# --------------------------------------------------------------------------- #


def part_d() -> None:
    section("D1 估计 → 风险模型 → 风险分解，全链路不变量成立")
    from quantos_core.risk_budget import portfolio_risk
    z1 = _returns(200, 41)
    z2 = _correlated(z1, _returns(200, 42), 0.5)
    z3 = _correlated(z1, _returns(200, 43), -0.2)
    series = [_series("AAA", z1, 0.02), _series("BBB", z2, 0.015),
              _series("CCC", z3, 0.025)]
    est = estimate(series, EstimationConfig(annualization=252, min_obs=60,
                                            shrinkage=D("0.3")))
    model = est.to_risk_model()
    risk = portfolio_risk({"AAA": D("100000"), "BBB": D("60000"),
                           "CCC": D("-40000")}, model)
    near(risk.sigma, sum((s.contribution for s in risk.by_symbol), Decimal(0)),
         D("0.01"), "Σ标的贡献 ≡ 组合 σ（Euler 分解在真实估计上仍成立）")
    truth(risk.sigma > 0, "组合 σ 为正")

    section("D2 公司行动之后，净值与归因口径仍然闭合")
    led = _book(div="800", split="2", interest="15")
    # 拆股后价格腰斩，除权后价格再扣分红影响
    price = D("45")
    p = led.performance({AAPL: price})
    near(p.attribution_sum, p.net_pnl, D("1e-9"), "六项之和 = 净收益")
    eq(p.fx, D("0"), "fx 残差为 0（分红与利息都没漏到残差里）")
    eq(p.dividend, D("800"), "分红项 800")
    eq(p.funding, D("-15"), "资金成本项 -15")
    truth(p.closes, "closes 为真")

    section("D3 对账拿不到账单 → 判不干净（与 B4 同口径）")


    class _Broken:
        def fetch_statement(self, account_id, day):
            raise RuntimeError("券商维护中")

    led = _book()
    r = EodReconciler(led, _Reg()).run("A", DAY, _Broken())
    falsy(r.clean, "拿不到账单 → 不干净")
    falsy(r.statement_ok, "statement_ok = False")
    truth("券商维护中" in r.statement_error, "错误原因透传")


# --------------------------------------------------------------------------- #
#  E. 页面探针：B7 三块能力必须真的上屏，且缺数据时必须降级
# --------------------------------------------------------------------------- #

def _js_func_body(src: str, name: str) -> str:
    """按大括号配对取一个函数体。用来静态检查渲染函数之间有没有互相调用。

    不用正则一把梭：正则数不清嵌套括号，而这里恰恰要判断"函数体里出现了谁"，
    边界错一个字结论就会反。配平失败直接返回空串，让断言自己报出来。
    """
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", src)
    if not m:
        return ""
    i = m.end() - 1
    depth = 0
    while i < len(src):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[m.end():i]
        i += 1
    return ""


def part_e() -> None:
    """
    页面探针。

    判据不是"页面打得开"，而是三件事：
      1. 后端给的数据，页面上真能读到（数字对得回去）
      2. 后端不给数据，页面说"看不到"，而不是编一个绿灯
      3. 渲染函数之间没有互相调用（互调就是递归的入口）
    """
    section("E1 页面探针可用性")

    node = subprocess.run(["node", "--version"], capture_output=True, text=True)
    if node.returncode != 0:
        truth(True, "E1 本机没有 node，跳过前端探针（不影响后端结论）")
        return
    truth(True, "E1 本机有 node，继续跑页面探针")

    page_path = os.path.join(ROOT, "quantos", "index.html")
    probe = os.path.join(ROOT, "scripts", "page_probe.js")
    with open(page_path, encoding="utf-8") as fh:
        page_src = fh.read()

    def _run(fx: str) -> subprocess.CompletedProcess:
        return subprocess.run(["node", probe, page_path, fx],
                              capture_output=True, text=True, timeout=180)

    # -- E2 离线：一个接口都通不了 -------------------------------------- #
    section("E2 离线（运维服务不可达）：三块都必须说看不到")
    off = _run("-")
    eq(off.returncode, 0, "E2 离线探针全部断言通过")
    if off.stdout.strip():
        print("      " + off.stdout.strip().splitlines()[-1])

    # -- 造 fixture：直接问真实 service 要 JSON ------------------------- #
    from quantos_ops_server import OpsService, build_runtime
    rt = build_runtime()
    service = OpsService(
        metrics=rt["metrics"], registry=rt["registry"],
        prices_provider=lambda: rt["prices"], token="t0ken",
        series=rt["series"], live_gate=rt["gate"],
        risk_model_store=rt["risk_model_store"],
        canary_router=rt["canary_router"],
        research_inbox=rt["research_inbox"],
    )

    full = {
        "snapshot": service.snapshot(),
        "strategies": service.strategies(),
        "audit": service.audit(30),
        "navHistory": service.nav_history(),
        "live": service.live(),
        "portfolio": service.portfolio(),
    }
    tmpdir = tempfile.mkdtemp()

    def _write(name: str, obj) -> str:
        path = os.path.join(tmpdir, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, default=str)
        return path

    # -- E3 全量 fixture：三块都要有真数字 ------------------------------ #
    section("E3 有数据：三块上屏且数字能对回后端")
    pf = full["portfolio"]
    rm = pf.get("risk_model") or {}
    truth(rm.get("available") is True, "E3 风险模型 available=True")
    truth(int(rm.get("n_obs") or 0) > 0, "E3 风险模型有观测数")
    truth(bool(rm.get("corr")), "E3 风险模型有相关系数")
    truth(bool(rm.get("raw_corr")), "E3 风险模型保留了收缩前的样本值")
    truth(bool(rm.get("warnings") is not None), "E3 风险模型告警字段存在")

    cy = pf.get("canary") or {}
    truth(cy.get("available") is True, "E3 灰度 available=True")
    truth(len(cy.get("cohorts") or []) > 0, "E3 灰度有路由项")

    rs = pf.get("research") or {}
    truth(rs.get("available") is True, "E3 收件箱 available=True")
    truth(len(rs.get("items") or []) > 0, "E3 收件箱有条目")

    on = _run(_write("full.json", full))
    eq(on.returncode, 0, "E3 全量 fixture 探针全部断言通过")
    if on.stdout.strip():
        print("      " + on.stdout.strip().splitlines()[-1])

    # -- E4 缺 portfolio：降级，但整页不许被判离线 ---------------------- #
    section("E4 缺 /api/ops/portfolio：三块降级，整页仍算在线")
    partial = dict(full)
    partial.pop("portfolio")
    deg = _run(_write("partial.json", partial))
    eq(deg.returncode, 0, "E4 缺 portfolio 时探针全部断言通过")
    if deg.stdout.strip():
        print("      " + deg.stdout.strip().splitlines()[-1])

    # -- E5 静态：页面确实挂了这三块，且没有外部依赖 -------------------- #
    section("E5 静态检查：挂载点、请求路径、零外链")
    for anchor_id, label in (("riskModelBody", "风险模型"),
                             ("canaryBody", "灰度"),
                             ("researchBody", "AI 收件箱")):
        truth('id="%s"' % anchor_id in page_src,
              "E5 页面有挂载点 %s（%s）" % (anchor_id, label))
    truth("/api/ops/portfolio" in page_src, "E5 页面确实请求只读组合端点")

    # 零外链：任何 http(s) 资源引用都会在离线/换机时 404
    for pat, what in ((r'src\s*=\s*["\']https?://', "外部脚本/图片"),
                      (r'href\s*=\s*["\']https?://', "外部样式/链接"),
                      (r'@import\s+url\(\s*["\']?https?://', "外部 @import"),
                      (r'cdn\.', "CDN 域名")):
        falsy(re.search(pat, page_src), "E5 页面没有%s" % what)

    # -- E6 静态：三个渲染函数互不调用 -------------------------------- #
    section("E6 调用链：渲染函数之间单向，不存在互调环")
    names = ["renderRiskModel", "renderCanary", "renderResearch"]
    for name in names:
        body = _js_func_body(page_src, name)
        truth(len(body) > 0, "E6 取到 %s 的函数体" % name)
        others = [n for n in names if n != name]
        for other in others:
            falsy(other + "(" in body,
                  "E6 %s 内不调用 %s（渲染函数互调会递归）" % (name, other))

    # 统一刷新入口必须按固定顺序调度它们
    refresh = _js_func_body(page_src, "refreshAll")
    truth(len(refresh) > 0, "E6 取到 refreshAll 的函数体")
    pos = []
    for name in names:
        p = refresh.find(name + "(")
        truth(p >= 0, "E6 refreshAll 里调用了 %s" % name)
        pos.append(p)
    eq(pos, sorted(pos), "E6 refreshAll 内三个渲染函数顺序固定且不重复")


def main() -> None:
    part_a()
    part_b()
    part_c()
    part_d()
    part_e()


if __name__ == "__main__":
    raise SystemExit(run("B7 估计层与公司行动", main))
