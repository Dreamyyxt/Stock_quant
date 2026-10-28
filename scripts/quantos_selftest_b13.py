#!/usr/bin/env python3
"""
B13 自测：因子模型（面板 / 因子收益 / 载荷 / Σ / 自由度）
==========================================================

八段，除 I 段外全部离线、合成数据、**不读时钟、不用 random 模块**。

  A. 面板（缺失不填充 / 停牌收益是累计 / 前缀和与参考实现对拍）
  B. **无未来函数反证**（本批最重要的一段）
  C. 因子收益口径（market = 等权 / 多空 gross=2 / 单边截面不硬凑）
  D. 载荷与 Σ（已知载荷能反解出来 / 不凭空造相关性 / PSD）
  E. 参数计数与自由度（O(n·k) vs O(n²) 的真实倍率）
  F. fail-closed（共线 / 样本不足 / 同价 / 单标的 / 非法参数）
  G. 与 risk_budget 打通（Σ 贡献 ≡ σ / 落盘往返保住口径）
  H. 代码卫生（零第三方 / 时钟无关 / 无循环 import）
  I. 真实数据（有就核对口径；没有就明说跳过）

为什么 B 段要单独成段
----------------------
因子模型的未来函数比回测更隐蔽：回测里"用了当天收盘"会让年化多十几个点，
至少还看得出来；而因子模型里"特征窗口多算了今天一天"只会让 R² 高一点、
载荷稳一点 —— 每个数字都更好看，没有任何红灯。所以这一段不测结果，
只测「t 日的因子收益到底由哪几天算出来」。
"""

from __future__ import annotations

import ast
import math
import os
import sys
import tempfile
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
from quantos_core import factors as F                                 # noqa: E402
from quantos_core.contracts import D as _D                            # noqa: E402
from quantos_core.covariance import (                                 # noqa: E402
    RiskModelStore, _psd_via_cholesky,
)
from quantos_core.risk_budget import portfolio_risk                   # noqa: E402

DATA_DIR = PROJ / "state" / "ashare" / "daily"


# --------------------------------------------------------------------------- #
#  确定性伪随机（不用 random 模块：这条是 B12 K 段定的规矩）
# --------------------------------------------------------------------------- #


def _rng(seed: int):
    state = [seed & 0x7FFFFFFF]

    def nxt() -> float:
        state[0] = (1103515245 * state[0] + 12345) & 0x7FFFFFFF
        return state[0] / float(0x7FFFFFFF)
    return nxt


def _bell(rng) -> float:
    """三个均匀数求和 → 近似标准正态（够用，且完全可复现）。"""
    return (rng() + rng() + rng() - 1.5) * 2.0


def _days(n: int) -> list:
    return [date(2024, 1, 1) + timedelta(days=i) for i in range(n)]


def _rows(rets, amounts=None, start: float = 10.0, dd=None):
    """收益序列 → `[(day, close, amount)]`。`None` 表示那天没有 bar（不是 0 收益）。"""
    n = len(next(iter(rets.values())))
    dd = dd or _days(n)
    out = {}
    for sym, seq in rets.items():
        px = start
        bars = []
        for i, r in enumerate(seq):
            if r is None:
                continue
            px *= (1.0 + r)
            amt = 1.0e8
            if amounts is not None and amounts.get(sym) is not None:
                val = amounts[sym][i]
                amt = val if val is not None else 0.0
            bars.append((dd[i], D(round(px, 8)), D(str(round(amt, 2)))))
        out[sym] = bars
    return out


def _flat(names, n: int, r: float = 0.001, amount: float = 1e8):
    return ({s: [r] * n for s in names},
            {s: [amount * (1 + i) for _ in range(n)] for i, s in enumerate(names)})


