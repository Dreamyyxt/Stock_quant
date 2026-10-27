#!/usr/bin/env python3
"""
QuantOS 2.0 — 运维中心启动器（T-301 / T-302 / T-303 的运行入口）
=============================================================

把真实对象图（Ledger / MarketDataBook / Oms / RiskEngine / StrategyRegistry）
组装起来，并在 127.0.0.1 上暴露 `/api/ops/*`，同时把控制台页面
（默认 `quantos/index.html`）挂在同一个源上 —— 页面和 API 同源，浏览器
不会把请求当 mixed content 拦掉。

用法：

    # 看一份快照就退出（CI / 抽检用）
    .venv/bin/python scripts/quantos_ops_server.py --once

    # 起服务
    QUANTOS_OPS_TOKEN=dev-token .venv/bin/python scripts/quantos_ops_server.py
    # → http://127.0.0.1:8787/

    # 故意制造异常，验证页面能不能定位到环节
    .venv/bin/python scripts/quantos_ops_server.py --fault stale-market
    .venv/bin/python scripts/quantos_ops_server.py --fault risk-halt
    .venv/bin/python scripts/quantos_ops_server.py --fault unknown-order

注意：这是**本机运维口**，不是后端 API。它不接券商、不下单；下单永远走
OMS。所有状态都在本进程内，进程退出即消失（真实部署时可换成回放事件重建）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quantos_core.capital_allocator import StrategyPolicy          # noqa: E402
from quantos_core.calendar import TradingCalendar                  # noqa: E402
from quantos_core.contracts import (                               # noqa: E402
    D, Fill, OrderType, Side, StrategyRunState, TargetDecision,
    TargetPosition, q, utcnow,
)
from quantos_core.eod import (                                     # noqa: E402
    BrokerStatement, DeclaredAction, EodService, NavSeries, ledger_trade_ids,
)
from quantos_core.live_gate import (                               # noqa: E402
    Confirmation, LiveGate,
)
from quantos_core.idempotency import ClientOrderIdStore            # noqa: E402
from quantos_core.ledger import Ledger                             # noqa: E402
from quantos_core.market import MarketDataBook, MarketSnapshot     # noqa: E402
from quantos_core.metrics import OpsMetrics                        # noqa: E402
from quantos_core.oms import Oms                                   # noqa: E402
from quantos_core.ops_server import OpsHttpServer, OpsService      # noqa: E402
from quantos_core.reconciliation import UnknownReconciler          # noqa: E402
from quantos_core.risk_engine import (                             # noqa: E402
    RiskContext, RiskEngine, RiskPolicy, RiskState,
)
from quantos_core.strategy_registry import (                       # noqa: E402
    AuditLog, StrategyInstance, StrategyRegistry, StrategyStage,
)
from quantos_core.ai_research import ResearchInbox, ResearchKind    # noqa: E402
from quantos_core.canary import CanaryRouter                        # noqa: E402
from quantos_core.covariance import (                               # noqa: E402
    EstimationConfig, PriceSeries, RiskModelStore, estimate,
)

UTC = timezone.utc
ACCOUNT = "TIGER_MAIN"

#: 演示标的与价格。全部为构造值，不代表任何真实行情。
BOOK = {
    "QQQ": D("400.00"),
    "SPY": D("560.00"),
    "TLT": D("92.00"),
}


class InProcessRisk:
    """把 RiskEngine 包成 OMS 期望的 `check(intent, ctx)` 接口。"""

    def __init__(self, engine: RiskEngine, state: RiskState):
        self.engine = engine
        self.state = state
        self.calls = 0

    def check(self, intent, ctx):
        self.calls += 1
        return self.engine.check_order(intent, ctx, self.state)


def _demo_price_series(symbol: str, days: int, sigma_daily: float,
                       seed: int, drift: float = 0.0,
                       common: Optional[list] = None, beta: float = 0.0):
    """
    造一段确定性的价格序列（同一个 seed 永远同一个结果）。

    不用真行情：这是演示环境，用真数据反而会让人以为页面上的 σ 是当前的
    市场估计。用固定 seed 的合成序列，任何人在任何机器上都能复现同一组数字，
    出了问题能对得上。

    `common` + `beta` 是关键：三个独立随机序列算出来的相关系数会接近 0，
    于是页面会显示「QQQ 与 SPY 几乎不相关」—— 而真实世界里它们高度同向。
    一个假的低相关会让分散化收益看起来凭空多出十几个百分点，
    这比数字本身不真实更危险。所以按 beta 注入共同因子。
    """
    import math
    import random
    from datetime import date, timedelta
    rng = random.Random(seed)
    raw = []
    for i in range(days):
        idio = rng.gauss(0.0, 1.0)
        if common is None:
            raw.append(idio)
        else:
            raw.append(beta * common[i]
                       + math.sqrt(max(0.0, 1.0 - beta * beta)) * idio)
    # 标准化到单位样本方差：这样 sigma_daily 就是真实的日波动，不打折。
    mean = sum(raw) / len(raw)
    centered = [x - mean for x in raw]
    sd = math.sqrt(sum(v * v for v in centered) / (len(centered) - 1))
    shocks = [x / sd if sd > 0 else 0.0 for x in centered]
    px = [100.0]
    for z in shocks:
        px.append(max(1.0, px[-1] * (1.0 + drift + sigma_daily * z)))
    start = date(2026, 5, 1)
    return PriceSeries(symbol, tuple(
        (start + timedelta(days=i), D(f"{p:.10f}")) for i, p in enumerate(px)))


def _build_demo_portfolio(fault: str = ""):
    """
    演示用的组合层三件套：一次**真实跑过**的协方差估计、一个灰度队列、
    两条待复核的研究提交。

    为什么真的跑估计而不是写死一份 JSON：页面要展示的是"这套流程的输出"，
    写死的数字看不出来估计器有没有坏。真跑一次只要几十毫秒。
    """
    now = utcnow()
    # -- 协方差估计 -- #
    # 股票端共用一个因子（QQQ/SPY 真实里高度同向），债券端给负 beta。
    # 这样页面上的相关系数才像一份真实的股票+债券组合：
    # 股票间高相关、股债低/负相关 —— 也才有分散化可谈。
    import random
    _rng = random.Random(2026)
    equity_factor = [_rng.gauss(0.0, 1.0) for _ in range(160)]
    series = [
        _demo_price_series("QQQ", 160, 0.012, seed=11, drift=0.0006,
                           common=equity_factor, beta=0.95),
        _demo_price_series("SPY", 160, 0.009, seed=22, drift=0.0004,
                           common=equity_factor, beta=0.90),
        _demo_price_series("TLT", 160, 0.007, seed=33, drift=-0.0002,
                           common=equity_factor, beta=-0.30),
    ]
    est = estimate(
        series,
        EstimationConfig(annualization=252, min_obs=120, shrinkage=D("0.3")),
        label="演示估计（QQQ/SPY/TLT · 合成价格 · 不是真实行情）")
    store = RiskModelStore(os.path.join(tempfile.mkdtemp(prefix="qos-risk-"),
                                        "model.json"))
    # `stale-model` 故障：把估计时间往前推 45 天，验证页面是否警示"模型过期"。
    at = now - timedelta(days=45) if fault == "stale-model" else now
    store.save(est, estimated_at=at)

    # -- 灰度队列：稳定版 + 10% 灰度版 -- #
    router = CanaryRouter(clock=utcnow)
    router.register_stable("etf_momentum", "v1", venue="TIGER",
                           actor="human:owner", reason="已过模拟盘观察")
    router.open_canary("etf_momentum", "v2", weight=D("0.1"),
                       actor="human:owner",
                       reason="新加成交量过滤，先放 10% 观察")
    router.register_stable("risk_parity", "v1", venue="TIGER",
                           actor="human:owner", reason="基线策略")

    # -- AI 研究收件箱：两条待复核 -- #
    inbox = ResearchInbox(Path(tempfile.mkdtemp(prefix="qos-research-"))
                          / "inbox.jsonl")
    inbox.submit(
        author="ai:researcher", kind=ResearchKind.CANDIDATE_PARAMS,
        strategy_id="etf_momentum",
        summary="把 QQQ 的回看窗口从 20 日缩短到 15 日，样本内夏普从 0.8 升到 1.1，"
                "但最大回撤也从 -18% 扩大到 -23%，需要人工判断这个交换是否值得。",
        payload={"params": {"lookback": 15}},
        baseline_params={"lookback": 20},
        evidence=["bt:etf_momentum/2026-10-20/lookback15", "replay:r-0912"])
    inbox.submit(
        author="ai:researcher", kind=ResearchKind.REPORT,
        strategy_id="risk_parity",
        summary="风险平价在 2026 年三季度的实际风险贡献偏离目标 8 个百分点，"
                "主要来自 TLT 与 SPY 的相关性假设失效，建议每季度重估一次协方差。")
    return store, router, inbox


def build_runtime(fault: str = "", seed: int = 0,
                  include_unknown: Optional[bool] = None) -> dict:
    """
    组装一个真实走链路的小型运行时。

    基线故意保持**健康**（最差只到 `warn`：OKX 通道降级 + 一个仅减仓策略），
    这样 `--fault X` 的输出才能看出「这个故障把哪一环推到了 critical」。
    如果基线本身就是 critical（比如长期挂着一笔待对账订单），
    每次注入故障的 headline 都会被那笔订单占住，等于没法做对比。

    「待对账订单」只在 `fault="unknown-order"` 时造出来；需要显式要求时
    用 `include_unknown=True`。
    """
    if include_unknown is None:
        include_unknown = (fault or "").strip().lower() == "unknown-order"
    now = utcnow()
    ledger = Ledger(base_currency="USD")
    ledger.deposit(ACCOUNT, "USD", D("100000"), ts=now)

    market = MarketDataBook()
    for symbol, price in BOOK.items():
        # 用 mid 两侧各留 5bp，避免交叉盘口。
        market.update(MarketSnapshot(
            venue="TIGER", symbol=symbol, as_of=now, received_at=now,
            bid=q(price * D("0.9995"), 4), ask=q(price * D("1.0005"), 4),
            last=price, source="ops-demo", sequence=1,
        ), now=now)

    policy = RiskPolicy(
        max_order_notional_pct=D("20.0"),       # 演示：单笔上限放宽，避免全是拒单
        max_strategy_allocation_pct=D("35.0"),
        daily_loss_pause_pct=D("1.0"),
        drawdown_pause_pct=D("5.0"),
        min_order_notional=D("100"),
        stale_market_data_seconds=120,
    )
    risk_state = RiskState()
    engine = RiskEngine(policy)
    risk = InProcessRisk(engine, risk_state)

    idem = ClientOrderIdStore(os.path.join(tempfile.mkdtemp(prefix="qos-ops-"),
                                           "cid.json"))
    oms = Oms(ACCOUNT, "PF", risk, idem, order_type=OrderType.LIMIT,
              limit_slippage_bps=D("10"))

    audit = AuditLog()
    registry = StrategyRegistry(audit=audit)

    # -- 三个策略实例：一个实盘、一个仅减仓、一个暂停（演示冻结存量） --- #
    registry.register(StrategyInstance(
        "etf_momentum", "v2.1", "TIGER",
        StrategyPolicy("etf_momentum", "TIGER", quota_pct=D("0.30")),
        stage=StrategyStage.LIVE, code_commit="a1b2c3d",
        data_version="2026q4", cost_model_version="2026-10-v1",
        backtest_report_id="bt-2026-10-18", approved_by="human:owner",
        approved_at=now - timedelta(days=20),
        paper_started_at=now - timedelta(days=21),
    ), actor="human:owner")

    registry.register(StrategyInstance(
        "crypto_trend", "v1.4", "OKX",
        StrategyPolicy("crypto_trend", "OKX", quota_pct=D("0.15")),
        stage=StrategyStage.PAPER, code_commit="e4f5a6b",
        data_version="2026q4", cost_model_version="2026-10-v1",
        backtest_report_id="bt-2026-10-20",
        paper_started_at=now - timedelta(days=4),
    ), actor="human:owner")

    registry.register(StrategyInstance(
        "mean_reversion", "v1.3", "TIGER",
        StrategyPolicy("mean_reversion", "TIGER", quota_pct=D("0.12")),
        stage=StrategyStage.LIVE, code_commit="c7d8e9f",
        data_version="2026q4", cost_model_version="2026-10-v1",
        backtest_report_id="bt-2026-10-11", approved_by="human:owner",
        approved_at=now - timedelta(days=30),
        paper_started_at=now - timedelta(days=31),
        run_state=StrategyRunState.REDUCE_ONLY,
    ), actor="human:owner")

    registry.apply_to(risk_state)

    # -- 走两笔真实订单（产生执行延迟样本）；待对账订单按需造 ------------- #
    _trade_once(oms, risk_state, now, symbol="QQQ", notional=D("8000"),
                strategy="etf_momentum", age_sec=0)
    _trade_once(oms, risk_state, now, symbol="SPY", notional=D("6000"),
                strategy="etf_momentum", age_sec=0)
    unknown_cid = ""
    if include_unknown:
        unknown_cid = _timeout_once(oms, risk_state, now, symbol="TLT",
                                   notional=D("5000"), strategy="etf_momentum")

    ledger.apply_fill(
        Fill(fill_id="f-demo-1", order_id="TIG-000001", client_order_id="",
             venue_trade_id="T-9001", quantity=D("19.5"), price=D("400.10"),
             fee=D("1.6"), fee_currency="USD", ts=now),
        venue="TIGER", symbol="QQQ", account_id=ACCOUNT, side=Side.BUY,
        quote_ccy="USD", decision_price=D("400.00"),
        attribution={"etf_momentum": D("19.5")}, ts=now)

    reconciler = UnknownReconciler(oms.registry, _NoGateway(),
                                   max_unknown_sec=600)

    # -- T-401 三级实盘开关 ---------------------------------------------- #
    # 演示环境也走完整的三道确认：AI 不能武装、必须逐字回显、到期失效。
    # 这些约束不能在"演示模式"里被绕过，否则演示就成了唯一没测过的路径。
    gate = LiveGate(clock=utcnow)
    gate.arm_account(ACCOUNT, gross_cap_pct=D("60"), now=now,
                     confirm=Confirmation("human:owner", "演示环境账户级额度",
                                          ACCOUNT))
    gate.arm_strategy(ACCOUNT, "etf_momentum", notional_cap=D("40000"), now=now,
                      confirm=Confirmation("human:owner", "该策略已过模拟盘观察",
                                           "etf_momentum"))
    gate.set_order_cap(ACCOUNT, max_notional=D("9000"), now=now,
                       confirm=Confirmation("human:owner", "单笔风险限额",
                                            str(q(D("9000"), 2))))

    # -- T-403 净值时序 + T-402 日终对账 --------------------------------- #
    series = NavSeries(os.path.join(tempfile.mkdtemp(prefix="qos-nav-"),
                                    "nav_series.json"))
    eod = EodService(ledger, oms.registry, series=series,
                     report_dir=tempfile.mkdtemp(prefix="qos-eod-"),
                     gate=gate, clock=utcnow)
    # 演示券商账单由当前账面状态生成，只有当 fault=eod-mismatch 时故意偏差 50。
    # fault=unbooked-dividend：券商声明了一笔 $120 分红，但账本没入账。
    # 演示的重点不是"分红长什么样"，而是"公司行动差异排在现金差异之前" ——
    # 同一个错误不该看起来像两个无关的错误。
    demo_actions: Tuple[DeclaredAction, ...] = ()
    cash_bias = None
    if fault == "eod-mismatch":
        cash_bias = D("50")
    elif fault == "unbooked-dividend":
        cash_bias = D("120")
        demo_actions = (DeclaredAction("dividend", amount=D("120"),
                                       currency="USD", ref="div-demo-1"),)
    broker = _DemoBroker(ledger, oms.registry, cash_bias=cash_bias,
                         extra_actions=demo_actions)
    prices = {(v, s): D(p) for s, p in BOOK.items() for v in ("TIGER",)}
    eod_run = eod.run(ACCOUNT, now.date(), broker, prices, now=now)

    # -- 心跳：实盘策略正常，模拟盘策略故意漏一拍 ------------------------ #
    rm_store, canary_router, research_inbox = _build_demo_portfolio(fault)
    metrics = OpsMetrics(market=market, oms=oms, risk_state=risk_state,
                         ledger=ledger, registry=registry,
                         reconciler=reconciler, live_gate=gate, clock=utcnow,
                         risk_model_store=rm_store,
                         canary_router=canary_router,
                         research_inbox=research_inbox,
                         stale_sec=120)
    metrics.set_feed_status("TIGER", "connected", "WS 已订阅 3 个标的", at=now)
    metrics.set_feed_status("OKX", "degraded", "重连中（第 2 次）", at=now)
    for inst in registry:
        if inst.run_state is not StrategyRunState.PAUSED:
            metrics.beat(inst.strategy_id, at=now)

    _apply_fault(fault, metrics, registry, risk_state, market, now, seed,
                 gate=gate)

    return {
        "ledger": ledger, "market": market, "oms": oms, "risk_state": risk_state,
        "risk": risk, "registry": registry, "reconciler": reconciler,
        "metrics": metrics, "prices": prices, "unknown_cid": unknown_cid,
        "now": now, "gate": gate, "series": series, "eod": eod,
        "eod_run": eod_run,
        "risk_model_store": rm_store, "canary_router": canary_router,
        "research_inbox": research_inbox,
    }


def _schedule_provider(fault: str):
    """
    策略排期（B9）的**只读**快照来源。

    默认读真实的 `state/quantos/schedule.json` —— 看板显示的就是调度器此刻的真实
    状态，这样"页面上说今天跑过"和"调度器认为今天跑过"是同一份数据。

    `--fault` 会在临时副本上造两种红状态，用来证明这张卡片**真的会红**：
    一张只会显示绿灯的看板等于没有看板（B3 基线一直是红的那个教训）。
    """
    from quantos_core.calendar import load_calendar as _load_cal
    from quantos_core.schedule import (
        RebalanceSpec, ScheduleStore, status_snapshot,
    )

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cal_path = os.path.join(root, "config", "calendar", "ashare.json")
    sched_path = os.path.join(root, "state", "quantos", "schedule.json")

    cal = _load_cal(cal_path)
    if cal is None:
        return lambda: status_snapshot(None, None)

    fault = (fault or "").strip().lower()
    if fault == "sched-stale-calendar":
        # 日历停在 400 天前：数据日落在未校准区间 → 调度器拒跑 → 页面必须红。
        stale = TradingCalendar(
            name=cal.name, holidays=cal.holidays,
            extra_open_days=cal.extra_open_days, first_day=cal.first_day,
            verified_until=utcnow().date() - timedelta(days=400),
            source=cal.source + "（演示：故意截断覆盖区间）")
        return lambda: status_snapshot(stale, ScheduleStore(sched_path))

    if fault == "sched-in-doubt":
        tmp = os.path.join(tempfile.mkdtemp(prefix="qos-sched-"), "schedule.json")
        if os.path.isfile(sched_path):
            shutil.copyfile(sched_path, tmp)
        store = ScheduleStore(tmp)
        entries = store.entries()
        if entries:
            entry = entries[0]
        else:
            entry = store.register(
                RebalanceSpec("ashare_ew20", "v1", cal.verified_until, 20), cal)
        # 造一条"开过工但没落结果"的记录：页面必须显示 🔴 与"可能已下单"。
        store.claim(entry, cal.verified_until, utcnow())
        return lambda: status_snapshot(cal, store)

    return lambda: status_snapshot(cal, ScheduleStore(sched_path))


def _run_demo_tick(ledger_path: str) -> None:
    """在给定路径上跑一拍真实的 A 股调仓，只为把账本造出来（演示用）。"""
    import sys as _sys
    from datetime import date as _date

    here = os.path.dirname(os.path.abspath(__file__))
    if here not in _sys.path:
        _sys.path.insert(0, here)
    import quantos_schedule as _qs
    from quantos_core.schedule import RebalanceSpec as _Spec

    day = _date.fromisoformat(os.environ.get("QOS_DEMO_DAY", "2026-09-30"))
    runner = _qs.AshareRunner(
        pack_path=_qs.PACK_PATH, data_dir=_qs.DATA_DIR, venue=_qs.VENUE,
        params={"top_n": "4"}, symbols=6, bars=80, min_bars=40,
        nav=Decimal("100000"), quota_pct=Decimal("0.9"),
        ledger_path=ledger_path, risk_path=ledger_path + ".risk.json",
        dry_frame_day=day)
    runner(_Spec("ashare_ew20", "v1", _date(2026, 1, 5), 20, "rebalance"), day)


def _ledger_provider(fault: str):
    """
    账本（B10）的**只读**快照来源。

    读的是调度器同一份文件 —— 页面上说"持有 4 只"和下一拍拿去算差额的
    必须是同一份账本，否则看板就是一封二次转述的信。

    `--fault ledger-corrupt` 把临时副本截半，验证这张卡片面对坏档时
    是**红着说"读不出来"**，而不是显示 0 持仓（那会被读成"空仓"）。
    """
    from quantos_core.ledger_store import LedgerStore

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ledger_path = os.path.join(root, "state", "quantos", "ashare_ledger.json")
    risk_path = os.path.join(root, "state", "quantos", "ashare_risk.json")

    fault = (fault or "").strip().lower()
    if fault == "ledger-demo":
        # 用真实行情在**临时目录**里跑一拍，落出真账本给页面看。
        # 为什么不直接读真实账本：真实账本要等第一次 cron tick 才有内容，
        # 而"卡片长什么样"这件事现在就得验证。临时目录保证不碰真状态。
        tmpdir = tempfile.mkdtemp(prefix="qos-ledger-demo-")
        ledger_path = os.path.join(tmpdir, "ashare_ledger.json")
        try:
            _run_demo_tick(ledger_path)
        except Exception as exc:                        # noqa: BLE001
            return lambda: {
                "available": False,
                "reason": f"演示账本没造出来：{type(exc).__name__}: {exc}",
                "status": "corrupt", "positions": [], "position_count": 0,
                "cash": {}, "entry_count": 0, "net_deposits_base": "0",
                "healthy": False,
            }

    if fault == "ledger-corrupt":
        tmp = os.path.join(tempfile.mkdtemp(prefix="qos-ledger-"),
                           "ashare_ledger.json")
        if os.path.isfile(ledger_path):
            with open(ledger_path, "rb") as src:
                blob = src.read()
            with open(tmp, "wb") as dst:
                dst.write(blob[: len(blob) // 2])          # 写一半 = 断电现场
        else:
            with open(tmp, "wb") as dst:
                dst.write(b'{"kind":"quantos.ledger","schema":1,')
        ledger_path = tmp

    def snapshot():
        store = LedgerStore(ledger_path)
        result = store.load()
        ledger = result.ledger
        positions = []
        cash = {}
        net_deposits = "0"
        entry_count = result.entry_count
        if ledger is not None:
            net_deposits = str(ledger.net_deposits_base)
            entry_count = ledger.entry_count
            cash = {f"{acct}|{ccy}": str(amount)
                    for (acct, ccy), amount in ledger.cash_native().items()}
            for (venue, symbol), pos in sorted(ledger.positions().items()):
                positions.append({
                    "venue": venue, "symbol": symbol,
                    "quote_ccy": pos.quote_ccy, "qty": str(pos.qty),
                    "strategy_ids": sorted(pos.attr_qty),
                })
        return {
            "available": True,
            "status": result.status,
            "path": os.path.relpath(ledger_path, root),
            "source": result.source,
            "detail": result.detail,
            "saved_at": result.saved_at,
            "entry_count": entry_count,
            "warnings": list(result.warnings),
            "cash": cash,
            "positions": positions,
            "position_count": len(positions),
            "net_deposits_base": net_deposits,
            "risk_state_path": os.path.relpath(risk_path, root),
            # 只有 ok 才算健康：absent 是"还没有"，corrupt/schema 是"不可信"，
            # 三者在页面上必须长得不一样。
            "healthy": result.status == "ok",
        }

    return snapshot


def _gateway_provider(fault: str):
    """
    真实网关（B11）的**只读**快照来源。

    为什么给一个函数而不是 `AlpacaGateway` 对象：看板能看见"连得上、
    回查端点实测通"，但不能拿到 `place()`。给看板一个能下单的对象，
    等于把下单按钮画进了一个公网可打开页面。

    探针结果缓存 30 秒：它每次都要打两三个真实 HTTP 请求，
    而页面每 15 秒刷新一次 —— 不缓存等于用看板去撞券商限流。
    """
    from quantos_core.alpaca_gateway import (build_gateway_from_env, DATA_BASE)
    from quantos_core.gateway import GatewayError

    fault = (fault or "").strip().lower()

    if fault in ("gateway-down", "gateway-noquery", "gateway-demo"):
        # 故障演示：不碰网络，给出确定的坏样子 / 好样子。
        if fault == "gateway-demo":
            return lambda: {
                "available": True, "mode": "paper", "ok": True, "healthy": True,
                "endpoint": "https://paper-api.alpaca.markets",
                "key": "PK2N***", "detail": "纸面账户可用（USD 100000，ACTIVE）",
                "account": {"account_number": "***ST01", "status": "ACTIVE",
                            "currency": "USD", "cash": "100000", "equity": "100000",
                            "buying_power": "400000", "trading_blocked": False},
                "is_open": True, "next_close": "2026-10-27T16:00:00-04:00",
                "query_by_client_id_ok": True, "query_latency_ms": 743,
                "max_order_notional": "1000",
                "verified_at": "2026-10-27T15:39:10.048043+00:00",
                "declared_from": "alpaca trading api + 纸面实测（B11）",
                "notes": ["市场当前开盘", "能力声明已实测"],
            }
        bad = fault == "gateway-down"
        return lambda: {
            "available": True, "mode": "paper", "ok": False, "healthy": False,
            "endpoint": "https://paper-api.alpaca.markets", "key": "PK2N***",
            "detail": ("TransportError: GET /v2/account 超时（15.0s）："
                       "请求可能已经到达场所 —— 这是「不知道」，不是「失败」"
                       if bad else "无法按客户端单号回查 —— 拒绝承担自动交易"),
            "account": {} if bad else {
                "account_number": "***ST01", "status": "ACTIVE",
                "currency": "USD", "cash": "100000", "equity": "100000"},
            "is_open": None if bad else True,
            "next_close": None if bad else "2026-10-27T16:00:00-04:00",
            # 两种故障的 query 标志都是 False（回查确实不通），区别在账户：
            # down 连账户都读不到，noquery 账户读得到 —— 后者才是最难发现的那种。
            "query_by_client_id_ok": False, "query_latency_ms": 0,
            "max_order_notional": "1000", "verified_at": None,
            "notes": ["回查端点坏了，等于超时之后永远救不回来"] if not bad else [],
        }

    state = {"at": 0.0, "payload": None}

    def snapshot():
        import time as _t
        if state["payload"] is not None and _t.time() - state["at"] < 30:
            return state["payload"]
        try:
            gw = build_gateway_from_env(require_market_open=True)
        except (GatewayError, OSError, ValueError) as exc:
            payload = {
                "available": False, "mode": "absent", "ok": False, "healthy": False,
                "reason": f"网关未接入：{type(exc).__name__}: {exc}",
                "detail": "", "account": {}, "is_open": None,
                "query_by_client_id_ok": False, "query_latency_ms": 0,
                "max_order_notional": "", "notes": [],
            }
            state.update(at=_t.time(), payload=payload)
            return payload

        result = gw.probe("AAPL")
        caps = gw.capabilities
        payload = {
            "available": True,
            "mode": result.mode,
            "ok": result.ok,
            "healthy": bool(result.ok and result.mode == "paper"),
            "endpoint": gw.credentials.base,
            "key": gw.credentials.masked_key,
            "detail": result.detail,
            "account": dict(result.account),
            "is_open": result.clock.get("is_open"),
            "next_close": result.clock.get("next_close"),
            "query_by_client_id_ok": result.query_by_client_id_ok,
            "query_latency_ms": result.query_latency_ms,
            "max_order_notional": str(gw.max_order_notional),
            "verified_at": caps.verified_at.isoformat() if caps.verified_at else None,
            "declared_from": caps.declared_from,
            "data_host": DATA_BASE,
            "notes": list(result.notes),
        }
        state.update(at=_t.time(), payload=payload)
        return payload

    return snapshot


class _DemoBroker:
    """
    演示用券商账单端。

    正常情况下它把**当前账面状态**当账单报回来（等于一个诚实的券商），
    这样日终对账会干净、实盘开关才可能 ready。
    `cash_bias` 用来人为制造差异，验证"对账不干净 → 不许实盘"这条链。
    """

    def __init__(self, ledger: Ledger, registry, *,
                 cash_bias: Optional[Decimal] = None,
                 extra_actions: Tuple[DeclaredAction, ...] = (),
                 hide_actions: bool = False) -> None:
        self.ledger = ledger
        self.registry = registry
        self.cash_bias = cash_bias
        #: 演示"券商声明了但本地没入账"的公司行动（fault=unbooked-dividend）。
        self.extra_actions = extra_actions
        #: 演示"本地入账了但券商没声明"（反向差异，验证不误报）。
        self.hide_actions = hide_actions

    def fetch_statement(self, account_id: str, day):
        cash = {k: D(v) for k, v in self.ledger.cash_native().items()
                if k[0] == account_id}
        if self.cash_bias is not None:
            key = (account_id, "USD")
            cash[key] = cash.get(key, D("0")) + D(self.cash_bias)
        positions = {k: p.qty for k, p in self.ledger.positions().items()
                     if abs(p.qty) > Decimal("0.000001")}
        actions = () if self.hide_actions else self.extra_actions
        return BrokerStatement(
            account_id=account_id, day=day, as_of=utcnow(),
            cash=cash, positions=positions,
            open_client_order_ids=tuple(o.client_order_id
                                        for o in self.registry.active_orders()),
            trade_ids=tuple(sorted(ledger_trade_ids(self.ledger, day))),
            corporate_actions=tuple(actions),
        )


class _NoGateway:
    """演示用的空网关：查不到任何订单（对账会保持 UNKNOWN）。"""

    def query_order(self, client_order_id):
        from quantos_core.reconciliation import VenueQueryError
        raise VenueQueryError("演示环境未接券商查询接口")

    def cancel_order(self, client_order_id):
        return False


def _make_target(symbol: str, notional: Decimal, strategy: str,
                 price: Decimal) -> TargetPosition:
    qty = q(notional / price, 8)
    return TargetPosition(
        symbol=symbol, venue="TIGER", per_strategy={strategy: notional},
        net_target_notional=notional, current_notional=Decimal(0),
        inflight_notional=Decimal(0), delta_notional=notional, price=price,
        net_target_qty=qty, delta_qty=qty, decision=TargetDecision.TRADE,
    )


def _ctx(now: datetime, risk_state: RiskState, age_sec: int = 0) -> RiskContext:
    return RiskContext(nav=D("100000"), now=now,
                       market_data_age_sec={"TIGER": D(age_sec),
                                            "OKX": D(age_sec)},
                       strategy_capacity={"etf_momentum": D("30000"),
                                          "mean_reversion": D("12000")})


def _trade_once(oms: Oms, risk_state: RiskState, now: datetime, *,
                symbol: str, notional: Decimal, strategy: str,
                age_sec: int) -> str:
    """提交 → 接受 → 全部成交。"""
    price = BOOK[symbol]
    target = _make_target(symbol, notional, strategy, price)
    intent, _res = oms.prepare(target, now)
    if intent is None:
        return ""
    result = oms.submit(intent, _ctx(now, risk_state, age_sec))
    if not result.accepted:
        return ""
    cid = result.client_order_id
    oms.on_ack(cid, at=now)
    oms.on_fill(Fill(fill_id=f"f-{cid}", order_id=result.order.order_id,
                     client_order_id=cid, venue_trade_id=f"T-{cid}",
                     quantity=D(intent.quantity), price=price,
                     fee=D("1.2"), fee_currency="USD",
                     ts=now + timedelta(milliseconds=180)), at=now)
    return cid


def _timeout_once(oms: Oms, risk_state: RiskState, now: datetime, *,
                  symbol: str, notional: Decimal, strategy: str) -> str:
    """提交 → 接受 → 交易所没有回报 → 超时转 UNKNOWN（不重提）。"""
    price = BOOK[symbol]
    target = _make_target(symbol, notional, strategy, price)
    intent, _res = oms.prepare(target, now)
    if intent is None:
        return ""
    result = oms.submit(intent, _ctx(now, risk_state))
    if not result.accepted:
        return ""
    cid = result.client_order_id
    oms.on_ack(cid, at=now)
    oms.on_timeout(cid, at=now + timedelta(seconds=3))
    return cid


def _apply_fault(fault: str, metrics: OpsMetrics, registry: StrategyRegistry,
                 risk_state: RiskState, market: MarketDataBook,
                 now: datetime, seed: int, *, gate=None) -> None:
    """人为制造一个异常，用于验证「页面能否定位到环节」。"""
    fault = (fault or "").strip().lower()

    if fault == "stale-market":
        # 直接造一个「10 分钟前的行情簿」：MarketDataBook 会拒收乱序快照，
        # 所以在原簿上倒回时间是不行的（那恰好也是它该有的行为）。
        old = now - timedelta(minutes=10)
        stale_book = MarketDataBook()
        for symbol, price in BOOK.items():
            stale_book.update(MarketSnapshot(
                venue="TIGER", symbol=symbol, as_of=old, received_at=old,
                bid=price, ask=price, last=price, source="ops-demo", sequence=2,
            ), now=old)
        metrics.market = stale_book
        metrics.set_feed_status("TIGER", "degraded", "行情推送停滞 10 分钟",
                                at=old)

    elif fault == "risk-halt":
        risk_state.halt("drawdown_breach")
        risk_state.drawdown_pct = D("6.2")
        metrics.beat("etf_momentum", at=now)

    elif fault == "unknown-order":
        # 订单已在 build_runtime 里造好（include_unknown），这里补标冻结标记，
        # 让「标的被冻结」也出现在风险面。
        risk_state.symbols_with_unknown_orders.add("TLT")

    elif fault == "silent-strategy":
        # 模拟盘策略心跳停 8 分钟：应报 critical，但页面要能指出是哪个策略。
        metrics.beat("crypto_trend", at=now - timedelta(minutes=8))

    elif fault == "eod-mismatch":
        # 账单的偏差由 `_DemoBroker(cash_bias=...)` 注入，日终对账会不干净，
        # gate 的 reconciliation 因此不成立 —— 这里不用再做什么。
        pass

    elif fault == "stale-model":
        # 模型年龄在 _build_demo_portfolio 里注入（estimated_at 往前推 45 天）。
        # 这里不用额外动作：风险模型本身没坏，坏的是它已经过期了。
        pass

    elif fault == "unbooked-dividend":
        # 差异由 _DemoBroker(extra_actions=...) 注入。
        pass

    elif fault == "live-not-armed":
        # 撤掉策略级：账户级与单笔上限都还在，只有一级缺 ——
        # 这正是「少一级确认就无法下单」要演示的那一格。
        if gate is not None:
            gate.revoke(ACCOUNT, "etf_momentum", by="human:owner",
                        why="演示：策略级实盘被撤销")

    elif fault == "sched-in-doubt" or fault == "sched-stale-calendar":
        # 排期的红状态由 `_schedule_provider(fault)` 在临时副本上造，
        # 这里不用额外动作 —— 也不该去动真实状态文件。
        pass

    elif fault in ("ledger-corrupt", "ledger-demo", "gateway-down",
                   "gateway-noquery", "gateway-demo"):
        # 账本坏档 / 演示账本都由 `_ledger_provider(fault)` 在临时副本上造，
        # 同样不动真文件。
        pass

    elif fault:
        raise SystemExit(
            "未知故障类型：" + fault + "（可选：stale-market / risk-halt / "
            "unknown-order / silent-strategy / eod-mismatch / live-not-armed / "
            "stale-model / unbooked-dividend / sched-in-doubt / "
            "sched-stale-calendar / ledger-corrupt / ledger-demo / "
            "gateway-down / gateway-noquery / gateway-demo）")


# --------------------------------------------------------------------------- #
#  命令行
# --------------------------------------------------------------------------- #


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="QuantOS 运维中心")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--html", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "quantos", "index.html"))
    ap.add_argument("--token", default=os.environ.get("QUANTOS_OPS_TOKEN", ""))
    ap.add_argument("--fault", default="")
    ap.add_argument("--once", action="store_true",
                    help="打印快照 JSON 后退出（不启服务）")
    ap.add_argument("--eod", action="store_true",
                    help="与 --once 一起用：打印日终对账 + 归因日报")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    rt = build_runtime(fault=args.fault)
    service = OpsService(metrics=rt["metrics"], registry=rt["registry"],
                         prices_provider=lambda: rt["prices"],
                         token=args.token, series=rt["series"],
                         live_gate=rt["gate"],
                         risk_model_store=rt["risk_model_store"],
                         canary_router=rt["canary_router"],
                         research_inbox=rt["research_inbox"],
                         schedule_provider=_schedule_provider(args.fault),
                         ledger_provider=_ledger_provider(args.fault),
                         gateway_provider=_gateway_provider(args.fault))

    if args.once and args.eod:
        print(json.dumps(rt["eod_run"].to_dict(), ensure_ascii=False, indent=2,
                         default=str))
        return 0

    if args.once:
        print(json.dumps(service.snapshot(), ensure_ascii=False, indent=2,
                         default=str))
        return 0

    html = args.html if os.path.isfile(args.html) else None
    server = OpsHttpServer(service, host=args.host, port=args.port,
                           html_path=html, quiet=args.quiet)
    server.start()
    print(f"QuantOS 运维中心已启动：{server.url}/")
    print(f"  指标快照：{server.url}/api/ops/snapshot")
    print(f"  策略实例：{server.url}/api/ops/strategies")
    print(f"  审计记录：{server.url}/api/ops/audit?limit=20")
    print(f"  净值时序：{server.url}/api/ops/nav-history")
    print(f"  实盘开关：{server.url}/api/ops/live")
    print(f"  组合视图：{server.url}/api/ops/portfolio")
    print(f"  策略排期：{server.url}/api/ops/schedule")
    print(f"  账本状态：{server.url}/api/ops/ledger")
    print(f"  写操作：{'已启用（需 X-Ops-Token）' if args.token else '已禁用（未配置 QUANTOS_OPS_TOKEN）'}")
    if html is None:
        print(f"  [警告] 找不到页面文件 {args.html}，只提供 API")
    print("Ctrl+C 退出")
    try:
        while True:
            import time
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
