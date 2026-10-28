"""
QuantOS 2.0 — Factor Model（B13 / T-1301）
==========================================

`covariance.estimate()` 的问题不是不准，是**参数太多**：n 个标的要
`n + n(n-1)/2` 个参数。794 只 A 股 = 315,271 个，而观测只有 O(T) 天 ——
自由度比 0.006，相关矩阵基本是噪声。收缩能压住噪声，但压不出信息。

因子模型换一个问题：不问「这两只一起动吗」，问「它们对同一批共同驱动各有多少
暴露」。于是

    Σ = B · F · Bᵀ + diag(Ψ)

参数从 `n + n(n-1)/2` 降到 `n·k + k(k+1)/2 + n`。k=4 时 794 只只要约 4,000 个，
自由度比从 0.006 变成 0.47。

四个刻意选择
------------

1. **因子必须能讲清经济含义，所以不用 PCA。** PCA 载荷不可解释，符号还会在
   相邻窗口之间整体翻转 —— 这跟 B7 立下的规矩（「σ 和 ρ 是从哪来的」必须能追）
   直接冲突。这里用四个能从日线价量直接算出来的特征因子：
   市场 / 规模 / 动量 / 短期反转。
2. **因子收益用「特征组合」口径，不用逐日截面 WLS。** `f_k,t = Σ_i w_ik,t · r_i,t`，
   w 是截面 z 值标准化后的多空权重。它与截面回归的因子收益等价，但不需要每天
   解一次线性方程，而且每一个 f 都能用一句话解释：「该因子多空组合当天的收益」。
3. **特征只用 t-1 及之前的数据。** t 日的权重必须完全由 t 日收盘之前已知的信息
   构成 —— 与 B12 的「`_settle` 必须先于 `_signals`」是同一条规矩。
4. **残差方差逐标的单独估。** 不假设同方差：一只银行和一只题材股的特质波动能差
   三倍，压成同一个数等于把风险预算算错。

三条诚实边界（写进报告，不藏）
------------------------------

- 因子收益是**等权特征组合**，没有市值加权、没有行业中性化。行业效应会落到残差里，
  表现为残差偏大 —— 这是「宁可残差大，也不假装解释得了行业」。
- 载荷是**窗口内静态回归**，不滚动。因子暴露会漂移（成长股变价值股），
  本模块给的是区间平均暴露，不是今天的暴露。
- 参数个数降到 O(n·k) **不等于估计变准**。k 个因子解释不了的共同变动会被当成
  「特质」，于是相关性被系统性低估 —— 这是因子模型最经典的失效方式，
  所以 `r2_avg` / `residual_share` 必须跟着结果一起交出去。

历史价格里的坑沿用 `covariance` 那四条（年化口径 / 交集对齐 / 停牌同价 / 未复权跳变），
本模块不重复实现，只补一件它没做的事：**缺失不填充**。某只标的某天没有 bar，
它在截面上就缺席，不会被当成「今天涨 0%」。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from quantos_core.contracts import ContractViolation, D, q
from quantos_core.covariance import (
    MIN_DOF_RATIO,
    MIN_OBS_HARD,
    TRADING_DAYS_EQUITY,
    CovarianceEstimate,
    _psd_via_cholesky,
    ewma_weights,
)


# --------------------------------------------------------------------------- #
#  异常与常量
# --------------------------------------------------------------------------- #


class FactorError(ContractViolation):
    """
    因子层给不出模型。

    刻意不复用 `covariance.EstimationError`：那一个说的是「数据不够」，
    这一个说的是「模型结构不成立」（因子共线、截面太窄、载荷回归奇异）。
    两种错误该被不同的人修：前者等数据，后者改设计。
    """


#: 支持的因子。新增因子必须同时改 `_exposures()`，否则会在构造期被拒。
KNOWN_FACTORS: Tuple[str, ...] = ("market", "size", "momentum", "reversal", "volatility")

#: 因子收益为 0 的容忍度：z 值归一后单日 |f| < 1e-15 视为「没算出来」。
_EPS = 1e-15

#: 解线性方程时判定奇异的阈值。
_SINGULAR = 1e-12


# --------------------------------------------------------------------------- #
#  配置
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FactorConfig:
    """
    因子模型的估计参数。**会影响数量级的项都没有默认值。**

    `annualization` 与 `covariance.EstimationConfig` 同理由：填错不报错，
    只让 σ 差 20%，然后额度跟着差 20%。股票 252、Crypto 365。
    """

    annualization: int
    #: 用哪些因子。顺序决定载荷列的顺序，落盘后要保持一致才能比较。
    factors: Tuple[str, ...] = ("market", "size", "momentum", "reversal")
    #: 规模用「近 N 日平均成交额的对数」。成交额是价量数据里唯一能摸到
    #: 规模/流动性的东西，不是真正的市值 —— 这一点必须承认。
    size_window: int = 20
    momentum_window: int = 20
    reversal_window: int = 5
    volatility_window: int = 20
    #: 截面至少要这么多只标的，z 值才有意义。低于它当天该因子不成立。
    min_names: int = 8
    #: 单只标的最少参与的因子收益天数。少于它直接剔除（进 `dropped`）。
    min_obs: int = 60
    #: 窗口内数据覆盖率下限。低于它，这个标的当天不参与该因子的截面。
    #: 用覆盖率而不是「窗口必须全满」：A 股停牌太常见，要求全满等于把
    #: 一半标的踢出去；但也不能用 3 天数据算 20 日动量。
    min_coverage: Decimal = Decimal("0.6")
    #: 截面 z 值的截断。单只妖股的 z 能到 8，不截断的话一个标的就决定
    #: 整天的因子收益。
    z_clip: Decimal = Decimal("3")
    #: 因子收益加权的半衰期（交易日）。None = 等权。
    halflife: Optional[int] = None
    #: 残差**年化波动率**的下限。完美拟合（Ψ=0）会让 Σ 对角为 0，
    #: 协方差矩阵变成奇异的，后面的 Cholesky 会炸。给一个极小值并计数告警，
    #: 而不是让它悄悄变成一个除零。
    residual_vol_floor: Decimal = Decimal("0.0001")

    def __post_init__(self) -> None:
        if self.annualization <= 0:
            raise FactorError("annualization 必须为正（252 或 365）")
        fs = tuple(self.factors)
        object.__setattr__(self, "factors", fs)
        if not fs:
            raise FactorError("至少要有一个因子")
        if len(set(fs)) != len(fs):
            raise FactorError(f"因子重复：{fs}")
        unknown = [f for f in fs if f not in KNOWN_FACTORS]
        if unknown:
            raise FactorError(f"未知因子 {unknown}；可用：{list(KNOWN_FACTORS)}")
        for name in ("size_window", "momentum_window", "reversal_window",
                     "volatility_window"):
            w = getattr(self, name)
            if w < 2:
                raise FactorError(f"{name} 至少为 2（{w}）")
        if self.min_names < 4:
            raise FactorError("min_names 至少为 4：少于 4 只的截面算不出像样的 z 值")
        if self.min_obs < 2:
            raise FactorError("min_obs 至少为 2")
        cov = D(self.min_coverage)
        object.__setattr__(self, "min_coverage", cov)
        if not (Decimal(0) < cov <= Decimal(1)):
            raise FactorError(f"min_coverage 必须在 (0,1]，收到 {cov}")
        clip = D(self.z_clip)
        object.__setattr__(self, "z_clip", clip)
        if clip <= 0:
            raise FactorError("z_clip 必须为正")
        floor = D(self.residual_vol_floor)
        object.__setattr__(self, "residual_vol_floor", floor)
        if floor < 0:
            raise FactorError("residual_vol_floor 不能为负")
        if self.halflife is not None and self.halflife <= 0:
            raise FactorError("halflife 必须为正")

    @property
    def window(self) -> int:
        """构造截面特征需要的最大回看长度（含 1 日滞后）。"""
        return 1 + max(self.size_window, self.momentum_window,
                       self.reversal_window, self.volatility_window)


# --------------------------------------------------------------------------- #
#  收益面板
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ReturnPanel:
    """
    对齐到全局日历的收益矩阵。内部用 float，进契约时才转 Decimal。

    为什么不用 Bar 对象：794 只 × 1880 天 = 149 万个 Bar dataclass 会吃掉
    几百 MB，而这里只需要两列（收益、成交额）。与 B12 的 `BarPanel` 同一个理由，
    同一个取舍。缺失是 `None`，**不是 0** —— 停牌日不是「涨 0%」。
    """

    days: Tuple[date, ...]
    symbols: Tuple[str, ...]
    rets: Dict[str, Tuple[Optional[float], ...]]
    amounts: Dict[str, Tuple[Optional[float], ...]]
    skipped: Tuple[str, ...] = ()

    # -- 构造 ------------------------------------------------------------ #

    @classmethod
    def from_rows(cls, rows: Mapping[str, Sequence[Sequence]],
                  *, min_bars: int = 2) -> "ReturnPanel":
        """
        `rows`: `{symbol: [(day, close), ...]}` 或 `{symbol: [(day, close, amount), ...]}`。

        收益按**相邻两根可得 bar** 算：停牌 30 天后复牌那天，它的收益是这 30 天的
        累计涨跌 —— 这是持仓人真实承受的收益，不该被摊成 30 个零。
        """
        if not rows:
            raise FactorError("没有任何标的")
        cleaned: Dict[str, List[Tuple[date, Decimal, Optional[Decimal]]]] = {}
        skipped: List[str] = []
        all_days: set = set()
        for sym in sorted(rows):
            bars = rows[sym]
            seen: Dict[date, Tuple[Decimal, Optional[Decimal]]] = {}
            for row in bars:
                if len(row) < 2:
                    raise FactorError(f"{sym} 的 bar 至少要有 (day, close)，收到 {row!r}")
                day = row[0]
                close = D(row[1])
                amount = None
                if len(row) >= 3 and row[2] is not None:
                    amount = D(row[2])
                if close <= 0:
                    raise FactorError(
                        f"{sym} 在 {day} 的收盘价 {close} <= 0；"
                        f"不许用 0 兜底，那会让收益变成 ±100%")
                if day in seen:
                    raise FactorError(f"{sym} 在 {day} 有重复 bar")
                seen[day] = (close, amount)
            if len(seen) < min_bars:
                skipped.append(f"{sym}:bars<{min_bars}")
                continue
            seq = [(d, seen[d][0], seen[d][1]) for d in sorted(seen)]
            cleaned[sym] = seq
            all_days.update(d for d, _, _ in seq)
        if not cleaned:
            raise FactorError(f"没有标的满足 min_bars={min_bars}；跳过原因：{skipped}")
        days = tuple(sorted(all_days))
        index = {d: i for i, d in enumerate(days)}
        rets: Dict[str, Tuple[Optional[float], ...]] = {}
        amounts: Dict[str, Tuple[Optional[float], ...]] = {}
        for sym, seq in cleaned.items():
            r: List[Optional[float]] = [None] * len(days)
            a: List[Optional[float]] = [None] * len(days)
            prev: Optional[Decimal] = None
            for day, close, amount in seq:
                i = index[day]
                a[i] = float(amount) if amount is not None else None
                if prev is not None and prev > 0:
                    r[i] = float(close / prev - 1)
                prev = close
            rets[sym] = tuple(r)
            amounts[sym] = tuple(a)
        return cls(days=days, symbols=tuple(cleaned), rets=rets,
                   amounts=amounts, skipped=tuple(skipped))

    # -- 便捷 ------------------------------------------------------------ #

    @classmethod
    def load(cls, data_dir: str, *, adjust: bool = True, max_names: Optional[int] = None,
             start: Optional[date] = None, end: Optional[date] = None,
             min_bars: int = 2) -> "ReturnPanel":
        """
        读 `state/ashare/daily/*.csv`
        （`date,open,close,high,low,volume,amount,chg_pct,adj_factor`）。

        与 `backtest.BarPanel.load` 共用同一份数据与同一个复权口径（后复权：
        `价格 × adj_factor`，分红视为再投资）。为什么不让因子层自己去读未复权价：
        除权日会凭空出现 -30% 的「收益」，那会直接变成因子收益里的异常值，
        再被 z_clip 截断 —— 于是错误被藏起来而不是被修掉。

        `amount` 不乘复权因子：它是成交金额（元），跨时间可比性由截面 z 值解决
        （每天的公共缩放会在去均值/除标准差时抵消）。
        """
        import csv
        import os

        if not os.path.isdir(data_dir):
            raise FactorError(f"数据目录不存在：{data_dir}")
        files = sorted(f for f in os.listdir(data_dir) if f.endswith(".csv"))
        if not files:
            raise FactorError(f"{data_dir} 里没有 csv")
        if max_names:
            files = files[:max_names]
        rows: Dict[str, List[Tuple[date, Decimal, Optional[Decimal]]]] = {}
        for name in files:
            sym = name[:-4]
            seq: List[Tuple[date, Decimal, Optional[Decimal]]] = []
            with open(os.path.join(data_dir, name), "r", encoding="utf-8-sig") as fh:
                for rec in csv.DictReader(fh):
                    try:
                        day = date.fromisoformat(rec["date"])
                        if start and day < start:
                            continue
                        if end and day > end:
                            continue
                        adj = (D(rec["adj_factor"])
                               if adjust and rec.get("adj_factor") else Decimal(1))
                        close = D(rec["close"]) * adj
                        amount_s = (rec.get("amount") or "").strip()
                        amount = D(amount_s) if amount_s else None
                    except (KeyError, ArithmeticError, ValueError):
                        continue
                    if close <= 0:
                        continue
                    seq.append((day, close, amount))
            if len(seq) >= min_bars:
                rows[sym] = seq
        return cls.from_rows(rows, min_bars=min_bars)

    def __len__(self) -> int:
        return len(self.days)

    def coverage(self, symbol: str) -> float:
        """
        该标的在全局日历上的覆盖率 —— 分母是交易日，分子是**有收益的天数**。

        注意第一根 bar 出不了收益（没有前值），所以满覆盖的标的是
        `(T-1)/T` 而不是 1.0。这个细节写在这里，是因为回归样本覆盖率门槛
        （`min_coverage`）用的就是这个数：如果按 1.0 期待满覆盖，
        所有标的都会被判成"缺一天"，然后全被剔除。
        """
        seq = self.rets.get(symbol)
        if not seq:
            return 0.0
        return sum(1 for v in seq if v is not None) / float(len(seq))


# --------------------------------------------------------------------------- #
#  因子收益
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FactorReturns:
    """
    因子收益序列，与 `days` 一一对齐。某天某因子不成立记 `None`。

    为什么要保留 `None` 而不是把那天删掉：删掉之后，`values[k][i]` 与
    `panel.days` 的下标关系就断了，做「有没有未来函数」的反证测试会非常难写。
    保留空洞，索引恒等于 `panel.days[1:]`。
    """

    days: Tuple[date, ...]
    factors: Tuple[str, ...]
    values: Dict[str, Tuple[Optional[float], ...]]
    breadth: Dict[str, Tuple[Optional[int], ...]]

    def valid_indices(self) -> Tuple[int, ...]:
        """所有因子都有值的那些下标。回归只在这批天上做。"""
        out: List[int] = []
        for i in range(len(self.days)):
            ok = True
            for f in self.factors:
                v = self.values[f][i]
                if v is None:
                    ok = False
                    break
            if ok:
                out.append(i)
        return tuple(out)

    def describe(self) -> str:
        idx = self.valid_indices()
        if not idx:
            return "因子收益：没有一天是所有因子同时成立的"
        head = f"因子收益 {len(idx)} 天（{self.days[idx[0]]}~{self.days[idx[-1]]}）"
        parts = []
        for f in self.factors:
            n = sum(1 for i in range(len(self.days)) if self.values[f][i] is not None)
            parts.append(f"{f} {n}")
        return f"{head} · " + " / ".join(parts)


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / float(len(xs)) if xs else 0.0


def _stdev(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / float(len(xs) - 1))


def _window(series: Sequence[Optional[float]], t: int, w: int,
            min_cov: float) -> List[float]:
    """
    取 `[t-w, t-1]` 之间的非缺失值。覆盖率不足时返回空列表。

    这是**参考实现**，保留它是为了跟下面的前缀和版本做差分测试：
    两套实现算出同一批数字，才敢说前缀和没写错窗口边界。速度不够，
    但正确性可读 —— 生产路径走 `_exposures()` 的前缀和。
    """
    lo = max(0, t - w)
    vals = [v for v in series[lo:t] if v is not None]
    need = int(math.ceil(w * min_cov))
    if len(vals) < max(2, need):
        return []
    return vals


@dataclass(frozen=True)
class _Prefix:
    """
    逐标的的前缀和。把 O(w) 的窗口统计压成 O(1)。

    794 只 × 1880 天 × 4 因子 × 20 日窗口 ≈ 1.2 亿次循环 —— 纯 Python 要几分钟。
    前缀和把它降到 600 万次，同一份口径，快 20 倍。这个优化值得做，
    但它必须与 `_window()` 的参考实现逐步对拍，否则窗口边界错一格不会报错，
    只会让因子收益悄悄错位一天。
    """

    n_r: List[int]
    s_r: List[float]
    s_r2: List[float]
    s_lr: List[float]
    n_a: List[int]
    s_la: List[float]


def _prefix(rets: Sequence[Optional[float]],
            amounts: Sequence[Optional[float]]) -> _Prefix:
    T = len(rets)
    n_r = [0] * (T + 1)
    s_r = [0.0] * (T + 1)
    s_r2 = [0.0] * (T + 1)
    s_lr = [0.0] * (T + 1)
    n_a = [0] * (T + 1)
    s_la = [0.0] * (T + 1)
    for i in range(T):
        n_r[i + 1] = n_r[i]
        s_r[i + 1] = s_r[i]
        s_r2[i + 1] = s_r2[i]
        s_lr[i + 1] = s_lr[i]
        n_a[i + 1] = n_a[i]
        s_la[i + 1] = s_la[i]
        v = rets[i]
        if v is not None:
            n_r[i + 1] += 1
            s_r[i + 1] += v
            s_r2[i + 1] += v * v
            # 价格恒为正 ⇒ 1+v>0，log1p 不会炸
            s_lr[i + 1] += math.log1p(v)
        a = amounts[i]
        if a is not None and a > 0:
            n_a[i + 1] += 1
            s_la[i + 1] += math.log(a)
    return _Prefix(n_r=n_r, s_r=s_r, s_r2=s_r2, s_lr=s_lr, n_a=n_a, s_la=s_la)


def _need(w: int, min_cov: float) -> int:
    return max(2, int(math.ceil(w * min_cov)))


def _win_rets(pre: _Prefix, t: int, w: int, need: int
              ) -> Optional[Tuple[int, float, float, float]]:
    """窗口内的 (个数, Σr, Σr², Σlog1p(r))。覆盖不足返回 None。"""
    lo = max(0, t - w)
    cnt = pre.n_r[t] - pre.n_r[lo]
    if cnt < need:
        return None
    return (cnt, pre.s_r[t] - pre.s_r[lo], pre.s_r2[t] - pre.s_r2[lo],
            pre.s_lr[t] - pre.s_lr[lo])


def _win_amount(pre: _Prefix, t: int, w: int, need: int
                ) -> Optional[Tuple[int, float]]:
    lo = max(0, t - w)
    cnt = pre.n_a[t] - pre.n_a[lo]
    if cnt < need:
        return None
    return (cnt, pre.s_la[t] - pre.s_la[lo])


def _zscores(raw: Mapping[str, float], min_names: int,
             clip: float) -> Dict[str, float]:
    """
    截面 z 值 + 截断。少于 `min_names` 只或截面无离散度时返回空字典。

    截面无离散度不是「z 全等于 0」，是**这个因子今天不成立**：全都一样值时
    多空组合根本不存在，硬算出 0 会让它假装自己中性。
    """
    if len(raw) < min_names:
        return {}
    xs = list(raw.values())
    m = _mean(xs)
    sd = _stdev(xs) if len(xs) > 1 else 0.0
    if sd <= 0:
        return {}
    out: Dict[str, float] = {}
    for k, v in raw.items():
        z = (v - m) / sd
        if z > clip:
            z = clip
        elif z < -clip:
            z = -clip
        out[k] = z
    return out


def _exposures(panel: ReturnPanel, config: FactorConfig, t: int,
               pre: Optional[Mapping[str, _Prefix]] = None
               ) -> Dict[str, Dict[str, float]]:
    """
    第 t 根 bar 的截面特征，**只用 t-1 及之前**。

    注意 `t` 的语义：`t` 是「要做预测的那一天」，所有窗口都截止到 `t-1`。
    这里写错一格就是未来函数，而且回测会变得更漂亮、实盘会变差 —— 不报错。
    所以本函数有一个刻意保留的慢速参考实现（`_exposures_reference`），
    自测拿两者对拍。
    """
    if pre is None:
        pre = {s: _prefix(panel.rets[s], panel.amounts[s]) for s in panel.symbols}
    min_cov = float(config.min_coverage)
    clip = float(config.z_clip)
    out: Dict[str, Dict[str, float]] = {}
    want = config.factors

    if "market" in want:
        # 市场因子没有截面特征：它就是一个等权组合，权重在 `_weights` 里给。
        out["market"] = {s: 0.0 for s in panel.symbols
                         if panel.rets[s][t] is not None}

    if "size" in want:
        raw: Dict[str, float] = {}
        need = _need(config.size_window, min_cov)
        for s in panel.symbols:
            got = _win_amount(pre[s], t, config.size_window, need)
            if got is None:
                continue
            cnt, total = got
            raw[s] = total / cnt
        out["size"] = _zscores(raw, config.min_names, clip)

    if "momentum" in want:
        raw = {}
        need = _need(config.momentum_window, min_cov)
        for s in panel.symbols:
            got = _win_rets(pre[s], t, config.momentum_window, need)
            if got is None:
                continue
            raw[s] = math.expm1(got[3])
        out["momentum"] = _zscores(raw, config.min_names, clip)

    if "reversal" in want:
        raw = {}
        need = _need(config.reversal_window, min_cov)
        for s in panel.symbols:
            got = _win_rets(pre[s], t, config.reversal_window, need)
            if got is None:
                continue
            # 短期反转：过去跌得多的往后涨。取负号是方向约定，不是技巧。
            raw[s] = -math.expm1(got[3])
        out["reversal"] = _zscores(raw, config.min_names, clip)

    if "volatility" in want:
        raw = {}
        need = _need(config.volatility_window, min_cov)
        for s in panel.symbols:
            got = _win_rets(pre[s], t, config.volatility_window, need)
            if got is None:
                continue
            cnt, s1, s2, _ = got
            if cnt < 2:
                continue
            var = (s2 - s1 * s1 / cnt) / (cnt - 1)
            raw[s] = math.sqrt(max(0.0, var))
        out["volatility"] = _zscores(raw, config.min_names, clip)

    return {k: v for k, v in out.items() if v}


def _exposures_reference(panel: ReturnPanel, config: FactorConfig,
                         t: int) -> Dict[str, Dict[str, float]]:
    """
    慢速参考实现：直接切窗口求值，不碰前缀和。

    留着它不是怀旧，是为了让「前缀和的窗口边界」这件事**可被证明**。
    没有对拍，错一格的窗口会安静地让因子收益落后一天 —— 而因子收益错一天，
    载荷和 Σ 都会跟着错，报告上看不出任何异常。
    """
    min_cov = float(config.min_coverage)
    clip = float(config.z_clip)
    out: Dict[str, Dict[str, float]] = {}
    want = config.factors

    if "market" in want:
        out["market"] = {s: 0.0 for s in panel.symbols
                         if panel.rets[s][t] is not None}

    if "size" in want:
        raw: Dict[str, float] = {}
        for s in panel.symbols:
            vals = _window(panel.amounts[s], t, config.size_window, min_cov)
            if not vals or any(v <= 0 for v in vals):
                continue
            raw[s] = _mean([math.log(v) for v in vals])
        out["size"] = _zscores(raw, config.min_names, clip)

    if "momentum" in want:
        raw = {}
        for s in panel.symbols:
            vals = _window(panel.rets[s], t, config.momentum_window, min_cov)
            if not vals:
                continue
            acc = 1.0
            for v in vals:
                acc *= (1.0 + v)
            raw[s] = acc - 1.0
        out["momentum"] = _zscores(raw, config.min_names, clip)

    if "reversal" in want:
        raw = {}
        for s in panel.symbols:
            vals = _window(panel.rets[s], t, config.reversal_window, min_cov)
            if not vals:
                continue
            acc = 1.0
            for v in vals:
                acc *= (1.0 + v)
            raw[s] = -(acc - 1.0)
        out["reversal"] = _zscores(raw, config.min_names, clip)

    if "volatility" in want:
        raw = {}
        for s in panel.symbols:
            vals = _window(panel.rets[s], t, config.volatility_window, min_cov)
            if not vals:
                continue
            raw[s] = _stdev(vals)
        out["volatility"] = _zscores(raw, config.min_names, clip)

    return {k: v for k, v in out.items() if v}


def _weights(factor: str, z: Mapping[str, float],
             available: Iterable[str]) -> Dict[str, float]:
    """
    把 z 值变成组合权重。

    市场因子：等权，权重和 = 1，收益就是当天的等权平均。
    其余因子：多空各归一化到 1（gross 2），这样不同因子之间的收益可比 ——
    否则规模因子的 z 分布比反转因子宽，两者收益就不是一个量级。
    """
    names = [s for s in z if s in available]
    if factor == "market":
        if not names:
            return {}
        w = 1.0 / float(len(names))
        return {s: w for s in names}
    longs = {s: z[s] for s in names if z[s] > 0}
    shorts = {s: z[s] for s in names if z[s] < 0}
    if not longs or not shorts:
        # 单边截面（全正或全负）没有多空组合。不硬凑，那天该因子不成立。
        return {}
    lp = sum(longs.values())
    sn = -sum(shorts.values())
    out: Dict[str, float] = {}
    for s, v in longs.items():
        out[s] = v / lp
    for s, v in shorts.items():
        out[s] = v / sn
    return out


def build_factor_returns(panel: ReturnPanel,
                         config: FactorConfig) -> FactorReturns:
    """
    逐日构造因子收益。复杂度 O(T · n · k)，不需要任何矩阵求逆。
    """
    n_days = len(panel.days)
    days: List[date] = []
    values: Dict[str, List[Optional[float]]] = {f: [] for f in config.factors}
    breadth: Dict[str, List[Optional[int]]] = {f: [] for f in config.factors}
    pre = {s: _prefix(panel.rets[s], panel.amounts[s]) for s in panel.symbols}

    for t in range(1, n_days):
        days.append(panel.days[t])
        expo = _exposures(panel, config, t, pre)
        for f in config.factors:
            z = expo.get(f) or {}
            if not z:
                values[f].append(None)
                breadth[f].append(None)
                continue
            have = {s: panel.rets[s][t] for s in z if panel.rets[s][t] is not None}
            if len(have) < config.min_names:
                values[f].append(None)
                breadth[f].append(None)
                continue
            w = _weights(f, z, have)
            if not w:
                values[f].append(None)
                breadth[f].append(None)
                continue
            f_ret = math.fsum(w[s] * have[s] for s in w)
            if abs(f_ret) < _EPS:
                f_ret = 0.0
            values[f].append(f_ret)
            breadth[f].append(len(w))

    return FactorReturns(
        days=tuple(days),
        factors=tuple(config.factors),
        values={f: tuple(v) for f, v in values.items()},
        breadth={f: tuple(b) for f, b in breadth.items()},
    )


# --------------------------------------------------------------------------- #
#  线性代数（纯 stdlib，k 很小，不值得引 numpy）
# --------------------------------------------------------------------------- #


def _solve(a: Sequence[Sequence[float]], b: Sequence[float]) -> List[float]:
    """
    高斯消元 + 部分选主元。奇异就抛错，**坚决不做伪逆兜底**。

    伪逆会把「两个因子完全共线」这种结构性错误悄悄摊平：载荷变成一堆没有意义
    但数值好看的数，回归残差看着还行，Σ 也正定。这正是这个项目最怕的那种失败 ——
    不报错，只给出一个编出来的模型。
    """
    n = len(a)
    if n == 0 or len(b) != n:
        raise FactorError("求解维度不匹配")
    m = [list(row) + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < _SINGULAR:
            raise FactorError(
                "因子收益矩阵奇异：因子之间存在完全共线（或某因子收益恒为 0）。"
                "这不是数值精度问题，是模型结构问题 —— 去掉冗余因子再估。")
        m[col], m[piv] = m[piv], m[col]
        pv = m[col][col]
        for r in range(col + 1, n):
            factor = m[r][col] / pv
            if factor:
                for c in range(col, n + 1):
                    m[r][c] -= factor * m[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        s = m[r][n] - math.fsum(m[r][c] * x[c] for c in range(r + 1, n))
        x[r] = s / m[r][r]
    return x


def _matmul_btb(b: Sequence[Sequence[float]],
                f: Sequence[Sequence[float]]) -> List[List[float]]:
    """`B · F · Bᵀ`（n×n）。只为诊断与组装，不做分解。"""
    n = len(b)
    k = len(b[0]) if n else 0
    bf = [[math.fsum(b[i][a] * f[a][c] for a in range(k)) for c in range(k)]
          for i in range(n)]
    out = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i, n):
            v = math.fsum(bf[i][c] * b[j][c] for c in range(k))
            out[i][j] = v
            out[j][i] = v
    return out


# --------------------------------------------------------------------------- #
#  估计
# --------------------------------------------------------------------------- #


def estimate_factor_model(panel: ReturnPanel, config: FactorConfig,
                          *, label: str = "") -> CovarianceEstimate:
    """
    主入口：面板 → 因子收益 → 载荷 → Σ = B·F·Bᵀ + diag(Ψ)。

    流程顺序不能换：
    1. 先定样本（所有因子都成立的交易日）；
    2. 再算因子协方差 F（这是「共同驱动之间怎么互相动」）；
    3. 再逐标的回归求载荷与残差；
    4. 最后组装并对角化检查。

    第 2 步必须在第 3 步之前：如果先用每个标的自己的样本估 F，不同标的的
    Σ 就建立在不同的 F 上，`Σ策略贡献 ≡ σ` 这条恒等式会在 B6 那边对不上。
    """
    fr = build_factor_returns(panel, config)
    idx = fr.valid_indices()
    if len(idx) < config.min_obs:
        raise FactorError(
            f"因子收益只有 {len(idx)} 个有效交易日，少于 min_obs={config.min_obs}。"
            f"{fr.describe()}。样本不足不返回「凑合能用」的模型。")
    k = len(config.factors)
    if k < 1:
        raise FactorError("至少要有一个因子")

    # ---- 1. 因子收益矩阵（去均值） ------------------------------------ #
    raw_f = [[float(fr.values[f][i]) for f in config.factors] for i in idx]
    f_mean = [_mean([row[a] for row in raw_f]) for a in range(k)]
    fm = [[row[a] - f_mean[a] for a in range(k)] for row in raw_f]
    n_obs = len(idx)
    wts = [float(w) for w in ewma_weights(n_obs, config.halflife)]
    wsum = math.fsum(wts)

    # ---- 2. 因子协方差 F（k×k，按日频） ------------------------------- #
    f_cov = [[math.fsum(wts[t] * fm[t][a] * fm[t][b] for t in range(n_obs)) / wsum
              for b in range(k)] for a in range(k)]
    ok, why = _psd_via_cholesky(f_cov)
    if not ok:
        raise FactorError(f"因子协方差不是正定矩阵（{why}）：因子高度共线或有常数项，"
                          f"不能拿去组装 Σ")

    # 权重对齐到各标的样本时用同一套，先记下每日权重
    ann = float(config.annualization)
    floor_var = (float(config.residual_vol_floor) ** 2) / ann if ann > 0 else 0.0

    dropped: List[str] = []
    warnings: List[str] = []
    floored = 0
    loadings: Dict[str, List[float]] = {}
    resid_var: Dict[str, float] = {}
    r2s: List[float] = []
    total_var = 0.0
    total_resid = 0.0

    for sym in panel.symbols:
        series = panel.rets[sym]
        sample: List[Tuple[int, float]] = []
        for pos, i in enumerate(idx):
            v = series[i + 1]
            if v is not None:
                sample.append((pos, v))
        if len(sample) < config.min_obs:
            dropped.append(f"{sym}:obs<{config.min_obs}")
            continue
        cover = len(sample) / float(n_obs)
        if cover < float(config.min_coverage):
            dropped.append(f"{sym}:coverage<{config.min_coverage}")
            continue
        if len(sample) == n_obs:
            a_mat = f_cov                     # 完整样本，复用同一份
            sw = wsum
        else:
            # 缺日：按自己的样本重算 A，权重重新归一。不填充、不外推。
            a_mat = [[math.fsum(wts[t] * fm[t][a] * fm[t][b]
                                for t, _ in sample) / math.fsum(wts[t] for t, _ in sample)
                      for b in range(k)] for a in range(k)]
            sw = math.fsum(wts[t] for t, _ in sample)
        r_vals = [v for _, v in sample]
        r_mean = math.fsum(wts[t] * v for t, v in sample) / sw
        c_vec = [math.fsum(wts[t] * fm[t][a] * (v - r_mean)
                           for t, v in sample) for a in range(k)]
        try:
            beta = _solve(a_mat, c_vec)
        except FactorError as exc:
            raise FactorError(f"{sym} 的载荷回归失败：{exc}") from exc
        # 残差
        ss = 0.0
        for t, v in sample:
            fit = math.fsum(beta[a] * fm[t][a] for a in range(k))
            ss += wts[t] * (v - r_mean - fit) ** 2
        psi = ss / sw
        if psi <= floor_var:
            psi = floor_var
            floored += 1
        var_i = math.fsum(wts[t] * (v - r_mean) ** 2 for t, v in sample) / sw
        loadings[sym] = beta
        resid_var[sym] = psi
        total_var += var_i
        total_resid += psi
        if var_i > 0:
            r2s.append(max(0.0, min(1.0, 1.0 - psi / var_i)))

    if not loadings:
        raise FactorError(f"没有一个标的能估出载荷；剔除原因：{dropped[:5]}")

    names = sorted(loadings)
    b_mat = [loadings[s] for s in names]
    bfb = _matmul_btb(b_mat, f_cov)

    # ---- 3. 组装 Σ 并转成 vols / corr -------------------------------- #
    cov_diag = [(bfb[i][i] + resid_var[s]) for i, s in enumerate(names)]
    vols: Dict[str, Decimal] = {}
    for i, s in enumerate(names):
        if cov_diag[i] <= 0:
            raise FactorError(f"{s} 的方差非正（{cov_diag[i]}）：Σ 不成立")
        v = math.sqrt(cov_diag[i] * ann)
        vols[s] = q(v, 10)

    corr: Dict[Tuple[str, str], Decimal] = {}
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            denom = math.sqrt(cov_diag[i] * cov_diag[j])
            if denom <= 0:
                continue
            rho = bfb[i][j] / denom
            rho = max(-1.0, min(1.0, rho))
            corr[(names[i], names[j])] = q(rho, 8)

    # PSD 检查：B·F·Bᵀ 是 PSD，加上对角正数后仍是 PSD；这里查的是「组装没错」
    sub = [[0.0] * min(8, len(names)) for _ in range(min(8, len(names)))]
    for i in range(len(sub)):
        for j in range(len(sub)):
            denom = math.sqrt(cov_diag[i] * cov_diag[j]) or 1.0
            sub[i][j] = bfb[i][j] / denom
        sub[i][i] = 1.0
    ok, why = _psd_via_cholesky(sub)
    if not ok:
        warnings.append(f"相关矩阵前 8 只的子块不是正定（{why}），实盘前必须复核")

    # ---- 4. 诊断 ------------------------------------------------------ #
    r2_avg = _mean(r2s) if r2s else 0.0
    resid_share = (total_resid / total_var) if total_var > 0 else 1.0
    if floored:
        warnings.append(
            f"{floored} 只标的的残差方差被抬到年化 {config.residual_vol_floor} 的下限："
            f"因子把它们解释得太完美，真实情况更可能是样本太短或价格被平滑过")
    if r2_avg < 0.10:
        warnings.append(
            f"平均 R² 只有 {r2_avg:.3f}：这批因子几乎没解释力，"
            f"Σ 退化成一个对角矩阵 + 微弱共同项，等效于假设「各标的互不相关」")
    if resid_share < 0.05:
        warnings.append(
            f"残差只占 {resid_share:.1%}：因子解释度过高，检查是不是有未来函数"
            f"（例如特征用了 t 日当天的收益）")
    n_params = len(names) * k + k * (k + 1) // 2 + len(names)
    sample_params = len(names) + len(names) * (len(names) - 1) // 2
    breadth_by = {}
    for f in config.factors:
        got = [v for v in fr.breadth[f] if v is not None]
        breadth_by[f] = q(_mean([float(v) for v in got]) if got else 0.0, 1)

    factor_corr = {
        f"{config.factors[a]}|{config.factors[b]}":
            str(q(f_cov[a][b] / math.sqrt(f_cov[a][a] * f_cov[b][b]), 6))
        for a in range(k) for b in range(a + 1, k)
        if f_cov[a][a] > 0 and f_cov[b][b] > 0}

    # 因子高度共线时必须说出来：|ρ|>0.85 的两列在数值上仍然可解，但载荷会
    # 两边分账，谁对哪只股票重要变得无法解释 —— 模型还「能跑」，含义却没了。
    for pair, val in factor_corr.items():
        if abs(float(val)) >= 0.85:
            warnings.append(
                f"因子 {pair} 的相关系数 {val} 绝对值 ≥0.85：这两列在回归里会互相"
                f"分账，载荷失去可解释性。考虑去掉其中一个。")

    factor_info = {
        "factors": list(config.factors),
        "factor_vol_annual": {
            f: str(q(math.sqrt(max(0.0, f_cov[a][a]) * ann), 8))
            for a, f in enumerate(config.factors)},
        "factor_mean_annual": {
            f: str(q(f_mean[a] * ann, 8)) for a, f in enumerate(config.factors)},
        "factor_corr": factor_corr,
        "breadth_avg": {f: str(v) for f, v in breadth_by.items()},
        "loadings": {s: {config.factors[a]: str(q(loadings[s][a], 6))
                         for a in range(k)} for s in names[:50]},
        "r2_avg": str(q(r2_avg, 4)),
        "residual_share": str(q(resid_share, 4)),
        "n_params": n_params,
        "sample_n_params": sample_params,
        "dof_ratio_sample": str(q(Decimal(n_obs) / Decimal(sample_params), 4)
                                if sample_params else 0),
        # 逐回归口径。必须与总量口径一起摆出来：总量口径会把一个
        # 每次回归都有几百个观测的模型判成「自由度不足」，那不是保守，是错配。
        # 但本模块不据此自行放行 —— 两个数都交给人看。
        "dof_loadings": str(q(Decimal(n_obs) / Decimal(k), 1)),
        "dof_factor_cov": str(q(Decimal(n_obs) / Decimal(k * (k + 1) // 2), 1)),
        "n_days": n_obs,
        "floored": floored,
        "method_note": ("因子口径：Σ = B·F·Bᵀ + diag(Ψ)；"
                        "因子收益为等权特征组合，非市值加权、非行业中性"),
    }

    if fr.days:
        window_start, window_end = fr.days[idx[0]], fr.days[idx[-1]]
    else:                                        # pragma: no cover - 上面已拦
        window_start, window_end = panel.days[0], panel.days[-1]

    return CovarianceEstimate(
        vols=vols,
        corr=corr,
        n_obs=n_obs,
        n_assets=len(names),
        window_start=window_start,
        window_end=window_end,
        annualization=config.annualization,
        halflife=config.halflife,
        shrinkage=Decimal(0),
        dropped=tuple(dropped),
        warnings=tuple(warnings),
        label=label or (f"因子模型（{k} 因子 · {n_obs} 观测 · "
                        f"{window_start}~{window_end}）"),
        raw_corr={},
        method="factor",
        n_params_override=n_params,
        factor_info=factor_info,
    )


def describe_model(est: CovarianceEstimate) -> List[str]:
    """
    给人看的多行摘要。命令行与运维面共用同一份文案，避免两边说法不一致。
    """
    info = est.factor_info or {}
    out = [est.describe()]
    if est.method != "factor":
        out.append("  口径：样本矩 + 收缩（不是因子模型）")
        return out
    fs = info.get("factors") or []
    out.append(f"  口径：因子模型 Σ = B·F·Bᵀ + diag(Ψ)，k={len(fs)} → {fs}")
    out.append(f"  参数个数：{est.n_params}（样本口径要 {info.get('sample_n_params')} 个）")
    out.append(
        f"  自由度比：{est.dof_ratio}（总量口径）／ 逐回归口径 "
        f"每个载荷 {info.get('dof_loadings')} obs、F 每个参数 "
        f"{info.get('dof_factor_cov')} obs（样本口径 {info.get('dof_ratio_sample')}，"
        f"门槛 {MIN_DOF_RATIO}）")
    out.append("  注：因子口径下「参数数 / 观测数」不是恰当门槛（载荷是逐标的单独回归的），"
               "但本模块照旧把两个数都摆出来，不替人放行。")
    out.append(f"  平均 R²：{info.get('r2_avg')}　残差占比：{info.get('residual_share')}"
               f"　截面宽度：{info.get('breadth_avg')}")
    if info.get("factor_vol_annual"):
        out.append(f"  因子年化波动：{info['factor_vol_annual']}")
    if info.get("factor_corr"):
        out.append(f"  因子间相关：{info['factor_corr']}")
    if est.dropped:
        out.append(f"  剔除 {len(est.dropped)} 只：{list(est.dropped[:5])}"
                   f"{' …' if len(est.dropped) > 5 else ''}")
    for w in est.warnings:
        out.append(f"  ⚠ {w}")
    if est.n_obs < MIN_OBS_HARD:
        out.append(f"  ⚠ 观测数 {est.n_obs} 低于硬门槛 {MIN_OBS_HARD}")
    return out
