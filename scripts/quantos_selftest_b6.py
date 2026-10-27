"""
B6 验收：风险预算（T-601）、策略灰度（T-602）、AI 研究通道（T-603）
==================================================================

每条断言都对应一个具体的错误做法，不是为了凑数量。四个部分：

  A 风险预算：Euler 恒等式、同向不重复、对锁告警、ERC 收敛、预算→额度
  B 灰度路由：版本分流、不归一化、reduce_only 不缩放、回滚、自动化只降不升
  C AI 通道：生产字段硬拒（含嵌套）、证据强制、人签名、AST 隔离
  D 端到端：三者串起来跑一遍，确认拼装后不变量仍成立
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quantos_core.capital_allocator import (
    NettingInput, StrategyPolicy, allocate, build_order_intents, net_targets,
)
from quantos_core.canary import (
    AutomationForbidden, CanaryError, CanaryRouter, RolloutState, UnknownVersion,
)
from quantos_core.contracts import (
    AllocationMode, D, OrderType, Side, StrategyIntent, StrategyRunState, q,
    utcnow,
)
from quantos_core.risk_budget import (
    RiskModel, RiskModelIncomplete, apply_budget_plan, budget_drift,
    budget_to_quotas, budgets_from_policies, equal_risk_weights, portfolio_risk,
)
from quantos_core import ai_research as AIR

from _asserts import (
    eq, falsy, guard, near, ne, raises, run, section, truth,
)

NOW = datetime(2026, 10, 27, 2, 0, tzinfo=timezone.utc)
UTC = timezone.utc
DAY = "2026-10-27"


# --------------------------------------------------------------------------- #
#  工具
# --------------------------------------------------------------------------- #

def mk_intent(sid: str, version: str, symbol: str = "BTC/USDT:USDT",
              weight: str = "1.0", *, ro: bool = False, n=None) -> StrategyIntent:
    n = n if n is not None else [0]
    n[0] += 1
    return StrategyIntent(
        intent_id=f"oi-{sid}-{n[0]}", strategy_id=sid, strategy_version=version,
        symbol=symbol, venue="binanceusdm", target_weight=D(weight),
        confidence=D("0.5"), reason="b6-selftest",
        signal_ids=(f"sig-{n[0]}",), as_of=NOW, ts=NOW,
        expires_at=NOW + timedelta(minutes=5), reduce_only_hint=ro)


def model_2(vol_a="0.60", vol_b="0.70", rho="0.50") -> RiskModel:
    return RiskModel.uniform({"BTC/USDT:USDT": D(vol_a), "ETH/USDT:USDT": D(vol_b)},
                             D(rho), label=f"常数 ρ={rho}")


# --------------------------------------------------------------------------- #
#  A. 风险预算
# --------------------------------------------------------------------------- #

def part_a() -> None:
    section("A1 风险模型：不猜波动率、不猜相关性")

    m_single = RiskModel.diagonal({"BTC/USDT:USDT": D("0.60")})
    raises(RiskModelIncomplete,
           lambda: portfolio_risk({"ETH/USDT:USDT": D("1000")}, m_single),
           "缺 σ 直接抛错（不拿 15% 之类的默认值顶上）")

    raw = RiskModel(vols={"BTC/USDT:USDT": D("0.6"), "ETH/USDT:USDT": D("0.7")},
                    label="未声明相关")
    raises(RiskModelIncomplete,
           lambda: raw.rho("BTC/USDT:USDT", "ETH/USDT:USDT"),
           "缺 ρ 直接抛错（默认 0 会系统性低估同向风险）")
    raises(RiskModelIncomplete,
           lambda: RiskModel(vols={"X": D("0.3"), "Y": D("0.4")}).rho("X", "Y"),
           "默认构造不会悄悄给一个 ρ：跨标的相关必须显式声明")

    truth(model_2().rho("BTC/USDT:USDT", "ETH/USDT:USDT") == D("0.5"),
          "uniform 模型把常数相关显式写进 label，可追溯")
    raises(RiskModelIncomplete,
           lambda: RiskModel(vols={"A": D("0.5")}, corr={("A", "B"): D("1.5")}).rho("A", "B"),
           "相关系数越界 → 拒绝（ρ>1 会让方差为负）")

    section("A2 Euler 分解：Σ标的贡献 ≡ 组合波动率")

    r1 = portfolio_risk({"BTC/USDT:USDT": D("100000")}, m_single)
    eq(r1.sigma, D("60000"), "单标的下 σ 精确等于 |名义|×σ")
    truth(r1.closes, "单标的下 Σ贡献 = σ（Euler 恒等式）")

    m2 = model_2()
    net2 = {"BTC/USDT:USDT": D("100000"), "ETH/USDT:USDT": D("100000")}
    per2 = {"s1": {"BTC/USDT:USDT": D("100000")},
            "s2": {"ETH/USDT:USDT": D("100000")}}
    r2 = portfolio_risk(net2, m2, per_strategy=per2, nav=D("1000000"),
                        risk_budget_pct=D("0.15"))
    truth(r2.closes, "两标的下 Σ贡献 = σ")
    truth(r2.attribution_closes, "Σ策略贡献 = σ（按净额占比切分不丢钱）")
    truth(r2.standalone_sum > r2.sigma,
          f"各自独立波动之和 {q(r2.standalone_sum, 2)} > 组合 σ {q(r2.sigma, 2)}"
          f"（分散化必然降低波动，反了说明协方差矩阵不正定）")
    near(r2.sigma_pct_nav, r2.sigma / D("1000000"), D("1e-12"),
         "σ 占 NAV 比例口径正确")
    truth(r2.used_budget_pct is not None and Decimal(0) < r2.used_budget_pct <= 1,
          "风险预算占用率落在 (0,1]")
    # 朴素相加（不扣相关）会高估：这是"重复计算"的具体形态
    naive = D("100000") * D("0.6") + D("100000") * D("0.7")
    truth(r2.sigma < naive,
          f"朴素相加 {q(naive, 2)} 高于真实 σ {q(r2.sigma, 2)}："
          f"不做相关处理就是重复计算共同因子")

    section("A3 同向不重复计算（T-601 的 DoD）")

    per3 = {"s1": {"BTC/USDT:USDT": D("100000")},
            "s2": {"BTC/USDT:USDT": D("100000")}}
    r3 = portfolio_risk({"BTC/USDT:USDT": D("200000")}, m_single, per_strategy=per3)
    eq(r3.sigma, D("120000"), "两策略同向各 10 万 → 组合风险是 20 万的风险（一次）")
    eq(r3.by_strategy["s1"], D("60000"), "s1 承担一半")
    eq(r3.by_strategy["s2"], D("60000"), "s2 承担一半")
    eq(r3.attribution_sum, D("120000"), "两者之和 = 组合风险，不是两倍")
    truth(r3.attribution_sum < D("240000"),
          "若按各自独立算会得到 24 万（两倍）—— 现在没有")
    eq(len(r3.by_symbol), 1, "同一标的只出现一个风险条目（净额口径）")

    # 关键发现：同一标的下"各自算再相加"与净额算法**数值完全一样**，
    # 因为风险对同一个标的是线性的。也就是说单一标的场景根本测不出重复计算，
    # 这个 bug 只会在"不同标的但相关"时暴露（见 A2 的朴素相加对比）。
    single = portfolio_risk({"BTC/USDT:USDT": D("100000")}, m_single).sigma
    near(single * 2, r3.sigma, D("0.01"),
         "同一标的下两法同值：线性使得单标的场景测不出重复计算（这也是该 bug 能长期潜伏的原因）")

    section("A4 对锁：净额为 0 也要喊出来")

    per4 = {"s1": {"BTC/USDT:USDT": D("100000")},
            "s2": {"BTC/USDT:USDT": D("-100000")}}
    r4 = portfolio_risk({"BTC/USDT:USDT": D("0")}, m_single, per_strategy=per4)
    eq(r4.sigma, Decimal(0), "完全对锁 → 模型认为价格风险为 0")
    eq(r4.hedged, ("BTC/USDT:USDT",), "对锁标的被标记出来")
    truth(len(r4.notes) >= 1, "对锁必须带告警（模型说没风险，真风险在基差）")
    truth(any("对冲" in n for n in r4.notes), "告警文案说明净额被对冲掉了")
    truth(any("基差" in n for n in r4.notes), "告警文案点出未建模的那部分风险")

    section("A5 等风险贡献（块相关下 1/σ 会失效）")

    vols = {"A": D("0.5"), "B": D("0.5"), "C": D("0.5")}
    block = RiskModel(vols=vols,
                      corr={("A", "B"): D("0.95"), ("A", "C"): D("0.1"),
                            ("B", "C"): D("0.1")}, label="块相关")
    erc = equal_risk_weights(block, ["A", "B", "C"])
    truth(erc.converged, f"等风险贡献收敛（{erc.iterations} 次迭代）")
    truth(erc.max_deviation < D("0.000001"),
          f"最大偏差 {q(erc.max_deviation, 10)} 远小于 1e-6")
    eq(q(erc.weights["A"], 6), q(erc.weights["B"], 6), "A/B 对称 → 权重相等")
    truth(erc.weights["C"] > erc.weights["A"],
          "与 A/B 弱相关的 C 拿到更多权重（正是 1/σ 给不出的）")
    near(sum(erc.weights.values(), Decimal(0)), Decimal(1), D("0.000001"),
         "权重归一到 1")

    r_erc = portfolio_risk({k: v * D("1000000") for k, v in erc.weights.items()}, block)
    eq(len({q(s.pct, 6) for s in r_erc.by_symbol}), 1,
       "三个标的的风险占比完全相同（这才是等风险）")

    inv = {k: Decimal(1) / v for k, v in vols.items()}
    tot = sum(inv.values(), Decimal(0))
    inv = {k: v / tot for k, v in inv.items()}
    r_inv = portfolio_risk({k: v * D("1000000") for k, v in inv.items()}, block)
    pct = {s.symbol: q(s.pct, 4) for s in r_inv.by_symbol}
    truth(pct["A"] > pct["C"],
          f"1/σ 方案把风险压在 A/B 这一簇上（A {pct['A']} vs C {pct['C']}）")
    truth(r_inv.sigma > r_erc.sigma,
          f"等风险组合的波动 {q(r_erc.sigma, 2)} 低于 1/σ 组合 "
          f"{q(r_inv.sigma, 2)}：同样的预算承担更少风险")

    diag = equal_risk_weights(RiskModel.diagonal({"A": D("0.2"), "B": D("0.4"),
                                                  "C": D("0.8")}), ["A", "B", "C"])
    truth(abs(diag.weights["A"] - D("1") / D("0.2") /
              sum((Decimal(1) / v for v in (D("0.2"), D("0.4"), D("0.8"))),
                  Decimal(0))) < D("0.000001"),
          "零相关特例：等风险贡献退化为倒数波动率（数学自检）")

    raises(Exception,
           lambda: equal_risk_weights(RiskModel.diagonal({"A": D("0.2")}), []),
           "空标的列表 → 拒绝（否则会返回一个空权重集）")
    raises(RiskModelIncomplete,
           lambda: equal_risk_weights(RiskModel.diagonal({"A": D("0.2")}),
                                      ["A", "B"]),
           "缺 σ 的标的不能混进等风险求解")

    section("A6 风险预算 → 额度")

    plan = budget_to_quotas({"s1": D("0.5"), "s2": D("0.5")},
                            {"s1": D("0.2"), "s2": D("0.4")},
                            risk_target_pct=D("0.10"))
    eq(plan.quotas["s1"], D("0.25"), "等预算 σ=20% 的策略配额 25%")
    eq(plan.quotas["s2"], D("0.125"), "等预算 σ=40% 的策略配额只有一半")
    truth(plan.quotas["s1"] > plan.quotas["s2"],
          "高波动策略拿到更少额度（按风险分配，不是按钱分配）")
    eq(plan.total, D("0.375"), "总会话 37.5%")
    eq(plan.cash_pct, D("0.625"), "剩余现金比例 = max_total - total")

    big = budget_to_quotas({"s1": D("1"), "s2": D("1")},
                           {"s1": D("0.2"), "s2": D("0.4")},
                           risk_target_pct=D("0.20"))
    truth(big.scale < 1, f"预算超上限时被压缩（scale={q(big.scale, 4)}）")
    truth(big.total <= D("1"), "压缩后总额不超过 100%（不加杠杆）")
    truth(any("压缩" in n for n in big.notes), "压缩这件事必须写进 notes")

    zero = budget_to_quotas({"s1": Decimal(0)}, {"s1": D("0.3")},
                           risk_target_pct=D("0.1"))
    eq(zero.quotas["s1"], Decimal(0), "零预算就是不给配额")
    truth(any("不给配额" in n for n in zero.notes), "零预算要显式说明，不是静默 0")

    raises(RiskModelIncomplete,
           lambda: budget_to_quotas({"s1": D("0.5")}, {}, risk_target_pct=D("0.1")),
           "缺 σ 无法换算 → 抛错")
    raises(Exception,
           lambda: budget_to_quotas({"s1": D("0.5")}, {"s1": D("0.2")},
                                    risk_target_pct=D("0")),
           "风险目标为 0 → 拒绝")
    raises(Exception,
           lambda: budget_to_quotas({"s1": D("0.5")}, {"s1": D("0.2")},
                                    risk_target_pct=D("0.1"), max_total=D("1.5")),
           "max_total > 1 → 拒绝（不允许预设杠杆）")

    section("A7 与 capital_allocator 拼装")

    pols = [StrategyPolicy(strategy_id="s1", venue="binanceusdm",
                           mode=AllocationMode.FIXED_QUOTA,
                           quota_pct=D("0.2"), risk_budget_pct=D("0.5")),
            StrategyPolicy(strategy_id="s2", venue="binanceusdm",
                           quota_pct=D("0.2"), risk_budget_pct=D("0.5"))]
    budgets = budgets_from_policies(pols)
    eq(budgets, {"s1": D("0.5"), "s2": D("0.5")}, "从策略配置取风险预算")
    plan2 = budget_to_quotas(budgets, {"s1": D("0.2"), "s2": D("0.4")},
                             risk_target_pct=D("0.10"))
    patched = apply_budget_plan(pols, plan2)
    eq(patched[0].quota_pct, D("0.25"), "预算计划写回 s1 的 quota_pct")
    eq(patched[1].quota_pct, D("0.125"), "s2 配额同步更新")
    eq(patched[1].risk_budget_pct, D("0.5"),
       "写回时不改 risk_budget_pct（预算是输入，配额是结果）")

    all_zero = [StrategyPolicy(strategy_id="x", venue="v", risk_budget_pct=Decimal(0))]
    eq(budgets_from_policies(all_zero), {}, "全 0 预算时返回空 → 调用方退回固定配额口径")

    drifts = budget_drift({"s1": D("0.7"), "s2": D("0.2")},
                          {"s1": D("0.5"), "s2": D("0.2")})
    truth(any(d.strategy_id == "s1" and d.over for d in drifts), "s1 风险占比超预算被识别")
    falsy(any(d.strategy_id == "s2" and d.over for d in drifts), "s2 未超预算")


# --------------------------------------------------------------------------- #
#  B. 灰度路由
# --------------------------------------------------------------------------- #

def part_b() -> None:
    section("B1 灰度开通：必须人工、必须登记基线")

    r = CanaryRouter(clock=lambda: NOW)
    raises(CanaryError,
           lambda: r.open_canary("ghost", "v2", actor="human:owner"),
           "未登记稳定版就开灰度 → 拒绝（没有对照基线）")

    r.register_stable("mom", "v1", venue="binanceusdm")
    eq(r.get("mom").shares(), {"v1": Decimal(1)}, "稳定版独占 100%")
    eq(r.get("mom").state, RolloutState.SINGLE, "初始状态 SINGLE")

    raises(AutomationForbidden,
           lambda: r.open_canary("mom", "v2", weight=D("0.1"), actor="ai:researcher"),
           "AI 不能开灰度（这会把未验证代码接进真钱）")
    truth(len(r.journal()) >= 2, "被拒的操作同样留痕")
    eq(r.journal()[-1]["result"], "rejected", "拒绝记录标记 result=rejected")

    r.open_canary("mom", "v2", weight=D("0.1"), actor="human:owner",
                  reason="20 日窗口回测优于 60 日")
    sh = r.get("mom").shares()
    eq(sh["v1"], D("0.9"), "稳定版份额 0.9")
    eq(sh["v2"], D("0.1"), "灰度版份额 0.1")
    eq(sum(sh.values(), Decimal(0)), Decimal(1), "份额之和 = 1")

    raises(CanaryError,
           lambda: r.open_canary("mom", "v3", actor="human:owner"),
           "已有一个灰度版时不能同时开第二个")
    section("B2 路由：按版本缩权，且不重新归一化")

    outs, reps = r.route([mk_intent("mom", "v1"), mk_intent("mom", "v2")])
    eq(len(outs), 2, "两个版本的意图都通过")
    eq(reps[0].weight_by_symbol["BTC/USDT:USDT"], D("1.0"),
       "两版各发满权重 → 路由后合计仍是 1.0（不是 2.0）")
    eq({i.strategy_version: i.target_weight for i in outs},
       {"v1": D("0.9"), "v2": D("0.1")}, "份额分别落到两个版本上")

    outs2, reps2 = r.route([mk_intent("mom", "v2")])
    eq(reps2[0].weight_by_symbol["BTC/USDT:USDT"], D("0.1"),
       "只有灰度版有信号 → 合计 0.1，**不**把稳定版的 0.9 补给它")
    truth(reps2[0].weight_by_symbol["BTC/USDT:USDT"] < D("1"),
          "不重新归一化：灰度 10% 就是 10% 的风险，不是 100%")

    outs3, reps3 = r.route([mk_intent("mom", "v1")])
    eq(reps3[0].weight_by_symbol["BTC/USDT:USDT"], D("0.9"),
       "只有稳定版有信号 → 0.9，灰度份额不会回流稳定版")

    raises(UnknownVersion,
           lambda: r.route([mk_intent("mom", "v9")]),
           "未知版本 → 抛错（默默放行或默默丢弃都是事故）")

    section("B3 reduce_only：缩放会把清仓变成半清仓")

    outs4, reps4 = r.route([mk_intent("mom", "v1", weight="-1.0", ro=True),
                            mk_intent("mom", "v2", weight="-1.0", ro=True)])
    eq(len(outs4), 1, "两个版本都喊清仓 → 只保留一份（叠加会变两倍减仓）")
    eq(outs4[0].target_weight, D("-1.0"), "清仓意图不缩放，原样 -1.0")
    truth(any("不缩放" in n for n in reps4[0].notes), "notes 说明为何不缩放")
    truth(any("重复" in n for n in reps4[0].notes), "notes 说明重复意图被合并")

    outs5, _ = r.route([mk_intent("mom", "v1", weight="-0.5", ro=True),
                        mk_intent("mom", "v2", weight="-0.8", ro=True)])
    eq(len(outs5), 1, "重复 reduce_only 合并")
    eq(outs5[0].target_weight, D("-0.8"), "保留幅度最大的一份")

    # 灰度权重为 0 的版本（回滚后）不该再产生意图
    r_zero = CanaryRouter(clock=lambda: NOW)
    r_zero.register_stable("z", "v1")
    r_zero.open_canary("z", "v2", weight=D("0.1"), actor="human:o")
    r_zero.rollback("z", reason="成交异常", actor="ai:guard")
    outs6, reps6 = r_zero.route([mk_intent("z", "v2")])
    eq(len(outs6), 0, "回滚后灰度版的意图被丢弃")
    eq(reps6[0].dropped, 1, "丢弃数被记录（不是静默消失）")

    section("B4 权重调整：降可以、升必须人工")

    raises(AutomationForbidden,
           lambda: r.set_weight("mom", D("0.5"), actor="ai:guard"),
           "AI 提灰度权重 → 拒绝")
    eq(r.get("mom").weight, D("0.1"), "被拒后权重不变（不是先改再拒）")
    r.set_weight("mom", D("0.05"), actor="ai:guard", reason="波动异常缩小敞口")
    eq(r.get("mom").weight, D("0.05"), "AI 降权重允许（安全方向免摩擦）")
    r.set_weight("mom", D("0.0001"), actor="ai:guard")
    eq(r.get("mom").weight, Decimal(0), "低于下限直接归零，不留 0.01% 残渣")
    eq(r.get("mom").state, RolloutState.ROLLED_BACK, "权重归零即回滚状态")

    section("B5 回滚与扶正")

    r2 = CanaryRouter(clock=lambda: NOW)
    r2.register_stable("t", "v1")
    r2.open_canary("t", "v2", weight=D("0.2"), actor="human:owner")
    c = r2.rollback("t", reason="滑点异常放大", actor="ai:guard")
    eq(c.weight, Decimal(0), "回滚后灰度权重为 0")
    eq(c.state, RolloutState.ROLLED_BACK, "状态置 ROLLED_BACK")
    eq(r2.get("t").shares(), {"v1": Decimal(1)}, "回滚后稳定版独占 100%")
    ne(r2.get("t").canary_version, None, "灰度版本号留档（复盘要用）")
    raises(CanaryError, lambda: r2.rollback("t", reason="再滚一次", actor="human:o"),
           "已回滚的队列不能重复回滚")

    r3 = CanaryRouter(clock=lambda: NOW)
    r3.register_stable("u", "v1")
    r3.open_canary("u", "v2", weight=D("0.1"), actor="human:owner")
    raises(AutomationForbidden,
           lambda: r3.promote("u", actor="human:owner"),
           "10% 直接扶正 → 拒绝（10% 没出事不代表全额没出事）")
    raises(AutomationForbidden,
           lambda: r3.promote("u", actor="ai:agent"),
           "AI 扶正 → 拒绝（AI 不能自己上线）")
    r3.set_weight("u", D("1"), actor="human:owner")
    eq(r3.get("u").shares(), {"v1": Decimal(0), "v2": Decimal(1)},
       "权重跑到 1 时稳定版份额为 0")
    c3 = r3.promote("u", actor="human:owner", reason="两周观察无异常")
    eq(c3.stable_version, "v2", "扶正后稳定版变成 v2")
    eq(c3.state, RolloutState.PROMOTED, "状态 PROMOTED")
    eq(r3.get("u").shares(), {"v2": Decimal(1)}, "扶正后只剩一个版本")

    r4 = CanaryRouter(clock=lambda: NOW)
    r4.register_stable("w", "v1")
    r4.open_canary("w", "v2", weight=D("0.3"), actor="human:o")
    raises(CanaryError, lambda: r4.register_stable("w", "v9"),
           "灰度期间不能直接换稳定版（先回滚或扶正）")

    section("B6 自动化只能降：任何路径都不许提")

    r5 = CanaryRouter(clock=lambda: NOW)
    r5.register_stable("s", "v1")
    r5.open_canary("s", "v2", weight=D("0.4"), actor="human:o")
    eq(r5.auto_adjust("s", "ok")[1], D("0.4"), "判定健康 → 不变（不自动加仓）")
    eq(r5.auto_adjust("s", "degrade")[1], D("0.2"), "降级 → 减半")
    eq(r5.auto_adjust("s", "degrade")[1], D("0.1"), "再降 → 0.1")
    eq(r5.auto_adjust("s", "abort")[1], Decimal(0), "中止 → 归零")
    eq(r5.get("s").state, RolloutState.ROLLED_BACK, "中止后状态回滚")
    eq(r5.auto_adjust("s", "degrade")[1], Decimal(0),
       "已回滚的队列再调是幂等空操作（自动化按周期反复调用，抛错只会制造假告警）")
    raises(CanaryError, lambda: r5.auto_adjust("s", "surge"),
           "未知判定值 → 拒绝（判定值校验在状态判断之前，否则拼错字会被静默放过）")
    raises(CanaryError, lambda: r5.auto_adjust("nobody", "ok"),
           "未登记的策略 → 拒绝（不能对不存在的队列做自动化决策）")

    # 单调性：无论怎么调，权重序列不增
    r6 = CanaryRouter(clock=lambda: NOW)
    r6.register_stable("m", "v1")
    r6.open_canary("m", "v2", weight=D("0.8"), actor="human:o")
    seq = [r6.get("m").weight]
    for verdict in ("ok", "degrade", "ok", "degrade", "degrade", "ok"):
        try:
            seq.append(r6.auto_adjust("m", verdict)[1])
        except CanaryError:
            break
    truth(all(seq[i + 1] <= seq[i] for i in range(len(seq) - 1)),
          f"权重序列单调不增 {[str(x) for x in seq]}")
    truth(all(v <= D("0.8") for v in seq), "任何自动化路径都不超过初始权重")

    section("B7 不在路由表里的策略原样通过")

    r7 = CanaryRouter(clock=lambda: NOW)
    outs7, reps7 = r7.route([mk_intent("free", "v1", weight="0.6")])
    eq(len(outs7), 1, "未纳管策略照常交易")
    eq(outs7[0].target_weight, D("0.6"), "权重不变（不做无依据的缩放）")
    eq(r7.get("free"), None, "路由不会凭空给未纳管策略登记队列")

    truth(r7.digest() != "", "空路由表也能算出 digest（便于对账）")


# --------------------------------------------------------------------------- #
#  C. AI 研究通道
# --------------------------------------------------------------------------- #

LONG_SUMMARY = "2023-01 至 2026-09 回测：20 日回看窗口的年化优于 60 日窗口 2.1pp"


def part_c() -> None:
    section("C1 静态隔离：模块根本拿不到生产对象")

    rep = AIR.assert_isolated()
    eq(rep.bad_imports, (), "ai_research 只 import 白名单内的模块")
    eq(rep.forbidden_methods, (), "模块内不存在 apply/promote/set_quota 之类的方法")
    truth(rep.ok, "隔离体检通过（没有生产写入路径）")

    tmpdir = tempfile.mkdtemp(prefix="b6-ai-")
    box = AIR.ResearchInbox(path=Path(tmpdir) / "inbox.jsonl", clock=lambda: NOW)

    eq([m for m in AIR.FORBIDDEN_METHODS if hasattr(box, m)], [],
       "收件箱实例上没有任何生产写入方法")
    falsy(hasattr(box, "apply"), "没有 apply()")
    falsy(hasattr(box, "promote"), "没有 promote()")

    section("C2 生产字段硬拒（含嵌套）")

    def submit(**kw):
        return box.submit(author=kw.pop("author", "ai:researcher"),
                          kind=kw.pop("kind", AIR.ResearchKind.REPORT),
                          strategy_id=kw.pop("strategy_id", "mom"),
                          summary=kw.pop("summary", LONG_SUMMARY), **kw)

    e1 = raises(AIR.ResearchRejected,
                lambda: submit(payload={"quota_pct": "0.4"}),
                "顶层 quota_pct → 拒绝")
    truth(e1 is not None and "quota_pct" in str(e1), "拒绝理由点名字段")

    e2 = raises(AIR.ResearchRejected,
                lambda: submit(payload={"suggestions": [{"changes": {"armed": True}}]}),
                "嵌套两层的 armed → 拒绝（递归检查）")
    truth(e2 is not None and "armed" in str(e2), "拒绝理由带上完整路径")
    truth(e2 is not None and "suggestions" in str(e2), "路径能定位到嵌套位置")

    for key in ("canary_weight", "permission_level", "account_id", "api_key"):
        raises(AIR.ResearchRejected,
               lambda k=key: submit(payload={k: "x"}),
               f"{key} 同样被拒（黑名单不是只拦一两个）")

    truth(submit(payload={"lookback": 20}).submission_id.startswith("rs-"),
          "普通研究字段可以入箱")
    truth(len(box) == 1, "只有合法提交进了收件箱")

    section("C3 候选参数必须有可复现证据")

    raises(AIR.ResearchRejected,
           lambda: submit(kind=AIR.ResearchKind.CANDIDATE_PARAMS,
                          payload={"params": {"lookback": 15}}),
           "候选参数无证据 → 拒绝")
    raises(AIR.ResearchRejected,
           lambda: submit(kind=AIR.ResearchKind.CANDIDATE_PARAMS,
                          payload={"params": {"lookback": 15}},
                          evidence=("我觉得这样更好",)),
           "证据没有可追溯前缀 → 拒绝")
    raises(AIR.ResearchRejected,
           lambda: submit(kind=AIR.ResearchKind.CANDIDATE_PARAMS,
                          summary="把回看窗口从 60 日改到 15 日效果更好一些",
                          evidence=("bt:2026-10-25-mom",)),
           "候选参数缺 payload['params'] → 拒绝")
    ok_sub = submit(kind=AIR.ResearchKind.CANDIDATE_PARAMS,
                    payload={"params": {"lookback": 20, "top_n": 3}},
                    evidence=("bt:2026-10-25-mom-lookback",))
    truth(ok_sub.submission_id != "", "带证据的候选参数正常入箱")

    raises(AIR.ResearchRejected,
           lambda: submit(summary="好"),
           "summary 过短 → 拒绝（一句话结论无法复核）")

    section("C4 参数漂移闸")

    raises(AIR.ResearchRejected,
           lambda: submit(kind=AIR.ResearchKind.CANDIDATE_PARAMS,
                          summary="把 top_n 从 1 提到 5 可以显著提高收益，回测已确认",
                          payload={"params": {"lookback": 20, "top_n": 5}},
                          evidence=("bt:x",),
                          baseline_params={"lookback": 20, "top_n": 1}),
           "参数相对变化 400% → 拒绝（量级跳变通常是单位或杠杆搞错）")
    truth(submit(kind=AIR.ResearchKind.CANDIDATE_PARAMS,
                 summary="把 top_n 从 5 微调到 6，回测显示夏普略有提升",
                 payload={"params": {"lookback": 20, "top_n": 6}},
                 evidence=("bt:y",),
                 baseline_params={"lookback": 20, "top_n": 5}).submission_id != "",
          "20% 以内的微调允许入箱")

    drifts = AIR.parameter_drift({"a": 0.5, "b": 3, "c": "x"}, {"a": 0.4, "b": 0})
    by_key = {d.key: d for d in drifts}
    eq(set(by_key), {"a", "b"}, "非数值键被跳过，数值键逐项出现")
    near(by_key["a"].rel, D("0.25"), D("1e-9"), "相对变化口径 = (新-旧)/|旧|")
    truth(by_key["b"].rel is None and by_key["b"].blocking,
          "基准为 0 时无法算相对变化 → 一律视为阻断（0→3 比 4→3 危险）")

    section("C5 复核与草案：必须人签名")

    raises(AIR.ReviewRequired,
           lambda: box.review(ok_sub.submission_id, reviewer="ai:reviewer",
                              verdict=AIR.Verdict.ACCEPTED),
           "AI 复核 AI → 拒绝（等于没有复核）")
    raises(AIR.ReviewRequired,
           lambda: box.prepare_draft(ok_sub.submission_id, version="v2",
                                     prepared_by="human:owner"),
           "未复核就出草案 → 拒绝")
    raises(AIR.ResearchRejected,
           lambda: box.review(ok_sub.submission_id, reviewer="human:owner",
                              verdict=AIR.Verdict.REJECTED),
           "拒绝不写理由 → 拒绝（AI 下一轮还会提同样的东西）")

    box.review(ok_sub.submission_id, reviewer="human:owner",
               verdict=AIR.Verdict.ACCEPTED, note="区间够长，先灰度 10%")
    eq(box.verdict(ok_sub.submission_id), AIR.Verdict.ACCEPTED, "判定落库")
    eq(len(box.pending()), len(box) - 1, "已复核的不再出现在待办里")
    eq(len(box.accepted()), 1, "已通过的计数正确")

    raises(AIR.ReviewRequired,
           lambda: box.prepare_draft(ok_sub.submission_id, version="v2",
                                     prepared_by="ai:agent"),
           "AI 出草案 → 拒绝（草案意味着有人认下这份参数）")
    raises(AIR.ResearchRejected,
           lambda: box.prepare_draft(ok_sub.submission_id, version="",
                                     prepared_by="human:owner"),
           "草案缺版本号 → 拒绝（没版本就没法灰度也没法回滚）")

    draft = box.prepare_draft(ok_sub.submission_id, version="v2",
                              prepared_by="human:owner")
    eq(draft.strategy_id, "mom", "草案带上策略 ID")
    eq(draft.params, {"lookback": 20, "top_n": 3}, "草案参数原样带出")
    truth("human:owner" in draft.describe() and "v2" in draft.describe(),
          "describe() 一行就能看全人和版本")
    falsy(hasattr(draft, "apply"), "草案本身也没有 apply()（它只是数据）")

    section("C6 落盘与幂等")

    path = Path(tmpdir) / "inbox.jsonl"
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    truth(len(lines) >= 4, f"JSONL 已落盘 {len(lines)} 行")
    truth(all(l.startswith("{") for l in lines), "每行都是独立 JSON 对象")
    raises(AIR.ResearchRejected,
           lambda: submit(submission_id=ok_sub.submission_id),
           "重复 submission_id → 拒绝（幂等键不能撞）")
    truth(box.digest() != "", "收件箱有 digest 可对账")


# --------------------------------------------------------------------------- #
#  D. 端到端拼装
# --------------------------------------------------------------------------- #

def part_d() -> None:
    section("D1 三块拼起来：风险预算 → 净额 → 灰度 → 风险再计算")

    # 1) 两个策略，按风险预算分额度
    policies = [
        StrategyPolicy(strategy_id="mom", venue="binanceusdm", quota_pct=D("0.2"),
                       risk_budget_pct=D("0.5")),
        StrategyPolicy(strategy_id="carry", venue="binanceusdm", quota_pct=D("0.2"),
                       risk_budget_pct=D("0.5")),
    ]
    vols = {"mom": D("0.25"), "carry": D("0.50")}
    plan = budget_to_quotas(budgets_from_policies(policies), vols,
                            risk_target_pct=D("0.10"))
    eq(plan.quotas["mom"], D("0.20"), "mom（σ25%）配额 20%")
    eq(plan.quotas["carry"], D("0.10"), "carry（σ50%）配额 10%")
    patched = {p.strategy_id: p for p in apply_budget_plan(policies, plan)}
    eq(patched["carry"].quota_pct, D("0.10"), "写回后 carry 配额 10%")

    nav = D("1000000")
    allocs = {sid: allocate(p, nav) for sid, p in patched.items()}
    eq(allocs["mom"].quota_usd, D("200000"), "mom 拿到 20 万额度")
    eq(allocs["carry"].quota_usd, D("100000"), "carry 拿到 10 万额度")

    # 2) 灰度：mom 的 v2 只接 10%
    router = CanaryRouter(clock=lambda: NOW)
    router.register_stable("mom", "v1")
    router.register_stable("carry", "v1")
    router.open_canary("mom", "v2", weight=D("0.1"), actor="human:owner")

    intents = [
        mk_intent("mom", "v1", "BTC/USDT:USDT", "1.0"),
        mk_intent("mom", "v2", "BTC/USDT:USDT", "1.0"),
        mk_intent("carry", "v1", "ETH/USDT:USDT", "1.0"),
    ]
    routed, reports = router.route(intents)
    eq(len(routed), 3, "三条意图全部通过")

    inp = NettingInput(
        intents=routed, allocations=allocs,
        policies=patched,
        prices={("binanceusdm", "BTC/USDT:USDT"): D("30000"),
                ("binanceusdm", "ETH/USDT:USDT"): D("2000")},
        current_notional={}, inflight_notional={},
        strategy_states={"mom": StrategyRunState.ACTIVE,
                         "carry": StrategyRunState.ACTIVE},
        now=NOW, min_notional={"binanceusdm": Decimal(0)},
    )
    targets = {t.symbol: t for t in net_targets(inp)}
    # mom 的 v1+v2 合计 1.0 → 目标名义 = 额度 × 1.0
    eq(targets["BTC/USDT:USDT"].net_target_notional, D("200000"),
       "v1+v2 合计 1.0 → BTC 目标名义 = mom 额度 20 万（不是 22 万，也不是 40 万）")
    eq(targets["ETH/USDT:USDT"].net_target_notional, D("100000"),
       "carry 目标名义 = 10 万")
    truth(router.get("mom").state is RolloutState.CANARY,
          "灰度状态在拼装后依然是 CANARY")

    # 3) 用实际目标算风险，验证预算大致被遵守
    m = RiskModel.uniform({"BTC/USDT:USDT": D("0.6"), "ETH/USDT:USDT": D("0.7")},
                          D("0.4"), label="端到端")
    per = {"mom": {"BTC/USDT:USDT": targets["BTC/USDT:USDT"].net_target_notional},
           "carry": {"ETH/USDT:USDT": targets["ETH/USDT:USDT"].net_target_notional}}
    risk = portfolio_risk(
        {"BTC/USDT:USDT": targets["BTC/USDT:USDT"].net_target_notional,
         "ETH/USDT:USDT": targets["ETH/USDT:USDT"].net_target_notional},
        m, per_strategy=per, nav=nav, risk_budget_pct=D("0.10"))
    truth(risk.closes, "端到端：Σ标的贡献 = σ")
    truth(risk.attribution_closes, "端到端：Σ策略贡献 = σ")
    truth(risk.sigma_pct_nav is not None and risk.sigma_pct_nav < D("0.30"),
          f"组合波动占 NAV {q(risk.sigma_pct_nav * 100, 2)}%，没有因为灰度而翻倍")
    eq(set(risk.by_strategy), {"mom", "carry"},
       "两个策略的风险都被归因（灰度版不影响归属）")

    section("D2 回滚后风险确实降下来")

    router.rollback("mom", reason="端到端演练", actor="ai:guard")
    routed2, _ = router.route([mk_intent("mom", "v1", "BTC/USDT:USDT", "1.0"),
                               mk_intent("carry", "v1", "ETH/USDT:USDT", "1.0")])
    inp2 = NettingInput(
        intents=routed2, allocations=allocs, policies=patched,
        prices={("binanceusdm", "BTC/USDT:USDT"): D("30000"),
                ("binanceusdm", "ETH/USDT:USDT"): D("2000")},
        current_notional={}, inflight_notional={},
        strategy_states={"mom": StrategyRunState.ACTIVE,
                         "carry": StrategyRunState.ACTIVE},
        now=NOW, min_notional={"binanceusdm": Decimal(0)},
    )
    t2 = {t.symbol: t for t in net_targets(inp2)}
    eq(t2["BTC/USDT:USDT"].net_target_notional, D("200000"),
       "回滚后 mom 仍是满额度（回滚的是版本，不是策略）")
    truth(router.get("mom").canary_version == "v2", "灰度版本留档可查")

    section("D3 三块的不变量汇总")

    truth(risk.closes and risk.attribution_closes, "风险分解双重恒等式成立")
    truth(all(x.weight <= 1 for x in router.cohorts()), "所有队列权重不超过 1")
    truth(router.digest() != "", "路由审计可摘要")
    truth(AIR.assert_isolated().ok, "AI 通道隔离性在拼装后仍成立")


def main() -> None:
    part_a()
    part_b()
    part_c()
    part_d()


if __name__ == "__main__":
    raise SystemExit(run("B6 多策略与 AI 研究", main))