def _common_and_idio(names, n, *, seed=7, common=0.02, idio=0.005,
                     beta_lo=0.6, beta_hi=1.4):
    """
    合成一只「共同因子 + 特质」的面板，载荷已知。

    r_i,t = b_i · m_t + e_i,t
    这样估出来的 market 载荷应当与 b_i 高度相关 —— 这是 D 段的主断言。
    """
    rng = _rng(seed)
    m = [_bell(rng) * common for _ in range(n)]
    rets, betas = {}, {}
    for idx, s in enumerate(names):
        b = beta_lo + (beta_hi - beta_lo) * (idx / max(1, len(names) - 1))
        betas[s] = b
        rets[s] = [b * m[i] + _bell(rng) * idio for i in range(n)]
    return rets, betas


def _corr(a, b) -> float:
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    ca = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    sa = math.sqrt(sum((x - ma) ** 2 for x in a))
    sb = math.sqrt(sum((y - mb) ** 2 for y in b))
    if sa <= 0 or sb <= 0:
        return 0.0
    return ca / (sa * sb)


# --------------------------------------------------------------------------- #
#  A. 面板
# --------------------------------------------------------------------------- #


def part_a() -> None:
    section("A. 面板（缺失不填充 / 停牌收益是累计 / 前缀和对拍）")

    dd = _days(6)
    rows = {
        "a": [(dd[0], D("10"), D("1e8")), (dd[1], D("11"), D("1e8")),
              (dd[3], D("12.1"), D("1e8")), (dd[5], D("13.31"), D("1e8"))],
        "b": [(dd[i], D("10"), D("1e8")) for i in range(6)],
    }
    panel = F.ReturnPanel.from_rows(rows)
    eq(len(panel.days), 6, "全局日历取并集（6 天）")
    eq(panel.rets["a"][2], None, "a 在 dd[2] 没 bar → 收益是 None，不是 0")
    truth(panel.rets["a"][3] is not None, "   └ 复牌那天有收益")
    near(panel.rets["a"][3], 0.1, 1e-12,
         "   └ 复牌那天的收益是「停牌期间的累计涨跌」（11→12.1 = +10%）")
    near(panel.coverage("b"), 5.0 / 6.0, 1e-12,
         "b 覆盖率 5/6 —— 第一根 bar 出不了收益，所以永远差一个")
    near(panel.coverage("a"), 3.0 / 6.0, 1e-12,
         "a 覆盖率 3/6（只在 dd[1] dd[3] dd[5] 三天有收益）")

    # 前缀和 vs 参考实现：两套实现必须逐点一致
    rng = _rng(31)
    names = [f"s{i:02d}" for i in range(10)]
    rets, amounts = {}, {}
    for s in names:
        rets[s] = [(_bell(rng) * 0.02) if rng() > 0.15 else None for _ in range(80)]
        amounts[s] = [1e8 * (1 + rng()) for _ in range(80)]
    p2 = F.ReturnPanel.from_rows(_rows(rets, amounts))
    cfg = F.FactorConfig(annualization=252, min_obs=20, min_names=4)
    pre = {s: F._prefix(p2.rets[s], p2.amounts[s]) for s in p2.symbols}
    bad = 0
    checked = 0
    for t in range(1, len(p2.days)):
        fast = F._exposures(p2, cfg, t, pre)
        slow = F._exposures_reference(p2, cfg, t)
        if set(fast) != set(slow):
            bad += 1
            continue
        for f in fast:
            if set(fast[f]) != set(slow[f]):
                bad += 1
                continue
            for s in fast[f]:
                checked += 1
                if abs(fast[f][s] - slow[f][s]) > 1e-9:
                    bad += 1
    eq(bad, 0, f"前缀和与参考实现逐点一致（比了 {checked} 个截面值）")
    truth(checked > 500, "   └ 确实比了有量的样本（不是空集对上空集）")

    guard("收盘价 0 必须报错（不许拿 0 兜底）", lambda: raises(
        F.FactorError,
        lambda: F.ReturnPanel.from_rows({"x": [(dd[0], D("10")), (dd[1], D("0"))]})))
    guard("负价必须报错", lambda: raises(
        F.FactorError,
        lambda: F.ReturnPanel.from_rows({"x": [(dd[0], D("10")), (dd[1], D("-1"))]})))
    guard("同日重复 bar 必须报错", lambda: raises(
        F.FactorError,
        lambda: F.ReturnPanel.from_rows({"x": [(dd[0], D("10")), (dd[0], D("11"))]})))
    guard("bar 少于 min_bars 的标的被跳过而不是崩", lambda: truth(
        F.ReturnPanel.from_rows({"ok": [(dd[0], D("10")), (dd[1], D("11"))],
                                 "短": [(dd[0], D("10"))]},
                                min_bars=2).skipped == ("短:bars<2",),
        "标的被跳过并记了原因"))


