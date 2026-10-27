"""
QuantOS 2.0 — Risk Budget（T-601）
=================================

对应需求 R-C04 与契约附录 F。这个模块只回答三个问题：

  1. **风险贡献怎么算**：组合波动率按 Euler 分解到标的，再按净额占比分摊到
     策略。`Σ贡献 ≡ 组合波动率` 是数学恒等式，本模块把它写成断言而不是注释。
  2. **两个策略同向会不会重复计算**：不会。风险永远在**净额**上算，再按各策略
     在净额里的占比切回去。两个策略各买 10 万 BTC，组合承担的是 20 万 BTC 的
     风险（一次），不是两次。
  3. **怎么把风险预算换成额度**：`quota_i = budget_i × 风险目标 / σ_i`。
     等预算下高波动策略拿到的额度更小 —— 这才是"按风险分配"而不是"按钱分配"。

刻意不做的事：

  - **不猜波动率**。缺 σ 直接抛错。编一个"年化 15%"能让报告看起来很精确，
    实际上是假数字，而且会正好落在审批想看的那个数量级上。
  - **不猜相关性**。缺相关系数也抛错，要么显式声明对角线（假设不相关），
    要么显式声明一个常数 ρ。默认 0 是危险的：两个 Crypto 多头被当成不相关，
    风险会被系统性低估。
  - 不做协方差估计（那是研究层的事，本模块只消费估计结果）。
  - 不判断策略好坏，只回答"它占了多少风险"。

与 `capital_allocator` 的分工：

  - `capital_allocator` 按**配额**给钱（固定配额 / 波动率目标），管"这笔钱归谁"；
  - 本模块按**风险**给配额（风险预算 / 等风险贡献），管"风险怎么摊"。

两者都产出 `quota_pct`，谁生效由配置决定。不要同时用两套口径算同一个策略 ——
那样两边都会说自己对。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal, localcontext
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from quantos_core.contracts import ContractViolation, D, q

#: 净额小到可以当成 0 的绝对阈值（货币单位）。低于它就不再分摊占比 ——
#: 除以一个接近 0 的净额会把风险贡献放大到荒谬的数量级。
FLAT_EPS = Decimal("0.01")

#: 判定"对锁"的相对阈值：|净额| / 毛额 低于它就算净额被对冲掉了。
HEDGE_RATIO = Decimal("0.05")

#: 等风险贡献迭代的默认收敛判据（最大偏差 / 平均贡献）。
ERC_TOL = Decimal("1e-10")


class RiskModelIncomplete(ContractViolation):
    """风险模型缺输入。刻意不复用通用异常：这类错误要能一眼区分于"算错了"。"""


# --------------------------------------------------------------------------- #
#  1. 风险模型
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RiskModel:
    """
    波动率 + 相关系数。两者都是**年化**口径。

    缺相关系数时**不会**默认成 0，而是抛 `RiskModelIncomplete`。
    理由：ρ 默认 0 会让"两个同向 Crypto 仓位"的风险被低估一半，而这个低估
    不会以任何形式报错 —— 它会安静地让风控额度看起来还有余量。

    想要"假设不相关"就显式用 `RiskModel.diagonal(vols)`，
    想要"假设同涨同跌"就 `RiskModel.uniform(vols, Decimal(1))`。
    """

    vols: Mapping[str, Decimal]
    corr: Mapping[Tuple[str, str], Decimal] = field(default_factory=dict)
    default_corr: Optional[Decimal] = None
    label: str = "未命名风险模型"

    # -- 便捷构造 -------------------------------------------------------- #

    @classmethod
    def diagonal(cls, vols: Mapping[str, Decimal], *, label: str = "对角线（假设不相关）"
                 ) -> "RiskModel":
        return cls(vols=vols, corr={}, default_corr=Decimal(0), label=label)

    @classmethod
    def uniform(cls, vols: Mapping[str, Decimal], rho: Decimal, *,
                label: str = "") -> "RiskModel":
        return cls(vols=vols, corr={}, default_corr=D(rho),
                   label=label or f"常数相关 ρ={D(rho)}")

    # -- 取值 ------------------------------------------------------------ #

    def vol(self, symbol: str) -> Decimal:
        if symbol not in self.vols:
            raise RiskModelIncomplete(
                f"风险模型 {self.label!r} 没有 {symbol} 的波动率。"
                f"不允许用默认值代替：请补估或把该标的移出组合")
        v = D(self.vols[symbol])
        if v <= 0:
            raise RiskModelIncomplete(f"{symbol} 的波动率必须为正，收到 {v}")
        return v

    def rho(self, a: str, b: str) -> Decimal:
        if a == b:
            return Decimal(1)
        key = (a, b) if (a, b) in self.corr else (b, a)
        if key in self.corr:
            r = D(self.corr[key])
            if not (Decimal(-1) <= r <= Decimal(1)):
                raise RiskModelIncomplete(f"相关系数 ρ({a},{b})={r} 越界")
            return r
        if self.default_corr is None:
            raise RiskModelIncomplete(
                f"风险模型 {self.label!r} 没有 ρ({a},{b})，也没有声明默认相关性。"
                f"用 RiskModel.diagonal() 或 RiskModel.uniform() 显式表态")
        return D(self.default_corr)

    def cov(self, a: str, b: str) -> Decimal:
        return self.rho(a, b) * self.vol(a) * self.vol(b)

    def to_dict(self) -> Dict:
        return {
            "label": self.label,
            "vols": {k: str(D(v)) for k, v in self.vols.items()},
            "default_corr": None if self.default_corr is None else str(D(self.default_corr)),
            "pairs": {f"{a}|{b}": str(D(v)) for (a, b), v in self.corr.items()},
        }


# --------------------------------------------------------------------------- #
#  2. 风险分解
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SymbolRisk:
    """单个标的的风险贡献。`contribution` 是货币口径（年化）。"""

    symbol: str
    net_notional: Decimal
    gross_notional: Decimal
    marginal: Decimal          # dσ/dn，单位：波动率 / 货币
    contribution: Decimal      # n × marginal，加总 = 组合波动率
    pct: Decimal               # contribution / σ
    hedged: bool = False       # 净额被对冲掉，本模型认为无风险

    def to_dict(self) -> Dict:
        return {
            "symbol": self.symbol,
            "net_notional": str(q(self.net_notional, 2)),
            "gross_notional": str(q(self.gross_notional, 2)),
            "marginal": str(q(self.marginal, 8)),
            "contribution": str(q(self.contribution, 4)),
            "pct": str(q(self.pct, 6)),
            "hedged": self.hedged,
        }


@dataclass(frozen=True)
class PortfolioRisk:
    """
    一次组合风险度量。所有 Decimal 都是货币口径（年化波动率 × 名义金额）。

    `closes` 校验 `Σ symbol.contribution == sigma`。
    `attribution_closes` 校验 `Σ by_strategy == sigma`。

    注意（和 B4 归因日报同样的教训）：`closes` 是 Euler 分解的数学恒等式，
    只要算法没写错就恒成立，它**证明不了**输入是对的。真正能抓错的是别处：

      - `standalone_sum > sigma` 必须成立（有分散化就必然如此），
        如果算出来 standalone ≤ sigma，说明协方差矩阵不是半正定的；
      - 单个标的组合下 `sigma` 必须精确等于 `|n| × σ`；
      - `hedged` 标的数 > 0 时要人看一眼 —— 模型说它没风险，真风险在基差上。
    """

    sigma: Decimal
    by_symbol: Tuple[SymbolRisk, ...]
    by_strategy: Mapping[str, Decimal]
    strategy_pct: Mapping[str, Decimal]
    standalone_sum: Decimal
    diversification: Decimal
    nav: Optional[Decimal] = None
    sigma_pct_nav: Optional[Decimal] = None
    used_budget_pct: Optional[Decimal] = None
    model_label: str = ""
    notes: Tuple[str, ...] = ()

    # -- 校验 ------------------------------------------------------------ #

    @property
    def contribution_sum(self) -> Decimal:
        return sum((s.contribution for s in self.by_symbol), Decimal(0))

    @property
    def closes(self) -> bool:
        return _close(self.contribution_sum, self.sigma)

    @property
    def attribution_sum(self) -> Decimal:
        return sum(self.by_strategy.values(), Decimal(0))

    @property
    def attribution_closes(self) -> bool:
        return _close(self.attribution_sum, self.sigma)

    @property
    def hedged(self) -> Tuple[str, ...]:
        return tuple(s.symbol for s in self.by_symbol if s.hedged)

    @property
    def top(self) -> Optional[SymbolRisk]:
        if not self.by_symbol:
            return None
        return max(self.by_symbol, key=lambda s: abs(s.contribution))

    # -- 序列化 ---------------------------------------------------------- #

    def to_dict(self) -> Dict:
        return {
            "sigma": str(q(self.sigma, 4)),
            "sigma_pct_nav": (None if self.sigma_pct_nav is None
                              else str(q(self.sigma_pct_nav, 6))),
            "used_budget_pct": (None if self.used_budget_pct is None
                                else str(q(self.used_budget_pct, 6))),
            "diversification": str(q(self.diversification, 6)),
            "standalone_sum": str(q(self.standalone_sum, 4)),
            "closes": self.closes,
            "attribution_closes": self.attribution_closes,
            "by_symbol": [s.to_dict() for s in self.by_symbol],
            "by_strategy": {k: str(q(v, 4)) for k, v in self.by_strategy.items()},
            "strategy_pct": {k: str(q(v, 6)) for k, v in self.strategy_pct.items()},
            "hedged": list(self.hedged),
            "model": self.model_label,
            "notes": list(self.notes),
        }


def _close(a: Decimal, b: Decimal, rel: Decimal = Decimal("1e-9")) -> bool:
    tol = abs(b) * rel + Decimal("1e-6")
    return abs(a - b) <= tol


def portfolio_risk(net: Mapping[str, Decimal], model: RiskModel, *,
                   per_strategy: Optional[Mapping[str, Mapping[str, Decimal]]] = None,
                   nav: Optional[Decimal] = None,
                   risk_budget_pct: Optional[Decimal] = None
                   ) -> PortfolioRisk:
    """
    组合风险 + 标的/策略两级分解。

    `net` 是**净额**（已经过 Capital Allocator 的净额层）。传毛额进来会得到
    一个偏大的数字，而且同向重复计算就从这里开始 —— 所以这个函数只接受净额，
    参数名就叫 `net`。

    `per_strategy` 可选：strategy_id → symbol → 该策略的名义归属。提供时按
    占比把标的贡献切给策略；不提供时 `by_strategy` 为空。
    """
    per = per_strategy or {}
    # 标的集合必须同时来自净额**和**策略持仓：净额被对冲成 0 的标的在 net 里
    # 只剩一个 0，只按 net 筛会把最需要告警的情形整个丢掉。
    held = {s for book in per.values() for s, v in book.items()
            if abs(D(v)) > FLAT_EPS}
    names = sorted({s for s, v in net.items() if abs(D(v)) > FLAT_EPS} | held)

    # 只在真有净敞口时要求 σ。净额为 0 的标的风险贡献恒为 0，不该因为它
    # 没估波动率就让整个组合算不出来。
    for s in names:
        if abs(D(net.get(s, Decimal(0)))) > FLAT_EPS:
            model.vol(s)

    with localcontext() as ctx:
        ctx.prec = 34
        # ---- σ² = ΣΣ n_i n_j cov(i,j) ---------------------------------- #
        sigma_sq = Decimal(0)
        for i in names:
            for j in names:
                sigma_sq += D(net[i]) * D(net[j]) * model.cov(i, j)
        if sigma_sq < 0:
            # 半正定矩阵下不可能为负。为负说明有人手写了不自洽的相关矩阵。
            raise RiskModelIncomplete(
                f"组合方差算成负数（{sigma_sq}）：风险模型的协方差矩阵不自洽，"
                f"检查相关系数是否满足半正定")
        sigma = sigma_sq.sqrt() if sigma_sq > 0 else Decimal(0)

        # sigma == 0 时**也要**建条目：完全对锁恰恰是最需要喊出来的情形。
        # 早先写成 `if sigma > 0` 才建，结果对锁组合既没有 hedged 标记也没有
        # 告警 —— 一个看起来"零风险、很干净"的空列表。
        by_symbol: List[SymbolRisk] = []
        for s in names:
            if sigma > 0:
                row = sum((D(net[j]) * model.cov(s, j) for j in names), Decimal(0))
                marginal = row / sigma
                contrib = D(net[s]) * marginal
                pct = contrib / sigma
            else:
                marginal = Decimal(0)
                contrib = Decimal(0)
                pct = Decimal(0)
            by_symbol.append(SymbolRisk(
                symbol=s, net_notional=D(net[s]),
                gross_notional=_gross(s, per),
                marginal=marginal, contribution=contrib, pct=pct,
                hedged=_is_hedged(s, D(net[s]), per),
            ))

        # ---- 策略级：按净额占比切 ------------------------------------- #
        by_strategy: Dict[str, Decimal] = {}
        strategy_pct: Dict[str, Decimal] = {}
        for item in by_symbol:
            n = item.net_notional
            if abs(n) <= FLAT_EPS:
                continue
            for sid, book in per.items():
                own = D(book.get(item.symbol, Decimal(0)))
                if own == 0:
                    continue
                share = own / n
                by_strategy[sid] = by_strategy.get(sid, Decimal(0)) + item.contribution * share
        if sigma > 0:
            strategy_pct = {k: v / sigma for k, v in by_strategy.items()}

        # ---- 分散化：各自独立的波动之和 vs 净额组合波动 ---------------- #
        standalone = Decimal(0)
        for sid, book in per.items():
            var = Decimal(0)
            for a, va in book.items():
                for b, vb in book.items():
                    var += D(va) * D(vb) * model.cov(a, b)
            if var > 0:
                standalone += var.sqrt()
        if not per:
            standalone = sum((abs(D(net[s])) * model.vol(s) for s in names), Decimal(0))

        div = (Decimal(1) - sigma / standalone) if standalone > 0 else Decimal(0)

        notes: List[str] = []
        hedged = [s for s in by_symbol if s.hedged]
        for item in hedged:
            notes.append(
                f"{item.symbol} 净额被对冲掉（毛额 {q(item.gross_notional, 2)}，"
                f"净额 {q(item.net_notional, 2)}）：模型判它无价格风险。"
                f"真实风险是基差与两腿成交时差，本模型不建模")
        if per and sigma > 0 and not by_strategy:
            notes.append("有净额但没有策略归属：风险无法摊到策略，检查 per_strategy 是否漏了标的")
        if sigma == 0 and any(abs(D(net[s])) > FLAT_EPS for s in names):
            notes.append("组合波动率为 0 但仍有净额：多半是相关性写成 -1 的自洽性错误")

        sigma_pct_nav = None
        used_budget = None
        if nav is not None and D(nav) > 0:
            sigma_pct_nav = sigma / D(nav)
            if risk_budget_pct is not None and D(risk_budget_pct) > 0:
                used_budget = sigma_pct_nav / D(risk_budget_pct)

        return PortfolioRisk(
            sigma=sigma, by_symbol=tuple(by_symbol),
            by_strategy=by_strategy, strategy_pct=strategy_pct,
            standalone_sum=standalone, diversification=div,
            nav=None if nav is None else D(nav),
            sigma_pct_nav=sigma_pct_nav, used_budget_pct=used_budget,
            model_label=model.label, notes=tuple(notes),
        )


def _gross(symbol: str, per: Mapping[str, Mapping[str, Decimal]]) -> Decimal:
    return sum((abs(D(book.get(symbol, Decimal(0)))) for book in per.values()),
               Decimal(0))


def _is_hedged(symbol: str, net: Decimal, per: Mapping[str, Mapping[str, Decimal]]
               ) -> bool:
    gross = _gross(symbol, per)
    if gross <= FLAT_EPS:
        return False
    return abs(net) / gross <= HEDGE_RATIO


# --------------------------------------------------------------------------- #
#  3. 等风险贡献权重
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RiskParityResult:
    weights: Dict[str, Decimal]
    contributions: Dict[str, Decimal]
    sigma: Decimal
    iterations: int
    converged: bool
    max_deviation: Decimal
    notes: Tuple[str, ...] = ()

    def to_dict(self) -> Dict:
        return {
            "weights": {k: str(q(v, 8)) for k, v in self.weights.items()},
            "contributions": {k: str(q(v, 6)) for k, v in self.contributions.items()},
            "sigma": str(q(self.sigma, 8)),
            "iterations": self.iterations,
            "converged": self.converged,
            "max_deviation": str(q(self.max_deviation, 10)),
            "notes": list(self.notes),
        }


def equal_risk_weights(model: RiskModel, symbols: Sequence[str], *,
                       iterations: int = 200, tol: Decimal = ERC_TOL,
                       long_only: bool = True) -> RiskParityResult:
    """
    求让每个标的**风险贡献相等**的权重（Spinu 坐标下降）。

    为什么不用 `1/σ` 倒数波动率：它在**两个特例**下恰好等于等风险贡献 ——
    对角线（零相关）和**常数相关**（所有两两相关系数相同）。真正的相关性结构
    从来不是常数：一旦出现"某几个名字彼此高度相关、其余不相关"的块状结构，
    `1/σ` 会把风险集中到那个块上，看起来是三个仓位，实际是一个赌注。
    块相关下 `1/σ` 与等风险解的差异，就是本函数存在的理由。

    实现：目标是 `w_i · (Σw)_i = λ`（对所有 i 相同）。固定其它坐标时这是关于
    `w_i` 的一元二次方程，可直接解出闭式更新，比梯度法稳定得多。
    """
    names = [s for s in symbols]
    if not names:
        raise ContractViolation("equal_risk_weights 需要至少一个标的")
    if len(set(names)) != len(names):
        raise ContractViolation("symbols 有重复项")
    for s in names:
        model.vol(s)

    n = len(names)
    with localcontext() as ctx:
        ctx.prec = 40
        # 初值：倒数波动率并归一化（对角线情形下它就是答案）
        raw = {s: Decimal(1) / model.vol(s) for s in names}
        total = sum(raw.values(), Decimal(0))
        w = {s: raw[s] / total for s in names}

        converged = False
        used = 0
        dev = Decimal(0)
        for step in range(1, iterations + 1):
            used = step
            c = {i: sum((w[j] * model.cov(i, j) for j in names), Decimal(0))
                 for i in names}
            sigma_sq = sum((w[i] * c[i] for i in names), Decimal(0))
            if sigma_sq <= 0:
                raise RiskModelIncomplete(
                    "等风险贡献求解遇到 σ²≤0：协方差矩阵不自洽")
            sigma = sigma_sq.sqrt()
            lam = sigma_sq / Decimal(n)
            prev = dict(w)
            for i in names:
                a_ii = model.cov(i, i)
                b_i = sum((w[j] * model.cov(i, j) for j in names if j != i),
                          Decimal(0))
                # a·w² + b·w - λ = 0 的正根
                disc = b_i * b_i + 4 * a_ii * lam
                w[i] = (-b_i + disc.sqrt()) / (2 * a_ii)
                if long_only and w[i] < 0:
                    raise ContractViolation(
                        f"{i} 解出负权重 {w[i]}：在给定相关结构下无法用纯多头实现等风险")
            wiggle = sum((abs(w[i] - prev[i]) for i in names), Decimal(0))
            rc = {i: w[i] * sum((w[j] * model.cov(i, j) for j in names), Decimal(0)) / sigma
                  for i in names}
            mean_rc = sum(rc.values(), Decimal(0)) / Decimal(n)
            dev = (max(abs(v - mean_rc) for v in rc.values()) / mean_rc
                   if mean_rc > 0 else Decimal(0))
            if dev <= tol and wiggle <= tol:
                converged = True
                break

        c = {i: sum((w[j] * model.cov(i, j) for j in names), Decimal(0))
             for i in names}
        sigma_sq = sum((w[i] * c[i] for i in names), Decimal(0))
        sigma = sigma_sq.sqrt() if sigma_sq > 0 else Decimal(0)
        rc = ({i: w[i] * c[i] / sigma for i in names} if sigma > 0
              else {i: Decimal(0) for i in names})
        total_w = sum(w.values(), Decimal(0))
        w = {i: w[i] / total_w for i in names}

        notes: List[str] = []
        if not converged:
            notes.append(
                f"{iterations} 次迭代未收敛到 {tol}（当前最大偏差 {q(dev, 10)}）："
                f"目标可能不可达，或相关系数矩阵病态")
        if model.default_corr is not None:
            notes.append(
                f"相关性用的是常数假设 ρ={D(model.default_corr)}（{model.label}），"
                f"不是估计值。结论对这个假设非常敏感")
        return RiskParityResult(weights=w, contributions=rc, sigma=sigma,
                                iterations=used, converged=converged,
                                max_deviation=dev, notes=tuple(notes))


# --------------------------------------------------------------------------- #
#  4. 风险预算 → 额度
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BudgetPlan:
    """
    风险预算换算出的配额。`quotas` 是占 NAV 的比例，Σ 不超过 `max_total`。

    `scale < 1` 表示预算算出来的额度超过了可投资比例，被按比例压过 ——
    这件事必须显式记录，否则"预算给满了但只用了 60%"会变成没人解释的差额。
    """

    quotas: Dict[str, Decimal]
    raw_quotas: Dict[str, Decimal]
    scale: Decimal
    total: Decimal
    cash_pct: Decimal
    risk_target_pct: Decimal
    notes: Tuple[str, ...] = ()

    def to_dict(self) -> Dict:
        return {
            "quotas": {k: str(q(v, 8)) for k, v in self.quotas.items()},
            "raw_quotas": {k: str(q(v, 8)) for k, v in self.raw_quotas.items()},
            "scale": str(q(self.scale, 8)),
            "total": str(q(self.total, 8)),
            "cash_pct": str(q(self.cash_pct, 8)),
            "risk_target_pct": str(q(self.risk_target_pct, 8)),
            "notes": list(self.notes),
        }


def budget_to_quotas(budgets: Mapping[str, Decimal], vols: Mapping[str, Decimal], *,
                     risk_target_pct: Decimal,
                     max_total: Decimal = Decimal("1")) -> BudgetPlan:
    """
    `quota_i = budget_i × risk_target_pct / σ_i`，超上限时整体等比压缩。

    推导很朴素：策略 i 的风险 ≈ quota_i × σ_i，希望它等于 `budget_i × 风险目标`，
    于是 quota_i 就解出来了。所以等预算下 σ 高的策略拿到更少的钱 —— 这才是
    "按风险分配"。

    三条硬规则：

      - `budget ≤ 0` 的策略配额为 0（不是"随便给点"）。零预算就是不给钱。
      - 缺 σ 抛错，不猜。
      - Σquota > max_total 时按比例压缩并**在 notes 里说明压了多少**。
        不允许悄悄加杠杆把预算塞满。
    """
    if D(risk_target_pct) <= 0:
        raise ContractViolation(f"risk_target_pct 必须为正，收到 {risk_target_pct}")
    if D(max_total) <= 0 or D(max_total) > 1:
        raise ContractViolation(f"max_total 必须在 (0,1]，收到 {max_total}")

    raw: Dict[str, Decimal] = {}
    notes: List[str] = []
    for sid, b in budgets.items():
        budget = D(b)
        if budget < 0:
            raise ContractViolation(f"{sid} 的风险预算不能为负，收到 {budget}")
        if budget == 0:
            raw[sid] = Decimal(0)
            notes.append(f"{sid} 风险预算为 0：不给配额（不是给个小额度）")
            continue
        if sid not in vols:
            raise RiskModelIncomplete(f"{sid} 缺波动率，无法把风险预算换成额度")
        v = D(vols[sid])
        if v <= 0:
            raise RiskModelIncomplete(f"{sid} 的波动率必须为正，收到 {v}")
        raw[sid] = budget * D(risk_target_pct) / v

    total_raw = sum(raw.values(), Decimal(0))
    scale = Decimal(1)
    if total_raw > D(max_total):
        scale = D(max_total) / total_raw
        notes.append(
            f"预算换算出的额度合计 {q(total_raw, 4)} 超过可投资比例 {q(D(max_total), 4)}，"
            f"整体压缩到 {q(scale, 6)} 倍。压缩不是加杠杆，是承认预算目标定高了")

    quotas = {sid: q(v * scale, 8) for sid, v in raw.items()}
    total = sum(quotas.values(), Decimal(0))
    return BudgetPlan(quotas=quotas, raw_quotas={k: q(v, 8) for k, v in raw.items()},
                      scale=q(scale, 8), total=q(total, 8),
                      cash_pct=q(D(max_total) - total, 8),
                      risk_target_pct=q(D(risk_target_pct), 8), notes=tuple(notes))


def budgets_from_policies(policies: Iterable) -> Dict[str, Decimal]:
    """从 `StrategyPolicy` 列表取 `risk_budget_pct`。全为 0 时返回空字典。"""
    out = {p.strategy_id: D(p.risk_budget_pct) for p in policies}
    if all(v == 0 for v in out.values()):
        return {}
    return out


def apply_budget_plan(policies: Iterable, plan: BudgetPlan) -> List:
    """
    把配额写回 `StrategyPolicy`。**不动** `risk_budget_pct` 本身 ——
    预算是输入，配额是算出来的结果，混在一起以后无法重算。
    """
    out = []
    for p in policies:
        if p.strategy_id in plan.quotas:
            out.append(replace(p, quota_pct=plan.quotas[p.strategy_id]))
        else:
            out.append(p)
    return out


# --------------------------------------------------------------------------- #
#  5. 风险预算偏离检查（B4 之后的工作台指标源）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BudgetDrift:
    """实际风险占用 vs 预算。运维面用来看"谁超支了"。"""

    strategy_id: str
    budget_pct: Decimal
    actual_pct: Decimal
    over: bool

    def to_dict(self) -> Dict:
        return {
            "strategy_id": self.strategy_id,
            "budget_pct": str(q(self.budget_pct, 6)),
            "actual_pct": str(q(self.actual_pct, 6)),
            "over": self.over,
        }


def budget_drift(actual: Mapping[str, Decimal],
                 budgets: Mapping[str, Decimal],
                 *, tol: Decimal = Decimal("0.02")) -> List[BudgetDrift]:
    """
    逐策略比对"预算占比"与"实际风险占比"。`tol` 是相对预算的容忍度。

    注意 `actual` 传的是 `PortfolioRisk.strategy_pct`（占组合风险的比例），
    不是名义占比 —— 用名义占比跟风险预算比会得出"全都超支"的假结论。
    """
    out: List[BudgetDrift] = []
    for sid in sorted(set(actual) | set(budgets)):
        b = D(budgets.get(sid, Decimal(0)))
        a = D(actual.get(sid, Decimal(0)))
        limit = b + (b * D(tol) if b > 0 else D(tol))
        out.append(BudgetDrift(strategy_id=sid, budget_pct=b, actual_pct=a,
                               over=a > limit))
    return out


__all__ = [
    "RiskModel", "RiskModelIncomplete", "SymbolRisk", "PortfolioRisk",
    "portfolio_risk", "RiskParityResult", "equal_risk_weights",
    "BudgetPlan", "budget_to_quotas", "budgets_from_policies", "apply_budget_plan",
    "BudgetDrift", "budget_drift",
]
