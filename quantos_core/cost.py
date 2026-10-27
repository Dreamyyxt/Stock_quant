"""
QuantOS 2.0 — 成本模型（T-105）
==============================

对应需求 R-S05（成本模型必须真实）、R-D03（手续费与滑点单独归因）。

核心约定
--------
**回测与实盘共用同一份参数文件**（`config/cost_model.json`）。这是本模块存在的
唯一理由：如果回测里用 5bps 滑点、实盘实际吃 15bps，策略的"盈利"只是记账幻觉。

因此本模块同时提供两件事：
  `estimate(...)`      —— 下单前预估，回测用它模拟成交与费用
  `measure_drift(...)` —— 实盘成交后比对预估与真实值，偏差进日报

偏差持续为正说明参数偏乐观，必须调参而不是"接受它"。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, Mapping, Optional

from quantos_core.contracts import D, q

BPS = Decimal(10000)
DEFAULT_CONFIG_PATH = os.path.join("config", "cost_model.json")


# --------------------------------------------------------------------------- #
#  参数
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class VenueCostSchedule:
    """单一场所的费率与滑点。pct 类字段用 bps（1bp = 0.01%）。"""

    taker_fee_bps: Decimal = Decimal("5")
    maker_fee_bps: Decimal = Decimal("2")
    #: 单笔最低费用（股票常见 $1，Crypto 常为 0）。**只约束佣金，不约束税费**。
    min_fee: Decimal = Decimal(0)
    #: 基础滑点：正常流动性下买卖价差 + 冲击的保守估计。
    slippage_bps: Decimal = Decimal("8")
    #: 冲击成本增量：订单名义占 NAV 每 1% 额外增加的 bps。
    #: 订单越大，市场越不愿意按当前价成交，必须体现出来。
    impact_bps_per_pct_nav: Decimal = Decimal("0.5")
    #: 卖方单边税（A 股印花税 5bps）。这是**方向性**成本，买卖不对称，
    #: 所以不能揉进 `taker_fee_bps` —— 揉进去等于给买方也收一遍印花税。
    sell_tax_bps: Decimal = Decimal(0)
    #: 双边过户费（A 股 0.1bps）。金额极小但方向敏感，漏掉会让
    #: "回测比实盘便宜一点点"，而那一点点乘以笔数就是可以看得见的偏差。
    transfer_bps: Decimal = Decimal(0)

    def __post_init__(self):
        for name in ("taker_fee_bps", "maker_fee_bps", "min_fee",
                     "slippage_bps", "impact_bps_per_pct_nav",
                     "sell_tax_bps", "transfer_bps"):
            if D(getattr(self, name)) < 0:
                raise ValueError(f"cost.{name} 不能为负")

    def to_dict(self) -> Dict[str, str]:
        return {k: str(getattr(self, k)) for k in (
            "taker_fee_bps", "maker_fee_bps", "min_fee",
            "slippage_bps", "impact_bps_per_pct_nav",
            "sell_tax_bps", "transfer_bps")}

    @staticmethod
    def from_dict(raw: Mapping[str, Any], *, base: Optional["VenueCostSchedule"] = None
                  ) -> "VenueCostSchedule":
        if not raw:
            return base or VenueCostSchedule()
        ref = base or VenueCostSchedule()
        kwargs = {}
        for key in ("taker_fee_bps", "maker_fee_bps", "min_fee",
                    "slippage_bps", "impact_bps_per_pct_nav",
                    "sell_tax_bps", "transfer_bps"):
            kwargs[key] = D(raw[key]) if key in raw else getattr(ref, key)
        return VenueCostSchedule(**kwargs)


@dataclass(frozen=True)
class CostEstimate:
    """
    一次下单的成本预估。费用与滑点必须分开，否则归因无法拆开（R-D03）。

    费用内部再分三层，因为它们的**方向和下限不同**：
      `fee`      佣金 —— 有最低收费（A 股 5 元），双边收
      `tax`      卖方税 + 过户费 —— 无最低收费，且只有卖出才收
      `slippage` 滑点 —— 不是现金流，是成交价差造成的损失
    账本的 FEE 分录吃的是 `total_fee`（佣金 + 税），滑点走 `slippage` 归因。
    """

    venue: str
    side: str
    notional: Decimal
    fee: Decimal
    slippage_cost: Decimal
    fee_bps: Decimal
    slippage_bps: Decimal
    reference_price: Decimal
    expected_price: Decimal
    model_version: str
    #: 卖方税 + 过户费。买方通常为 0（A 股印花税只在卖出收）。
    tax: Decimal = Decimal(0)
    tax_bps: Decimal = Decimal(0)

    @property
    def total_fee(self) -> Decimal:
        """佣金 + 税费。这个才是要打进 `Fill.fee` 的数字。"""
        return q(D(self.fee) + D(self.tax), 8)

    @property
    def total(self) -> Decimal:
        return q(self.total_fee + D(self.slippage_cost), 8)

    @property
    def total_bps(self) -> Decimal:
        return q(D(self.fee_bps) + D(self.tax_bps) + D(self.slippage_bps), 4)

    def to_dict(self) -> Dict[str, str]:
        return {
            "venue": self.venue, "side": self.side,
            "notional": str(q(self.notional, 2)),
            "fee": str(q(self.fee, 8)),
            "tax": str(q(self.tax, 8)),
            "total_fee": str(self.total_fee),
            "slippage_cost": str(q(self.slippage_cost, 8)),
            "total": str(self.total),
            "fee_bps": str(self.fee_bps), "tax_bps": str(self.tax_bps),
            "slippage_bps": str(self.slippage_bps),
            "expected_price": str(self.expected_price),
            "model_version": self.model_version,
        }


@dataclass(frozen=True)
class FeeDrift:
    """预估 vs 实际的偏差。用于发现"回测参数太乐观"。"""

    venue: str
    estimated_fee: Decimal
    actual_fee: Decimal
    estimated_slippage: Decimal
    actual_slippage: Decimal
    model_version: str

    @property
    def fee_drift(self) -> Decimal:
        return q(D(self.actual_fee) - D(self.estimated_fee), 8)

    @property
    def slippage_drift(self) -> Decimal:
        return q(D(self.actual_slippage) - D(self.estimated_slippage), 8)

    @property
    def is_material(self) -> bool:
        """任一维度偏差超过预估值 20% 即视为需要复核参数。"""
        for est, act in ((self.estimated_fee, self.actual_fee),
                         (self.estimated_slippage, self.actual_slippage)):
            e, a = D(est), D(act)
            if e == 0:
                if abs(a) > Decimal("0.5"):
                    return True
                continue
            if abs(a - e) / abs(e) > Decimal("0.2"):
                return True
        return False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "venue": self.venue,
            "estimated_fee": str(q(self.estimated_fee, 8)),
            "actual_fee": str(q(self.actual_fee, 8)),
            "fee_drift": str(self.fee_drift),
            "estimated_slippage": str(q(self.estimated_slippage, 8)),
            "actual_slippage": str(q(self.actual_slippage, 8)),
            "slippage_drift": str(self.slippage_drift),
            "is_material": self.is_material,
            "model_version": self.model_version,
        }


# --------------------------------------------------------------------------- #
#  模型
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class CostModel:
    """`base` 是默认费率，`venues` 覆盖单个场所。参数只能来自文件，不允许散落代码里。"""

    base: VenueCostSchedule = field(default_factory=VenueCostSchedule)
    venues: Mapping[str, VenueCostSchedule] = field(default_factory=dict)
    version: str = "builtin-v1"
    source_path: Optional[str] = None

    def schedule_for(self, venue: str) -> VenueCostSchedule:
        return self.venues.get(venue, self.base)

    # -- 预估 ------------------------------------------------------------ #

    def estimate(self, venue: str, side, quantity: Decimal, price: Decimal,
                 nav: Optional[Decimal] = None,
                 liquidity: str = "TAKER") -> CostEstimate:
        """
        预估单笔成本。

        `nav` 只用于冲击成本：同一笔 1 万美元订单，在 10 万和 1000 万的组合里
        对市场的压力完全不同，不区分就是自欺欺人。
        """
        sched = self.schedule_for(venue)
        qty = D(quantity)
        px = D(price)
        if qty <= 0 or px <= 0:
            raise ValueError(f"estimate 需要正的 quantity/price，收到 {qty}/{px}")
        notional = q(qty * px, 8)
        side_u = getattr(side, "value", str(side)).upper()

        fee_bps = D(sched.taker_fee_bps if liquidity.upper() == "TAKER"
                    else sched.maker_fee_bps)
        fee = q(notional * fee_bps / BPS, 8)
        if fee < D(sched.min_fee):
            fee = q(D(sched.min_fee), 8)

        # 税费是方向性的：印花税只对卖方收，过户费双边收。
        tax_bps = D(sched.transfer_bps)
        if side_u == "SELL":
            tax_bps += D(sched.sell_tax_bps)
        tax = q(notional * tax_bps / BPS, 8)

        slip_bps = D(sched.slippage_bps)
        if nav is not None and D(nav) > 0:
            pct_nav = notional / D(nav) * Decimal(100)
            slip_bps += D(sched.impact_bps_per_pct_nav) * pct_nav
        slippage_cost = q(notional * slip_bps / BPS, 8)

        drift = slip_bps / BPS
        expected = q(px * (Decimal(1) + drift) if side_u == "BUY"
                     else px * (Decimal(1) - drift), 10)

        return CostEstimate(
            venue=venue, side=side_u, notional=notional, fee=fee,
            slippage_cost=slippage_cost, fee_bps=q(fee_bps, 4),
            slippage_bps=q(slip_bps, 4), reference_price=px,
            expected_price=expected, model_version=self.version,
            tax=tax, tax_bps=q(tax_bps, 4),
        )

    def expected_fill_price(self, venue: str, side, price: Decimal,
                            quantity: Decimal, nav: Optional[Decimal] = None) -> Decimal:
        """回测撮合用：按模型把参考价推成预期成交价。"""
        return self.estimate(venue, side, quantity, price, nav).expected_price

    # -- 事后比对 -------------------------------------------------------- #

    def measure_drift(self, estimate: CostEstimate, actual_fee: Decimal,
                      actual_price: Decimal, quantity: Decimal,
                      decision_price: Decimal) -> FeeDrift:
        """
        实盘成交后核对：实际滑点 = 实际成交价相对决策价的偏离成本。

        与账本口径一致（ledger 里 slippage = qty×(p0-p) for BUY），
        这样日报两边能直接相加不打架。
        """
        qty = D(quantity)
        side = estimate.side.upper()
        p0, px = D(decision_price), D(actual_price)
        actual_slip = q(qty * (px - p0) if side == "BUY" else qty * (p0 - px), 8)
        return FeeDrift(
            venue=estimate.venue, estimated_fee=D(estimate.fee),
            actual_fee=D(actual_fee), estimated_slippage=D(estimate.slippage_cost),
            actual_slippage=actual_slip, model_version=estimate.model_version,
        )

    # -- 序列化 ---------------------------------------------------------- #

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "base": self.base.to_dict(),
            "venues": {v: s.to_dict() for v, s in self.venues.items()},
        }

    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> "CostModel":
        base = VenueCostSchedule.from_dict(raw.get("base") or {})
        venues = {name: VenueCostSchedule.from_dict(cfg, base=base)
                  for name, cfg in (raw.get("venues") or {}).items()}
        return CostModel(base=base, venues=venues,
                         version=str(raw.get("version", "file-v1")))


def load_cost_model(path: str = DEFAULT_CONFIG_PATH) -> CostModel:
    """
    从单一参数文件加载。文件不存在时回退内置默认并保留 `source_path=None`，
    便于在测试环境跑，但生产启动必须断言 source_path 不为 None。
    """
    if not os.path.exists(path):
        return CostModel()
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    model = CostModel.from_dict(raw)
    return CostModel(base=model.base, venues=model.venues,
                     version=model.version, source_path=os.path.abspath(path))


__all__ = [
    "BPS", "DEFAULT_CONFIG_PATH", "VenueCostSchedule", "CostEstimate",
    "FeeDrift", "CostModel", "load_cost_model",
]