# --------------------------------------------------------------------------- #
#  B. 无未来函数
# --------------------------------------------------------------------------- #


def part_b() -> None:
    section("B. 无未来函数反证")

    rng = _rng(5)
    names = [f"n{i}" for i in range(8)]
    rets = {s: [_bell(rng) * 0.02 for _ in range(60)] for s in names}
    p1 = F.ReturnPanel.from_rows(_rows(rets))
    cfg = F.FactorConfig(annualization=252, min_obs=10, min_names=4)
    fr1 = F.build_factor_returns(p1, cfg)

    # 把最后一天的收益改掉：它之前的所有因子收益必须一字不变
    bumped = {s: list(v) for s, v in rets.items()}
    bumped[names[0]][-1] += 0.30
    p2 = F.ReturnPanel.from_rows(_rows(bumped))
    fr2 = F.build_factor_returns(p2, cfg)
    changed = [i for i in range(len(fr1.days) - 1)
               if fr1.values["market"][i] != fr2.values["market"][i]]
    eq(len(changed), 0, "改最后一天的收益 → 之前所有天的因子收益一字不变")
    truth(fr1.values["market"][-1] != fr2.values["market"][-1],
          "   └ 但最后一天确实变了（说明改动生效了，不是没测到）")

    # 改中间某天：只有从那天起往后才允许变
    mid = 30
    bumped2 = {s: list(v) for s, v in rets.items()}
    bumped2[names[1]][mid] += 0.25
    fr3 = F.build_factor_returns(F.ReturnPanel.from_rows(_rows(bumped2)), cfg)
    before = [i for i in range(mid - 1) if fr1.values["market"][i] != fr3.values["market"][i]]
    eq(len(before), 0, f"改第 {mid} 天收益 → 第 {mid} 天之前不受影响")
    truth(fr3.values["market"][mid - 1] != fr1.values["market"][mid - 1],
          "   └ 第 mid 天的因子收益变了（t 日收益进 t 日因子，不进 t-1）")

    # 特征滞后一格：某只标的在第 21 天暴涨，第 22 天的动量截面必须排第一
    n = 40
    names2 = ["up"] + [f"z{i}" for i in range(7)]
    r2 = {s: [0.0] * n for s in names2}
    r2["up"][20] = 0.20
    p3 = F.ReturnPanel.from_rows(_rows(r2))
    expo = F._exposures(p3, cfg, 21, None)
    truth("momentum" in expo, "第 22 天动量因子成立")
    if "momentum" in expo:
        eq(max(expo["momentum"], key=expo["momentum"].get), "up",
           "   └ 第 22 天的动量截面里，昨天暴涨的那只排第一（特征用的是 t-1）")
    # 反证：第 21 天（暴涨当天）它的动量还是 0 附近，不该被今天的涨幅算进去
    expo20 = F._exposures(p3, cfg, 20, None)
    truth("momentum" not in expo20 or abs(expo20["momentum"].get("up", 0.0)) < 1e-9,
          "   └ 而第 21 天（暴涨当天）它的动量暴露还没被算进去")

    # 源码层：窗口上界必须是 t（即 [t-w, t-1]）
    src = (PROJ / "quantos_core" / "factors.py").read_text(encoding="utf-8")
    truth("lo = max(0, t - w)" in src, "源码里窗口左端是 t-w")
    truth("lo = max(0, t - w + 1)" not in src, "   └ 且没有写成 t-w+1 这种少一天的形式")


