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
B10 起账本**落盘**（`state/quantos/ashare_ledger.json`）：每拍结束整份快照
原子替换，下一拍（哪怕是新的进程、新的机器重启）从同一份持仓继续算差额。
首次启动才按 `--nav` 建仓。

以前这里是进程内账本，"独立进程跑两次 = 第二次从空仓开始"。那不只是
"差不多能用"，是会算错的：第二次的"目标 - 当前"会把已有持仓当成 0。

账本文件读不出来时（损坏/版本不符）本命令**直接拒绝运行**，不拿空账本顶上 ——
空账本和没落盘是同一个后果，但前者看起来一切正常。
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
from quantos_core.ledger_store import LedgerStore                       # noqa: E402
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
from quantos_core.state_store import RiskStateStore                    # noqa: E402

DATA_DIR = ROOT / "state" / "ashare" / "daily"
INDEX_CSV = ROOT / "state" / "ashare" / "510300_daily.csv"
CAL_PATH = ROOT / "config" / "calendar" / "ashare.json"
SCHED_PATH = ROOT / "state" / "quantos" / "schedule.json"
LEDGER_PATH = ROOT / "state" / "quantos" / "ashare_ledger.json"
RISK_PATH = ROOT / "state" / "quantos" / "ashare_risk.json"
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


def last_close(data_dir: Path, symbol: str, day: date) -> Optional[Decimal]:
    """
    单只标的在 `day`（含）之前的最后收盘价。取不到返回 None。

    为什么要单独一个函数：`build_frame_for_day` 只装股票池前 N 只，
    而持仓里可能有已经掉出前 N 的标的。算 NAV 必须给它估值 ——
    找不到价格时**宁可报错也不填默认值**，否则 NAV 凭空变成现金值，
    仓位预算跟着缩水，而报表上看不出任何异常。
    """
    path = data_dir / f"{symbol}.csv"
    if not path.exists():
        return None
    last: Optional[Decimal] = None
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
                    break
                if parts[2] in ("0", "0.0", ""):
                    continue                      # 停牌行，收盘价是补出来的
                try:
                    last = Decimal(parts[2])
                except Exception:                 # noqa: BLE001
                    continue
    except OSError:
        return None
    return last


# --------------------------------------------------------------------------- #
#  runner：真跑一次 run_once
# --------------------------------------------------------------------------- #

