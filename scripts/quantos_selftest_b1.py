#!/usr/bin/env python
"""
QuantOS 2.0 — B1 自检（T-101 ~ T-105）
=====================================

覆盖四件事：
  1. 事件存储：单调 seq、幂等追加、尾部残缺自愈、坏行拒绝、JSONL/SQLite 一致
  2. 行情契约：缺价显式 None、0 价拒绝、陈旧判定、乱序拒收
  3. 成本模型：费用/滑点分离、冲击随仓位增长、预估偏差可测
  4. 事件回放：两次回放 digest 一致、反向意图净额成一笔、在途扣除、账本闭合

运行：`.venv/bin/python scripts/quantos_selftest_b1.py`
退出码 0 表示全过。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "scripts"))

from _asserts import eq, falsy, near, raises, run, section, truth  # noqa: E402

from quantos_core.capital_allocator import StrategyPolicy                # noqa: E402
from quantos_core.contracts import D, Side, StrategyRunState             # noqa: E402
from quantos_core.cost import CostModel, load_cost_model, VenueCostSchedule  # noqa: E402
from quantos_core.events import (                                        # noqa: E402
    EventEnvelope, EventStoreCorrupt, JsonlEventStore, MemoryEventStore,
    SqliteEventStore, canonical_json, from_jsonable, to_jsonable,
)
from quantos_core.market import (                                        # noqa: E402
    MarketDataBook, MarketSnapshot, PriceUnavailable,
)
from quantos_core.replay import Replayer, ReplayConfig                    # noqa: E402
from quantos_core.risk_engine import RiskPolicy                           # noqa: E402
from quantos_core.strategy_adapter import (                              # noqa: E402
    AdapterConfig, adapt_signals, intent_to_payload,
)

UTC = timezone.utc
T0 = datetime(2026, 10, 26, 13, 30, tzinfo=UTC)
TMP = tempfile.mkdtemp(prefix="qos-b1-")

PRICE = D("400.0")
BID = D("399.9")
ASK = D("400.1")


# --------------------------------------------------------------------------- #
#  1. 事件存储（T-101）
# --------------------------------------------------------------------------- #

def test_event_store() -> None:
    section("T-101 事件存储")

    path = os.path.join(TMP, "events.jsonl")
    store = JsonlEventStore(path, stream="b1")
    env1, c1 = store.append("deposit", {"amount": D("100000"), "currency": "USD"}, ts=T0)
    env2, c2 = store.append("cycle", {"now": T0}, ts=T0)
    eq(store.last_seq, 2, "seq 单调递增到 2")
    truth(c1 and c2, "两次追加都标记为新建")
    eq(env1.seq, 1, "首个事件 seq = 1")

    # 幂等追加：同一个 fill 回报重复到达，不得产生第二条事件
    same, created = store.append("fill", {"qty": D("1")}, event_id="fill:TRADE-1", ts=T0)
    dup, created2 = store.append("fill", {"qty": D("1")}, event_id="fill:TRADE-1", ts=T0)
    falsy(created2, "相同 event_id 再次追加 → created=False")
    eq(dup.seq, same.seq, "重复追加返回原事件（不新建）")
    eq(store.count, 3, "事件总数仍为 3")

    # Decimal 精确往返（不允许 0.1+0.2 变成 0.30000000000000004）
    store.append("probe", {"x": D("0.1") + D("0.2")}, ts=T0)
    probe = list(store.read(event_types=["probe"]))[0]
    eq(probe.payload["x"], D("0.3"), "Decimal 精确往返")
    truth(isinstance(probe.payload["x"], Decimal), "反序列化后仍是 Decimal 而非 float")

    # 读取过滤
    eq(len(list(store.read(from_seq=2))), 3, "read(from_seq=2) 返回后 3 条")
    eq(len(list(store.read(event_types=["deposit"]))), 1, "按类型过滤")

    digest_before = store.digest()
    store.close()

    # 重新打开：digest 必须一致（否则"同一段历史"无法复现）
    reopened = JsonlEventStore(path, stream="b1")
    eq(reopened.digest(), digest_before, "重开后 digest 不变")
    eq(reopened.last_seq, 4, "重开后 seq 从 4 继续")

    # 尾部残缺（断电/磁盘满写了一半）：只准截掉残行，不准静默跳过
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"seq":5,"event_id":"torn","event_ty')
    repaired = JsonlEventStore(path, stream="b1")
    truth(repaired.repaired_bytes > 0, "检测并截断尾部残缺行")
    eq(repaired.count, 4, "残缺行不计入事件数")
    env5, _ = repaired.append("after_repair", {"ok": True}, ts=T0)
    eq(env5.seq, 5, "修复后 seq 继续为 5（不回退、不跳号）")

    # 中间损坏：必须抛异常，不许静默跳过
    bad_path = os.path.join(TMP, "bad.jsonl")
    good_rec = json.loads(canonical_json(env1.to_record()))
    with open(bad_path, "w", encoding="utf-8") as fh:
        fh.write(canonical_json(good_rec) + "\n")
        fh.write("{ this is not json }\n")
        fh.write(canonical_json(good_rec) + "\n")
    raises(EventStoreCorrupt, lambda: JsonlEventStore(bad_path),
           "中间行损坏 → EventStoreCorrupt（拒绝静默跳过）")

    # SQLite 与 JSONL 内容一致
    db_path = os.path.join(TMP, "events.db")
    sq = SqliteEventStore(db_path, stream="b1")
    for env in reopened.read():
        _, created = sq.append(env.event_type, env.payload, as_of=env.as_of,
                               ts=env.ts, event_id=env.event_id)
    eq(sq.count, repaired.count, "SQLite 与 JSONL 事件数一致")
    eq(sq.digest(), repaired.digest(), "SQLite 与 JSONL digest 一致")
    sq.close()
    repaired.close()


# --------------------------------------------------------------------------- #
#  2. 行情快照（T-102）
# --------------------------------------------------------------------------- #

def test_market() -> None:
    section("T-102 行情快照")

    raises(ValueError, lambda: MarketSnapshot(
        venue="TIGER", symbol="QQQ", as_of=T0, received_at=T0, bid=D("0")),
        "价格为 0 → 直接拒绝（缺价必须传 None）")

    missing_ask = MarketSnapshot(venue="TIGER", symbol="QQQ", as_of=T0,
                                 received_at=T0, bid=BID, last=PRICE)
    raises(PriceUnavailable, lambda: missing_ask.executable_price(Side.BUY),
           "缺卖一 → 买入定价抛出 PriceUnavailable")
    eq(missing_ask.executable_price(Side.SELL), BID, "卖出仍可用买一价")
    eq(missing_ask.reference_price(), PRICE, "估值回退到 last，而不是 0")
    eq(missing_ask.defects(), ["missing_ask"], "缺陷清单指出 missing_ask")

    full = MarketSnapshot(venue="TIGER", symbol="QQQ", as_of=T0, received_at=T0,
                          bid=BID, ask=ASK, last=PRICE, sequence=1)
    eq(full.mid, D("400"), "mid = (bid+ask)/2")
    eq(full.spread_bps, D("5"), "价差 0.2/400 = 5bps")
    truth(full.is_usable, "完整快照可用于交易")

    old = MarketSnapshot(venue="TIGER", symbol="QQQ",
                         as_of=T0 - timedelta(seconds=200), received_at=T0,
                         bid=BID, ask=ASK, last=PRICE)
    truth(old.is_stale(T0, 120), "200s 前的行情在 120s 上限下判为陈旧")
    eq(old.age_ms(T0), 200000, "age_ms 精确到毫秒")

    crossed = MarketSnapshot(venue="TIGER", symbol="QQQ", as_of=T0, received_at=T0,
                             bid=D("401"), ask=D("400"), last=PRICE)
    falsy(crossed.is_usable, "买一 > 卖一（交叉盘）判为不可用")

    book = MarketDataBook()
    truth(book.update(full, now=T0), "接受首条快照")
    older = MarketSnapshot(venue="TIGER", symbol="QQQ",
                           as_of=T0 - timedelta(seconds=30), received_at=T0,
                           bid=D("390"), ask=D("391"), last=D("390.5"))
    falsy(book.update(older, now=T0), "乱序（更旧）快照被拒收")
    eq(book.get("TIGER", "QQQ").mid, D("400"), "拒收后当前价未被覆盖")

    future = MarketSnapshot(venue="TIGER", symbol="QQQ",
                            as_of=T0 + timedelta(minutes=5), received_at=T0,
                            bid=BID, ask=ASK, last=PRICE)
    falsy(book.update(future, now=T0), "未来时间戳被拒收（时钟异常）")

    eq(book.missing("TIGER", ["QQQ", "SPY"]), ["SPY"], "缺失标的可枚举")
    eq(book.age_sec_by_venue(T0)["TIGER"], D("0"), "同场所年龄取最新一条")
    book.update(MarketSnapshot(venue="TIGER", symbol="SPY",
                               as_of=T0 - timedelta(seconds=300), received_at=T0,
                               bid=D("500"), ask=D("500.2"), last=D("500.1")), now=T0)
    eq(book.age_sec_by_venue(T0)["TIGER"], D("300"), "场所年龄取最旧的一条（不自我安慰）")


# --------------------------------------------------------------------------- #
#  3. 成本模型（T-105）
# --------------------------------------------------------------------------- #

def test_cost() -> None:
    section("T-105 成本模型")

    model = load_cost_model(os.path.join(_ROOT, "config", "cost_model.json"))
    truth(model.source_path is not None, "参数来自文件（不是代码里的魔法数字）")

    est = model.estimate("OKX", Side.BUY, D("1"), D("10000"), nav=D("1000000"))
    eq(est.fee_bps, D("8"), "OKX taker 费率 8bps")
    eq(est.fee, D("8"), "费用 = 10000 × 8bps")
    eq(est.slippage_bps, D("3.6"), "滑点 = 3bps + 0.6bps×1%(占NAV)")
    truth(est.expected_price > D("10000"), "买入预期成交价高于参考价")

    big = model.estimate("OKX", Side.BUY, D("5"), D("10000"), nav=D("100000"))
    truth(big.slippage_bps > est.slippage_bps, "订单占 NAV 越大，滑点越高（冲击成本）")

    sell = model.estimate("OKX", Side.SELL, D("1"), D("10000"), nav=D("1000000"))
    truth(sell.expected_price < D("10000"), "卖出预期成交价低于参考价")

    tiny = model.estimate("IBKR", Side.BUY, D("0.001"), D("100"), nav=D("100000"))
    eq(tiny.fee, D("1"), "低于最低费用时按 min_fee 收取")

    maker = model.estimate("OKX", Side.BUY, D("1"), D("10000"),
                           nav=D("1000000"), liquidity="MAKER")
    truth(maker.fee < est.fee, "maker 费率低于 taker")

    # 偏差检测：预估滑点 3.6，实际吃到 10 → 偏差 178%，必须报"需要复核参数"
    drift = model.measure_drift(est, actual_fee=D("8"), actual_price=D("10010"),
                                quantity=D("1"), decision_price=D("10000"))
    truth(drift.is_material, "实际滑点显著高于预估 → 标记为需复核")
    eq(drift.slippage_drift, D("6.4"), "滑点偏差 = 实际 10 - 预估 3.6")
    falsy(model.measure_drift(est, actual_fee=D("8"), actual_price=D("10003.6"),
                              quantity=D("1"), decision_price=D("10000")).is_material,
          "实际与预估一致时不告警")

    # 缺参数文件时回退内置模型，但 source_path 为空 —— 生产启动应据此告警
    fallback = load_cost_model(os.path.join(TMP, "nope.json"))
    truth(fallback.source_path is None, "缺文件时回退内置模型且标记来源为空")
    eq(fallback.version, "builtin-v1", "内置模型版本可识别")


# --------------------------------------------------------------------------- #
#  4. 策略适配器（T-104）
# --------------------------------------------------------------------------- #

class _LegacySignal:
    """模拟 strategies/base_strategy.py 的 Signal（duck typing）。"""

    def __init__(self, symbol, weight, score=0.5, direction=1, reason="momentum"):
        self.symbol = symbol
        self.direction = direction
        self.suggested_weight = D(weight) if not isinstance(weight, Decimal) else weight
        self.score = score
        self.reason = reason


def test_adapter() -> None:
    section("T-104 策略适配器")

    cfg = AdapterConfig(strategy_id="etf_mom", strategy_version="v2.1", venue="TIGER")
    batch = adapt_signals([_LegacySignal("QQQ", "0.25")], cfg, as_of=T0)
    eq(len(batch.intents), 1, "一条信号 → 一个意图")
    eq(batch.intents[0].target_weight, D("0.25"), "权重原样搬运（不擅自缩放）")
    eq(batch.intents[0].strategy_id, "etf_mom", "策略 id 正确")
    truth(bool(batch.intents[0].signal_ids), "意图可追溯到信号")

    # 同一标的多个信号求和并夹到 [-1,1]
    summed = adapt_signals([_LegacySignal("QQQ", "0.8"), _LegacySignal("QQQ", "0.5")],
                           cfg, as_of=T0)
    eq(summed.intents[0].target_weight, D("1"), "同标的多信号求和后夹到 +1")

    # 做空默认关闭
    short = adapt_signals([_LegacySignal("QQQ", "-0.3", direction=-1)], cfg, as_of=T0)
    eq(len(short.intents), 0, "做空关闭时负权重被丢弃")
    truth(any("short_disabled" in d for d in short.dropped), "丢弃原因留痕")

    # confidence 夹到 0..1，且只是展示字段
    high = adapt_signals([_LegacySignal("QQQ", "0.1", score=3.7)], cfg, as_of=T0)
    eq(high.intents[0].confidence, D("1"), "score 超界时 confidence 夹到 1")

    # 只有方向没有权重的信号：不猜金额
    class _DirOnly(_LegacySignal):
        def __init__(self):
            super().__init__("SPY", "0", score=0.5)

    only_dir = adapt_signals([_DirOnly()], cfg, as_of=T0)
    eq(len(only_dir.intents), 0, "只有方向没有权重 → 丢弃而不是猜")
    truth(any("direction_without_weight" in d for d in only_dir.dropped), "留痕")

    # 编解码往返（策略进程写、回放进程读必须一致）
    payload = intent_to_payload(batch.intents[0])
    restored = from_jsonable(json.loads(canonical_json(payload)))
    eq(restored["target_weight"], D("0.25"), "意图权重往返精确")
    eq(restored["as_of"], T0, "意图时间往返保持时区与精度")


# --------------------------------------------------------------------------- #
#  5. 事件回放（T-103）
# --------------------------------------------------------------------------- #

def _policies() -> list:
    return [
        StrategyPolicy("etf_mom", "TIGER", quota_pct=D("0.50")),
        StrategyPolicy("mean_rev", "TIGER", quota_pct=D("0.50")),
    ]


def _permissive_policy() -> RiskPolicy:
    """放开订单级上限，专门测组合层净额；风控上限另有单独用例。"""
    return RiskPolicy(
        max_order_notional_pct=D("50"), max_strategy_allocation_pct=D("90"),
        min_order_notional=D("50"), max_gross_exposure_pct=D("200"),
        drawdown_pause_pct=D("50"), daily_loss_pause_pct=D("50"),
    )


def _config(policy=None) -> ReplayConfig:
    return ReplayConfig(
        account_id="TIGER_MAIN",
        strategy_policies=_policies(),
        risk_policy=policy or _permissive_policy(),
        cost_model=load_cost_model(os.path.join(_ROOT, "config", "cost_model.json")),
        min_notional={"TIGER": D("50")},
    )


def _add_market(store, symbol="QQQ", ts=T0, price=None, seq=1, venue="TIGER"):
    price = D(price or PRICE)
    store.append("market_snapshot", {
        "venue": venue, "symbol": symbol, "as_of": ts,
        "received_at": ts, "bid": price - D("0.1"), "ask": price + D("0.1"),
        "last": price, "sequence": seq,
    }, ts=ts)


def _add_intent(store, strategy_id, weight, symbol="QQQ", ts=T0, venue="TIGER",
                allow_short=False):
    batch = adapt_signals(
        [_LegacySignal(symbol, weight)],
        AdapterConfig(strategy_id=strategy_id, strategy_version="v1", venue=venue,
                      allow_short=allow_short),
        as_of=ts)
    for intent in batch.intents:
        store.append("strategy_intent", intent_to_payload(intent), ts=ts)
    return batch


def test_replay() -> None:
    section("T-103 事件回放")

    store = MemoryEventStore("b1-replay")
    store.append("deposit", {"amount": D("100000"), "currency": "USD"}, ts=T0)
    _add_market(store)

    # 两个策略反向：+20% 与 -12% → 净 8%（占各自额度 50k → 10000 与 -6000）
    _add_intent(store, "etf_mom", "0.20")
    _add_intent(store, "mean_rev", "-0.12", allow_short=True)
    store.append("cycle", {"now": T0 + timedelta(seconds=5)}, ts=T0)

    cfg = _config()
    result = Replayer(cfg).run(store)

    eq(len(result.targets), 1, "两个策略的反向意图汇聚成 1 个标的")
    target = result.targets[0]
    eq(target.per_strategy.get("etf_mom"), D("10000"), "多头策略目标名义 10000")
    eq(target.per_strategy.get("mean_rev"), D("-6000"), "空头策略目标名义 -6000")
    eq(target.net_target_notional, D("4000"), "净敞口 = 10000 - 6000 = 4000")

    eq(len(result.orders), 1, "只产生 1 笔订单（不是 2 笔）")
    order = list(result.orders.values())[0]
    eq(D(order["remaining_qty"]) * D("400"), D("4000"), "订单数量 = 净额 4000")
    truth(order["client_order_id"].startswith("TIGERMAIN-QOS-QQQ"),
          "client_order_id 结构可读且可复算")

    # 同一批事件重放两次，结果必须逐字段一致
    second = Replayer(cfg).run(store)
    eq(second.digest, result.digest, "两次回放 digest 完全一致")
    eq(second.summary()["orders"], result.summary()["orders"], "订单数一致")
    eq(second.summary()["targets"], result.summary()["targets"], "目标数一致")

    # ---- 在途扣除：没成交前不得重复下单 --------------------------------- #
    # 真实策略每轮都会重发意图；这里必须补上，否则第二轮没有待处理意图，
    # cycle 会被整轮跳过（那测的就不是"在途扣除"了）。
    cid = order["client_order_id"]
    _add_intent(store, "etf_mom", "0.20", ts=T0 + timedelta(seconds=29))
    _add_intent(store, "mean_rev", "-0.12", ts=T0 + timedelta(seconds=29),
                allow_short=True)
    store.append("cycle", {"now": T0 + timedelta(seconds=30)}, ts=T0)
    again = Replayer(cfg).run(store)
    eq(len(again.orders), 1, "在途订单被扣除后不再新增订单")
    eq(again.targets[-1].decision.value, "NOOP", "第二轮判为 NOOP（差额已被在途覆盖）")

    # ---- 成交入账 + 重复回报去重 --------------------------------------- #
    store.append("order_ack", {"client_order_id": cid}, ts=T0 + timedelta(seconds=6))
    fill_payload = {
        "client_order_id": cid, "venue_trade_id": "TRADE-1",
        "quantity": D("10"), "price": D("400.2"), "fee": D("2"),
        "fee_currency": "USD", "quote_ccy": "USD",
        "ts": T0 + timedelta(seconds=7),
    }
    store.append("fill", fill_payload, event_id="fill:TRADE-1", ts=T0 + timedelta(seconds=7))
    store.append("fill", fill_payload, event_id="fill:TRADE-1", ts=T0 + timedelta(seconds=8))
    store.append("market_snapshot", {
        "venue": "TIGER", "symbol": "QQQ", "as_of": T0 + timedelta(seconds=10),
        "received_at": T0 + timedelta(seconds=10),
        "bid": D("400.1"), "ask": D("400.3"), "last": D("400.2"), "sequence": 2,
    }, ts=T0 + timedelta(seconds=10))

    traded = Replayer(cfg).run(store)
    eq(traded.fills, 1, "同一 venue_trade_id 重复回报只入账一次")
    eq(traded.orders[cid]["state"], "FILLED", "订单状态流转到 FILLED")

    report = traded.ledger.assert_attribution_closes({("TIGER", "QQQ"): D("400.2")})
    truth(report.closes, "账本归因闭合（五项之和 = 净收益）")
    eq(report.net_pnl, D("-2"), "净收益 = 手续费 -2（价格未变，滑点自相抵消）")
    eq(report.trading, D("2"), "交易收益 10×(400.2-400) = 2")
    eq(report.slippage, D("-2"), "滑点 -2（买贵了 0.2）")
    eq(report.fee, D("-2"), "费用 -2")

    # ---- fail-closed：缺价的标的整轮跳过 --------------------------------- #
    store2 = MemoryEventStore("b1-noprice")
    store2.append("deposit", {"amount": D("100000")}, ts=T0)
    _add_market(store2, "QQQ")
    _add_intent(store2, "etf_mom", "0.10", symbol="SPY")     # SPY 没有行情
    store2.append("cycle", {"now": T0 + timedelta(seconds=5)}, ts=T0)
    blocked = Replayer(_config()).run(store2)
    eq(len(blocked.orders), 0, "缺价标的不下单")
    eq(len(blocked.blocked_cycles), 1, "整轮被标记为 blocked 并留痕")
    truth("no_price" in blocked.blocked_cycles[0]["reason"], "阻塞原因指向缺价")

    # ---- 超时 → UNKNOWN → 冻结 → 对账解冻 ------------------------------- #
    store3 = MemoryEventStore("b1-unknown")
    store3.append("deposit", {"amount": D("100000")}, ts=T0)
    _add_market(store3)
    _add_intent(store3, "etf_mom", "0.10")
    store3.append("cycle", {"now": T0 + timedelta(seconds=5)}, ts=T0)
    first = Replayer(_config()).run(store3)
    cid3 = list(first.orders)[0]

    store3.append("order_timeout", {"client_order_id": cid3},
                  ts=T0 + timedelta(seconds=40))
    timed_out = Replayer(_config()).run(store3)
    eq(timed_out.orders[cid3]["state"], "UNKNOWN", "超时迁移到 UNKNOWN")
    truth("QQQ" in timed_out.risk_state.symbols_with_unknown_orders, "未知状态标的被冻结")
    falsy(any(o["client_order_id"] != cid3 for o in timed_out.orders.values()),
          "超时后没有生成新的重试订单")

    store3.append("venue_status", {
        "client_order_id": cid3, "venue_found": False,
    }, ts=T0 + timedelta(seconds=60))
    resolved = Replayer(_config()).run(store3)
    eq(resolved.orders[cid3]["state"], "EXPIRED", "交易所查无此单 → 判为未生效")
    eq(len(resolved.risk_state.symbols_with_unknown_orders), 0, "对账完成后解冻标的")

    # ---- 风控上限：单笔超限 → REDUCE_REQUIRED → 组合层重算 -------------- #
    store4 = MemoryEventStore("b1-cap")
    store4.append("deposit", {"amount": D("100000")}, ts=T0)
    _add_market(store4)
    _add_intent(store4, "etf_mom", "0.40")     # 20000 = NAV 20%
    store4.append("cycle", {"now": T0 + timedelta(seconds=5)}, ts=T0)
    capped = Replayer(_config(RiskPolicy(
        max_order_notional_pct=D("2"), max_strategy_allocation_pct=D("90"),
        min_order_notional=D("50"), max_gross_exposure_pct=D("200"),
    ))).run(store4)
    verdicts = [d.verdict.value for d in capped.decisions]
    eq(verdicts[0], "REDUCE_REQUIRED", "首笔超 2% NAV → 要求重算（风控不代改单量）")
    eq(verdicts[-1], "ALLOW", "组合层按 cap 重算后放行")
    eq(len(capped.orders), 1, "重算后只留 1 笔订单")
    eq(D(list(capped.orders.values())[0]["remaining_qty"]) * D("400"), D("2000"),
       "实际下单金额被压到 2% NAV = 2000")


# --------------------------------------------------------------------------- #

def body() -> None:
    test_event_store()
    test_market()
    test_cost()
    test_adapter()
    test_replay()


def main() -> int:
    try:
        return run("B1 数据层与回放", body)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