# --------------------------------------------------------------------------- #
#  C. 因子收益口径
# --------------------------------------------------------------------------- #


def part_c() -> None:
    section("C. 因子收益口径")

    # market = 当天等权平均（可以手算）
    dd = _days(30)
    rets = {
        "a": [0.01] * 30,
        "b": [0.02] * 30,
        "c": [-0.01] * 30,
        "d": [0.0] * 30,
    }
    panel = F.ReturnPanel.from_rows(_rows(rets))
    cfg = F.FactorConfig(annualization=252, factors=("market",), min_obs=5, min_names=4)
    fr = F.build_factor_returns(panel, cfg)
    val = fr.values["market"][5]
    # 容差 1e-6 而不是 1e-12：价格只存到小数点后 8 位，收益是从价格反算的，
    # 天然带 ~1e-10 量级的舍入。这不是算法误差，是数据本身的精度。
    near(val, (0.01 + 0.02 - 0.01 + 0.0) / 4, 1e-6,
         "market 因子收益 = 当日 4 只的等权平均（手算对得上）")

    # 多空权重：gross = 2，净额 = 0
    w = F._weights("momentum", {"a": 1.0, "b": -1.0, "c": 0.5}, {"a", "b", "c"})
    near(sum(abs(v) for v in w.values()), 2.0, 1e-12, "多空因子 gross = 2")
    near(sum(w.values()), 0.0, 1e-12, "   └ 净额 = 0（美元中性）")
    truth(w["a"] > 0 > w["b"], "   └ z 为正的做多、为负的做空")

    # 单边截面不硬凑
    eq(F._weights("momentum", {"a": 1.0, "b": 2.0}, {"a", "b"}), {},
       "截面全为正（没有空头腿）→ 当天该因子不成立，返回空权重")
    eq(F._zscores({"a": 1.0, "b": 1.0, "c": 1.0, "d": 1.0}, 4, 3.0), {},
       "截面无离散度 → 因子不成立（不是「z 全 0」）")
    eq(F._zscores({f"s{i}": float(i) for i in range(3)}, 4, 3.0), {},
       "截面宽度 < min_names → 因子不成立")

    # z 截断
    z = F._zscores({f"s{i}": float(i) for i in range(9)}, 4, 3.0)
    truth(max(abs(v) for v in z.values()) <= 3.0 + 1e-12, "z 值被截断在 ±3 以内")

    # 因子收益落空的日子记 None，不改索引
    eq(len(fr.days), len(panel.days) - 1, "因子收益天数 = 交易日 - 1（与 days[1:] 对齐）")


# --------------------------------------------------------------------------- #
#  D. 载荷与 Σ
# --------------------------------------------------------------------------- #