class AshareRunner:
    """
    把 `RebalanceScheduler` 的 `runner(spec, day)` 接到 B8 的运行时上。

    B10 起账本与风控状态都**落盘**（给了 `ledger_path` / `risk_path` 时）：
    首次启动才按 `nav` 建仓，之后每拍结束整份快照原子替换。下一拍哪怕是新进程、
    新开机，也从同一份持仓继续算差额。不给路径时退回进程内账本（测试用）。
    """

    def __init__(self, *, pack_path: Path, data_dir: Path, venue: str,
                 params: Dict[str, str], symbols: int = 8, bars: int = 130,
                 min_bars: int = 40, nav: Decimal = D("100000"),
                 quota_pct: Decimal = D("0.4"),
                 ledger_path: Optional[Path] = None,
                 risk_path: Optional[Path] = None,
                 nav_mode: Optional[str] = None,
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
        self.ledger_store = LedgerStore(str(ledger_path)) if ledger_path else None
        self.risk_store = RiskStateStore(str(risk_path)) if risk_path else None
        # 预算口径：有持久账本时**必须**用真实 NAV。用固定初始资金会让上一拍的
        # 盈亏永远不进预算 —— NAV 涨到 12 万，目标仓位还按 10 万的 40% 算。
        self.nav_mode = nav_mode or ("ledger" if self.ledger_store else "fixed")
        self.ledger_status = "in-memory"
        self.ledger = self._open_ledger()
        self.risk_state = self._open_risk_state()
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

    # -- 状态开关 -------------------------------------------------------- #

    def _open_ledger(self) -> Ledger:
        """
        取账本：有盘读盘，没盘建仓，坏盘拒绝。

        坏盘时抛 `PreflightAbort` —— 这一拍确实还没做任何事（没有下单），
        所以清掉 claim 重试是符合事实的。但它会一直失败，直到人来处理：
        `consecutive_failures` 到 3 次排期卡片就变红。
        """
        if self.ledger_store is None:
            ledger = Ledger(base_currency="CNY")
            ledger.deposit(ACCOUNT, "CNY", self.nav, ts=datetime.now(timezone.utc))
            return ledger
        result = self.ledger_store.load(base_currency="CNY", allow_absent=True)
        if result.ledger is None:
            raise PreflightAbort(
                f"账本不可用（{result.status}）：{result.detail}。"
                "拒绝以空账本继续 —— 空账本会把已有持仓当成 0 重新买一遍。")
        if result.status == "absent":
            result.ledger.deposit(ACCOUNT, "CNY", self.nav,
                                  ts=datetime.now(timezone.utc))
            self.ledger_store.save(result.ledger)
            self.ledger_status = "created"
            return result.ledger
        self.ledger_status = result.status
        if result.status == "recovered_from_backup":
            self.ledger_status = "from-backup"
        return result.ledger

    def _open_risk_state(self) -> RiskState:
        """熔断记忆不能按 NORMAL 重启 —— 亏 15% 被 HALTED，重启就复位等于没有熔断。"""
        if self.risk_store is None:
            return RiskState()
        state, info = self.risk_store.load_state()
        if state is None:
            if info.status == "absent":
                return RiskState()
            raise PreflightAbort(
                f"风控状态不可用（{info.status}）：{info.detail}。"
                "读不出来就不能当它是 NORMAL。")
        return state

    def _persist(self) -> None:
        """
        落盘。失败必须抛 —— 落盘失败却当成功，下一拍会从旧持仓再买一遍。

        顺序是先账本后风控：账本是"发生了什么"，风控是"还能不能做"。
        中间崩掉的话，排期那张 claim 还挂着，下一拍直接 REFUSED_IN_DOUBT
        等人工确认，不会带着半套状态继续跑。
        """
        if self.ledger_store is not None:
            try:
                self.ledger_store.save(self.ledger)
            except OSError as exc:
                raise RuntimeError(f"账本落盘失败：{exc}") from exc
        if self.risk_store is not None:
            try:
                self.risk_store.save_state(self.risk_state)
            except OSError as exc:
                raise RuntimeError(f"风控状态落盘失败：{exc}") from exc

    # -- 估值 ------------------------------------------------------------ #

    def _price_map(self, frame: MarketFrame, day: date) -> Dict[Tuple[str, str], Decimal]:
        """
        给"当前持仓 + 今日 frame"凑齐估值价格。

        持仓里掉了出股票池前 N 的标的，当日 frame 里没有 —— 那也必须给它
        找到收盘价，否则 NAV 会少算一块，仓位预算跟着缩水。找不到就报错。
        """
        prices: Dict[Tuple[str, str], Decimal] = {
            (self.venue, name): series.last()
            for name, series in frame.series.items()
        }
        missing: List[str] = []
        for key in self.ledger.positions():
            if key in prices:
                continue
            price = last_close(self.data_dir, key[1], day)
            if price is None:
                missing.append(f"{key[0]}|{key[1]}")
            else:
                prices[key] = price
        if missing:
            raise PreflightAbort(
                "缺少估值价格，拒绝用假价格算 NAV：" + "、".join(missing[:5]))
        return prices

    def _budget_nav(self, frame: MarketFrame, day: date) -> Decimal:
        if self.nav_mode == "fixed":
            return self.nav
        return self.ledger.nav(self._price_map(frame, day))

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

        # 预算口径必须与账本一致：NAV 是"现在有多少钱"，不是"当初放了多少钱"
        budget = self._budget_nav(frame, day)

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
            nav=budget, now=frame.as_of,
            allocations={spec.strategy_id: allocate(policy, budget)},
            policies={spec.strategy_id: policy}, risk_state=self.risk_state)

        # 成交已经写进账本，此刻不落盘 = 下一拍从旧持仓再来一遍
        self._persist()

        detail = report.summary()
        if stale:
            detail += f"；{len(stale)} 只标的当日无 bar（{', '.join(stale[:3])}…）"
        detail += f"；NAV {budget}"
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
            ledger_path=Path(args.ledger) if args.ledger else LEDGER_PATH,
            risk_path=Path(args.risk) if args.risk else RISK_PATH,
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


def tick_exit_code(report) -> int:
    """
    一拍 → 退出码。抽成纯函数是因为它是 cron 的报警线，必须能单独测。

    0  正常（跑了 / 没到期 / 今天不是交易日 / 已跑过）
    1  有 in_doubt 记录，要人确认"那天到底下没下单"
    2  整拍被拒或跑失败 —— 等于漏拍，不能安静过去
    """
    if report is None:
        return 0
    if report.refused or report.failed:
        return 2
    if report.needs_attention:
        return 1
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
        if isinstance(runner, AshareRunner):
            where = ("进程内账本（测试用）" if runner.ledger_store is None
                     else str(Path(runner.ledger_store.path).relative_to(ROOT)))
            print(f"   账本：{where}  状态={runner.ledger_status}  "
                  f"分录={runner.ledger.entry_count}  "
                  f"净入金={runner.ledger.net_deposits_base}")
            print("   下一拍（含新进程）从这份持仓继续算差额。")

    # 退出码就是 cron 的报警线，"拒绝运行"和"跑失败"都不能返回 0：
    # 日历没校准导致整拍被拒，等于今天该调仓却没调 —— 这是漏拍，不是"今天没事"。
    return tick_exit_code(last)


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


