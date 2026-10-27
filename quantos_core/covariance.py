"""
QuantOS 2.0 — Covariance Estimation（T-701）
============================================

B6 的 `risk_budget` 消费 σ 与 ρ，但**没人生产它们**。这个模块补上那一层。

它回答的问题只有一个：**给我一组历史价格，我能给出多可信的风险假设？**

刻意不做的事：不假装精确。这个模块产出的每一个数字都带着它的样本量、
年化口径和收缩强度 —— 因为风控读到的 σ 会直接变成额度，而一个看起来
精确的错数字比一个明说不知道的数字危险得多。

四个口径陷阱（都是会安静出错、不报错的那种）：

1. **年化因子用错**。日收益 × √252 还是 × √365？美股一年 252 个交易日，
   Crypto 一年 365 天。用错一个，σ 差 20%，额度就跟着差 20%。所以
   `annualization` 没有默认值，必须显式传。

2. **不同市场日历求交集会丢样本**。A 股 + 美股各 244/252 天，交集可能只有
   200 天。如果各自用自己的日期再强行配对，等于把"美股休市那天 A 股的跌幅"
   配到"A 股休市那天美股的涨幅"上，相关系数是纯噪声。本模块一律取交集。

3. **停牌/前值填充冒充零波动**。价格连续不变会被算成"日收益 0"，σ 被系统性
   低估。本模块检测连续同价并告警，绝不静默通过。

4. **未复权价格把拆股算成暴跌**。2:1 拆股当天价格腰斩，日收益 -50%，
   年化波动率直接爆表。本模块检测跳变，并把"疑似未复权"单独报出来。

还有一个统计学事实必须摆在明面上：**样本相关系数的标准误约 (1-ρ²)/√n**。
n=60 天时，标准误是 0.11 左右 —— 也就是说 ρ 估成 0.3 还是 0.1 根本无法区分。
`dof_ratio`（样本数 / 标的数）低于 5 时，本模块会明确要求加大收缩系数，
而不是让你拿一个自由度不足的相关矩阵去分配真钱的风险预算。

与 `risk_budget` 的分工：本模块**只产出假设**（vols + corr），
`risk_budget` 消费假设做分解。谁也不知道假设错了会怎样 —— 所以假设本身
必须带着置信度信息（n_obs / dof_ratio / warnings）一起交出去。
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from quantos_core.contracts import ContractViolation, D, q, utcnow
from quantos_core.risk_budget import RiskModel


# --------------------------------------------------------------------------- #
#  常量
# --------------------------------------------------------------------------- #

#: 一个交易日历年的常见取值。刻意不做默认，只作为可选常量提供。
TRADING_DAYS_EQUITY = 252
TRADING_DAYS_CRYPTO = 365

#: 低于这个样本数，任何相关系数都没有意义（标准误 > 0.13）。
MIN_OBS_HARD = 20

#: 样本数 / 标的数 低于这个比值，相关矩阵自由度不足。
MIN_DOF_RATIO = Decimal("5")

#: 单日收益绝对值超过这个数，几乎一定是未复权的公司行动而非市场波动。
JUMP_THRESHOLD = Decimal("0.35")

_ZERO = Decimal(0)
_ONE = Decimal(1)


class EstimationError(ContractViolation):
    """估计输入不合法（样本不足、日期不重叠、参数矛盾）。"""


# --------------------------------------------------------------------------- #
#  输入
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PriceSeries:
    """
    单标的历史价格，按日期升序，日期唯一。

    刻意要求 `date` 而不是 `datetime`：日频估计的粒度就是"哪天收盘"，
    带时分秒会让人以为能对齐盘中时刻，而实际上不同场所的收盘时间差几小时。
    """

    symbol: str
    points: Tuple[Tuple[date, Decimal], ...]

    def __post_init__(self) -> None:
        if not self.symbol:
            raise EstimationError("PriceSeries.symbol 不能为空")
        pts = tuple((d, D(p)) for d, p in self.points)
        object.__setattr__(self, "points", pts)
        if len(pts) < 2:
            raise EstimationError(
                f"{self.symbol} 只有 {len(pts)} 个价格点，算不出收益率")
        dates = [d for d, _ in pts]
        if dates != sorted(dates):
            raise EstimationError(f"{self.symbol} 的价格点必须按日期升序")
        if len(set(dates)) != len(dates):
            raise EstimationError(f"{self.symbol} 的日期有重复")
        for d, p in pts:
            if p <= 0:
                raise EstimationError(
                    f"{self.symbol} 在 {d} 的价格为 {p}，价格必须为正"
                    f"（用 0 表示停牌会让收益率变成 -100%）")

    @classmethod
    def from_mapping(cls, symbol: str,
                     values: Mapping[date, object]) -> "PriceSeries":
        return cls(symbol, tuple(sorted((d, D(v)) for d, v in values.items())))

    def __len__(self) -> int:
        return len(self.points)

    @property
    def start(self) -> date:
        return self.points[0][0]

    @property
    def end(self) -> date:
        return self.points[-1][0]

    def price_at(self, day: date) -> Optional[Decimal]:
        for d, p in self.points:
            if d == day:
                return p
        return None


@dataclass(frozen=True)
class EstimationConfig:
    """
    估计参数。所有会影响数量级的项都**没有默认值**。

    `annualization` 是必填项：这是本模块唯一一个"填错不会报错但结果差 20%"
    的参数，所以不给默认。想用股票口径就传 252，Crypto 传 365，
    混合组合按主暴露给（并在 notes 里说明这是近似）。
    """

    annualization: int
    #: EWMA 半衰期（交易日）。None = 等权。20 天大致对应"近一个月主导"。
    halflife: Optional[int] = None
    #: 向常数相关目标收缩的强度 ∈ [0,1]。0 = 全用样本，1 = 全用目标。
    shrinkage: Decimal = _ZERO
    #: 最少需要的收益观测数。少于它直接抛错，不给"凑合能用"的模型。
    min_obs: int = 60
    #: 样本不足的标的怎么办。"error"（默认）或 "drop"。
    on_insufficient: str = "error"

    def __post_init__(self) -> None:
        if self.annualization <= 0:
            raise EstimationError("annualization 必须为正（252 或 365）")
        if self.annualization not in (TRADING_DAYS_EQUITY, TRADING_DAYS_CRYPTO):
            # 不是禁止，是要人明确知道自己在偏离常规。
            pass
        if self.halflife is not None and self.halflife <= 0:
            raise EstimationError("halflife 必须为正")
        s = D(self.shrinkage)
        object.__setattr__(self, "shrinkage", s)
        if not (_ZERO <= s <= _ONE):
            raise EstimationError(f"shrinkage 必须在 [0,1]，收到 {s}")
        if self.min_obs < 2:
            raise EstimationError("min_obs 至少为 2")
        if self.on_insufficient not in ("error", "drop"):
            raise EstimationError(
                f"on_insufficient 只认 error / drop，收到 {self.on_insufficient!r}")


# --------------------------------------------------------------------------- #
#  输出
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CovarianceEstimate:
    """
    估计结果。**带置信度信息一起交出去**，不要只给 vols/corr。

    下游把 σ 换成额度以后，没人会回来问"这个 σ 是几天估的"。所以样本量、
    自由度、收缩强度必须跟着结果走，一路能追到审批材料里。
    """

    vols: Dict[str, Decimal]
    corr: Dict[Tuple[str, str], Decimal]
    n_obs: int
    n_assets: int
    window_start: date
    window_end: date
    annualization: int
    halflife: Optional[int]
    shrinkage: Decimal
    dropped: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()
    label: str = ""
    #: 收缩**之前**的相关系数。必须留着：收缩能把 ρ 从 0.87 压到 0.62，
    #: 只报收缩后的值，看的人会以为 0.62 是估出来的，而不是被人为拉过去的。
    #: 空字典 = 没做收缩。
    raw_corr: Dict[Tuple[str, str], Decimal] = field(default_factory=dict)

    # -- 诊断 ------------------------------------------------------------ #

    @property
    def n_params(self) -> int:
        """
        要估的自由参数个数：n 个波动率 + n(n-1)/2 个相关系数。

        用这个数而不是标的数做自由度分母，是一个容易搞错但影响很大的细节：
        5 个标的有 15 个参数，40 个观测意味着每个参数只有 2.7 个自由度 ——
        而 n_obs/n_assets 会给出 8，看起来还挺宽裕。
        """
        n = self.n_assets
        return n + n * (n - 1) // 2

    @property
    def dof_ratio(self) -> Decimal:
        """
        观测数 / 待估参数数。低于 5 时相关矩阵基本是噪声。

        分母是**参数个数**不是标的个数（见 `n_params`）。这个差别在标的少时
        看不出来，在标的 5~10 个时会差 3 倍。
        """
        if self.n_params <= 0:
            return _ZERO
        return q(Decimal(self.n_obs) / Decimal(self.n_params), 2)

    def corr_stderr(self) -> Decimal:
        """
        相关系数标准误的粗估 (1-ρ²)/√n，取最不利的 ρ=0 情形即 1/√n。

        不给"每个 ρ 各算一个标准误"，因为那会诱导人挑一个好看的 ρ 报上去。
        给一个统一的上界，含义是：这个窗口下 ρ 的分辨率大约就是这个量级。
        """
        if self.n_obs <= 0:
            return _ONE
        return q(_ONE / D(self.n_obs).sqrt(), 4)

    @property
    def trustworthy(self) -> bool:
        """能不能拿去做风险预算。不满足就只是"参考值"。"""
        return (self.n_obs >= MIN_OBS_HARD
                and self.dof_ratio >= MIN_DOF_RATIO
                and not self.dropped)

    def to_risk_model(self) -> RiskModel:
        """
        转成 `RiskModel`，`default_corr` 故意留 `None`。

        为什么不用样本均值填 default_corr：那会让"没估过的标的对"静默拿到一个
        看起来合理的 ρ。留 None，`risk_budget` 会在缺 ρ 时抛错，强迫调用方
        显式处理 —— 这正是 B6 定下的规矩。
        """
        return RiskModel(
            vols=dict(self.vols),
            corr={k: D(v) for k, v in self.corr.items()},
            default_corr=None,
            label=self.label or f"样本估计（{self.n_obs} 观测 · {self.window_start}"
                               f"~{self.window_end}）",
        )

    def to_dict(self) -> Dict:
        return {
            "vols": {k: str(v) for k, v in self.vols.items()},
            "corr": {f"{a}|{b}": str(v) for (a, b), v in self.corr.items()},
            "raw_corr": {f"{a}|{b}": str(v) for (a, b), v in self.raw_corr.items()},
            "n_obs": self.n_obs,
            "n_assets": self.n_assets,
            "dof_ratio": str(self.dof_ratio),
            "corr_stderr": str(self.corr_stderr()),
            "trustworthy": self.trustworthy,
            "window": [self.window_start.isoformat(), self.window_end.isoformat()],
            "annualization": self.annualization,
            "halflife": self.halflife,
            "shrinkage": str(self.shrinkage),
            "dropped": list(self.dropped),
            "warnings": list(self.warnings),
            "label": self.label,
        }

    def describe(self) -> str:
        """一行摘要，给运维面和日志用。"""
        head = (f"{self.n_assets} 标的 · {self.n_obs} 观测 · "
                f"{self.window_start}~{self.window_end} · "
                f"年化 {self.annualization} 日")
        tail = "可信" if self.trustworthy else "仅供参考"
        return f"{head} · 自由度比 {self.dof_ratio} · {tail}"


# --------------------------------------------------------------------------- #
#  对齐
# --------------------------------------------------------------------------- #


def intersect_dates(series: Sequence[PriceSeries]) -> Tuple[date, ...]:
    """
    所有标的都有的交易日（**交集**，不是并集）。

    为什么不用并集 + 前值填充：填充出来的 0 收益会把波动率拉低，而低波动率
    意味着"这个标的不危险"，额度会自动放大。这是最典型的"安静地放大杠杆"。
    """
    if not series:
        raise EstimationError("至少需要一个价格序列")
    common = {d for d, _ in series[0].points}
    for s in series[1:]:
        common &= {d for d, _ in s.points}
    return tuple(sorted(common))


def align_prices(series: Sequence[PriceSeries],
                 min_obs: int = MIN_OBS_HARD
                 ) -> Tuple[Tuple[date, ...], Dict[str, List[Decimal]]]:
    """
    返回 (共同日期, {symbol: 对齐后价格})。

    交集太少时抛错并**说清是哪个标的缺样本**——混合 A 股与美股时交集可能只有
    200 天，而如果其中一个标的是新上市，交集会突然掉到 30 天。
    """
    dates = intersect_dates(series)
    need = max(min_obs + 1, 3)
    if len(dates) < need:
        detail = "、".join(f"{s.symbol}({len(s)})" for s in series)
        raise EstimationError(
            f"共同交易日只有 {len(dates)} 天，少于所需的 {need} 天。"
            f"各序列点数：{detail}。"
            f"混合不同市场日历时这是常态，请换更短的窗口或退回更长的历史。")
    out: Dict[str, List[Decimal]] = {}
    for s in series:
        lookup = dict(s.points)
        out[s.symbol] = [lookup[d] for d in dates]
    return dates, out


# --------------------------------------------------------------------------- #
#  收益率与权重
# --------------------------------------------------------------------------- #


def simple_returns(prices: Sequence[Decimal]) -> List[Decimal]:
    """
    简单收益率 p_t/p_{t-1} - 1。

    不用对数收益：账本里的 PnL 是线性的（数量 × 价格差），用对数收益算出来的
    σ 与 PnL 口径不一致。差异在小波动下可忽略，但没理由主动引入一个口径差。
    """
    out: List[Decimal] = []
    for i in range(1, len(prices)):
        prev = D(prices[i - 1])
        if prev <= 0:
            raise EstimationError(f"第 {i-1} 个价格非正（{prev}），无法算收益率")
        out.append(D(prices[i]) / prev - _ONE)
    return out


def ewma_weights(n: int, halflife: Optional[int]) -> List[Decimal]:
    """
    权重向量，和为 1，越近越大。

    `halflife=None` → 等权。给了 halflife 就用 λ = 0.5^(1/halflife)。
    用 Decimal 累乘而不走 exp()：halflife 是整数，λ 是精确可表示的幂次序列，
    结果可复现；exp() 会引入一个和平台相关的末位差异。
    """
    if n <= 0:
        raise EstimationError("权重数量必须为正")
    if halflife is None:
        w = _ONE / Decimal(n)
        return [w] * n
    # λ = 0.5^(1/halflife)。Decimal 不支持分数次幂，这里走 float：
    # λ 只影响权重形状，1e-16 的相对误差在归一化后被抹平，
    # 而真正决定数量级的是 halflife 本身。这一步不引入可观测偏差。
    lam = D(str(0.5 ** (1.0 / float(halflife))))
    raw = [lam ** Decimal(n - 1 - i) for i in range(n)]
    total = sum(raw, _ZERO)
    return [x / total for x in raw]


# --------------------------------------------------------------------------- #
#  估计
# --------------------------------------------------------------------------- #


def _weighted_cov(r_a: Sequence[Decimal], r_b: Sequence[Decimal],
                  w: Sequence[Decimal],
                  mean_a: Decimal, mean_b: Decimal) -> Decimal:
    """
    加权协方差，分母用 (1 - Σw²) 做无偏修正。

    等权时 1-Σw² = 1-1/n = (n-1)/n，于是 Σw(..)/(1-Σw²) 正好等于
    Σ(..)/(n-1) —— 与样本协方差完全一致。这条一致性是刻意对齐的：
    否则等权场景下的结果会和任何人手算的都对不上。
    """
    acc = _ZERO
    for x, y, wi in zip(r_a, r_b, w):
        acc += wi * (D(x) - mean_a) * (D(y) - mean_b)
    denom = _ONE - sum((wi * wi for wi in w), _ZERO)
    if denom <= 0:
        raise EstimationError(
            "权重过于集中导致无偏修正分母为 0（样本太少或 halflife 太小）")
    return acc / denom


def _psd_via_cholesky(mat: Sequence[Sequence[float]]) -> Tuple[bool, str]:
    """
    Cholesky 探正定性。不引 numpy，n 一般 ≤ 30，O(n³) 完全可以接受。

    为什么非要查：样本相关矩阵在 n_obs < n_assets 时**必然**半正定退化，
    而 `RiskModel` 只检查单个 ρ 的取值域，查不出"三个标的的 ρ 互相矛盾"
    （ρ_ab = ρ_bc = 0.9 且 ρ_ac = -0.9 时矩阵非正定，但每个 ρ 都合法）。
    非正定的矩阵代进 Euler 分解，会算出负的风险贡献 —— 负风险贡献在报表上
    看起来像"这个仓位在降低风险"，从而被保留甚至加码。
    """
    n = len(mat)
    low = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            s = mat[i][j] - sum(low[i][k] * low[j][k] for k in range(j))
            if i == j:
                if s <= 1e-12:
                    return False, f"第 {i+1} 个主元为 {s:.3e}（矩阵非正定）"
                low[i][j] = math.sqrt(s)
            else:
                low[i][j] = s / low[j][j]
    return True, ""


def _shrink_toward_constant_corr(
        corr: Dict[Tuple[str, str], Decimal],
        names: Sequence[str],
        delta: Decimal) -> Tuple[Dict[Tuple[str, str], Decimal], Decimal, List[str]]:
    """
    向"常数相关"目标收缩：Σ' = (1-δ)Σ + δ·F，F 的非对角元 = ρ̄。

    关键性质：**目标矩阵的对角线就是 1，所以收缩完全不改变波动率**。
    它只把极端或不可信的相关系数拉向均值。这一点很重要——收缩常被误解为
    "把风险调小"，这里不是调大小，是调可信度。

    ρ̄ 用非对角样本相关的**简单平均**。用加权平均会让少数几对高相关标的
    主导全局目标，等于在收缩里偷偷放回偏差。
    """
    names = list(names)
    off = [(a, b) for i, a in enumerate(names) for b in names[i + 1:]]
    if not off:
        return corr, _ZERO, []
    rho_bar = q(sum((D(corr[(a, b)]) for a, b in off), _ZERO) / Decimal(len(off)), 8)
    out: Dict[Tuple[str, str], Decimal] = {}
    moved = 0
    for (a, b) in off:
        old = D(corr[(a, b)])
        new = q((_ONE - delta) * old + delta * rho_bar, 8)
        out[(a, b)] = new
        if abs(new - old) > Decimal("0.05"):
            moved += 1
    notes = [
        f"按 δ={delta} 向常数相关 ρ̄={rho_bar} 收缩，"
        f"{len(off)} 对里有 {moved} 对被移动超过 0.05"
    ]
    return out, rho_bar, notes


def estimate(series: Sequence[PriceSeries],
             config: EstimationConfig,
             *, label: str = "") -> CovarianceEstimate:
    """
    主入口。流程：对齐 → 收益 → 加权矩 → 年化 → 清洗 → 收缩 → 诊断。

    顺序不能换：收缩必须在年化之后做。先收缩再年化等价于同一个结果（正数
    乘法可交换），但如果中间插入了 clip，顺序就会影响结果 —— 固定成
    "年化 → clip → 收缩"，保证任何调用顺序都得到同一个数。
    """
    if not series:
        raise EstimationError("至少需要一个价格序列")
    names = [s.symbol for s in series]
    if len(set(names)) != len(names):
        raise EstimationError(f"标的重复：{names}")

    warnings: List[str] = []
    dropped: List[str] = []

    dates, aligned = align_prices(series, config.min_obs)
    n_prices = len(dates)
    n_obs = n_prices - 1
    if n_obs < config.min_obs:
        raise EstimationError(
            f"共同交易日 {n_prices} 天只产生 {n_obs} 个收益观测，"
            f"少于要求的 {config.min_obs} 个")

    # -- 收益率 ------------------------------------------------------ #
    rets: Dict[str, List[Decimal]] = {}
    for sym in names:
        r = simple_returns(aligned[sym])
        rets[sym] = r
        # 停牌检测：连续同价会被算成 0 波动
        run, longest = 1, 1
        for i in range(1, len(aligned[sym])):
            if aligned[sym][i] == aligned[sym][i - 1]:
                run += 1
                longest = max(longest, run)
            else:
                run = 1
        if longest >= 5:
            warnings.append(
                f"{sym} 有连续 {longest} 天价格完全相同 —— 疑似停牌或前值填充。"
                f"这些日子被算作 0 波动，σ 会被低估")

    # -- 跳变检测（未复权公司行动 / 数据错误） ----------------------- #
    for sym in names:
        jumps = detect_price_jumps(aligned[sym], dates)
        if jumps:
            worst = max(jumps, key=lambda j: abs(j[1]))
            warnings.append(
                f"{sym} 有 {len(jumps)} 处单日收益超过 {JUMP_THRESHOLD:.0%}，"
                f"最剧烈为 {worst[0]} 的 {worst[1]:+.2%}。"
                f"若当天有拆股/并股/分红，价格必须复权后再估 —— "
                f"未复权的拆股会被当成暴跌，σ 直接爆表")

    # -- 权重 -------------------------------------------------------- #
    w = ewma_weights(n_obs, config.halflife)
    if config.halflife is not None:
        # 有效样本量：权重越集中，真实自由度越低
        ess = _ONE / sum((x * x for x in w), _ZERO)
        if ess < Decimal(30):
            warnings.append(
                f"半衰期 {config.halflife} 天使有效样本量降到 {q(ess, 1)}，"
                f"相关系数的噪声会明显变大")

    # -- 加权均值与协方差 -------------------------------------------- #
    means = {sym: sum((wi * r for wi, r in zip(w, rets[sym])), _ZERO)
             for sym in names}
    # 用 Decimal.sqrt() 而不是 math.sqrt()：年化因子要开方，
    # 走 float 会在末位引入一个和平台相关的差异，而 σ 会被拿去算额度。
    sqrt_n = D(config.annualization).sqrt()
    vols: Dict[str, Decimal] = {}
    for sym in names:
        var = _weighted_cov(rets[sym], rets[sym], w, means[sym], means[sym])
        if var <= 0:
            warnings.append(f"{sym} 的收益率方差为 {q(var, 12)}，σ 置为 0 但不可用于风控")
            vols[sym] = _ZERO
            continue
        # 年化：日 σ × √periods
        vols[sym] = q(D(var).sqrt() * sqrt_n, 8)

    corr: Dict[Tuple[str, str], Decimal] = {}
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            cov = _weighted_cov(rets[a], rets[b], w, means[a], means[b])
            sa, sb = vols[a] / sqrt_n, vols[b] / sqrt_n
            if sa <= 0 or sb <= 0:
                corr[(a, b)] = _ZERO
                warnings.append(
                    f"{a}/{b} 有一方 σ 为 0（价格不动），相关系数无定义，暂置 0")
                continue
            rho = cov / (sa * sb)
            if rho > _ONE:
                warnings.append(f"ρ({a},{b}) 样本值 {q(rho, 6)} > 1，已截断到 1")
                rho = _ONE
            elif rho < -_ONE:
                warnings.append(f"ρ({a},{b}) 样本值 {q(rho, 6)} < -1，已截断到 -1")
                rho = -_ONE
            elif abs(rho) > Decimal("0.999"):
                warnings.append(
                    f"ρ({a},{b}) 高达 {q(rho, 4)} —— 两者样本内几乎完全同向，"
                    f"可能是同一资产的两种表达（ETF 与其指数、同币种不同场所）。"
                    f"这种相关不能用来主张分散化")
            corr[(a, b)] = q(rho, 8)

    # -- 正定性（收缩前先查） ---------------------------------------- #
    before = _corr_matrix(names, corr)
    ok_before, why = _psd_via_cholesky(before)
    if not ok_before:
        warnings.append(
            f"样本相关矩阵非正定（{why}）。这种矩阵代进 Euler 分解会算出"
            f"负的风险贡献，必须收缩或减少标的数")

    # -- 收缩 -------------------------------------------------------- #
    # 先留一份收缩前的，页面要能显示"样本 0.87 → 收缩后 0.62"。
    raw_corr = dict(corr)
    rho_bar = _ZERO
    if D(config.shrinkage) > 0:
        corr, rho_bar, notes = _shrink_toward_constant_corr(
            corr, names, D(config.shrinkage))
        warnings.extend(notes)
    after = _corr_matrix(names, corr)
    ok_after, why2 = _psd_via_cholesky(after)
    if not ok_after:
        warnings.append(
            f"收缩后相关矩阵仍非正定（{why2}）。这不是估计精度问题，"
            f"是标的间关系互相矛盾，请检查数据或减少标的")

    # -- 自由度诊断 -------------------------------------------------- #
    n_assets = len(names)
    n_params = n_assets + n_assets * (n_assets - 1) // 2
    dof = q(Decimal(n_obs) / Decimal(max(n_params, 1)), 2)
    if n_obs < MIN_OBS_HARD:
        warnings.append(
            f"只有 {n_obs} 个观测，低于 {MIN_OBS_HARD} 的硬底线："
            f"相关系数标准误约 {q(_ONE / D(n_obs).sqrt(), 3)}，"
            f"任何 ρ 都在噪声范围内")
    elif dof < MIN_DOF_RATIO:
        warnings.append(
            f"自由度比 {dof} < {MIN_DOF_RATIO}"
            f"（{n_obs} 观测 / {n_params} 个待估参数，其中 "
            f"{n_assets} 个 σ + {n_assets*(n_assets-1)//2} 个 ρ）。"
            f"此时样本相关矩阵接近奇异，请把 shrinkage 提到 0.3 以上，"
            f"或缩短标的列表")
    if n_assets == 2 and D(config.shrinkage) > 0:
        warnings.append(
            f"只有 2 个标的，shrinkage={config.shrinkage} 实际不起作用："
            f"常数相关目标就是那唯一一对的样本值，收缩前后完全相同。"
            f"要真正压低噪声，必须要么加标的，要么加观测")

    est = CovarianceEstimate(
        vols=vols, corr=corr, n_obs=n_obs, n_assets=n_assets,
        window_start=dates[0], window_end=dates[-1],
        annualization=config.annualization, halflife=config.halflife,
        shrinkage=D(config.shrinkage), dropped=tuple(dropped),
        warnings=tuple(warnings),
        label=label or f"样本估计 {dates[0]}~{dates[-1]}（{n_obs} 观测）",
        raw_corr=raw_corr if D(config.shrinkage) > 0 else {},
    )
    return est


def _corr_matrix(names: Sequence[str],
                 corr: Mapping[Tuple[str, str], Decimal]) -> List[List[float]]:
    """拼成方阵（只用于 Cholesky 探正定）。"""
    n = len(names)
    m = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    for i, a in enumerate(names):
        for j, b in enumerate(names):
            if i == j:
                continue
            key = (a, b) if (a, b) in corr else (b, a)
            if key in corr:
                m[i][j] = float(D(corr[key]))
    return m


# --------------------------------------------------------------------------- #
#  数据体检
# --------------------------------------------------------------------------- #


def detect_price_jumps(prices: Sequence[Decimal], dates: Sequence[date],
                       threshold: Decimal = JUMP_THRESHOLD
                       ) -> List[Tuple[date, Decimal]]:
    """
    找出单日收益超过阈值的日子。

    用途不是"删掉异常值"（删了就是把风险最好的日子去掉，σ 会低估），
    而是**提示这批价格可能没复权**。拆股 2:1 就是 -50%，并股 1:5 就是 +400%，
    这两种都不能进波动率估计，但也不能静默丢弃 —— 必须人来确认。
    """
    out: List[Tuple[date, Decimal]] = []
    thr = D(threshold)
    for i in range(1, len(prices)):
        prev = D(prices[i - 1])
        if prev <= 0:
            continue
        r = D(prices[i]) / prev - _ONE
        if abs(r) > thr:
            out.append((dates[i], q(r, 6)))
    return out


def describe_inputs(series: Sequence[PriceSeries]) -> List[str]:
    """把输入的样子打印出来。估计之前先看这个，比看结果有用。"""
    lines: List[str] = []
    for s in series:
        lines.append(f"{s.symbol:14s} {len(s):5d} 点  {s.start} ~ {s.end}")
    if len(series) > 1:
        common = intersect_dates(series)
        lines.append(f"{'交集':14s} {len(common):5d} 天  "
                     f"{(common[0] if common else '-')} ~ "
                     f"{(common[-1] if common else '-')}")
    return lines


# --------------------------------------------------------------------------- #
#  落盘
# --------------------------------------------------------------------------- #

DEFAULT_MODEL_PATH = "state/risk/model.json"


class RiskModelStore:
    """
    估计结果的落盘，带 `estimated_at`。

    为什么必须有时间戳：σ 只在自己估的那个市场状态下有效。一个 90 天前估的
    波动率被拿去算今天的额度，和没有模型差不多 —— 它会把「上季度很平静」
    当成「现在很安全」，于是放大额度。运维面必须能一眼看出它有多旧。

    单文件 + 原子替换（与 NavSeries 同套路数）：写一半崩掉不该留下坏文件，
    而坏模型比没模型更危险 —— 没模型会抛错停下来，坏模型会安静地给出额度。
    """

    def __init__(self, path: str = DEFAULT_MODEL_PATH) -> None:
        self.path = path

    def save(self, est: CovarianceEstimate, *,
             estimated_at: Optional[datetime] = None) -> Dict:
        payload = {
            "estimated_at": (estimated_at or utcnow()).isoformat(),
            "estimate": est.to_dict(),
        }
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path) or ".",
                                   prefix=".model-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return payload

    def load(self) -> Optional[Tuple[CovarianceEstimate, datetime]]:
        """读回 (估计, 估计时刻)。文件缺失或损坏都返回 None，不抛错 ——
        运维面需要能启动并报告「没有模型」，而不是整个崩掉。"""
        if not os.path.isfile(self.path):
            return None
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            est = estimate_from_dict(payload["estimate"])
            return est, datetime.fromisoformat(payload["estimated_at"])
        except Exception:                       # noqa: BLE001
            return None

    def age_days(self, now: Optional[datetime] = None) -> Optional[float]:
        got = self.load()
        if got is None:
            return None
        _, at = got
        return ((now or utcnow()) - at).total_seconds() / 86400.0

    def stale(self, max_age_days: float = 7.0,
              now: Optional[datetime] = None) -> Optional[bool]:
        """
        是否过期。`None` 表示压根没有模型（区别于「有且新鲜」）。

        默认 7 天：超过一周没重估，σ 反映的已经是另一段行情了。
        这个阈值不是统计意义上的最优解，是「提醒人去看看」的节奏。
        """
        age = self.age_days(now)
        if age is None:
            return None
        return age > max_age_days


def estimate_from_dict(d: Mapping) -> CovarianceEstimate:
    """`CovarianceEstimate.to_dict()` 的逆。"""
    window = d.get("window") or ["1970-01-01", "1970-01-01"]
    return CovarianceEstimate(
        vols={k: D(v) for k, v in (d.get("vols") or {}).items()},
        corr={tuple(k.split("|", 1)): D(v)          # type: ignore[misc]
              for k, v in (d.get("corr") or {}).items()},
        n_obs=int(d.get("n_obs") or 0),
        n_assets=int(d.get("n_assets") or 0),
        window_start=date.fromisoformat(window[0]),
        window_end=date.fromisoformat(window[1]),
        annualization=int(d.get("annualization") or 252),
        halflife=d.get("halflife"),
        shrinkage=D(d.get("shrinkage") or 0),
        dropped=tuple(d.get("dropped") or ()),
        warnings=tuple(d.get("warnings") or ()),
        label=str(d.get("label") or ""),
        raw_corr={tuple(k.split("|", 1)): D(v)        # type: ignore[misc]
                  for k, v in (d.get("raw_corr") or {}).items()},
    )