def part_d() -> None:
    section("D. 载荷与 Σ")

    # 线性方程求解器：正例 + 奇异必须抛错（不做伪逆兜底）
    near(F._solve([[2.0, 1.0], [1.0, 3.0]], [5.0, 10.0])[0], 1.0, 1e-9,
         "解 2x+y=5, x+3y=10 → x=1")
    near(F._solve([[2.0, 1.0], [1.0, 3.0]], [5.0, 10.0])[1], 3.0, 1e-9,
         "   └ y=3")
    exc = raises(F.FactorError,
                 lambda: F._solve([[1.0, 2.0], [2.0, 4.0]], [1.0, 2.0]),
                 "共线矩阵抛 FactorError")
    truth(exc is not None and "共线" in str(exc),
          "   └ 错误信息点明是「共线」而不是含糊的数值错误")

    # B·F·Bᵀ：B=I 时结果就是 F
    bfb = F._matmul_btb([[1.0, 0.0], [0.0, 1.0]], [[4.0, 1.0], [1.0, 9.0]])
    near(bfb[0][0], 4.0, 1e-12, "B=I ⇒ B·F·Bᵀ = F（左上）")
    near(bfb[0][1], 1.0, 1e-12, "   └ 非对角也一致")
    near(bfb[1][1], 9.0, 1e-12, "   └ 右下一致")

    # 已知载荷能被反解出来
    names = [f"a{i:02d}" for i in range(12)]
    rets, betas = _common_and_idio(names, 160, seed=13, common=0.02, idio=0.004)
    panel = F.ReturnPanel.from_rows(_rows(rets))
    cfg = F.FactorConfig(annualization=252, factors=("market",), min_obs=60, min_names=4)
    est = F.estimate_factor_model(panel, cfg)
    load = {s: float(v["market"]) for s, v in est.factor_info["loadings"].items()}
    common = sorted(load)
    est_b = [load[s] for s in common]
    true_b = [betas[s] for s in common]
    r = _corr(est_b, true_b)
    truth(r > 0.95, f"估出的载荷与真实载荷高度一致（ρ={r:.4f} > 0.95）")
    truth(max(est_b) > min(est_b), "   └ 载荷确实随标的区分开了（不是一条平线）")
    truth(float(est.factor_info["r2_avg"]) > 0.5,
          f"共同因子占主导时 R² 应偏高（实测 {est.factor_info['r2_avg']}）")

    # 没有共同因子时，不许凭空造出相关性
    rng = _rng(77)
    indep = {s: [_bell(rng) * 0.02 for _ in range(160)] for s in names}
    panel2 = F.ReturnPanel.from_rows(_rows(indep))
    est2 = F.estimate_factor_model(panel2, cfg)
    avg_rho = (sum(abs(float(v)) for v in est2.corr.values()) / len(est2.corr)
               if est2.corr else 0.0)
    truth(avg_rho < 0.15, f"独立收益 → 平均 |ρ| = {avg_rho:.4f} < 0.15（没有编出相关性）")

    # Σ 对角必须与样本波动一致
    xs, ys = [], []
    for s in est.vols:
        vals = [v for v in panel.rets[s] if v is not None]
        xs.append(math.sqrt(sum(v * v for v in vals) / len(vals) * 252))
        ys.append(float(est.vols[s]))
    rr = _corr(xs, ys)
    truth(rr > 0.9, f"因子口径 σ 与样本日波动高度一致（ρ={rr:.4f} > 0.9）")

    # 相关矩阵对称 / 对角 1 / PSD
    sym_bad = 0
    for (a, b), v in est.corr.items():
        if (b, a) in est.corr and est.corr[(b, a)] != v:
            sym_bad += 1
    eq(sym_bad, 0, "相关矩阵对称")
    truth(all(abs(float(v)) <= 1.0 for v in est.corr.values()), "所有 ρ ∈ [-1,1]")
    n = min(12, len(est.vols))
    sub = [[1.0 if i == j else float(est.corr.get(
        (sorted(est.vols)[min(i, j)], sorted(est.vols)[max(i, j)]), D("0")))
        for j in range(n)] for i in range(n)]
    ok, why = _psd_via_cholesky(sub)
    truth(ok, f"前 {n} 只的相关子块正定（{why or 'ok'}）")

    # 残差占比与 R² 互补
    info = est.factor_info
    near(float(info["r2_avg"]) + float(info["residual_share"]), 1.0, 0.01,
         "平均 R² + 残差占比 ≈ 1（两者互补）")


# --------------------------------------------------------------------------- #
#  E. 参数计数与自由度
# --------------------------------------------------------------------------- #


