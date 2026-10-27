"""
QuantOS 2.0 — 运维指标采集（T-301）
==================================

对应需求 R-O01..O04（运维中心）与 B3 的 DoD：

> 出现任一异常时，运维中心能定位到**具体环节**，而不是只显示"系统异常"。

因此本模块的每个指标都必须回答三个问题：
  1. 现在是多少（`value`）；
  2. 好不好（`severity` + `threshold`）；
  3. **从哪读出来的**（`source`），出问题时能直接去那个对象上看。

设计约束：
  - **拉取式**（pull），不是上报式（push）：指标从真实对象现读，避免
    "上报了但实际没生效"这种最坑的情况；
  - 缺依赖就报 `not_configured`，**绝不**默认 ok —— 没接上的探针等于没监控；
  - 时间一律由注入的 clock 提供，回放/测试可确定；
  - 纯 stdlib，无网络。

指标分组：
  market.*     行情延迟与缺价
  feed.*       行情通道（WS）状态
  strategy.*   策略心跳
  exec.*       执行延迟 p95
  orders.*     在途 / 未知 / 失败
  reconcile.*  待对账与对账断裂
  risk.*       权限级、熔断原因、冻结标的
  portfolio.*  敞口与归因闭合（需提供价格）
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from quantos_core.contracts import (
    D, OrderState, PermissionLevel, StrategyRunState, TERMINAL_STATES, q, utcnow,
)

# --------------------------------------------------------------------------- #
#  严重级
# --------------------------------------------------------------------------- #


class Severity(str, Enum):
    OK = "ok"
    WARN = "warn"
    CRITICAL = "critical"
    #: 探针没接上。既不是 ok 也不是故障 —— 页面必须显式标出来。
    NOT_CONFIGURED = "not_configured"

    @property
    def rank(self) -> int:
        return {"ok": 0, "warn": 1, "not_configured": 2, "critical": 3}[self.value]


_OVERALL_ORDER = (Severity.CRITICAL, Severity.NOT_CONFIGURED,
                  Severity.WARN, Severity.OK)


@dataclass(frozen=True)
class Metric:
    """一条可定位的指标。"""

    key: str
    label: str
    value: Any
    unit: str
    severity: Severity
    threshold: str = ""
    detail: str = ""
    #: 数据来源（类名.方法），运维照这个去查。
    source: str = ""
    #: 所属环节，页面按这个分组，一眼看出是哪一段坏了。
    component: str = ""

    def to_dict(self) -> Dict:
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "unit": self.unit,
            "severity": self.severity.value,
            "threshold": self.threshold,
            "detail": self.detail,
            "source": self.source,
            "component": self.component,
        }


def _sev(flag: bool, bad: Severity) -> Severity:
    return bad if flag else Severity.OK


def _num(value: Any) -> Any:
    """Decimal → float 便于 JSON；保留 None 表示"没有这个数"。"""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return value


def p95(samples: Sequence[float]) -> Optional[float]:
    """
    nearest-rank 分位数。样本为空返回 None（不返回 0，0 会被误读成"很快"）。
    """
    if not samples:
        return None
    ordered = sorted(float(s) for s in samples)
    n = len(ordered)
    rank = max(1, math.ceil(0.95 * n))
    return ordered[rank - 1]


def _order_latency_ms(order) -> Optional[int]:
    """SUBMITTED → 终态 的毫秒数。没有终态返回 None（不是 0）。"""
    if order.state not in TERMINAL_STATES:
        return None
    submitted = next((ts for ts, st, _ in order.history
                      if st is OrderState.SUBMITTED), None)
    if submitted is None or not order.history:
        return None
    delta = (order.history[-1][0] - submitted).total_seconds() * 1000.0
    return int(delta) if delta >= 0 else None


# --------------------------------------------------------------------------- #
#  快照
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class OpsSnapshot:
    as_of: datetime
    metrics: Tuple[Metric, ...]
    facts: Mapping[str, Any] = field(default_factory=dict)

    # -- 汇总 ------------------------------------------------------------ #

    def by_key(self) -> Dict[str, Metric]:
        return {m.key: m for m in self.metrics}

    def worst(self) -> Severity:
        worst = Severity.OK
        for m in self.metrics:
            if m.severity.rank > worst.rank:
                worst = m.severity
        return worst

    def counts(self) -> Dict[str, int]:
        out = {s.value: 0 for s in Severity}
        for m in self.metrics:
            out[m.severity.value] += 1
        return out

    def root_causes(self) -> List[Dict[str, str]]:
        """
        出问题的指标，按严重级排序。

        这就是"定位到具体环节"的实现：页面直接展示这几条，而不是一句
        「系统异常，请联系管理员」。
        """
        bad = [m for m in self.metrics
               if m.severity in (Severity.CRITICAL, Severity.WARN)]
        bad.sort(key=lambda m: (-m.severity.rank, m.key))
        return [{
            "key": m.key,
            "component": m.component,
            "severity": m.severity.value,
            "detail": m.detail or m.label,
            "source": m.source,
        } for m in bad]

    def components(self) -> Dict[str, Dict[str, Any]]:
        """按环节聚合，给页面的健康条用。"""
        out: Dict[str, Dict[str, Any]] = {}
        for m in self.metrics:
            bucket = out.setdefault(m.component or "other",
                                    {"worst": "ok", "total": 0, "bad": 0})
            bucket["total"] += 1
            if m.severity is not Severity.OK:
                bucket["bad"] += 1
            if m.severity.rank > Severity(bucket["worst"]).rank:
                bucket["worst"] = m.severity.value
        return out

    def to_dict(self) -> Dict:
        overall = self.worst()
        return {
            "as_of": self.as_of.isoformat(),
            "overall": overall.value,
            # 一句话结论：能直接贴到告警里。
            "headline": self._headline(overall),
            "counts": self.counts(),
            "root_causes": self.root_causes(),
            "components": self.components(),
            "metrics": [m.to_dict() for m in self.metrics],
            "facts": dict(self.facts),
        }

    def _headline(self, overall: Severity) -> str:
        causes = self.root_causes()
        if overall is Severity.OK:
            return f"全部 {len(self.metrics)} 项指标正常"
        if not causes:
            missing = [m for m in self.metrics
                       if m.severity is Severity.NOT_CONFIGURED]
            if missing:
                parts = sorted({m.component for m in missing})
                return (f"{len(missing)} 项探针未接入"
                        f"（{', '.join(parts)}）—— 不等于正常，是没测")
            return f"整体 {overall.value}，但无具体条目"
        top = causes[0]
        extra = f"（另有 {len(causes) - 1} 项）" if len(causes) > 1 else ""
        return f"{top['component']} · {top['detail']}{extra}"


# --------------------------------------------------------------------------- #
#  采集器
# --------------------------------------------------------------------------- #


class OpsMetrics:
    """
    从真实对象拉取指标。所有依赖都是可选的：

      - 传了就从它读；
      - 没传就产出 `not_configured` 指标（显式暴露"这个探针没接"）。

    这样页面永远不会因为少接一个组件而显示"一切正常"。
    """

    #: 执行延迟阈值（毫秒）。中低频交易口径：1.5s 起警，5s 视为异常。
    EXEC_WARN_MS = 1500.0
    EXEC_CRIT_MS = 5000.0

    def __init__(self, *, market=None, oms=None, risk_state=None, ledger=None,
                 registry=None, reconciler=None,
                 live_gate=None, risk_model_store=None,
                 canary_router=None, research_inbox=None,
                 clock: Callable[[], datetime] = utcnow,
                 stale_sec: Optional[float] = None,
                 heartbeat_warn_sec: float = 90.0,
                 heartbeat_crit_sec: float = 300.0,
                 base_currency: str = "USD"):
        self.market = market
        self.oms = oms
        self.risk_state = risk_state
        self.ledger = ledger
        self.registry = registry
        self.reconciler = reconciler
        #: T-401 三级实盘开关。没接上就报 `not_configured` ——
        #: 「不知道实盘开着没有」和「实盘关着」是两件事，不能都显示正常。
        self.live_gate = live_gate
        #: B7：风险模型来源。没接上就报 `not_configured` ——
        #: 「不知道 σ 从哪来」和「σ 很健康」是两件事。
        self.risk_model_store = risk_model_store
        #: B6 灰度路由。直接读它而不是另存一份状态，避免两边不同步。
        self.canary_router = canary_router
        #: B6 AI 研究收件箱。
        self.research_inbox = research_inbox
        self._clock = clock
        self.stale_sec = stale_sec
        self.heartbeat_warn_sec = float(heartbeat_warn_sec)
        self.heartbeat_crit_sec = float(heartbeat_crit_sec)
        self.base_currency = base_currency

        #: venue → (status, note, updated_at)。由行情网关在连接事件里写入。
        self._feed_status: Dict[str, Tuple[str, str, datetime]] = {}
        #: strategy_id → 最近心跳时间。
        self._heartbeats: Dict[str, datetime] = {}

    # -- 外部写入的两个事实源 ------------------------------------------- #

    def set_feed_status(self, venue: str, status: str, note: str = "",
                        at: Optional[datetime] = None) -> None:
        """
        status: connected / degraded / disconnected。
        连接状态只能由网关上报（和价格不同，它没有"从别处推出来"的办法）。
        """
        st = (status or "").lower()
        if st not in ("connected", "degraded", "disconnected"):
            raise ValueError(f"未知的 feed 状态：{status!r}")
        self._feed_status[venue] = (st, note, at or self._clock())

    def beat(self, strategy_id: str, at: Optional[datetime] = None) -> None:
        self._heartbeats[strategy_id] = at or self._clock()

    def forget_heartbeat(self, strategy_id: str) -> None:
        self._heartbeats.pop(strategy_id, None)

    # -- 采集 ------------------------------------------------------------ #

    def sample(self, prices: Optional[Mapping[Tuple[str, str], Decimal]] = None
               ) -> OpsSnapshot:
        now = self._clock()
        metrics: List[Metric] = []
        metrics.extend(self._market_metrics(now))
        metrics.extend(self._feed_metrics(now))
        metrics.extend(self._heartbeat_metrics(now))
        metrics.extend(self._execution_metrics())
        metrics.extend(self._order_metrics())
        metrics.extend(self._reconcile_metrics())
        metrics.extend(self._risk_metrics())
        metrics.extend(self._live_metrics(now))
        if prices is not None:
            metrics.extend(self._portfolio_metrics(prices))
        metrics.extend(self._risk_model_metrics(now))
        metrics.extend(self._canary_metrics())
        metrics.extend(self._research_metrics(now))
        metrics.extend(self._strategy_metrics())

        snap = OpsSnapshot(as_of=now, metrics=tuple(metrics),
                           facts=self._facts(now, prices))
        return snap

    # -- 行情 ------------------------------------------------------------ #

    def _stale_limit(self) -> float:
        if self.stale_sec is not None:
            return float(self.stale_sec)
        # 与风控同一口径：风控认为陈旧的价格，这里也必须报出来。
        policy = getattr(self.risk_state, "_policy", None)
        if policy is not None:
            return float(getattr(policy, "stale_market_data_seconds", 120))
        return 120.0

    def _market_metrics(self, now: datetime) -> List[Metric]:
        out: List[Metric] = []
        if self.market is None:
            out.append(Metric(
                "market.latency", "行情延迟", None, "s", Severity.NOT_CONFIGURED,
                detail="未接入 MarketDataBook，行情延迟不可知",
                source="OpsMetrics.market", component="market"))
            return out

        limit = self._stale_limit()
        ages = self.market.age_sec_by_venue(now)
        if not ages:
            out.append(Metric(
                "market.latency", "行情延迟", None, "s", Severity.CRITICAL,
                threshold=f"< {limit:.0f}s",
                detail="行情簿为空：本次进程从未收到任何快照",
                source="MarketDataBook.age_sec_by_venue()", component="market"))
        for venue, age in sorted(ages.items()):
            age_s = float(D(age))
            sev = Severity.OK
            if age_s >= limit:
                sev = Severity.CRITICAL
            elif age_s >= limit * 0.6:
                sev = Severity.WARN
            out.append(Metric(
                f"market.latency.{venue}", f"{venue} 行情延迟", round(age_s, 3),
                "s", sev, threshold=f"< {limit:.0f}s（超限禁下单）",
                detail=(f"{venue} 最旧快照已 {age_s:.1f}s 未更新"
                        if sev is not Severity.OK else f"{venue} 行情新鲜"),
                source="MarketDataBook.age_sec_by_venue()", component="market"))

        report = self.market.staleness_report(limit, now)
        stale_list = list(report.get("stale") or ())
        defective = list(report.get("defective") or ())
        crossed = self.market.crossed()
        rejections = self.market.rejection_log
        out.append(Metric(
            "market.stale_symbols", "陈旧标的数", len(stale_list), "个",
            _sev(bool(stale_list), Severity.CRITICAL), threshold="= 0",
            detail=(", ".join(stale_list) if stale_list else "无陈旧标的"),
            source="MarketDataBook.staleness_report()['stale']",
            component="market"))
        out.append(Metric(
            "market.defective_symbols", "缺陷快照数", len(defective), "个",
            _sev(bool(defective), Severity.CRITICAL), threshold="= 0",
            detail=(", ".join(defective) if defective else "无缺价/单边/非法快照"),
            source="MarketDataBook.staleness_report()['defective']",
            component="market"))
        out.append(Metric(
            "market.crossed", "交叉盘口", len(crossed), "个",
            _sev(bool(crossed), Severity.CRITICAL), threshold="= 0",
            detail=", ".join(crossed) if crossed else "无交叉盘口（买价<卖价）",
            source="MarketDataBook.crossed()", component="market"))
        out.append(Metric(
            "market.rejected_quotes", "被拒快照数", len(rejections), "条",
            _sev(len(rejections) > 0, Severity.WARN), threshold="= 0",
            detail=(f"最近一条：{rejections[-1]}" if rejections else "无非法快照"),
            source="MarketDataBook.rejection_log()", component="market"))
        return out

    # -- 行情通道 -------------------------------------------------------- #

    def _feed_metrics(self, now: datetime) -> List[Metric]:
        venues = set(self._feed_status)
        if self.market is not None:
            venues |= {v for v, _ in self.market.keys()}
        if self.registry is not None:
            venues |= {i.venue for i in self.registry}
        if not venues:
            return [Metric(
                "feed.status", "行情通道", None, "", Severity.NOT_CONFIGURED,
                detail="没有已知场所，无法判断 WS 状态",
                source="OpsMetrics.set_feed_status()", component="feed")]

        out: List[Metric] = []
        for venue in sorted(venues):
            rec = self._feed_status.get(venue)
            if rec is None:
                out.append(Metric(
                    f"feed.status.{venue}", f"{venue} 行情通道", "unknown", "",
                    Severity.NOT_CONFIGURED,
                    detail=f"{venue} 从未上报连接状态（探针未接）",
                    source="OpsMetrics.set_feed_status()", component="feed"))
                continue
            status, note, at = rec
            sev = {"connected": Severity.OK,
                   "degraded": Severity.WARN,
                   "disconnected": Severity.CRITICAL}[status]
            age = (now - at).total_seconds()
            detail = f"{venue} 通道 {status}"
            if note:
                detail += f"：{note}"
            detail += f"（{age:.0f}s 前更新）"
            out.append(Metric(
                f"feed.status.{venue}", f"{venue} 行情通道", status, "",
                sev, detail=detail, source="OpsMetrics.set_feed_status()",
                component="feed"))
        return out

    # -- 策略心跳 -------------------------------------------------------- #

    def _heartbeat_metrics(self, now: datetime) -> List[Metric]:
        if self.registry is None:
            return [Metric(
                "strategy.heartbeat", "策略心跳", None, "s",
                Severity.NOT_CONFIGURED,
                detail="未接入 StrategyRegistry，无法列出策略实例",
                source="OpsMetrics.registry", component="strategy")]

        out: List[Metric] = []
        for inst in self.registry:
            sid = inst.strategy_id
            last = self._heartbeats.get(sid)
            if last is None:
                sev = (Severity.WARN if inst.tradable else Severity.OK)
                detail = (f"{sid} 从未上报心跳（策略未启动或探针未接）"
                          if inst.tradable else f"{sid} 已暂停，无心跳属正常")
                out.append(Metric(
                    f"strategy.heartbeat.{sid}", f"{sid} 心跳", None, "s", sev,
                    detail=detail, source="OpsMetrics.beat()",
                    component="strategy"))
                continue
            age = (now - last).total_seconds()
            sev = Severity.OK
            if age >= self.heartbeat_crit_sec:
                sev = Severity.CRITICAL
            elif age >= self.heartbeat_warn_sec:
                sev = Severity.WARN
            out.append(Metric(
                f"strategy.heartbeat.{sid}", f"{sid} 心跳", round(age, 1), "s",
                sev,
                threshold=f"< {self.heartbeat_warn_sec:.0f}s（超 "
                          f"{self.heartbeat_crit_sec:.0f}s 视为死亡）",
                detail=f"{sid} 最近一次心跳 {age:.0f}s 前",
                source="OpsMetrics.beat()", component="strategy"))
        return out

    # -- 执行延迟 -------------------------------------------------------- #

    def _execution_metrics(self) -> List[Metric]:
        if self.oms is None:
            return [Metric(
                "exec.latency_p95", "执行延迟 p95", None, "ms",
                Severity.NOT_CONFIGURED, detail="未接入 OMS，无法统计执行延迟",
                source="OpsMetrics.oms", component="execution")]

        samples = self._execution_latencies_ms()
        value = p95(samples)
        sev = Severity.NOT_CONFIGURED
        detail = "还没有完成的订单，无法计算 p95（不是 0ms）"
        if value is not None:
            sev = Severity.OK
            if value >= self.EXEC_CRIT_MS:
                sev = Severity.CRITICAL
            elif value >= self.EXEC_WARN_MS:
                sev = Severity.WARN
            detail = f"最近 {len(samples)} 笔已终结订单，p95 = {value:.0f}ms"
        return [Metric(
            "exec.latency_p95", "执行延迟 p95", None if value is None else round(value, 1),
            "ms", sev,
            threshold=f"< {self.EXEC_WARN_MS:.0f}ms（≥{self.EXEC_CRIT_MS:.0f}ms 告警）",
            detail=detail, source="Order.history（SUBMITTED→终态）",
            component="execution")]

    def _execution_latencies_ms(self) -> List[float]:
        out: List[float] = []
        for order in self.oms.registry.orders():
            if order.state not in TERMINAL_STATES:
                continue
            submitted = next((ts for ts, st, _ in order.history
                              if st is OrderState.SUBMITTED), None)
            if submitted is None:
                continue
            ended = order.history[-1][0] if order.history else None
            if ended is None:
                continue
            delta = (ended - submitted).total_seconds() * 1000.0
            if delta >= 0:
                out.append(delta)
        return out[-200:]

    # -- 订单 ------------------------------------------------------------ #

    def _order_metrics(self) -> List[Metric]:
        if self.oms is None:
            return [Metric(
                "orders.inflight", "在途订单", None, "笔",
                Severity.NOT_CONFIGURED, detail="未接入 OMS",
                source="OpsMetrics.oms", component="orders")]

        orders = self.oms.registry.orders()
        open_orders = [o for o in orders if not o.is_terminal]
        unknown = [o for o in orders if o.state is OrderState.UNKNOWN]
        failed = [o for o in orders if o.state in (OrderState.REJECTED,
                                                   OrderState.EXPIRED)]
        frozen = sorted(self.oms.frozen_symbols())

        return [
            Metric("orders.total", "订单总数", len(orders), "笔", Severity.OK,
                   detail=f"其中未终结 {len(open_orders)} 笔",
                   source="OrderRegistry.orders()", component="orders"),
            Metric("orders.inflight", "在途订单", len(open_orders), "笔",
                   Severity.OK, detail="已下单未终结",
                   source="OrderRegistry.orders()", component="orders"),
            Metric("orders.unknown", "状态未知订单", len(unknown), "笔",
                   _sev(bool(unknown),
                        Severity.CRITICAL if unknown else Severity.OK),
                   threshold="= 0（>0 必须对账，禁止重提）",
                   detail=(", ".join(o.client_order_id for o in unknown)
                           if unknown else "无未知状态订单"),
                   source="Order.state == UNKNOWN", component="orders"),
            Metric("orders.failed", "失败订单", len(failed), "笔",
                   _sev(bool(failed), Severity.WARN), threshold="= 0",
                   detail=(", ".join(f"{o.client_order_id}:{o.state.value}"
                                     for o in failed) if failed else "无被拒/过期订单"),
                   source="Order.state ∈ {REJECTED, EXPIRED}", component="orders"),
            Metric("orders.frozen_symbols", "冻结标的", len(frozen), "个",
                   _sev(len(frozen) > 2, Severity.WARN),
                   detail=(", ".join(frozen) if frozen else "无冻结标的"),
                   source="Oms.frozen_symbols()", component="orders"),
        ]

    # -- 对账 ------------------------------------------------------------ #

    def _reconcile_metrics(self) -> List[Metric]:
        out: List[Metric] = []
        broken: Sequence[str] = ()
        if self.risk_state is not None:
            broken = sorted(getattr(self.risk_state,
                                    "reconciliation_broken_accounts", ()) or ())
            out.append(Metric(
                "reconcile.broken_accounts", "对账断裂账户", len(broken), "个",
                _sev(bool(broken), Severity.CRITICAL), threshold="= 0",
                detail=(", ".join(broken) if broken
                        else "现金/持仓与场所记录一致"),
                source="RiskState.reconciliation_broken_accounts",
                component="reconcile"))
        else:
            out.append(Metric(
                "reconcile.broken_accounts", "对账断裂账户", None, "个",
                Severity.NOT_CONFIGURED, detail="未接入 RiskState",
                source="OpsMetrics.risk_state", component="reconcile"))

        if self.reconciler is None:
            out.append(Metric(
                "reconcile.pending", "待对账订单", None, "笔",
                Severity.NOT_CONFIGURED,
                detail="未接入 UnknownReconciler",
                source="OpsMetrics.reconciler", component="reconcile"))
            return out

        history = getattr(self.reconciler, "history", [])
        # 待对账 = 仍处于 UNKNOWN 的订单。**不能**只看 reconciler 历史：
        # 刚超时还没跑对账时历史是空的，但风险敞口已经不确定了。
        unknown = ([o for o in self.oms.registry.orders()
                    if o.state is OrderState.UNKNOWN]
                   if self.oms is not None else [])
        actions: Dict[str, int] = {}
        for o in history:
            actions[o.action] = actions.get(o.action, 0) + 1
        out.append(Metric(
            "reconcile.pending", "待对账订单", len(unknown), "笔",
            _sev(bool(unknown), Severity.CRITICAL if unknown else Severity.OK),
            threshold="= 0（>0 时必须先查交易所再决定，禁止重提）",
            detail=(f"UNKNOWN：{', '.join(o.client_order_id for o in unknown)}"
                    f"；已处理 {len(history)} 笔"
                    + (f"（{'，'.join(f'{k}={v}' for k, v in sorted(actions.items()))}）"
                       if actions else "")
                    if unknown else
                    (f"无待对账；累计处理 {len(history)} 笔" if history
                     else "无待对账，也还没有对账记录")),
            source="Order.state == UNKNOWN + UnknownReconciler.history",
            component="reconcile"))
        return out

    # -- 风控 ------------------------------------------------------------ #

    def _risk_metrics(self) -> List[Metric]:
        if self.risk_state is None:
            return [Metric(
                "risk.permission", "交易权限", None, "",
                Severity.NOT_CONFIGURED, detail="未接入 RiskState",
                source="OpsMetrics.risk_state", component="risk")]

        level = getattr(self.risk_state, "permission_level",
                        PermissionLevel.NORMAL)
        reasons = list(getattr(self.risk_state, "halted_reasons", ()) or ())
        dd = D(getattr(self.risk_state, "drawdown_pct", Decimal(0)))
        daily = D(getattr(self.risk_state, "daily_pnl_pct", Decimal(0)))
        unknown_syms = sorted(getattr(self.risk_state,
                                      "symbols_with_unknown_orders", ()) or ())

        sev = {PermissionLevel.NORMAL: Severity.OK,
               PermissionLevel.REDUCE_ONLY: Severity.WARN,
               PermissionLevel.HALTED: Severity.CRITICAL}[level]
        return [
            Metric("risk.permission", "交易权限",
                   level.value if hasattr(level, "value") else str(level), "",
                   sev, threshold="NORMAL（REDUCE_ONLY/HALTED 需处置）",
                   detail=(f"权限 {getattr(level, 'value', level)}"
                           + (f"，原因：{' / '.join(reasons)}" if reasons else "")),
                   source="RiskState.permission_level", component="risk"),
            Metric("risk.drawdown", "当前回撤", float(dd), "%",
                   _sev(abs(dd) >= 5, Severity.CRITICAL)
                   if abs(dd) >= 5 else _sev(abs(dd) >= 2, Severity.WARN),
                   threshold="< 5%（可配置）",
                   detail=f"回撤 {dd}%",
                   source="RiskState.drawdown_pct", component="risk"),
            Metric("risk.daily_pnl", "当日盈亏", float(daily), "%",
                   _sev(abs(daily) >= 1, Severity.WARN), threshold="> -1%",
                   detail=f"当日 {daily}%",
                   source="RiskState.daily_pnl_pct", component="risk"),
            Metric("risk.unknown_symbols", "未知订单标的", len(unknown_syms), "个",
                   _sev(bool(unknown_syms),
                        Severity.CRITICAL if unknown_syms else Severity.OK),
                   threshold="= 0",
                   detail=(", ".join(unknown_syms) if unknown_syms
                           else "无标的有未知状态订单"),
                   source="RiskState.symbols_with_unknown_orders",
                   component="risk"),
        ]

    # -- 实盘开关（T-401） ----------------------------------------------- #

    def _live_metrics(self, now: datetime) -> List[Metric]:
        """
        三级开关的状态。这一组的意义是**让"能不能碰真钱"变成可见数字**。

        `live.ready` 不是在问"系统正常吗"，而是在问"此刻允许下实盘单吗"。
        它 false 的时候如果页面一片绿，运维就会以为可以上实盘。
        """
        gate = self.live_gate
        if gate is None:
            return [Metric(
                "live.switch", "实盘开关", None, "", Severity.NOT_CONFIGURED,
                detail="未接入实盘开关（T-401）—— 不等于禁止实盘，是不知道",
                source="OpsMetrics.live_gate", component="risk")]

        status = gate.status(now)
        out: List[Metric] = []

        recon = {r["account_id"]: r for r in status["reconciliation"]}
        missing = status.get("missing") or {}
        accounts = status["accounts"]
        if not accounts:
            out.append(Metric(
                "live.ready", "可实盘账户", 0, "个", Severity.OK,
                threshold="期望 0 或已武装账户",
                detail="没有任何账户被武装，全部禁止实盘",
                source="LiveGate.status()", component="risk"))
        for acc in accounts:
            acct_id = acc["account_id"]
            ready = bool(status["live_ready"].get(acct_id))
            gaps = missing.get(acct_id) or []
            out.append(Metric(
                f"live.ready.{acct_id}", f"{acct_id} 可实盘",
                1 if ready else 0, "", Severity.OK if ready else Severity.WARN,
                threshold="三级授权 + 对账清零",
                detail="三级授权齐全" if ready else "；".join(gaps),
                source="LiveGate.status()", component="risk"))

        for acct_id, r in sorted(recon.items()):
            out.append(Metric(
                f"live.reconcile.{acct_id}", f"{acct_id} 对账资格",
                1 if r["clean"] else 0, "",
                Severity.OK if r["clean"] else Severity.CRITICAL,
                threshold="差异 0",
                detail=r["detail"] or ("已清零" if r["clean"] else "未对账"),
                source="EodReconciler", component="risk"))
        return out

    # -- 组合 ------------------------------------------------------------ #

    def _portfolio_metrics(self, prices: Mapping[Tuple[str, str], Decimal]
                           ) -> List[Metric]:
        if self.ledger is None:
            return [Metric(
                "portfolio.nav", "基金净资产", None, self.base_currency,
                Severity.NOT_CONFIGURED, detail="未接入 Ledger",
                source="OpsMetrics.ledger", component="portfolio")]

        out: List[Metric] = []
        try:
            nav = self.ledger.nav(dict(prices))
            gross = self.ledger.gross_exposure(dict(prices))
            report = self.ledger.performance(dict(prices))
        except Exception as exc:                     # noqa: BLE001
            return [Metric(
                "portfolio.nav", "基金净资产", None, self.base_currency,
                Severity.CRITICAL,
                detail=f"估值失败（多半是缺价）：{exc}",
                source="Ledger.nav()", component="portfolio")]

        out.append(Metric(
            "portfolio.nav", "基金净资产", _num(q(nav, 2)), self.base_currency,
            Severity.OK, detail=f"NAV {q(nav, 2)} {self.base_currency}",
            source="Ledger.nav()", component="portfolio"))
        out.append(Metric(
            "portfolio.gross_exposure", "名义敞口",
            _num(q(gross / nav * 100, 2)) if nav > 0 else None, "%",
            Severity.OK, threshold="< 100%",
            detail=f"名义敞口 {q(gross, 2)}，占 NAV "
                   f"{q(gross / nav * 100, 2) if nav > 0 else 0}%",
            source="Ledger.gross_exposure()", component="portfolio"))
        out.append(Metric(
            "portfolio.attribution", "归因闭合", report.closes, "",
            _sev(not report.closes, Severity.CRITICAL),
            threshold="六项之和 = 净收益",
            detail=(f"trading {report.trading} / slippage {report.slippage} / "
                    f"fee {report.fee} / funding {report.funding} / "
                    f"dividend {getattr(report, 'dividend', 0)} / "
                    f"fx {report.fx} → 净 {report.net_pnl}"),
            source="Ledger.performance()", component="portfolio"))
        return out

    # -- 风险模型（T-701） ----------------------------------------------- #

    def _risk_model_metrics(self, now: datetime) -> List[Metric]:
        """
        风险模型的**来源与新鲜度**，而不是风险本身的数值。

        为什么新鲜度要进报警：σ 只在自己估的那段行情里有效。一个 30 天前估的
        低波动率，会让今天的额度看起来还有很大余量 —— 于是加仓。
        这个错误不会以任何异常的形式出现，只会让风险慢慢超配。
        """
        store = self.risk_model_store
        if store is None:
            return [Metric(
                "risk.model", "风险模型", None, "", Severity.NOT_CONFIGURED,
                detail="未接入协方差估计落盘（T-701）—— σ 和 ρ 从哪来不可知",
                source="OpsMetrics.risk_model_store", component="risk")]

        out: List[Metric] = []
        got = store.load()
        if got is None:
            out.append(Metric(
                "risk.model", "风险模型", 0, "", Severity.WARN,
                threshold="存在且新鲜",
                detail="没有任何落盘的估计结果：风险预算是拍出来的，不是估出来的",
                source="RiskModelStore.load()", component="risk"))
            return out

        est, at = got
        age = (now - at).total_seconds() / 86400.0
        # 超过 7 天 σ 反映的已经是另一段行情了；超过 30 天基本等于没有。
        age_sev = (Severity.CRITICAL if age > 30
                   else Severity.WARN if age > 7 else Severity.OK)
        out.append(Metric(
            "risk.model.age_days", "风险模型年龄", round(age, 2), "天", age_sev,
            threshold="≤ 7 天（> 30 天视为无效）",
            detail=f"估计于 {at.date()}，距今 {age:.1f} 天",
            source="RiskModelStore.age_days()", component="risk"))
        out.append(Metric(
            "risk.model.dof", "估计自由度比", _num(est.dof_ratio), "",
            Severity.OK if est.trustworthy else Severity.WARN,
            threshold="≥ 5（观测数 / 待估参数数）",
            detail=(f"{est.n_obs} 观测 / {est.n_params} 参数（{est.n_assets} 标的）"
                    + ("" if est.trustworthy else "：样本不足，相关系数在噪声范围内")),
            source="CovarianceEstimate.dof_ratio", component="risk"))
        out.append(Metric(
            "risk.model.corr_stderr", "相关系数分辨率", _num(est.corr_stderr()),
            "", Severity.OK, threshold="仅供解读，越大越不可信",
            detail=f"ρ 的标准误上界约 {est.corr_stderr()}：小于这个量级的差异无意义",
            source="CovarianceEstimate.corr_stderr()", component="risk"))
        if est.warnings:
            out.append(Metric(
                "risk.model.warnings", "估计告警", len(est.warnings), "条",
                Severity.WARN, threshold="= 0",
                detail="；".join(est.warnings[:3])
                       + (f"（另有 {len(est.warnings)-3} 条）"
                          if len(est.warnings) > 3 else ""),
                source="CovarianceEstimate.warnings", component="risk"))
        return out

    # -- 灰度（T-602） --------------------------------------------------- #

    def _canary_metrics(self) -> List[Metric]:
        """
        灰度中的策略。这不是报警项，是**知情项**：灰度中意味着有新版在跑，
        而"有新版在跑"这件事在出问题时是最重要的上下文。
        """
        router = self.canary_router
        if router is None:
            return [Metric(
                "canary", "策略灰度", None, "", Severity.NOT_CONFIGURED,
                detail="未接入灰度路由（T-602）—— 无法判断是否有新版在跑",
                source="OpsMetrics.canary_router", component="strategy")]

        cohorts = list(router.cohorts())
        if not cohorts:
            return [Metric(
                "canary", "策略灰度", 0, "个", Severity.OK,
                detail="没有任何策略登记灰度队列",
                source="CanaryRouter.cohorts()", component="strategy")]

        out: List[Metric] = []
        active = [c for c in cohorts if c.state.value == "canary"]
        out.append(Metric(
            "canary.active", "灰度中策略", len(active), "个",
            Severity.OK, threshold="知情项",
            detail=(", ".join(f"{c.strategy_id}@{c.weight}" for c in active)
                    if active else "当前没有灰度中的版本"),
            source="CanaryRouter.cohorts()", component="strategy"))
        for c in cohorts:
            out.append(Metric(
                f"canary.{c.strategy_id}", f"{c.strategy_id} 灰度权重",
                _num(q(c.weight, 4)), "", Severity.OK,
                threshold="升权重需人工",
                detail=(f"稳定版 {c.stable_version}"
                        + (f" / 灰度版 {c.canary_version} 权重 {c.weight}"
                           if c.canary_version else "")
                        + f" / 状态 {c.state.value}"
                        + (f" / 峰值 {c.peak_weight}"
                           if c.peak_weight > c.weight else "")),
                source="Cohort.weight", component="strategy"))
        return out

    # -- AI 研究收件箱（T-603） ------------------------------------------ #

    def _research_metrics(self, now: datetime) -> List[Metric]:
        """
        待复核数。**有积压本身不是错误**，但积压太久意味着有人提交了结论
        却没人看 —— 而那正是"AI 产出被当结论用"最常见的路径。
        """
        inbox = self.research_inbox
        if inbox is None:
            return [Metric(
                "research", "AI 研究通道", None, "", Severity.NOT_CONFIGURED,
                detail="未接入研究收件箱（T-603）",
                source="OpsMetrics.research_inbox", component="research")]

        pending = list(inbox.pending())
        if not pending:
            return [Metric(
                "research.pending", "待复核研究", 0, "条", Severity.OK,
                threshold="知情项",
                detail="没有待复核的提交",
                source="ResearchInbox.pending()", component="research")]

        oldest_days = 0.0
        for sub in pending:
            at = getattr(sub, "created_at", None)
            if at is not None:
                oldest_days = max(oldest_days,
                                  (now - at).total_seconds() / 86400.0)
        # 积压 7 天以上说明没人管它。不设成 critical：它不会自动影响生产，
        # 但它会以"大家默认同意"的方式影响决策，所以值得 warn。
        sev = Severity.WARN if oldest_days > 7 else Severity.OK
        return [Metric(
            "research.pending", "待复核研究", len(pending), "条", sev,
            threshold="知情项（> 7 天未复核为 warn）",
            detail=(f"最久已等待 {oldest_days:.1f} 天；"
                    f"提交清单：{', '.join(getattr(x, 'submission_id', '?') for x in pending[:3])}"
                    + ("…" if len(pending) > 3 else "")),
            source="ResearchInbox.pending()", component="research")]

    # -- 策略实例 -------------------------------------------------------- #

    def _strategy_metrics(self) -> List[Metric]:
        if self.registry is None:
            return []
        insts = list(self.registry)
        if not insts:
            return [Metric(
                "strategy.instances", "策略实例", 0, "个", Severity.WARN,
                threshold="≥ 1", detail="注册表为空：没有任何策略在跑",
                source="StrategyRegistry.__iter__", component="strategy")]

        live = [i for i in insts if i.stage.value == "LIVE"]
        paused = [i for i in insts if i.run_state is StrategyRunState.PAUSED]
        degraded = [i for i in insts if i.run_state is StrategyRunState.REDUCE_ONLY]
        return [
            Metric("strategy.instances", "策略实例", len(insts), "个",
                   Severity.OK,
                   detail=f"LIVE {len(live)} / 全部 {len(insts)}",
                   source="StrategyRegistry.__iter__", component="strategy"),
            Metric("strategy.paused", "暂停策略", len(paused), "个",
                   _sev(bool(paused), Severity.WARN),
                   detail=(", ".join(i.strategy_id for i in paused)
                           if paused else "无暂停策略（存量被冻结，不清仓）"),
                   source="StrategyInstance.run_state", component="strategy"),
            Metric("strategy.reduce_only", "仅减仓策略", len(degraded), "个",
                   _sev(bool(degraded), Severity.WARN),
                   detail=(", ".join(i.strategy_id for i in degraded)
                           if degraded else "无降级策略"),
                   source="StrategyInstance.run_state", component="strategy"),
        ]

    # -- 事实（给页面展示，不进报警） ----------------------------------- #

    def _facts(self, now: datetime,
               prices: Optional[Mapping[Tuple[str, str], Decimal]] = None
               ) -> Dict[str, Any]:
        facts: Dict[str, Any] = {"generated_by": "OpsMetrics.sample()",
                                 "as_of": now.isoformat()}
        if self.oms is not None:
            facts["account_id"] = getattr(self.oms, "account_id", "")
            facts["open_orders"] = [
                {"client_order_id": o.client_order_id, "symbol": o.symbol,
                 "state": o.state.value, "remaining_qty": str(o.remaining_qty),
                 "side": o.side.value, "filled_qty": str(o.filled_qty),
                 "ref_price": str(o.ref_price)}
                for o in self.oms.registry.orders() if not o.is_terminal]
            facts["all_orders"] = [
                {"client_order_id": o.client_order_id, "symbol": o.symbol,
                 "state": o.state.value, "side": o.side.value,
                 "quantity": str(o.quantity), "filled_qty": str(o.filled_qty),
                 "ref_price": str(o.ref_price),
                 "latency_ms": _order_latency_ms(o),
                 "updated_at": o.history[-1][0].isoformat() if o.history else None}
                for o in self.oms.registry.orders()]
        if self.registry is not None:
            facts["strategies"] = [i.to_dict() for i in self.registry]
        if self.risk_state is not None:
            facts["risk_state"] = (self.risk_state.to_dict()
                                  if hasattr(self.risk_state, "to_dict") else {})
        if self.ledger is not None:
            entries = getattr(self.ledger, "entry_count", None)
            facts["ledger_entries"] = entries() if callable(entries) else entries
            deposits = getattr(self.ledger, "net_deposits_base", None)
            if deposits is not None:
                facts["nav_start"] = str(deposits)
            # 策略 → 标的 的名义敞口：页面要显示"每个策略用了多少钱"，
            # 不允许前端自己拿配额瞎猜使用率。
            if prices is not None:
                try:
                    by_strategy = self.ledger.exposure_by_strategy(dict(prices))
                    facts["strategy_exposure"] = {k: str(q(v, 2))
                                                  for k, v in by_strategy.items()}
                except Exception:                    # noqa: BLE001
                    facts["strategy_exposure"] = {}
        facts["stale_limit_sec"] = self._stale_limit()
        facts["portfolio"] = self._portfolio_facts(now)
        return facts

    def _portfolio_facts(self, now: datetime) -> Dict[str, Any]:
        """
        页面要的原始结构（不塞进报警）。缺哪个就 `available: false` +
        原因，绝不补一个看起来合理的默认值。

        这是 B4 定下的规矩：宁可显示"不知道"，也不显示一个假的绿灯。
        """
        out: Dict[str, Any] = {"as_of": now.isoformat()}

        # -- 风险模型 -- #
        store = self.risk_model_store
        if store is None:
            out["risk_model"] = {"available": False, "reason": "未接入协方差估计落盘"}
        else:
            got = store.load()
            if got is None:
                out["risk_model"] = {
                    "available": False,
                    "reason": "没有任何落盘的估计结果（先用 scripts/quantos_risk_check.py 估一次）"}
            else:
                est, at = got
                payload = est.to_dict()
                payload["available"] = True
                payload["estimated_at"] = at.isoformat()
                payload["age_days"] = round((now - at).total_seconds() / 86400.0, 2)
                payload["stale"] = (now - at).total_seconds() / 86400.0 > 7
                out["risk_model"] = payload

        # -- 灰度 -- #
        router = self.canary_router
        if router is None:
            out["canary"] = {"available": False, "reason": "未接入灰度路由"}
        else:
            out["canary"] = {
                "available": True,
                "cohorts": [{
                    "strategy_id": c.strategy_id,
                    "venue": c.venue,
                    "stable_version": c.stable_version,
                    "canary_version": c.canary_version,
                    "weight": str(q(c.weight, 4)),
                    "state": c.state.value,
                    "peak_weight": str(q(c.peak_weight, 4)),
                    "retired": sorted(c.retired),
                    "note": c.note,
                } for c in router.cohorts()],
            }

        # -- 研究收件箱 -- #
        inbox = self.research_inbox
        if inbox is None:
            out["research"] = {"available": False, "reason": "未接入研究收件箱"}
        else:
            pending = list(inbox.pending())
            out["research"] = {
                "available": True,
                "pending": len(pending),
                "accepted": len(list(inbox.accepted())),
                "items": [{
                    "submission_id": s.submission_id,
                    "author": s.author,
                    "kind": s.kind.value if hasattr(s.kind, "value") else str(s.kind),
                    "strategy_id": s.strategy_id,
                    "summary": s.summary[:120],
                    "submitted_at": (s.created_at.isoformat()
                                     if getattr(s, "created_at", None) else None),
                } for s in pending[:10]],
            }
        return out


__all__ = ["Severity", "Metric", "OpsSnapshot", "OpsMetrics", "p95"]
