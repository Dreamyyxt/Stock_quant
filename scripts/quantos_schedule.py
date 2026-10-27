#!/usr/bin/env python3
"""
策略排期调度 —— 一条命令回答"现在该不该跑、上一次跑成没跑成、下次什么时候跑"。

用法
====
    # 1. 从真实行情重建交易日历（并做交叉对账），落盘 config/calendar/ashare.json
    .venv/bin/python scripts/quantos_schedule.py calendar --build

    # 2. 看每个策略的排期状态（只读，不起任何东西）
    .venv/bin/python scripts/quantos_schedule.py status

    # 3. 推演未来调仓日（未来日期没有真实数据，逐条标注是否已校准）
    .venv/bin/python scripts/quantos_schedule.py plan --days 90

    # 4. 推进一拍。默认只判定不执行，加 --execute 才真跑 run_once()
    .venv/bin/python scripts/quantos_schedule.py tick
    .venv/bin/python scripts/quantos_schedule.py tick --execute --now 2026-10-23T16:00:00+08:00

    # 5. 人工处理"跑了但没落结果"的那一拍
    .venv/bin/python scripts/quantos_schedule.py resolve \
        --strategy ashare_ew20 --version v1 --decision cleared \
        --actor human:owner --reason "查过券商流水，那天没有成交"

为什么要 `--execute` 才算真跑
==========================
A 股每天只有一次调仓窗口（收盘后）。一个"敲了就跑"的命令，会在你只是
想看看到期没到期的时候，顺手把今天的调仓用掉 —— 而它不会问你。
默认只判定，是让"看一眼"始终是安全的。

关于账本
========
本命令的账本是**进程内**的（`Ledger` 还没有落盘能力，那是部署层的事）。
所以独立进程跑两次 = 第二次从空仓开始建仓，目标权重全部重算一遍。

这条不是"差不多能用"，是会算错的：第二次的"目标 - 当前"会把已有持仓当成 0。
所以真跑时会在输出里显式提示；`--repeat N` 在同一进程内连推 N 拍，
账本也就跟着连续。真实部署必须先解决账本持久化（见 docs/QuantOS/04 的 B10）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from quantos_core.calendar import (                                    # noqa: E402
    CalendarError, TradingCalendar, audit_rules_against_data,
    build_from_observed, load_calendar, save_calendar,
)
from quantos_core.capital_allocator import StrategyPolicy, allocate    # noqa: E402
from quantos_core.contracts import D, OrderType                        # noqa: E402
from quantos_core.idempotency import ClientOrderIdStore                # noqa: E402
from quantos_core.ledger import Ledger                                 # noqa: E402
from quantos_core.oms import Oms                                       # noqa: E402
from quantos_core.risk_engine import RiskEngine, RiskPolicy, RiskState  # noqa: E402
from quantos_core.schedule import (                                    # noqa: E402
    Decision, PreflightAbort, RebalanceScheduler, RebalanceSpec,
    RunOutcome, ScheduleError, ScheduleStore, load_tz_or_fail,
)
from quantos_core.strategy_api import MarketFrame, Series              # noqa: E402
from quantos_core.strategy_pack import PackRejected, load_pack         # noqa: E402
from quantos_core.strategy_runtime import (                            # noqa: E402
    EngineRiskClient, PaperBroker, PaperFillConfig, StrategyRun,
    StrategyRuntime,
)

DATA_DIR = ROOT / "state" / "ashare" / "daily"
INDEX_CSV = ROOT / "state" / "ashare" / "510300_daily.csv"
CAL_PATH = ROOT / "config" / "calendar" / "ashare.json"
SCHED_PATH = ROOT / "state" / "quantos" / "schedule.json"
PACK_PATH = ROOT / "research" / "strategy_packs" / "ashare_ew20.strat.py"

TZ = load_tz_or_fail("Asia/Shanghai", "A 股排期")
ACCOUNT = "ASHARE_PAPER"
VENUE = "TIGER"


# --------------------------------------------------------------------------- #
#  读数据
# --------------------------------------------------------------------------- #

def observed_dates(path: Path) -> List[date]:
    """一个 CSV 里出现过的日期。用来建日历 / 对账。"""
    out: List[date] = []
    with open(path, encoding="utf-8") as fh:
        fh.readline()
        for line in fh:
            head = line.split(",", 1)[0].strip()
            if not head:
                continue
            try:
                out.append(date.fromisoformat(head))
            except ValueError:
                continue
    return out


def union_dates(data_dir: Path, limit: int = 0) -> List[date]:
    """多只标的的日期并集。停牌日会被别的标的补上，比单只更权威。"""
    files = sorted(data_dir.glob("*.csv"))
    if limit:
        files = files[:limit]
    seen = set()
    for path in files:
        seen.update(observed_dates(path))
    return sorted(seen)


def build_frame_for_day(data_dir: Path, venue: str, day: date,
                        symbols: int, bars: int, min_bars: int
                        ) -> Tuple[MarketFrame, List[str]]:
    """
    构造 `day` 当天的行情 frame。**只取 ≤ day 的 bar**。

    这个 `<= day` 是整条链上最容易漏掉的前视偏差防线：日线文件里带着未来
    所有日期，截不截完全靠调用方自觉。这里显式截断，并把"最后一根 bar 早于
    day"的标的单独列出来 —— 那是停牌或数据缺口，拿它算信号等于用几天前的
    价格做今天的决定。
    """
    files = sorted(data_dir.glob("*.csv"))[:symbols]
    series: Dict[str, Series] = {}
    stale: List[str] = []
    for path in files:
        points: List[Tuple[date, Decimal]] = []
        try:
            with open(path, encoding="utf-8") as fh:
                fh.readline()
                for line in fh:
                    parts = line.rstrip("\n").split(",")
                    if len(parts) < 3:
                        continue
                    try:
                        bar_day = date.fromisoformat(parts[0])
                    except ValueError:
                        continue
                    if bar_day > day:
                        break                       # 文件按日期升序，往后都不必看
                    if parts[1] in ("0", "0.0", ""):
                        continue                    # 停牌行（收盘价是补出来的）
                    try:
                        points.append((bar_day, Decimal(parts[2])))
                    except Exception:               # noqa: BLE001
                        continue
        except OSError:
            continue
        if len(points) < min_bars:
            continue
        series[path.stem] = Series(path.stem, tuple(points[-bars:]))
        if points[-1][0] < day:
            stale.append(f"{path.stem}({points[-1][0]})")
    as_of = datetime.combine(day, time(15, 0), tzinfo=TZ)
    return MarketFrame(as_of=as_of, venue=venue, series=series), stale


# --------------------------------------------------------------------------- #
#  runner：真跑一次 run_once
# --------------------------------------------------------------------------- #

class AshareRunner:
    """
    把 `RebalanceScheduler` 的 `runner(spec, day)` 接到 B8 的运行时上。

    账本、OMS、风控都留在进程内并跨拍复用 —— 因为 `Ledger` 还没有落盘能力。
    独立进程跑 = 新账本 = 从空仓开始，这一点会在输出里明说，不假装它是对的。
    """

    def __init__(self, *, pack_path: Path, data_dir: Path, venue: str,
                 params: Dict[str, str], symbols: int = 8, bars: int = 130,
                 min_bars: int = 40, nav: Decimal = D("100000"),
                 quota_pct: Decimal = D("0.4"), fresh_ledger: bool = True,
                 dry_frame_day: Optional[date] = None) -> None:
        self.pack_path = pack_path
        self.data_dir = data_dir
        self.venue = venue
        self.params = params
        self.symbols = symbols
        self.bars = bars
        self.min_bars = min_bars
        self.nav = D(nav)
        self.quota_pct = D(quota_pct)
        self.fresh_ledger = fresh_ledger
        self.ledger = Ledger(base_currency="CNY")
        self.ledger.deposit(ACCOUNT, "CNY", self.nav,
                            ts=datetime.now(timezone.utc))
        self.risk_state = RiskState()
        self.engine = RiskEngine(RiskPolicy(
            max_order_notional_pct=D("25.0"),
            max_strategy_allocation_pct=D("50.0"),
            daily_loss_pause_pct=D("3.0"),
            drawdown_pause_pct=D("12.0"),
            min_order_notional=D("1000"),
            stale_market_data_seconds=86400,
        ))
        self.risk = EngineRiskClient(self.engine, self.risk_state)
        self.oms = Oms(ACCOUNT, "PF", self.risk,
                       ClientOrderIdStore(os.path.join(
                           _state_dir(), "ashare_cid.json")),
                       order_type=OrderType.LIMIT,
                       limit_slippage_bps=D("10"))
        self.pack = None
        self._calls = 0
        self._prepare(dry_frame_day or date.today())

    def _prepare(self, day: date) -> None:
        frame, stale = build_frame_for_day(
            self.data_dir, self.venue, day, self.symbols, self.bars, self.min_bars)
        if not frame.series:
            raise PreflightAbort(
                f"{day} 之前没有任何可用行情（需要 {self.min_bars} 根 bar）")
        try:
            self.pack = load_pack(str(self.pack_path), dry_frame=frame,
                                  dry_params=self.params)
        except PackRejected as exc:
            # 体检不过 = 还没开始跑，确定没有副作用 → 可以安全重试
            raise PreflightAbort(f"策略包体检不过：{exc}") from exc

    def __call__(self, spec: RebalanceSpec, day: date) -> RunOutcome:
        if self.pack is None:
            self._prepare(day)
        frame, stale = build_frame_for_day(
            self.data_dir, self.venue, day, self.symbols, self.bars, self.min_bars)
        if not frame.series:
            raise PreflightAbort(f"{day} 之前没有可用行情")

        self._calls += 1
        policy = StrategyPolicy(strategy_id=spec.strategy_id, venue=self.venue,
                               quota_pct=self.quota_pct)
        runtime = StrategyRuntime(ledger=self.ledger, oms=self.oms,
                                  risk_client=self.risk, account_id=ACCOUNT,
                                  broker=PaperBroker(PaperFillConfig(
                                      slippage_bps=D("5"), fee_bps=D("2"),
                                      quote_ccy="CNY")))
        report = runtime.run_once(
            frame, [StrategyRun(self.pack, self.params)],
            nav=self.nav, now=frame.as_of,
            allocations={spec.strategy_id: allocate(policy, self.nav)},
            policies={spec.strategy_id: policy}, risk_state=self.risk_state)

        detail = report.summary()
        if stale:
            detail += f"；{len(stale)} 只标的当日无 bar（{', '.join(stale[:3])}…）"
        if self._calls > 1:
            detail += "；本进程内第 %d 拍" % self._calls
        return RunOutcome(
            detail=detail,
            signals=sum(n for _s, n in report.signals),
            submitted=len(report.submitted), fills=len(report.fills),
            rejected=len(report.rejected), errors=len(report.errors),
        )


def _state_dir() -> str:
    path = os.path.dirname(str(SCHED_PATH))
    os.makedirs(path, exist_ok=True)
    return path


# --------------------------------------------------------------------------- #
#  日历
# --------------------------------------------------------------------------- #

def cmd_calendar(args) -> int:
    source = Path(args.source) if args.source else INDEX_CSV
    if not source.is_file():
        print(f"找不到日历数据源：{source}", file=sys.stderr)
        return 2
    observed = observed_dates(source)
    cal = build_from_observed(observed, name="ASHARE",
                              source=f"{source.name} 的 {len(observed)} 个交易日")
    warnings = cal.unhealthy
    if warnings:
        print("⚠️  日历自身有问题：")
        for w in warnings:
            print(f"   · {w}")
        return 1

    print(f"✅ 建立 A 股交易日历  {cal.first_day} ~ {cal.verified_until}")
    print(f"   交易日 {len(observed)} 个 · 休市日（工作日但没行情）{len(cal.holidays)} 个")
    by_year: Dict[int, int] = {}
    for d in cal.holidays:
        by_year[d.year] = by_year.get(d.year, 0) + 1
    print("   各年休市日：" + "  ".join(f"{y}:{n}" for y, n in sorted(by_year.items())))

    # -- 对账 1：纯规则日历错几天（这是"未来按规则推算"的误差估计） -------- #
    rule = TradingCalendar(name="RULE(只认周末)", first_day=cal.first_day,
                           verified_until=cal.verified_until,
                           source="工作日规则，不含节假日")
    rule_audit = audit_rules_against_data(rule, observed)
    print(f"\n   规则日历对账：{rule_audit.summary()}")
    print("   ↑ 这就是「不带节假日表、只按工作日推算」的代价："
          f"每年约 {len(rule_audit.errors_rule_open_data_closed) / max(1, len(by_year)):.0f} 天")
    print("     它会把这些休市日当成交易日 → 那一拍算不出信号 → 被 fail-closed 拦下 → 晚一天调仓。")

    # -- 对账 2：换一批数据交叉验证（非同一来源，才不是同义反复） ---------- #
    limit = int(args.audit_files or 0)
    if DATA_DIR.is_dir() and limit != 0:
        cross = union_dates(DATA_DIR, limit=abs(limit))
        if cross:
            audit = audit_rules_against_data(cal, cross,
                                             start=cal.first_day,
                                             end=cal.verified_until)
            print(f"\n   交叉对账（{min(abs(limit), len(list(DATA_DIR.glob('*.csv'))))} 只个股）："
                  f"{audit.summary()}")
            if audit.silent_skips:
                print("   ⚠️  有交易日被日历判成休市 —— 这些日子会静默跳过整整一次调仓：")
                for d in audit.errors_rule_closed_data_open[:10]:
                    print(f"      · {d}")

    if args.write:
        save_calendar(cal, str(CAL_PATH))
        print(f"\n   已写入 {CAL_PATH.relative_to(ROOT)}")
        print(f"   注意：日历只负责到 {cal.verified_until}。之后的日子是未校准区间，"
              "调度器会拒绝运行而不是按规则猜。")
    if args.json:
        print(json.dumps({"calendar": cal.to_dict(),
                          "rule_audit": rule_audit.to_dict()},
                         ensure_ascii=False, indent=1))
    return 0


# --------------------------------------------------------------------------- #
#  组装
# --------------------------------------------------------------------------- #

def make_scheduler(args, *, runner=None) -> RebalanceScheduler:
    cal = load_calendar(str(CAL_PATH))
    if cal is None:
        raise SystemExit(
            f"没有可用的交易日历（{CAL_PATH.relative_to(ROOT)}）。\n"
            "先跑：.venv/bin/python scripts/quantos_schedule.py calendar --build")
    store = ScheduleStore(str(SCHED_PATH))
    if runner is None:
        runner = AshareRunner(
            pack_path=Path(args.pack), data_dir=Path(args.data_dir),
            venue=args.venue, params=_parse_sets(args.set),
            symbols=args.symbols, bars=args.bars, min_bars=args.min_bars,
            nav=D(args.nav),
        )
    return RebalanceScheduler(calendar=cal, store=store, runner=runner,
                              close_at=time(15, 0), tz_name="Asia/Shanghai",
                              settle_minutes=int(args.settle_minutes))


def register_default(sched: RebalanceScheduler, args,
                     *, create: bool = True):
    """
    拿到（必要时登记）策略的排期。

    **已落盘的排期优先**：命令行没显式给 `--anchor` 就沿用盘上那个。
    这条很重要 —— 如果每次都按"当年第一个交易日"重算锚点，那么跨年那天
    整个相位会自己跳一次，而 `last_run_day` 还留着，两边对不上。

    显式给了不同的 `--anchor` 又不带 `--rebase` 时直接报错：
    改相位必须是一次有意识的动作，不能是"顺手加了个参数"。
    """
    pack = getattr(sched.runner, "pack", None)
    if pack is None:
        raise ScheduleError("策略包没有加载，无法确定排期")
    manifest = pack.manifest
    sid, ver = manifest.strategy_id, manifest.version
    wanted_anchor = date.fromisoformat(args.anchor) if args.anchor else None

    existing = sched.store.find(sid, ver)
    if existing is not None and not args.rebase:
        if wanted_anchor is not None and wanted_anchor != existing.spec.anchor:
            raise ScheduleError(
                f"{existing.spec.key} 已登记的锚点是 {existing.spec.anchor}，"
                f"与 --anchor {wanted_anchor} 不一致。"
                "改相位会让历史与未来对不上；确认要换请加 --rebase。")
        return [existing]
    if existing is None and not create:
        return []

    anchor = wanted_anchor or _year_anchor(sched.calendar, date.today().year)
    spec = RebalanceSpec(sid, ver, anchor, int(manifest.rebalance_days),
                         manifest.kind)
    return [sched.register(spec, rebase=args.rebase)]


def _year_anchor(cal: TradingCalendar, year: int) -> date:
    """
    当年第一个交易日。日历没覆盖这一年时往回退到覆盖区间里最近的一年 ——
    因为 anchor 落在未校准区间是不允许的（`ScheduleStore.register` 会拒）。
    """
    for y in range(year, year - 30, -1):
        try:
            cand = cal.next_trading_day(date(y, 1, 1), include_self=True)
        except Exception:                                # noqa: BLE001
            continue
        if cal.covered(cand):
            return cand
    raise ScheduleError("日历覆盖区间里找不到任何可用的锚点日")


# --------------------------------------------------------------------------- #
#  子命令
# --------------------------------------------------------------------------- #

def cmd_status(args) -> int:
    sched = make_scheduler(args)
    # status 是只读的：不登记、不改状态。盘上没有排期就如实说没有。
    if not len(sched.store):
        print("盘上还没有任何排期记录。先跑一次 `tick`（默认只判定不执行）"
              "就会按策略包的 manifest 登记。")
        return 0
    snap = sched.status()
    if args.json:
        print(json.dumps(snap, ensure_ascii=False, indent=1))
        return 0

    cal = snap["calendar"]
    print(f"日历      {cal['name']}  {cal['first_day']} ~ {cal['verified_until']}"
          f"（休市 {cal['holidays']} 天）")
    print(f"数据日    {snap['signal_day']}"
          f"{'' if snap['signal_day_covered'] else '  ⚠️ ' + snap['calendar_gap']}")
    print(f"收盘口径  {snap['close_at']} {snap['timezone']} + 结算延迟后算当日\n")
    for row in snap["entries"]:
        flag = "🔴" if row["in_doubt"] else ("🟡" if not row["healthy"] else "🟢")
        print(f"{flag} {row['strategy_id']}@{row['version']}  "
              f"{row['kind']} / 每 {row['rebalance_days']} 个交易日")
        print(f"   锚点      {row['anchor']}（今天 offset {row['offset']}）")
        print(f"   今天到期  {'是' if row['due_today'] else '否'}"
              f"    下次到期 {row['next_due']}"
              f"{'' if row['next_due_verified'] else '（未校准）'}")
        print(f"   上次运行  {row['last_run_day'] or '从未'}  {row['last_run_at'] or ''}")
        print(f"   上次结论  {row['last_decision'] or '-'}  {row['last_detail']}")
        print(f"   累计      {row['runs']} 次 · 失败 {row['failures']} 次"
              f"（连续 {row['consecutive_failures']}） · 成交 {row['total_fills']} 笔")
        if row["in_doubt"]:
            print(f"   ⚠️ 卡在 claim {row['claim_day']}（{row['claim_at']}）："
                  "可能已经下过单，需人工 resolve")
        print()
    if snap["needs_attention"]:
        print(f"⚠️  需要人工处理：{', '.join(snap['needs_attention'])}")
    else:
        print("没有需要人工处理的记录。")
    return 0 if snap["healthy"] else 1


def cmd_plan(args) -> int:
    sched = make_scheduler(args)
    entries = register_default(sched, args)
    day = sched.signal_day(datetime.now(timezone.utc))
    print(f"从数据日 {day} 起推演：\n")
    unverified_total = 0
    for entry in entries:
        spec = entry.spec
        print(f"{spec.strategy_id}@{spec.version}（每 {spec.rebalance_days} 个交易日）")
        rows = []
        cur = spec.next_due(sched.calendar, day)
        for _ in range(int(args.count)):
            rows.append(cur)
            cur = spec.next_due(sched.calendar, cur)
        for d in rows:
            mark = "" if sched.calendar.covered(d) else "  ← 未校准（真实排期会拒绝跑）"
            k = sched.calendar.offset_from(spec.anchor, d)
            print(f"   #{k:>4}  {d} {d.strftime('%a')}{mark}")
        unverified = [d for d in rows if not sched.calendar.covered(d)]
        unverified_total += len(unverified)
        print(f"   共 {len(rows)} 次，其中 {len(unverified)} 次落在未校准区间\n")
    if unverified_total:
        print("提示：未校准区间来自「日历只到最后一个有数据的交易日」。"
              "补齐行情后重跑 `calendar --build` 即可。")
    return 0


def cmd_tick(args) -> int:
    sched = make_scheduler(args)
    register_default(sched, args)
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ)

    total_ran = 0
    last = None
    for i in range(max(1, int(args.repeat))):
        report = sched.tick(now, dry_run=not args.execute)
        last = report
        print(report.summary())
        for item in report.items:
            line = (f"   · {item.strategy_id}@{item.version}  {item.decision.value}")
            if item.reason:
                line += f"  {item.reason}"
            if item.missed_periods > 1:
                line += f"  （漏了 {item.missed_periods} 拍）"
            print(line)
        total_ran += len(report.ran)
        if args.json:
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=1))
        # 同一时间戳重复 tick 就是幂等测试：第二次必然全是 already_ran
        if args.repeat > 1 and not report.ran and not report.would_run:
            break

    if not args.execute:
        print("\n（默认只判定不执行。加 --execute 才会真的跑 run_once）")
    elif total_ran:
        print(f"\n✅ 本次共执行 {total_ran} 次调仓。")
        runner = getattr(sched, "runner", None)
        if isinstance(runner, AshareRunner) and runner._calls:
            print("⚠️  账本是进程内的：独立进程再跑一次将从空仓开始建仓。"
                  "连续多拍请用 --repeat 在一次进程里推。")
    return 1 if (last and last.needs_attention) else 0


def cmd_resolve(args) -> int:
    cal = load_calendar(str(CAL_PATH))
    if cal is None:
        raise SystemExit("没有可用的交易日历，无法处理")
    store = ScheduleStore(str(SCHED_PATH))
    entry = store.resolve(args.strategy, args.version or None,
                          actor=args.actor, reason=args.reason,
                          decision=args.decision)
    print(f"已处理 {entry.spec.key}：{entry.spec.anchor} 起始，"
          f"decision={args.decision}")
    print(f"  上次运行 {entry.last_run_day}，状态 {entry.last_decision}")
    return 0


def _parse_sets(pairs: Sequence[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for item in pairs:
        if "=" not in item:
            raise SystemExit(f"--set 需要 key=value 形式，收到 {item!r}")
        key, value = item.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="QuantOS 策略排期调度")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--pack", default=str(PACK_PATH))
        p.add_argument("--data-dir", default=str(DATA_DIR))
        p.add_argument("--venue", default=VENUE)
        p.add_argument("--symbols", type=int, default=8)
        p.add_argument("--bars", type=int, default=130)
        p.add_argument("--min-bars", type=int, default=40)
        p.add_argument("--nav", default="100000")
        p.add_argument("--settle-minutes", type=int, default=30)
        p.add_argument("--set", action="append", default=[], metavar="K=V")
        p.add_argument("--anchor", default="", help="锚点交易日（默认当年第一个交易日）")
        p.add_argument("--rebase", action="store_true",
                       help="显式重置排期相位（会清空运行历史）")
        p.add_argument("--json", action="store_true")

    p_cal = sub.add_parser("calendar", help="建立 / 对账交易日历")
    p_cal.add_argument("--build", action="store_true",
                       help="重建日历（默认行为；保留这个开关是为了让命令读起来明确）")
    p_cal.add_argument("--source", default="", help="日期来源 CSV（默认 510300 日线）")
    p_cal.add_argument("--write", action="store_true", default=True)
    p_cal.add_argument("--no-write", dest="write", action="store_false")
    p_cal.add_argument("--audit-files", type=int, default=60,
                       help="交叉对账用多少只个股（0 跳过）")
    p_cal.add_argument("--json", action="store_true")

    p_status = sub.add_parser("status", help="排期状态")
    common(p_status)

    p_plan = sub.add_parser("plan", help="推演未来调仓日")
    common(p_plan)
    p_plan.add_argument("--days", type=int, default=90)
    p_plan.add_argument("--count", type=int, default=6)

    p_tick = sub.add_parser("tick", help="推进一拍")
    common(p_tick)
    p_tick.add_argument("--now", default="", help="ISO 时间；不给用当前时刻")
    p_tick.add_argument("--execute", action="store_true", help="真跑 run_once")
    p_tick.add_argument("--repeat", type=int, default=1)

    p_res = sub.add_parser("resolve", help="人工处理 in_doubt 记录")
    p_res.add_argument("--strategy", required=True)
    p_res.add_argument("--version", default="")
    p_res.add_argument("--decision", default="cleared",
                       choices=["cleared", "assumed_done"])
    p_res.add_argument("--actor", default="human:owner")
    p_res.add_argument("--reason", required=True)

    args = ap.parse_args()
    handler = {"calendar": cmd_calendar, "status": cmd_status,
               "plan": cmd_plan, "tick": cmd_tick,
               "resolve": cmd_resolve}[args.cmd]
    try:
        return handler(args)
    except (ScheduleError, CalendarError) as exc:
        # 排期配置本身不对 = 用法问题，不是崩溃。退出码 2 让 CI 能分清。
        print(f"❌ {exc}", file=sys.stderr)
        return 2
    except PackRejected as exc:
        print(f"❌ 策略包被拒：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