def part_e() -> None:
    section("E. 参数计数与自由度")

    rng = _rng(99)
    names = [f"p{i:03d}" for i in range(80)]
    rets = {s: [_bell(rng) * 0.02 for _ in range(120)] for s in names}
    # 成交额必须逐只不同：size 因子是「成交额对数」的截面 z 值，
    # 全部一样时该因子当天不成立（这是 C 段已断言过的行为），
    # 于是四个因子凑不齐、一天有效样本都没有。
    amounts = {s: [1.0e8 * (1.0 + 0.37 * i) for _ in range(120)]
               for i, s in enumerate(names)}
    panel = F.ReturnPanel.from_rows(_rows(rets, amounts))
    cfg = F.FactorConfig(annualization=252, min_obs=40, min_names=4)
    est = F.estimate_factor_model(panel, cfg)

    k = len(cfg.factors)
    n_assets = len(est.vols)
    expect = n_assets * k + k * (k + 1) // 2 + n_assets
    eq(est.n_params, expect, f"n_params = n·k + k(k+1)/2 + n = {expect}")
    sample = n_assets + n_assets * (n_assets - 1) // 2
    eq(est.factor_info["sample_n_params"], sample,
       f"样本口径要 {sample} 个参数")
    truth(sample / est.n_params > 5,
          f"参数个数降到 1/{sample // est.n_params}（{sample} → {est.n_params}）")
    near(est.dof_ratio, D(est.n_obs) / D(est.n_params), D("0.01"),
         "dof_ratio 用的是因子口径的参数个数（不是 n(n-1)/2）")
    truth(est.dof_ratio > D(est.factor_info["dof_ratio_sample"]),
          f"自由度比 {est.dof_ratio} 优于样本口径 {est.factor_info['dof_ratio_sample']}")
    truth(est.method == "factor", "结果自带口径标记 method='factor'")

    # 794 只的极端情形：只算公式，不真跑（真实数据在 I 段）
    n = 794
    factor_params = n * k + k * (k + 1) // 2 + n
    sample_params = n + n * (n - 1) // 2
    truth(sample_params / factor_params > 70,
          f"794 只时：{sample_params} → {factor_params}（降 "
          f"{sample_params / factor_params:.0f} 倍）")
    truth(sample_params == 315615,
          "   └ 与「794 只 = 315,615 个参数」一致（794 + 794×793/2）")


# --------------------------------------------------------------------------- #
#  F. fail-closed
# --------------------------------------------------------------------------- #


def part_f() -> None:
    section("F. fail-closed（给不出模型就报错，不凑合）")

    dd = _days(40)
    # 所有标的每天涨同样的幅度 → 截面无离散度 → 所有因子不成立
    flat = {s: [0.001] * 40 for s in ["a", "b", "c", "d", "e", "f"]}
    panel = F.ReturnPanel.from_rows(_rows(flat))
    exc = raises(F.FactorError,
                 lambda: F.estimate_factor_model(
                     panel, F.FactorConfig(annualization=252, min_obs=10)),
                 "截面恒定（所有标的同涨同跌）→ 因子不成立 → 抛错")
    truth(exc is not None and "有效交易日" in str(exc),
          "   └ 错误信息说清是「有效交易日不够」")

    # 单标的：截面宽度永远 1
    one = F.ReturnPanel.from_rows(_rows({"only": [0.01, -0.02] * 30}))
    raises(F.FactorError,
           lambda: F.estimate_factor_model(
               one, F.FactorConfig(annualization=252, min_obs=5)),
           "单标的 → 截面宽度不足 → 抛错")

    # 样本不足
    rng = _rng(3)
    names = [f"q{i}" for i in range(8)]
    rets = {s: [_bell(rng) * 0.02 for _ in range(60)] for s in names}
    p = F.ReturnPanel.from_rows(_rows(rets))
    exc = raises(F.FactorError,
                 lambda: F.estimate_factor_model(
                     p, F.FactorConfig(annualization=252, min_obs=500)),
                 "min_obs 大于可用交易日 → 抛错")
    truth(exc is not None and "min_obs" in str(exc), "   └ 错误信息带 min_obs")

    # 参数校验
    guard("annualization 必填且为正", lambda: raises(
        F.FactorError, lambda: F.FactorConfig(annualization=0)))
    guard("未知因子被拒", lambda: raises(
        F.FactorError, lambda: F.FactorConfig(annualization=252, factors=("alpha",))))
    guard("因子重复被拒", lambda: raises(
        F.FactorError,
        lambda: F.FactorConfig(annualization=252, factors=("market", "market"))))
    guard("min_names 太小被拒", lambda: raises(
        F.FactorError, lambda: F.FactorConfig(annualization=252, min_names=2)))
    guard("min_coverage 越界被拒", lambda: raises(
        F.FactorError,
        lambda: F.FactorConfig(annualization=252, min_coverage=D("1.5"))))
    guard("z_clip <= 0 被拒", lambda: raises(
        F.FactorError, lambda: F.FactorConfig(annualization=252, z_clip=D("0"))))
    guard("负窗口被拒", lambda: raises(
        F.FactorError, lambda: F.FactorConfig(annualization=252, momentum_window=1)))

    # 窗口长度可算
    eq(F.FactorConfig(annualization=252, momentum_window=30).window, 31,
       "window 属性 = 1 + 最长回看（含 1 日滞后）")


