#!/usr/bin/env python3
"""
QuantOS 2.0 — B3 自检（T-301 指标 / T-302 真实数据源 / T-303 策略实例）
=====================================================================

每条断言对应一个**会真的害人的失败模式**，不是"函数能跑"。

    .venv/bin/python scripts/quantos_selftest_b3.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from _asserts import (  # noqa: E402
    eq, falsy, guard, near, ne, raises, run, section, truth,
)

from quantos_core.capital_allocator import (  # noqa: E402
    NettingInput, StrategyPolicy, allocate_all, net_targets,
)
from quantos_core.contracts import (  # noqa: E402
    D, Fill, OrderState, PermissionLevel, Side, StrategyIntent, StrategyRunState,
    TargetDecision,
)
from quantos_core.events import MemoryEventStore  # noqa: E402
from quantos_core.ledger import Ledger  # noqa: E402
from quantos_core.market import MarketDataBook, MarketSnapshot  # noqa: E402
from quantos_core.metrics import OpsMetrics, Severity, p95  # noqa: E402
from quantos_core.ops_server import OpsHttpServer, OpsService  # noqa: E402
from quantos_core.risk_engine import RiskContext, RiskEngine, RiskState  # noqa: E402
from quantos_core.strategy_registry import (  # noqa: E402
    AuditLog, AutomationForbidden, IllegalStageTransition, PromotionBlocked,
    StrategyInstance, StrategyRegistry, StrategyStage,
)

UTC = timezone.utc
NOW = datetime(2026, 10, 26, 3, 0, tzinfo=UTC)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t = self.t + timedelta(**kw)


# --------------------------------------------------------------------------- #
#  A. 净额层：暂停策略只冻结自己那份，不再拖垮整只标的
# --------------------------------------------------------------------------- #


def _intent(sid: str, symbol: str, weight: str, ts=None) -> StrategyIntent:
    when = ts or NOW
    return StrategyIntent(
        intent_id=f"{sid}-{symbol}-{weight}-{when.isoformat()}",
        strategy_id=sid, strategy_version="v1", venue="TIGER", symbol=symbol,
        target_weight=D(weight), confidence=D("0.8"), reason="test",
        signal_ids=(f"sig-{sid}",), as_of=when, ts=when,
        expires_at=when + timedelta(minutes=15),
    )


def _netting(intents, states, current_by_strategy, current_total=None):
    prices = {("TIGER", "QQQ"): D("400")}
    policies = {
        "alpha": StrategyPolicy("alpha", "TIGER", quota_pct=D("0.30")),
        "beta": StrategyPolicy("beta", "TIGER", quota_pct=D("0.20")),
    }
    nav = D("100000")
    allocs = allocate_all(list(policies.values()), nav)
    total = current_total
    if total is None:
        total = {k: sum(per.values()) for k, per in _fold(current_by_strategy).items()}
    return net_targets(NettingInput(
        intents=tuple(intents), allocations=allocs, policies=policies,
        prices=prices, current_notional=total, inflight_notional={},
        strategy_states=states, now=NOW, min_notional={"TIGER": D("10")},
        current_by_strategy=current_by_strategy,
    ))


def _fold(by_strategy):
    """{sid: {key: notional}} → {key: {sid: notional}}"""
    out = {}
    for sid, per in by_strategy.items():
        for key, value in per.items():
            out.setdefault(key, {})[sid] = D(value)
    return out


def test_netting() -> None:
    section("A. 净额层：暂停策略的存量只冻结、不误卖")

    # A1 暂停策略持有 +7500，活跃策略目标 -6000 → 只卖 6000
    targets = _netting(
        [_intent("beta", "QQQ", "-0.30")],
        {"alpha": StrategyRunState.PAUSED, "beta": StrategyRunState.ACTIVE},
        {"alpha": {("TIGER", "QQQ"): D("7500")},
         "beta": {("TIGER", "QQQ"): D("0")}},
    )
    t = targets[0]
    eq(t.delta_notional, D("-6000"), "A1 暂停策略存量不被卖出（-6000 而非 -13500）")
    eq(t.frozen_notional, D("7500"), "A1 冻结存量如实报出")
    truth("alpha:frozen" in t.notes, "A1 说明里写明哪个策略被冻结")
    truth(t.decision is TargetDecision.TRADE, "A1 仍允许其他策略交易（不整标的阻断）")

    # A2 策略恢复后：alpha 无意图则不动，delta 仍只来自 beta
    targets2 = _netting(
        [_intent("beta", "QQQ", "-0.30")],
        {"alpha": StrategyRunState.ACTIVE, "beta": StrategyRunState.ACTIVE},
        {"alpha": {("TIGER", "QQQ"): D("7500")},
         "beta": {("TIGER", "QQQ"): D("0")}},
        current_total={("TIGER", "QQQ"): D("7500")},
    )
    eq(targets2[0].delta_notional, D("-6000"), "A2 恢复后按策略各自差额汇总")
    eq(targets2[0].frozen_notional, D("7500"),
       "A2 未发意图的存量仍不动（没信号不等于清仓）")

    # A3 显式发 target_weight=0 才是清仓
    targets3 = _netting(
        [_intent("alpha", "QQQ", "0"), _intent("beta", "QQQ", "-0.30")],
        {"alpha": StrategyRunState.ACTIVE, "beta": StrategyRunState.ACTIVE},
        {"alpha": {("TIGER", "QQQ"): D("7500")},
         "beta": {("TIGER", "QQQ"): D("0")}},
        current_total={("TIGER", "QQQ"): D("7500")},
    )
    eq(targets3[0].delta_notional, D("-13500"),
       "A3 显式目标 0 → 平掉 alpha 的 7500，再叠加 beta 的 -6000")
    eq(targets3[0].frozen_notional, D("0"), "A3 显式清仓时没有冻结")

    # A4 REDUCE_ONLY 想加仓 → 完全不动
    targets4 = _netting(
        [_intent("alpha", "QQQ", "0.30")],
        {"alpha": StrategyRunState.REDUCE_ONLY},
        {"alpha": {("TIGER", "QQQ"): D("7500")}},
        current_total={("TIGER", "QQQ"): D("7500")},
    )
    eq(targets4[0].delta_notional, D("0"), "A4 仅减仓策略不许加仓")
    truth("alpha:reduce_only_no_add" in targets4[0].notes,
          "A4 说明里写清是仅减仓拦的")

    # A5 REDUCE_ONLY 想反手 → 只允许平掉自己那份
    targets5 = _netting(
        [_intent("alpha", "QQQ", "-0.30")],
        {"alpha": StrategyRunState.REDUCE_ONLY},
        {"alpha": {("TIGER", "QQQ"): D("7500")}},
        current_total={("TIGER", "QQQ"): D("7500")},
    )
    eq(targets5[0].delta_notional, D("-7500"),
       "A5 仅减仓策略反手时只能平仓，不许直接建反向仓")
    truth("alpha:reduce_only_flat_only" in targets5[0].notes,
          "A5 说明里写明只平不反")
    truth(targets5[0].decision is TargetDecision.TRADE, "A5 平仓路径必须放行（否则减不了风险）")

    # A6 无主残差（有持仓但无策略归属）→ 冻结，不猜
    targets6 = _netting(
        [_intent("beta", "QQQ", "-0.30")],
        {"beta": StrategyRunState.ACTIVE},
        {"beta": {("TIGER", "QQQ"): D("0")}},
        current_total={("TIGER", "QQQ"): D("3000")},
    )
    eq(targets6[0].frozen_notional, D("3000"), "A6 无归属持仓被冻结")
    truth("unattributed:frozen" in targets6[0].notes, "A6 明确标注无归属")
    eq(targets6[0].delta_notional, D("-6000"), "A6 差额只来自 beta 的目标")

    # A7 老调用方（不传 current_by_strategy）行为不变
    targets7 = _netting(
        [_intent("beta", "QQQ", "-0.30")],
        {"beta": StrategyRunState.ACTIVE},
        {}, current_total={("TIGER", "QQQ"): D("2000")},
    )
    eq(targets7[0].delta_notional, D("-8000"),
       "A7 不传策略分账时退化为整标的算法（-6000-2000）")


# --------------------------------------------------------------------------- #
#  B. 策略注册表与审计
# --------------------------------------------------------------------------- #


def _policy(sid: str, quota="0.20") -> StrategyPolicy:
    return StrategyPolicy(sid, "TIGER", quota_pct=D(quota))


def _instance(sid: str, stage=StrategyStage.RESEARCH, **kw) -> StrategyInstance:
    return StrategyInstance(sid, kw.pop("version", "v1"), "TIGER",
                            _policy(sid, kw.pop("quota", "0.20")),
                            stage=stage, **kw)


def test_registry() -> None:
    section("B. 策略实例：准入闸门 + 审计可回放")

    clock = Clock(NOW)
    registry = StrategyRegistry(AuditLog(MemoryEventStore("audit")),
                                clock=clock, min_paper_days=7)

    registry.register(_instance("alpha"), actor="human:owner")
    registry.register(_instance("beta"), actor="human:owner")
    eq(len(registry), 2, "B1 两个实例注册成功")
    eq(registry.audit.count(), 2, "B1 每次注册都落审计")

    # B2 已存在再注册被拒，且留下 rejected 记录
    raises(Exception, lambda: registry.register(_instance("alpha"),
                                                actor="human:owner"),
           "B2 重复注册被拒")
    recs = registry.audit.records(strategy_id="alpha")
    truth(any(r.result == "rejected" for r in recs), "B2 被拒也写审计")

    # B3 跳级被拒
    raises(IllegalStageTransition,
           lambda: registry.promote("alpha", StrategyStage.LIVE,
                                    actor="human:owner"),
           "B3 RESEARCH 直接跳 LIVE 被拒")

    # B4 缺准入材料不能进 PAPER
    registry.promote("alpha", StrategyStage.BACKTEST, actor="human:owner")
    raises(PromotionBlocked,
           lambda: registry.promote("alpha", StrategyStage.PAPER,
                                    actor="human:owner"),
           "B4 缺回测报告不能进 PAPER")

    # B5 材料齐全可进 PAPER
    registry.promote("alpha", StrategyStage.PAPER, actor="human:owner",
                     backtest_report_id="bt-1", code_commit="sha1",
                     data_version="md-1", cost_model_version="cm-1")
    inst = registry.get("alpha")
    truth(inst.stage is StrategyStage.PAPER, "B5 材料齐全后进入 PAPER")
    eq(inst.paper_started_at, NOW, "B5 记录模拟盘开始时间")

    # B6 自动化通道不能把策略放上实盘
    raises(AutomationForbidden,
           lambda: registry.promote("alpha", StrategyStage.LIVE,
                                    actor="ai:research-agent"),
           "B6 AI/调度器晋级 LIVE 被拒（只能提议）")

    # B7 观察期不足
    clock.advance(days=2)
    exc = raises(PromotionBlocked,
                 lambda: registry.promote("alpha", StrategyStage.LIVE,
                                          actor="human:owner"),
                 "B7 模拟盘观察期不足被拒")
    truth(exc is not None and "观察期" in str(exc), "B7 拒绝原因写明观察期")
    eq(registry.get("alpha").stage, StrategyStage.PAPER, "B7 被拒后阶段不变")

    # B8 观察期满后可晋级，并记录授权人
    clock.advance(days=6)
    registry.promote("alpha", StrategyStage.LIVE, actor="human:owner")
    inst = registry.get("alpha")
    truth(inst.stage is StrategyStage.LIVE, "B8 观察期满后晋级 LIVE")
    eq(inst.approved_by, "human:owner", "B8 记录授权人")

    # B9 已 LIVE 的版本不能被直接替换（热换代码）
    raises(Exception,
           lambda: registry.register(_instance("alpha", version="v2"),
                                     actor="human:owner", replace_existing=True),
           "B9 LIVE 版本的替换被拒（必须先降级）")

    # B10 LIVE 策略恢复必须显式确认
    registry.pause("alpha", "risk", actor="risk:engine")
    eq(registry.get("alpha").run_state, StrategyRunState.PAUSED, "B10 暂停生效")
    raises(Exception,
           lambda: registry.resume("alpha", "手动", actor="human:owner"),
           "B10 LIVE 策略恢复不传 live=true 被拒")
    registry.resume("alpha", "手动", actor="human:owner", live=True)
    eq(registry.get("alpha").run_state, StrategyRunState.ACTIVE, "B10 确认后可恢复")

    # B11 灰度真的缩配额，而不是标签
    registry.set_canary("alpha", D("0.25"), actor="human:owner")
    quotas = {p.strategy_id: D(p.quota_pct) for p in registry.policies()}
    eq(quotas["alpha"], D("0.05"), "B11 灰度 25% × 配额 0.20 = 0.05")

    # B12 PAUSED 策略不在可交易候选里
    registry.pause("beta", "manual", actor="human:owner")
    truth("beta" not in [p.strategy_id for p in registry.policies()],
          "B12 暂停策略不给配额（净额层拿不到它）")
    # B8 起收紧：`policies()` 是**资金分配器的候选池**，
    # RESEARCH / BACKTEST 阶段无论是否暂停都不该在里面 ——
    # `include_paused` 的意思是"连暂停的也列出来"，不是"连没准入的也列出来"。
    # 旧断言（beta 属于 RESEARCH 却期望它出现）把这两件事混成了一件，
    # 那正是"导入一个研究策略就可能真下单"的漏洞所在。
    truth("beta" not in [p.strategy_id for p in registry.policies(include_paused=True)],
          "B12 RESEARCH 阶段 include_paused=True 也不给额度")

    registry.register(_instance("gamma", stage=StrategyStage.PAPER), actor="human:owner")
    registry.pause("gamma", "manual", actor="human:owner")
    truth("gamma" not in [p.strategy_id for p in registry.policies()],
          "B12 PAPER 暂停后不给配额")
    truth("gamma" in [p.strategy_id for p in registry.policies(include_paused=True)],
          "B12 PAPER 阶段 include_paused=True 时仍可见（边界没切过头）")

    # B13 apply_to 让风控知道策略状态
    state = RiskState()
    registry.apply_to(state)
    eq(state.strategy_state("beta"), StrategyRunState.PAUSED,
       "B13 策略状态同步进 RiskState")
    eq(state.permission_level, PermissionLevel.NORMAL,
       "B13 策略暂停不影响基金级权限")

    # B14 审计回放能检出"绕过审计的修改"
    eq(registry.verify_against_audit(), [], "B14 未篡改时审计回放完全一致")
    registry.get("beta").run_state = StrategyRunState.ACTIVE
    problems = registry.verify_against_audit()
    truth(len(problems) == 1 and "beta" in problems[0],
          "B14 直接改内存会被审计回放抓出来")

    # B15 审计 digest 稳定且有内容
    eq(registry.audit.digest(), registry.audit.digest(), "B15 digest 可重复")
    truth(len(registry.audit.digest()) >= 16, "B15 digest 长度足够区分")


# --------------------------------------------------------------------------- #
#  C. 指标采集
# --------------------------------------------------------------------------- #


def test_metrics() -> None:
    section("C. 指标：没接探针必须报 not_configured，不许默认正常")

    empty = OpsMetrics(clock=lambda: NOW).sample().to_dict()
    eq(empty["overall"], "not_configured", "C1 无依赖时整体是 not_configured")
    truth(empty["counts"]["ok"] == 0, "C1 不会因为没数据而报 ok")
    truth("探针未接入" in empty["headline"], "C1 结论里写清是没测而不是正常")

    # C2 行情为空 → 严重（不是"没有延迟"）
    book = MarketDataBook()
    m2 = OpsMetrics(market=book, clock=lambda: NOW).sample().to_dict()
    by_key = {m["key"]: m for m in m2["metrics"]}
    eq(by_key["market.latency"]["severity"], "critical", "C2 空行情簿报 critical")
    truth("从未收到" in by_key["market.latency"]["detail"], "C2 说明原因")

    # C3 陈旧行情
    old = NOW - timedelta(minutes=10)
    book.update(MarketSnapshot(venue="TIGER", symbol="QQQ", as_of=old,
                               received_at=old, bid=D("400"), ask=D("400.1"),
                               last=D("400.05")), now=NOW)
    m3 = OpsMetrics(market=book, clock=lambda: NOW, stale_sec=120).sample().to_dict()
    by_key = {m["key"]: m for m in m3["metrics"]}
    eq(by_key["market.latency.TIGER"]["severity"], "critical",
       "C3 10 分钟未更新 → 延迟 critical")
    eq(by_key["market.stale_symbols"]["value"], 1, "C3 陈旧标的计数正确")

    # C4 p95：空样本必须是 None，不能是 0
    eq(p95([]), None, "C4 空样本 p95 为 None（0 会被误读成很快）")
    eq(p95(list(range(1, 101))), 95.0, "C4 nearest-rank p95 正确")
    eq(p95([7]), 7.0, "C4 单样本 p95 = 该样本")

    # C5 有 UNKNOWN 订单 → 订单与对账都报警
    from quantos_core.idempotency import ClientOrderIdStore
    import tempfile

    class AlwaysAllow:
        def check(self, intent, ctx):
            from quantos_core.risk_engine import RiskDecision
            from quantos_core.contracts import RiskLayer, Verdict
            return RiskDecision("d1", RiskLayer.ORDER, "allow", Verdict.ALLOW,
                                intent.client_order_id, {}, "ok", NOW)

    idem = ClientOrderIdStore(os.path.join(tempfile.mkdtemp(), "cid.json"))
    from quantos_core.oms import Oms
    from quantos_core.contracts import TargetPosition, q
    oms = Oms("ACC", "PF", AlwaysAllow(), idem)
    target = TargetPosition(
        symbol="QQQ", venue="TIGER", per_strategy={"alpha": D("4000")},
        net_target_notional=D("4000"), current_notional=D("0"),
        inflight_notional=D("0"), delta_notional=D("4000"), price=D("400"),
        net_target_qty=D("10"), delta_qty=D("10"),
        decision=TargetDecision.TRADE)
    intent, _res = oms.prepare(target, NOW)
    res = oms.submit(intent, RiskContext(nav=D("100000"), now=NOW))
    truth(res.accepted, "C5 订单提交成功")
    oms.on_ack(res.client_order_id, at=NOW)
    oms.on_timeout(res.client_order_id, at=NOW + timedelta(seconds=3))

    risk_state = RiskState()
    m5 = OpsMetrics(oms=oms, risk_state=risk_state, clock=lambda: NOW
                    ).sample().to_dict()
    by_key = {m["key"]: m for m in m5["metrics"]}
    eq(by_key["orders.unknown"]["value"], 1, "C5 UNKNOWN 订单被计数")
    eq(by_key["orders.unknown"]["severity"], "critical", "C5 UNKNOWN 是 critical")
    eq(by_key["orders.frozen_symbols"]["value"], 1, "C5 冻结标的被计数")
    eq(by_key["risk.unknown_symbols"]["value"], 0,
       "C5 RiskState 未同步时不虚报（指标只报事实）")

    # C6 HALTED 必须出现在 root_causes 里，且排在前面
    risk_state.halt("drawdown_breach")
    risk_state.drawdown_pct = D("6.2")
    m6 = OpsMetrics(oms=oms, risk_state=risk_state, clock=lambda: NOW
                    ).sample().to_dict()
    keys = [c["key"] for c in m6["root_causes"]]
    truth("risk.permission" in keys, "C6 HALTED 出现在 root_causes")
    eq(m6["root_causes"][0]["severity"], "critical", "C6 root_causes 按严重度排序")
    eq(m6["overall"], "critical", "C6 整体为 critical")
    by_key = {m["key"]: m for m in m6["metrics"]}
    truth("drawdown_breach" in by_key["risk.permission"]["detail"],
          "C6 权限项写明熔断原因")

    # C7 心跳超时
    registry = StrategyRegistry(AuditLog(MemoryEventStore("hb")), clock=lambda: NOW)
    registry.register(_instance("alpha", stage=StrategyStage.LIVE),
                      actor="human:owner")
    m7 = OpsMetrics(registry=registry, clock=lambda: NOW,
                    heartbeat_warn_sec=90, heartbeat_crit_sec=300)
    m7.beat("alpha", at=NOW - timedelta(minutes=6))
    d7 = {m["key"]: m for m in m7.sample().to_dict()["metrics"]}
    eq(d7["strategy.heartbeat.alpha"]["severity"], "critical",
       "C7 6 分钟无心跳 → critical")
    truth("alpha" in d7["strategy.heartbeat.alpha"]["label"],
          "C7 能定位到具体策略名，而不是笼统的策略心跳异常")

    # C8 未上报连接状态要报 not_configured，不能默认 connected
    d8 = {m["key"]: m for m in OpsMetrics(market=book, clock=lambda: NOW
                                         ).sample().to_dict()["metrics"]}
    eq(d8["feed.status.TIGER"]["severity"], "not_configured",
       "C8 未上报 WS 状态 → not_configured")

    # C9 归因不闭合要报 critical
    ledger = Ledger()
    ledger.deposit("ACC", "USD", D("100000"), ts=NOW)
    d9 = OpsMetrics(ledger=ledger, clock=lambda: NOW
                    ).sample({("TIGER", "QQQ"): D("400")}).to_dict()
    by_key = {m["key"]: m for m in d9["metrics"]}
    eq(by_key["portfolio.attribution"]["severity"], "ok", "C9 空账本归因闭合")
    eq(by_key["portfolio.nav"]["value"], 100000.0, "C9 NAV 正确")
    eq(d9["facts"]["nav_start"], "100000.0000", "C9 facts 提供净入金做分母")


# --------------------------------------------------------------------------- #
#  D. HTTP 服务
# --------------------------------------------------------------------------- #


def test_http() -> None:
    section("D. 运维服务：只回环、写操作必须带 token")

    from quantos_ops_server import build_runtime

    raises(ValueError, lambda: OpsHttpServer(
        OpsService(metrics=OpsMetrics(), registry=StrategyRegistry()),
        host="0.0.0.0"),
        "D1 拒绝绑定 0.0.0.0（持仓不能给整个局域网看）")

    rt = build_runtime()
    service = OpsService(metrics=rt["metrics"], registry=rt["registry"],
                         prices_provider=lambda: rt["prices"], token="t0ken")
    html_path = os.path.join(ROOT, "quantos", "index.html")

    with OpsHttpServer(service, port=0, html_path=html_path) as srv:
        base = srv.url

        def get(path):
            with urllib.request.urlopen(base + path, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))

        def post(path, payload, token=None):
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(base + path, data=data, method="POST")
            req.add_header("Content-Type", "application/json")
            if token:
                req.add_header("X-Ops-Token", token)
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return resp.status, json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))

        status, health = get("/api/ops/health")
        eq(status, 200, "D2 health 200")
        truth(health["write_enabled"], "D2 health 报告写操作已启用")

        status, snap = get("/api/ops/snapshot")
        eq(status, 200, "D3 snapshot 200")
        truth(len(snap["metrics"]) > 20, "D3 指标数量合理")
        truth("root_causes" in snap and "components" in snap,
              "D3 快照含可定位字段")

        # D4 服务端口径与进程内对象一致（T-302 的服务端一半）
        nav_metric = next(m for m in snap["metrics"] if m["key"] == "portfolio.nav")
        near(nav_metric["value"], float(rt["ledger"].nav(rt["prices"])), 0.01,
             "D4 API 的 NAV = 进程内账本 NAV")
        eq(len(snap["facts"]["strategies"]), len(rt["registry"]),
           "D4 facts 策略数 = 注册表实例数")
        eq(len(snap["facts"]["all_orders"]),
           len(rt["oms"].registry.orders()), "D4 facts 订单数 = OMS 订单数")

        status, strategies = get("/api/ops/strategies")
        eq(status, 200, "D5 strategies 200")
        eq(strategies["count"], 3, "D5 三个策略实例")
        eq(strategies["inconsistencies"], [], "D5 实例状态与审计一致")

        status, audit = get("/api/ops/audit?limit=5")
        eq(status, 200, "D6 audit 200")
        truth(len(audit["digest"]) >= 8, "D6 审计返回 digest")

        # D7 无 token 写操作被拒
        status, body = post("/api/ops/actions",
                            {"action": "pause", "strategy_id": "etf_momentum"})
        eq(status, 403, "D7 无 token 写操作返回 403")
        falsy(body.get("ok"), "D7 返回 ok=false")

        # D8 有 token 写操作生效并落审计
        before = service.registry.audit.count()
        status, body = post("/api/ops/actions",
                            {"action": "pause", "strategy_id": "etf_momentum",
                             "reason": "自检暂停"},
                            token="t0ken")
        eq(status, 200, "D8 带 token 写操作 200")
        truth(body.get("ok"), "D8 操作成功")
        eq(service.registry.audit.count(), before + 1, "D8 审计 +1")
        eq(service.registry.get("etf_momentum").run_state,
           StrategyRunState.PAUSED, "D8 策略真的变成 PAUSED")

        # D9 非法动作被当作业务拒绝（409），不是 500
        status, body = post("/api/ops/actions",
                            {"action": "resume", "strategy_id": "etf_momentum"},
                            token="t0ken")
        eq(status, 409, "D9 LIVE 恢复不传 live 被拒 → 409")
        falsy(body.get("ok"), "D9 返回 ok=false")
        truth("live" in str(body.get("error", "")), "D9 错误里说明缺 live 确认")
        eq(service.registry.audit.count(), before + 2, "D9 被拒也落审计")

        # D10 未知路径 404
        req = urllib.request.Request(base + "/api/ops/nope")
        try:
            urllib.request.urlopen(req, timeout=5)
            truth(False, "D10 未知路径应 404")
        except urllib.error.HTTPError as exc:
            eq(exc.code, 404, "D10 未知路径 404")

        # D11 页面同源可取
        with urllib.request.urlopen(base + "/", timeout=5) as resp:
            html = resp.read().decode("utf-8")
        eq(resp.status, 200, "D11 页面 200")
        truth("QuantOS" in html, "D11 返回的是控制台页面")
        truth("/api/ops/snapshot" in html, "D11 页面确实会请求运维接口")


# --------------------------------------------------------------------------- #
#  E. 页面渲染探针（前端一半）
# --------------------------------------------------------------------------- #


def test_page() -> None:
    section("E. 页面：离线标注演示、在线显示真实数字")

    node = subprocess.run(["node", "--version"], capture_output=True, text=True)
    if node.returncode != 0:
        truth(True, "E0 本机没有 node，跳过前端探针（不影响后端结论）")
        return

    html_path = os.path.join(ROOT, "quantos", "index.html")
    probe = os.path.join(ROOT, "scripts", "page_probe.js")

    offline = subprocess.run(["node", probe, html_path, "-"],
                             capture_output=True, text=True, timeout=120)
    eq(offline.returncode, 0, "E1 离线模式渲染无失败")
    # 离线断言条数随批次增长（B7 加三块能力 → 9 条，B9 加策略排期 → 10 条）。
    # 这里卡下限而不是等值：条数只增不减，但**「0 失败」必须是精确的** ——
    # 一旦某条断言静默消失，这里就会因为下限不够而报出来。
    truth("0 失败" in offline.stdout, "E1 离线断言全通过")
    _m = re.search(r"(\d+) 通过 / 0 失败", offline.stdout)
    truth(_m is not None and int(_m.group(1)) >= 10,
          "E1 离线断言条数不少于 10 条（防止断言被静默删掉）")

    # 生成 fixture：与 API 返回完全同构的三份 JSON
    from quantos_ops_server import build_runtime
    rt = build_runtime()
    service = OpsService(metrics=rt["metrics"], registry=rt["registry"],
                         prices_provider=lambda: rt["prices"], token="t0ken")
    fixture = {
        "snapshot": service.snapshot(),
        "strategies": service.strategies(),
        "audit": service.audit(30),
    }
    import tempfile
    fx_path = os.path.join(tempfile.mkdtemp(), "fixture.json")
    with open(fx_path, "w", encoding="utf-8") as fh:
        json.dump(fixture, fh, ensure_ascii=False, default=str)

    online = subprocess.run(["node", probe, html_path, fx_path],
                            capture_output=True, text=True, timeout=120)
    eq(online.returncode, 0, "E2 在线模式渲染无失败")
    truth("0 失败" in online.stdout, "E2 在线断言全通过")
    print("    " + online.stdout.strip().splitlines()[-1])

    # E3 页面里的 NAV 文本必须能由 fixture 精确推出（防止"差不多对"）
    snap = fixture["snapshot"]
    nav = next(m["value"] for m in snap["metrics"] if m["key"] == "portfolio.nav")
    truth(isinstance(nav, (int, float)), "E3 NAV 是数值而不是字符串")
    truth(nav != 105200, "E3 在线 NAV 不等于演示常量（确实换了数据源）")


def main() -> None:
    print("QuantOS 2.0 · B3 自检（指标 / 真实数据源 / 策略实例）")
    print("=" * 66)
    test_netting()
    test_registry()
    test_metrics()
    test_http()
    test_page()


if __name__ == "__main__":
    raise SystemExit(run("B3 运维中心", main))
