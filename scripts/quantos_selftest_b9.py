#!/usr/bin/env python3
"""
B9 验收：交易日历 + 策略排期 + 时间口径 + 上屏。

四条判据，跟其他批次一致：
  1. 说到的约束真的会拦（锚点不是交易日、日历未校准、in_doubt 重跑）
  2. 「漏跑不漂移」是可算出来的事实，不是口号
  3. 跑没跑成有三种状态，而且三种都能被区分开
  4. 真实 A 股数据上整条链能跑两拍，第二拍基于第一拍的持仓

跑法：`.venv/bin/python scripts/quantos_selftest_b9.py`
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, ROOT)

from _asserts import (  # noqa: E402
    eq, falsy, guard, ne, raises, run, section, truth,
)

from quantos_core.calendar import (                                    # noqa: E402
    CalendarError, TradingCalendar, audit_rules_against_data,
    build_from_observed, load_calendar, save_calendar, weekdays_between,
)
from quantos_core.ops_server import OpsHttpServer, OpsService          # noqa: E402
from quantos_core.schedule import (                                    # noqa: E402
    Decision, PreflightAbort, RebalanceScheduler, RebalanceSpec, RunOutcome,
    ScheduleError, ScheduleStore, signal_day_for, status_snapshot,
)

UTC = timezone.utc
CST = timezone(timedelta(hours=8))
CAL_PATH = Path(ROOT) / "config" / "calendar" / "ashare.json"
INDEX_CSV = Path(ROOT) / "state" / "ashare" / "510300_daily.csv"
DAILY = Path(ROOT) / "state" / "ashare" / "daily"
PACK = Path(ROOT) / "research" / "strategy_packs" / "ashare_ew20.strat.py"


def _tmpdir() -> str:
    return tempfile.mkdtemp(prefix="qb9-")


def _weekday_cal(start: date, end: date) -> TradingCalendar:
    """整段都是交易日的日历（不含节假日）。算术测试用它，数字才好看。"""
    return build_from_observed(weekdays_between(start, end), name="SIMPLE")


def _real_cal() -> TradingCalendar:
    raw = json.loads(CAL_PATH.read_text(encoding="utf-8"))
    return TradingCalendar.from_dict(raw)


def _cs(day: date, hh: int = 16, mm: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=CST)


# --------------------------------------------------------------------------- #
#  A. 交易日历
# --------------------------------------------------------------------------- #

def part_a() -> None:
    section("A1 从「有行情的日子」推出休市日（数据即事实，不手抄节假日）")

    # 2026-01-05~01-16 的两周里，故意抽掉 01-07 和 01-12 两天（假设是节日）
    days = [d for d in weekdays_between(date(2026, 1, 5), date(2026, 1, 16))
            if d not in (date(2026, 1, 7), date(2026, 1, 12))]
    cal = build_from_observed(days, name="FAKE")
    eq(len(days), 8, "A1 人造样本有 8 个交易日")
    truth(date(2026, 1, 7) in cal.holidays, "A1 01-07 被推成休市日")
    truth(date(2026, 1, 12) in cal.holidays, "A1 01-12 被推成休市日")
    eq(len(cal.holidays), 2, "A1 只推出这两天，不会多算")
    falsy(cal.is_trading_day(date(2026, 1, 7)), "A1 01-07 判为休市")
    truth(cal.is_trading_day(date(2026, 1, 8)), "A1 01-08 仍是交易日")

    section("A2 端点日不会被误判成休市（最容易造出来的假节假日）")

    # 只给 01-05~01-09，区间外的工作日不该被当成休市日
    narrow = build_from_observed(
        [date(2026, 1, 5), date(2026, 1, 9)], name="NARROW")
    eq(len(narrow.holidays), 3, "A2 区间内缺的 3 天是休市日")
    truth(date(2026, 1, 2) not in narrow.holidays,
          "A2 区间之前的日子没有被算进休市日（数据没开始 ≠ 休市）")
    truth(date(2026, 1, 12) not in narrow.holidays,
          "A2 区间之后的日子也没有被算进休市日")

    section("A3 交易日算术：往前、往后、数个数、算偏移")

    cal = _weekday_cal(date(2026, 3, 2), date(2026, 4, 30))
    eq(cal.next_trading_day(date(2026, 3, 6)), date(2026, 3, 9),
       "A3 周五的下一个交易日是周一")
    eq(cal.prev_trading_day(date(2026, 3, 9)), date(2026, 3, 6),
       "A3 周一的上一个交易日是周五")
    eq(cal.shift(date(2026, 3, 2), 5), date(2026, 3, 9),
       "A3 从周一往后数 5 个交易日还是下周一")
    eq(cal.offset_from(date(2026, 3, 2), date(2026, 3, 9)), 5,
       "A3 偏移 5：中间隔了 5 个交易日")
    eq(cal.offset_from(date(2026, 3, 9), date(2026, 3, 2)), -5,
       "A3 反向偏移是负的（排列错方向会让周期整体反向）")
    eq(cal.count(date(2026, 3, 2), date(2026, 3, 6)), 5,
       "A3 一周 5 个交易日（两端都算）")

    section("A4 未校准区间：能说清缺哪一段，不是只说「不可用」")

    falsy(cal.covered(date(2026, 5, 1)), "A4 覆盖区间之后的日子未校准")
    gap = cal.gap_for(date(2026, 5, 1))
    truth(gap is not None, "A4 能给出缺口说明")
    truth("2026-04-30" in gap.reason and "2026-05-01" in gap.reason,
          f"A4 缺口说明同时给出边界和本次要用的日期（{gap.reason}）")
    truth(cal.covered(date(2026, 3, 2)), "A4 区间内的日子是已校准的")
    falsy(cal.covered(date(2026, 2, 27)), "A4 区间起点之前也未校准")

    section("A5 规则日历 vs 真实数据：误差必须可量化（155 天量级）")

    if not INDEX_CSV.is_file():
        truth(True, "A5 本机缺 state/ashare/510300_daily.csv，跳过真实日历段")
    else:
        observed = []
        with open(INDEX_CSV, encoding="utf-8") as fh:
            fh.readline()
            for line in fh:
                head = line.split(",", 1)[0].strip()
                try:
                    observed.append(date.fromisoformat(head))
                except ValueError:
                    continue
        real = build_from_observed(observed, name="ASHARE")
        rule = TradingCalendar(name="RULE", first_day=real.first_day,
                               verified_until=real.verified_until)
        audit = audit_rules_against_data(rule, observed)
        eq(audit.silent_skips, 0,
           "A5 规则日历不会把真实交易日判成休市（A 股没有周末补班交易）")
        truth(len(audit.errors_rule_open_data_closed) > 100,
              f"A5 规则日历把 {len(audit.errors_rule_open_data_closed)} 个节假日"
              "当成了交易日（这就是「只按工作日推算」的代价）")
        years = max(1, real.verified_until.year - real.first_day.year + 1)
        per_year = len(audit.errors_rule_open_data_closed) / years
        truth(15 <= per_year <= 25,
              f"A5 年均错 {per_year:.1f} 天，落在 A 股节假日实际量级"
              "（每年 15~25 天）")

        self_audit = audit_rules_against_data(real, observed)
        eq(self_audit.silent_skips, 0, "A6 由数据推出的日历不会漏掉任何交易日")
        eq(len(self_audit.errors_rule_open_data_closed), 0,
           "A6 也不会多算 —— 但这是同义反复，只能证明构建逻辑自洽")

    section("A7 落盘 / 读取往返一致；坏文件返回 None 而不是崩")

    cal = _weekday_cal(date(2026, 3, 2), date(2026, 4, 30))
    path = os.path.join(_tmpdir(), "cal.json")
    save_calendar(cal, path)
    back = load_calendar(path)
    truth(back is not None, "A7 能读回日历")
    eq(back.holidays, cal.holidays, "A7 节假日集合一致")
    eq(back.verified_until, cal.verified_until, "A7 覆盖边界一致")
    eq(load_calendar(os.path.join(_tmpdir(), "nope.json")), None,
       "A7 文件不存在返回 None（运维面要能启动并说「没有日历」）")
    bad = os.path.join(_tmpdir(), "bad.json")
    with open(bad, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    eq(load_calendar(bad), None, "A7 损坏文件返回 None 而不是抛异常")

    section("A8 自相矛盾的日历能被自己报出来")

    broken = TradingCalendar(
        name="BROKEN",
        holidays=frozenset({date(2026, 3, 2)}),
        extra_open_days=frozenset({date(2026, 3, 2)}),
        first_day=date(2026, 3, 1), verified_until=date(2026, 3, 31))
    truth(any("同时被标为休市和开市" in w for w in broken.unhealthy),
          "A8 同一天既休市又开市会被报出来（而不是按某一边静默处理）")
    truth(any("没有覆盖区间" in w for w in
              TradingCalendar(name="X").unhealthy),
          "A8 没有覆盖区间的日历自己会告警")


# --------------------------------------------------------------------------- #
#  B. 到期判定：漏跑不漂移
# --------------------------------------------------------------------------- #

def part_b() -> None:
    section("B1 周期按交易日算，不按自然日")

    cal = _weekday_cal(date(2026, 3, 2), date(2026, 6, 30))
    anchor = date(2026, 3, 2)
    spec = RebalanceSpec("s1", "v1", anchor, 5)
    truth(spec.due_on(cal, anchor), "B1 锚点当天到期")
    truth(spec.due_on(cal, date(2026, 3, 9)), "B1 第 5 个交易日到期")
    falsy(spec.due_on(cal, date(2026, 3, 10)), "B1 第 6 个交易日不到期")
    # 自然日口径会算错的那一天：3-02 起 5 个自然日是 3-07（周六）
    falsy(spec.due_on(cal, date(2026, 3, 7)), "B1 自然日的第 5 天（周六）不是交易日")
    eq(spec.nth_due(cal, 4), date(2026, 3, 30),
       "B1 第 4 次调仓 = 20 个交易日之后（自然日口径会是 3-22）")

    section("B2 漏跑不漂移：这是本批最重要的一条")

    # anchor=3-02，每 5 个交易日。假设跑到 3-02 之后服务器挂了 3 拍，
    # 4-01 才恢复。两种算法给出的下一个到期日不同。
    last_run = date(2026, 3, 2)
    today = date(2026, 4, 1)
    correct = spec.next_due(cal, today)
    naive = cal.shift(last_run, 5)                 # 错：从上次运行算
    # 正确：从锚点重新算相位
    offset = cal.offset_from(anchor, today)
    eq(correct, spec.nth_due(cal, offset // 5 + 1),
       "B2 下一个到期日等于「锚点 + k×N」，不是「上次 + N」")
    ne(correct, naive, "B2 两种算法结果不同 —— 差的那几天就是漂移的起点")
    eq(correct, date(2026, 4, 6),
       "B2 恢复后第一次到期落在锚点相位上（4-06），不是 4-01")
    # 再推 10 次，漂移不会自己收敛
    drifted, fixed = naive, correct
    for _ in range(10):
        drifted = cal.shift(drifted, 5)
        fixed = spec.next_due(cal, fixed)
    ne(drifted, fixed, "B2 推 10 次之后两者仍然错开（漂移不会自愈）")

    eq(spec.cycles_between(cal, last_run, today), 4,
       "B2 3-02 到 4-01 之间漏了 4 拍（含 4-01 这一天不算，故为 4）")

    section("B3 锚点与周期本身的合法性")

    raises(ScheduleError, lambda: RebalanceSpec("s1", "v1", anchor, 0),
           "B3 rebalance_days=0 被拒（等于每根 bar 都调）")
    raises(ScheduleError, lambda: RebalanceSpec("", "v1", anchor, 5),
           "B3 没有 strategy_id 被拒")
    raises(ScheduleError, lambda: RebalanceSpec("s1", "", anchor, 5),
           "B3 没有版本号被拒（版本变了就是另一个排期）")

    store = ScheduleStore(os.path.join(_tmpdir(), "s.json"))
    raises(ScheduleError,
           lambda: store.register(RebalanceSpec("s1", "v1", date(2026, 3, 7), 5),
                                  cal),
           "B3 锚点落在周末被拒（相位会整体平移）")
    outside = RebalanceSpec("s2", "v1", date(2026, 8, 3), 5)
    raises(ScheduleError, lambda: store.register(outside, cal),
           "B3 锚点落在未校准区间被拒")

    section("B4 kind=signal 每个交易日都到期")

    sig = RebalanceSpec("s3", "v1", anchor, 1, kind="signal")
    truth(sig.due_on(cal, date(2026, 3, 4)), "B4 signal 型在任意交易日到期")
    eq(sig.next_due(cal, date(2026, 3, 6)), date(2026, 3, 9),
       "B4 signal 型的下一次是下一个交易日")


# --------------------------------------------------------------------------- #
#  C. 状态机：一天只跑一次，跑没跑成要说清
# --------------------------------------------------------------------------- #

def _stack(tmp: str, cal: TradingCalendar, anchor: date, n: int = 5):
    store = ScheduleStore(os.path.join(tmp, "sched.json"))
    calls = []

    def runner(spec, day):
        calls.append((spec.strategy_id, day))
        return RunOutcome(detail="ok", signals=2, submitted=1, fills=1)

    sched = RebalanceScheduler(calendar=cal, store=store, runner=runner,
                              settle_minutes=30, clock=lambda: _cs(date(2026, 3, 2)))
    sched.register(RebalanceSpec("s1", "v1", anchor, n))
    return sched, store, calls


def part_c() -> None:
    section("C1 同一天跑两次：runner 只被调一次")

    tmp = _tmpdir()
    cal = _weekday_cal(date(2026, 3, 2), date(2026, 6, 30))
    sched, store, calls = _stack(tmp, cal, date(2026, 3, 2))

    first = sched.tick(_cs(date(2026, 3, 2)))
    eq(first.items[0].decision, Decision.RAN, "C1 第一拍真的跑了")
    eq(len(calls), 1, "C1 runner 被调 1 次")

    second = sched.tick(_cs(date(2026, 3, 2)))
    eq(second.items[0].decision, Decision.SKIPPED_ALREADY_RAN,
       "C1 同一数据日第二次是「已跑过」")
    eq(len(calls), 1, "C1 runner 仍然只被调 1 次（幂等）")

    section("C2 重启之后仍然幂等（状态真的落盘了）")

    reloaded = ScheduleStore(os.path.join(tmp, "sched.json"))
    eq(len(reloaded), 1, "C2 新进程能从盘上读回排期")
    entry = reloaded.find("s1", "v1")
    eq(entry.last_run_day, date(2026, 3, 2), "C2 上次运行日期也读回来了")

    sched2 = RebalanceScheduler(calendar=cal, store=reloaded,
                               runner=lambda s, d: calls.append((s.strategy_id, d)),
                               settle_minutes=30)
    third = sched2.tick(_cs(date(2026, 3, 2)))
    eq(third.items[0].decision, Decision.SKIPPED_ALREADY_RAN,
       "C2 重启后的第一次 tick 也不会重跑")
    eq(len(calls), 1, "C2 runner 依旧只被调 1 次")

    section("C3 「确定没跑」：PreflightAbort 清 claim，下一拍可重试")

    tmp = _tmpdir()
    runs = {"n": 0}

    def flaky(spec, day):
        runs["n"] += 1
        if runs["n"] == 1:
            raise PreflightAbort("行情不够 warmup")
        return RunOutcome(detail="补跑成功", fills=1)

    store = ScheduleStore(os.path.join(tmp, "s.json"))
    sched = RebalanceScheduler(calendar=cal, store=store, runner=flaky,
                              settle_minutes=30)
    sched.register(RebalanceSpec("s1", "v1", date(2026, 3, 2), 5))

    r1 = sched.tick(_cs(date(2026, 3, 2)))
    eq(r1.items[0].decision, Decision.FAILED, "C3 预检放弃判为 FAILED")
    falsy(store.find("s1", "v1").in_doubt,
          "C3 但**不留 claim**（确定没跑，重试是安全的）")
    r2 = sched.tick(_cs(date(2026, 3, 2)))
    eq(r2.items[0].decision, Decision.RAN, "C3 下一拍直接重试成功")

    section("C4 「不知道跑没跑」：保留 claim，拒绝自动重跑")

    tmp = _tmpdir()
    boom = {"n": 0}

    def explode(spec, day):
        boom["n"] += 1
        raise RuntimeError("成交回报断了")

    store = ScheduleStore(os.path.join(tmp, "s.json"))
    sched = RebalanceScheduler(calendar=cal, store=store, runner=explode,
                              settle_minutes=30)
    sched.register(RebalanceSpec("s1", "v1", date(2026, 3, 2), 5))

    r1 = sched.tick(_cs(date(2026, 3, 2)))
    eq(r1.items[0].decision, Decision.FAILED_IN_DOUBT,
       "C4 普通异常判为「结果未知」")
    entry = store.find("s1", "v1")
    truth(entry.in_doubt, "C4 claim 被保留（这就是「可能已经下过单」的证据）")
    eq(entry.claim_day, date(2026, 3, 2), "C4 claim 记的正是那一拍的数据日")
    falsy(entry.healthy, "C4 in_doubt 直接算不健康")

    r2 = sched.tick(_cs(date(2026, 3, 2)))
    eq(r2.items[0].decision, Decision.REFUSED_IN_DOUBT,
       "C4 下一拍拒绝重跑，而不是猜一个方向")
    eq(boom["n"], 1, "C4 runner 没有被第二次调用（否则就是重复下单）")
    truth(len(r2.needs_attention) == 1, "C4 报告把这条列进「需人工」")

    section("C5 清 in_doubt 必须人工 + 写理由")

    raises(ScheduleError,
           lambda: store.resolve("s1", "v1", actor="ai:agent",
                                 reason="我觉得应该没事"),
           "C5 AI 不能清 in_doubt（这条决定「那天有没有下单」）")
    raises(ScheduleError,
           lambda: store.resolve("s1", "v1", actor="human:owner", reason="ok"),
           "C5 理由少于 6 个字被拒")
    raises(ScheduleError,
           lambda: store.resolve("s1", "v1", actor="human:owner",
                                 reason="查过券商流水没有这笔", decision="maybe"),
           "C5 decision 只认 cleared / assumed_done")

    entry = store.resolve("s1", "v1", actor="human:owner",
                          reason="查过券商流水，那天确实没有成交",
                          decision="cleared")
    falsy(entry.in_doubt, "C5 cleared 之后 claim 被清掉")
    eq(entry.last_decision, "resolved:cleared", "C5 结论写进状态（事后可查是谁判的）")
    r3 = sched.tick(_cs(date(2026, 3, 2)))
    eq(r3.items[0].decision, Decision.FAILED_IN_DOUBT,
       "C5 重跑仍会失败，但至少不再是「拒绝」—— 说明 claim 真的清了")

    section("C6 assumed_done：判定那天已经跑过，不再重跑")

    store2 = ScheduleStore(os.path.join(_tmpdir(), "s2.json"))
    sched2 = RebalanceScheduler(calendar=cal, store=store2,
                               runner=lambda s, d: RunOutcome(fills=1),
                               settle_minutes=30)
    sched2.register(RebalanceSpec("s9", "v1", date(2026, 3, 2), 5))
    store2.fail(store2.find("s9", "v1"), Decision.FAILED_IN_DOUBT,
                "模拟未知", keep_claim=True)
    store2.find("s9", "v1").claim_day = date(2026, 3, 2)
    store2.resolve("s9", "v1", actor="human:owner",
                   reason="券商流水显示那天已经成交了",
                   decision="assumed_done")
    r = sched2.tick(_cs(date(2026, 3, 2)))
    eq(r.items[0].decision, Decision.SKIPPED_ALREADY_RAN,
       "C6 assumed_done 之后那一天视为已跑，不再下单")

    section("C7 异常隔离：一个策略炸了不影响另一个")

    tmp = _tmpdir()
    store = ScheduleStore(os.path.join(tmp, "s3.json"))
    done = []

    def selective(spec, day):
        if spec.strategy_id == "bad":
            raise RuntimeError("这个策略坏了")
        done.append(spec.strategy_id)
        return RunOutcome(fills=1)

    sched = RebalanceScheduler(calendar=cal, store=store, runner=selective,
                              settle_minutes=30)
    sched.register(RebalanceSpec("bad", "v1", date(2026, 3, 2), 5))
    sched.register(RebalanceSpec("good", "v1", date(2026, 3, 2), 5))
    rep = sched.tick(_cs(date(2026, 3, 2)))
    eq(len(rep.ran), 1, "C7 只有一个策略跑成")
    eq(done, ["good"], "C7 坏策略没有拖住好策略（BaseException 也被兜住）")
    eq(len(rep.failed), 1, "C7 坏策略被单独列为失败")

    section("C8 没到期就不跑，也不留 claim")

    store = ScheduleStore(os.path.join(_tmpdir(), "s4.json"))
    called = []
    sched = RebalanceScheduler(calendar=cal, store=store,
                               runner=lambda s, d: called.append(d),
                               settle_minutes=30)
    sched.register(RebalanceSpec("s1", "v1", date(2026, 3, 2), 5))
    rep = sched.tick(_cs(date(2026, 3, 3)))
    eq(rep.items[0].decision, Decision.SKIPPED_NOT_DUE, "C8 未到期判为 skipped")
    eq(len(called), 0, "C8 runner 未被调用")
    falsy(store.find("s1", "v1").in_doubt, "C8 也没留下 claim")

    section("C9 dry-run 只判定，不落状态")

    store = ScheduleStore(os.path.join(_tmpdir(), "s5.json"))
    sched = RebalanceScheduler(calendar=cal, store=store,
                               runner=lambda s, d: RunOutcome(fills=1),
                               settle_minutes=30)
    sched.register(RebalanceSpec("s1", "v1", date(2026, 3, 2), 5))
    rep = sched.tick(_cs(date(2026, 3, 2)), dry_run=True)
    eq(rep.items[0].decision, Decision.WOULD_RUN, "C9 dry-run 报「到期待跑」")
    falsy(store.find("s1", "v1").in_doubt, "C9 不产生 claim")
    eq(store.find("s1", "v1").last_run_day, None, "C9 也不写上次运行日期")


# --------------------------------------------------------------------------- #
#  D. 时间口径：防前视偏差
# --------------------------------------------------------------------------- #

def part_d() -> None:
    section("D1 收盘 + 结算延迟之前，一律用上一个交易日")

    cal = _real_cal() if CAL_PATH.is_file() else _weekday_cal(
        date(2026, 10, 1), date(2026, 10, 31))
    last = cal.verified_until
    day = last if cal.is_trading_day(last) else cal.prev_trading_day(last)
    prev = cal.prev_trading_day(day)

    eq(signal_day_for(cal, _cs(day, 9, 0), settle_minutes=30), prev,
       "D1 09:00 用上一个交易日（今天的收盘价还不存在）")
    eq(signal_day_for(cal, _cs(day, 14, 59), settle_minutes=30), prev,
       "D1 14:59 仍然用上一个交易日 —— 那个价格不是 close")
    eq(signal_day_for(cal, _cs(day, 15, 0), settle_minutes=30), prev,
       "D1 15:00 整还没过结算延迟，仍用上一个交易日")
    eq(signal_day_for(cal, _cs(day, 15, 30), settle_minutes=30), day,
       "D1 15:30 才算今天收盘")

    section("D2 周末与节假日的数据日")

    sat = day + timedelta(days=(5 - day.weekday()) % 7 or 7)
    eq(signal_day_for(cal, _cs(sat, 10, 0), settle_minutes=30), day,
       "D2 周六的数据日仍是周五")
    holiday = max(cal.holidays) if cal.holidays else None
    if holiday is not None:
        eq(signal_day_for(cal, _cs(holiday, 16, 0), settle_minutes=30),
           cal.prev_trading_day(holiday),
           f"D2 休市日 {holiday} 收盘后的数据日退到上一个交易日")

    section("D3 未校准的数据日：整拍拒绝，runner 一次都不跑")

    stale = TradingCalendar(
        name="STALE", holidays=cal.holidays, first_day=cal.first_day,
        verified_until=date(2026, 1, 30))
    store = ScheduleStore(os.path.join(_tmpdir(), "s.json"))
    called = []
    sched = RebalanceScheduler(calendar=stale, store=store,
                               runner=lambda s, d: called.append(d))
    sched.register(RebalanceSpec("s1", "v1", date(2026, 1, 5), 5))
    rep = sched.tick(_cs(date(2026, 3, 2)))
    truth(rep.refused, "D3 整拍被拒绝")
    eq(rep.items[0].decision, Decision.REFUSED_NO_CALENDAR,
       "D3 单条也标成日历未校准")
    eq(len(called), 0, "D3 runner 没有被调用（绝不按规则猜一天去交易）")
    truth("2026-01-30" in rep.calendar_gap, "D3 拒绝理由写明日历覆盖到哪天")

    section("D4 时区取不到就报错，不退回 UTC")

    raises(ScheduleError,
           lambda: signal_day_for(cal, _cs(day, 16, 0), tz_name="Mars/Olympus"),
           "D4 未知时区直接报错")


# --------------------------------------------------------------------------- #
#  E. 真实 A 股：两拍全链路
# --------------------------------------------------------------------------- #

def part_e() -> None:
    section("E1 真实数据构造 frame 时只含 ≤ 数据日的 bar（防未来数据）")

    if not DAILY.is_dir() or not PACK.is_file():
        truth(True, "E1 本机缺 A 股日线或策略包，跳过真实链路段")
        return

    import quantos_schedule as qs

    cut = date(2026, 7, 15)
    frame, stale = qs.build_frame_for_day(DAILY, "TIGER", cut, 6, 60, 30)
    truth(len(frame.series) >= 3, f"E1 读到 {len(frame.series)} 只真实标的")
    latest = max(s.last_date() for s in frame.series.values())
    truth(latest <= cut,
          f"E1 最新一根 bar 是 {latest}，不晚于数据日 {cut}（未来数据没漏进来）")
    truth(frame.as_of.date() == cut, "E1 frame.as_of 就是数据日")

    section("E2 真实数据真跑一拍：信号 → 成交 → 净值下降")

    cal = _real_cal()
    anchor = cal.next_trading_day(date(2026, 6, 1), include_self=True)
    second = cal.shift(anchor, 20)
    truth(cal.covered(second), f"E2 两拍（{anchor} / {second}）都在日历覆盖区间内")

    tmp = _tmpdir()
    runner = qs.AshareRunner(pack_path=PACK, data_dir=DAILY, venue="TIGER",
                             params={"top_n": "3"}, symbols=6, bars=80,
                             min_bars=40, nav=Decimal("100000"),
                             dry_frame_day=anchor)
    store = ScheduleStore(os.path.join(tmp, "sched.json"))
    sched = RebalanceScheduler(calendar=cal, store=store, runner=runner,
                              settle_minutes=30)
    sched.register(RebalanceSpec("ashare_ew20", "v1", anchor, 20))

    r1 = sched.tick(_cs(anchor))
    eq(len(r1.ran), 1, "E2 第一拍真的跑了")
    out1 = r1.items[0].outcome
    truth(out1 is not None and out1.fills >= 1,
          f"E2 第一拍成交 {out1.fills if out1 else 0} 笔")
    frame_a, _stale = qs.build_frame_for_day(DAILY, "TIGER", anchor, 6, 80, 40)
    prices = {("TIGER", sym): s.last() for sym, s in frame_a.series.items()}
    nav1 = runner.ledger.nav(prices)
    truth(nav1 < Decimal("100000"),
          f"E2 买完之后净值 {nav1} 低于初始（手续费与滑点确实被扣了）")

    section("E3 第二拍（+20 个交易日）基于第一拍的持仓算差额")

    r2 = sched.tick(_cs(second))
    truth(len(r2.ran) == 1, "E3 第二拍到点并执行")
    entry = store.find("ashare_ew20", "v1")
    eq(entry.last_run_day, second, "E3 状态推进到第二拍")
    eq(entry.runs, 2, "E3 累计跑了 2 次")

    section("E4 重复 tick 不会重复下单（第二拍再敲一次）")

    before = entry.total_fills
    r3 = sched.tick(_cs(second))
    eq(r3.items[0].decision, Decision.SKIPPED_ALREADY_RAN,
       "E4 同一数据日再 tick 判为已跑过")
    eq(store.find("ashare_ew20", "v1").total_fills, before,
       "E4 成交笔数没有增加")

    section("E5 status 快照能说明「今天该不该跑」")

    snap = sched.status(_cs(second))
    truth(snap["available"] is not False, "E5 状态可用")
    eq(snap["entry_count"], 1, "E5 一条排期")
    row = snap["entries"][0]
    truth(row["due_today"], "E5 第二拍当天确实是到期日")
    eq(row["offset"], 20, "E5 偏移正好 20 个交易日")
    truth(snap["healthy"], "E5 没有 in_doubt 也没有连续失败，判为健康")


# --------------------------------------------------------------------------- #
#  F. 上屏契约
# --------------------------------------------------------------------------- #

def part_f() -> None:
    section("F1 没有日历 / 没有 store 时一律 available=False，不编绿灯")

    off = status_snapshot(None, None)
    eq(off["available"], False, "F1 无日历 → 不可用")
    falsy(off["healthy"], "F1 也不会说健康")
    truth("交易日历" in off["reason"], f"F1 理由指明缺什么（{off['reason']}）")

    cal = _weekday_cal(date(2026, 3, 2), date(2026, 6, 30))
    eq(status_snapshot(cal, None)["available"], False, "F1 无 store → 不可用")

    section("F2 in_doubt / 连续失败会体现在 healthy 与 needs_attention")

    store = ScheduleStore(os.path.join(_tmpdir(), "s.json"))
    store.register(RebalanceSpec("s1", "v1", date(2026, 3, 2), 5), cal)
    snap = status_snapshot(cal, store, now=_cs(date(2026, 3, 2)))
    truth(snap["healthy"], "F2 刚登记、没跑过 → 还不算不健康（只是从未运行）")
    eq(snap["entries"][0]["last_decision"], "", "F2 last_decision 为空表示从未运行")

    store.claim(store.find("s1", "v1"), date(2026, 3, 2), _cs(date(2026, 3, 2)))
    snap = status_snapshot(cal, store, now=_cs(date(2026, 3, 2)))
    falsy(snap["healthy"], "F2 有 in_doubt → 不健康")
    eq(snap["needs_attention"], ["s1"], "F2 needs_attention 点名到策略")

    section("F3 OpsService.schedule() 未接入时如实说没接入")

    svc = OpsService(metrics=None, registry=None)
    payload = svc.schedule()
    eq(payload["available"], False, "F3 未接入 → 不可用")
    truth("未接入" in payload["reason"], "F3 理由写清楚是「未接入」而不是「没有排期」")

    section("F4 HTTP /api/ops/schedule 端点可用")

    svc = OpsService(metrics=None, registry=None,
                     schedule_provider=lambda: status_snapshot(cal, store,
                                                                now=_cs(date(2026, 3, 2))))
    with OpsHttpServer(svc, port=0) as srv:
        with urllib.request.urlopen(srv.url + "/api/ops/schedule", timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    truth(body.get("available") is not False, "F4 端点返回可用快照")
    eq(body["entry_count"], 1, "F4 端点确实带上了排期条目")

    section("F5 页面：渲染函数存在、被 refreshAll 调用，且渲染函数之间互不调用")

    html = (Path(ROOT) / "quantos" / "index.html").read_text(encoding="utf-8")
    truth("function renderSchedule()" in html, "F5 有 renderSchedule()")
    truth("renderSchedule();      /* B9" in html, "F5 refreshAll 里调用了它")
    truth("id=\"scheduleBody\"" in html, "F5 卡片 DOM 容器存在")
    truth("/api/ops/schedule" in html, "F5 页面确实去拉这个端点")

    def body_of(name: str) -> str:
        start = html.index("function " + name + "(")
        depth, i = 0, html.index("{", start)
        begin = i
        while i < len(html):
            if html[i] == "{":
                depth += 1
            elif html[i] == "}":
                depth -= 1
                if depth == 0:
                    return html[begin:i]
            i += 1
        raise AssertionError(f"取不到 {name} 的函数体")

    sched_body = body_of("renderSchedule")
    for other in ("renderRiskModel", "renderCanary", "renderResearch",
                  "renderOverview", "refreshAll"):
        falsy(other + "(" in sched_body,
              f"F5 renderSchedule 不调用 {other}（渲染层之间必须单向）")

    section("F6 页面探针：排期数字能对回后端；in_doubt 与缺数据都必须降级")

    probe_node = subprocess.run(["node", "--version"], capture_output=True, text=True)
    if probe_node.returncode != 0:
        truth(True, "F6 本机没有 node，跳过前端探针（不影响后端结论）")
        return

    from quantos_ops_server import OpsService as _Svc, build_runtime
    rt = build_runtime()
    svc = _Svc(metrics=rt["metrics"], registry=rt["registry"],
               prices_provider=lambda: rt["prices"], token="t0ken",
               series=rt["series"], live_gate=rt["gate"],
               risk_model_store=rt["risk_model_store"],
               canary_router=rt["canary_router"],
               research_inbox=rt["research_inbox"])

    probe_cal = _weekday_cal(date(2026, 1, 5), date(2026, 12, 31))
    probe_store = ScheduleStore(os.path.join(_tmpdir(), "probe.json"))
    probe_store.register(RebalanceSpec("ashare_ew20", "v1",
                                       date(2026, 1, 5), 20), probe_cal)
    healthy = status_snapshot(probe_cal, probe_store, now=_cs(date(2026, 3, 2)))

    base = {
        "snapshot": svc.snapshot(), "strategies": svc.strategies(),
        "audit": svc.audit(30), "navHistory": svc.nav_history(),
        "live": svc.live(), "portfolio": svc.portfolio(),
        "schedule": healthy,
    }
    probe = os.path.join(ROOT, "scripts", "page_probe.js")
    page = os.path.join(ROOT, "quantos", "index.html")

    def _run_probe(payload, name: str) -> subprocess.CompletedProcess:
        path = os.path.join(_tmpdir(), name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, default=str)
        return subprocess.run(["node", probe, page, path],
                              capture_output=True, text=True, timeout=240)

    ok = _run_probe(base, "sched_ok.json")
    eq(ok.returncode, 0, "F6 健康排期 fixture 探针全部断言通过")
    if ok.stdout.strip():
        print("      " + ok.stdout.strip().splitlines()[-1])

    probe_store.claim(probe_store.find("ashare_ew20", "v1"),
                      date(2026, 3, 2), _cs(date(2026, 3, 2)))
    doubt = dict(base)
    doubt["schedule"] = status_snapshot(probe_cal, probe_store,
                                        now=_cs(date(2026, 3, 2)))
    falsy(doubt["schedule"]["healthy"], "F6 造出的 fixture 确实是红的")
    bad = _run_probe(doubt, "sched_doubt.json")
    eq(bad.returncode, 0, "F6 in_doubt fixture 探针通过（页面写清「可能下过单」）")

    missing = dict(base)
    missing.pop("schedule")
    gone = _run_probe(missing, "sched_missing.json")
    eq(gone.returncode, 0, "F6 缺 /api/ops/schedule 时探针通过（降级不挂绿灯）")


# --------------------------------------------------------------------------- #
#  G. 代码卫生
# --------------------------------------------------------------------------- #

STDLIB_OK = {
    "math", "statistics", "decimal", "fractions", "operator", "datetime",
    "typing", "dataclasses", "collections", "itertools", "functools",
    "enum", "string", "re", "ast", "hashlib", "os", "sys", "tempfile",
    "pathlib", "types", "__future__", "json", "time", "abc", "contextlib",
    "zoneinfo",
}


def part_g() -> None:
    section("G1 两个新模块只依赖标准库")

    for name in ("calendar.py", "schedule.py"):
        path = Path(ROOT) / "quantos_core" / name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        bad = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root not in STDLIB_OK:
                        bad.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue
                root = (node.module or "").split(".")[0]
                if root not in STDLIB_OK:
                    bad.append(node.module or "")
        eq(bad, [], f"G1 {name} 只依赖标准库（发现：{bad}）")

    section("G2 排期模块不碰网关 / 券商 / 账本（结构上无法直接下单）")

    src = (Path(ROOT) / "quantos_core" / "schedule.py").read_text(encoding="utf-8")
    for forbidden in ("gateway", "ccxt", "oms", "ledger", ".place("):
        falsy(forbidden in src,
              f"G2 schedule.py 不引用 {forbidden}（排期只管「什么时候跑」）")

    section("G3 日历不会被当成「可以猜未来」的东西")

    cal_src = (Path(ROOT) / "quantos_core" / "calendar.py").read_text(encoding="utf-8")
    truth("covered(" in cal_src, "G3 覆盖判定是显式 API")
    truth("verified_until" in cal_src, "G3 覆盖边界是日历的一等字段")


def main() -> None:
    part_a()
    part_b()
    part_c()
    part_d()
    part_e()
    part_f()
    part_g()


if __name__ == "__main__":
    raise SystemExit(run("B9 交易日历与策略排期", main))