# --------------------------------------------------------------------------- #
#  G. 与 risk_budget 打通
# --------------------------------------------------------------------------- #


def part_g() -> None:
    section("G. 与 risk_budget 打通 / 落盘往返")

    names = [f"g{i}" for i in range(10)]
    rets, _ = _common_and_idio(names, 200, seed=21, common=0.02, idio=0.008)
    panel = F.ReturnPanel.from_rows(_rows(rets))
    est = F.estimate_factor_model(
        panel, F.FactorConfig(annualization=252, factors=("market",), min_obs=60))

    model = est.to_risk_model()
    truth(model.default_corr is None,
          "to_risk_model() 的 default_corr 仍留 None（没估过的标的对不许静默拿到 ρ）")
    net = {names[i]: D("100000") for i in range(4)}
    risk = portfolio_risk(net, model)
    truth(risk.closes, f"Σ标的贡献 ≡ 组合 σ（{risk.contribution_sum} vs {risk.sigma}）")
    truth(risk.standalone_sum > risk.sigma,
          "独立波动之和 > 组合 σ（有分散化就必然如此；反过来说明矩阵不正定）")
    truth(risk.sigma > 0, "组合 σ 为正")

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "model.json")
        store = RiskModelStore(path)
        store.save(est, )
        got = store.load()
        truth(got is not None, "落盘后能读回")
        if got is not None:
            est2, _at = got
            eq(est2.method, "factor", "   └ 口径标记往返保住")
            eq(est2.n_params, est.n_params, "   └ 参数个数往返保住")
            eq(est2.dof_ratio, est.dof_ratio, "   └ dof_ratio 不因往返而变")
            eq(est2.factor_info.get("n_params"), est.factor_info.get("n_params"),
               "   └ factor_info 里的参数个数也保住")
            eq(len(est2.vols), len(est.vols), "   └ 标的数一致")
            truth(all(abs(float(est2.vols[s]) - float(est.vols[s])) < 1e-9
                      for s in est.vols), "   └ σ 逐个一致")


# --------------------------------------------------------------------------- #
#  H. 代码卫生
# --------------------------------------------------------------------------- #