def cmd_ledger(args) -> int:
    """
    账本运维：看状态 / 校验 / 归档重置。

    `--reset` 必须显式加 `--yes`，而且**先改名归档再删** —— 账本删了没有第二次
    机会，而这个命令离 `rm` 只有一步之遥，所以不给"默认重置"的入口。
    """
    path = Path(args.ledger) if args.ledger else LEDGER_PATH
    risk_path = Path(args.risk) if args.risk else RISK_PATH
    store = LedgerStore(str(path))
    risk = RiskStateStore(str(risk_path))

    if args.reset:
        if not args.yes:
            print("❌ 重置账本要显式加 --yes（会先归档成 .reset-<时间戳>）。",
                  file=sys.stderr)
            return 2
        if not store.exists():
            print("没有账本文件，无需重置。")
            return 0
        tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archived = [p for p in (store.archive(f"reset-{tag}"),
                                risk.archive(f"reset-{tag}")) if p]
        print("已归档：")
        for item in archived:
            print(f"  {_rel(item)}")
        print("下一拍会按 --nav 重新建仓。")
        return 0

    info = store.verify()
    if args.json:
        risk_state, risk_info = risk.load_state()
        print(json.dumps({"ledger": info, "risk": risk_info.to_dict()},
                         ensure_ascii=False, indent=1))
        return 0

    flag = "🟢" if info["status"] in ("ok", "absent") else "🔴"
    print(f"{flag} 账本    {_rel(path)}")
    print(f"   状态      {info['status']}  {info.get('detail', '')}")
    if info["exists"]:
        print(f"   备份      {'有' if info['bak_exists'] else '无'}"
              f"    写入 {info.get('saved_at', '')}")
        print(f"   分录      {info.get('entry_count', 0)} 条"
              f"    幂等键 {info.get('ids_rebuilt', 0)} 个")
        print(f"   现金      {info.get('cash_lines', 0)} 个(账户×币种)"
              f"    持仓 {info.get('positions', 0)} 只")
        print(f"   净入金    {info.get('net_deposits_base', '-')}")
    else:
        print("   还没有落过盘 —— 下一拍会按 --nav 建仓")

    risk_state, risk_info = risk.load_state()
    rflag = "🟢" if risk_info.status in ("ok", "absent") else "🔴"
    print(f"\n{rflag} 风控态  {_rel(risk_path)}")
    print(f"   状态      {risk_info.status}  {risk_info.detail}")
    if risk_state is not None:
        print(f"   权限      {risk_state.permission_level.value}"
              f"    回撤 {risk_state.drawdown_pct}%")
        if risk_state.halted_reasons:
            print(f"   熔断原因  {'、'.join(risk_state.halted_reasons)}")
    return 0 if info["status"] in ("ok", "absent") else 1


def _rel(path) -> str:
    """尽量显示相对项目根的路径，绝对路径原样输出。"""
    try:
        return str(Path(path).relative_to(ROOT))
    except ValueError:
        return str(path)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="QuantOS 策略排期调度")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--pack", default=str(PACK_PATH))
        p.add_argument("--data-dir", default=str(DATA_DIR))
        p.add_argument("--venue", default=VENUE)
        p.add_argument("--symbols", type=int, default=8)
        p.add_argument("--bars", type=int, default=130)
        p.add_argument("--min-bars", type=int, default=40)
        p.add_argument("--nav", default="100000", help="首次建仓的初始资金（之后按账本 NAV）")
        p.add_argument("--ledger", default="", help="账本文件（默认 state/quantos/ashare_ledger.json）")
        p.add_argument("--risk", default="", help="风控状态文件（默认 state/quantos/ashare_risk.json）")
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

    p_led = sub.add_parser("ledger", help="账本：状态 / 校验 / 归档重置")
    p_led.add_argument("--ledger", default="", help="账本文件（默认 state/quantos/ashare_ledger.json）")
    p_led.add_argument("--risk", default="", help="风控状态文件（默认 state/quantos/ashare_risk.json）")
    p_led.add_argument("--reset", action="store_true", help="归档并删除账本（需 --yes）")
    p_led.add_argument("--yes", action="store_true", help="确认重置")
    p_led.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)
    handler = {"calendar": cmd_calendar, "status": cmd_status,
               "plan": cmd_plan, "tick": cmd_tick,
               "resolve": cmd_resolve, "ledger": cmd_ledger}[args.cmd]
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