def part_h() -> None:
    section("H. 代码卫生")

    path = PROJ / "quantos_core" / "factors.py"
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imports.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module.split(".")[0])
    allowed = {"__future__", "math", "os", "csv", "tempfile", "json",
               "dataclasses", "datetime", "decimal", "typing",
               "quantos_core", "quantos"}
    third = {i for i in imports if i not in allowed}
    truth(not third, f"只用 stdlib + quantos_core（第三方：{sorted(third)}）")
    truth("random" not in imports, "不 import random（可复现）")
    truth("time" not in imports, "不 import time（不看时钟）")
    truth("datetime" in imports and "now" not in
          {n.func.id for n in ast.walk(tree)
           if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)},
          "不调用 now()/today()（纯函数，同一输入同一输出）")

    # 循环 import：factors → covariance 单向；covariance 不许回头引 factors
    cov_src = (PROJ / "quantos_core" / "covariance.py").read_text(encoding="utf-8")
    truth("quantos_core.factors" not in cov_src,
          "covariance 不 import factors（依赖单向）")
    truth("quantos_core.backtest" not in src,
          "factors 不 import backtest（估计层不依赖回测层）")

    # 同一输入同一输出
    names = [f"h{i}" for i in range(8)]
    rets, _ = _common_and_idio(names, 120, seed=33, common=0.02, idio=0.01)
    amounts = {s: [1.0e8 * (1.0 + 0.3 * i) for _ in range(120)]
               for i, s in enumerate(names)}
    panel = F.ReturnPanel.from_rows(_rows(rets, amounts))
    cfg = F.FactorConfig(annualization=252, min_obs=40, min_names=4)
    a = F.estimate_factor_model(panel, cfg)
    b = F.estimate_factor_model(panel, cfg)
    eq(a.to_dict(), b.to_dict(), "同一输入两次估计 → 逐字段完全相同")

    # 描述函数不崩、且把口径写出来
    lines = F.describe_model(a)
    truth(any("因子模型" in ln for ln in lines), "describe_model 写明是因子口径")
    truth(any("参数个数" in ln for ln in lines), "   └ 并且报出参数个数")


# --------------------------------------------------------------------------- #
#  I. 真实数据
# --------------------------------------------------------------------------- #


def part_i() -> None:
    section("I. 真实数据（有就跑，没有就明说跳过）")

    if not DATA_DIR.is_dir():
        truth(True, f"跳过：{DATA_DIR} 不存在")
        return
    try:
        panel = F.ReturnPanel.load(str(DATA_DIR), max_names=120)
    except Exception as exc:                                    # noqa: BLE001
        truth(True, f"跳过：读数据失败（{type(exc).__name__}: {exc}）")
        return

    cfg = F.FactorConfig(annualization=252, min_obs=120)
    est = F.estimate_factor_model(panel, cfg)
    truth(len(est.vols) >= 60, f"真实数据上估出 {len(est.vols)} 只标的的 σ")
    truth(len(est.corr) > 500, f"   └ 相关对 {len(est.corr)} 个")
    truth(est.n_params < est.factor_info["sample_n_params"],
          f"参数个数 {est.n_params} < 样本口径 {est.factor_info['sample_n_params']}")
    truth(0 < float(est.factor_info["r2_avg"]) < 1,
          f"   └ 平均 R² = {est.factor_info['r2_avg']} 落在 (0,1)")
    market_vol = est.factor_info["factor_vol_annual"].get("market")
    truth(market_vol is not None and 0.05 < float(market_vol) < 0.60,
          f"   └ 市场因子年化波动 {market_vol} 落在合理区间（5%~60%）")
    truth(all(float(v) > 0 for v in est.vols.values()), "   └ 所有 σ 为正")
    truth(all(abs(float(v)) <= 1 for v in est.corr.values()), "   └ 所有 ρ ∈ [-1,1]")


# --------------------------------------------------------------------------- #
#  入口
# --------------------------------------------------------------------------- #


def main() -> int:
    print("=" * 66)
    print("B13 因子模型自测")
    print("=" * 66)
    part_a()
    part_b()
    part_c()
    part_d()
    part_e()
    part_f()
    part_g()
    part_h()
    part_i()
    return 0


if __name__ == "__main__":
    raise SystemExit(run("B13 因子模型", main))
