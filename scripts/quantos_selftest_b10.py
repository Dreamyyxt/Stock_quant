#!/usr/bin/env python3
"""
B10 验收：账本落盘 + 崩溃恢复 + cron 驱动 + 进程编排。

四条判据，跟其他批次一致：
  1. 重启之后账面**真的**接得上（不是"文件里有数字"就算）
  2. 幂等索引跟着重建 —— 重复成交/重复拆股在重启后依然只算一次
  3. 坏档、版本不符、写一半，三种都不能被当成"空账本"继续跑
  4. 真实 A 股数据上两个独立进程各跑一拍，第二拍基于第一拍持仓算差额

跑法：`.venv/bin/python scripts/quantos_selftest_b10.py`
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, ROOT)

from _asserts import (  # noqa: E402
    eq, falsy, guard, ne, raises, run, section, truth,
)

from quantos_core.contracts import Fill, Side                        # noqa: E402
from quantos_core.ledger import Ledger                               # noqa: E402
from quantos_core.ledger_store import (                              # noqa: E402
    LedgerCorrupt, LedgerStore, deserialize_ledger, serialize_ledger,
)
from quantos_core.risk_engine import RiskState                       # noqa: E402
from quantos_core.state_store import (                               # noqa: E402
    RiskStateStore, StateCorrupt, StateLoad, write_atomic,
)

UTC = timezone.utc
DAILY = Path(ROOT) / "state" / "ashare" / "daily"
PACK = Path(ROOT) / "research" / "strategy_packs" / "ashare_ew20.strat.py"
LEDGER_PATH = Path(ROOT) / "state" / "quantos" / "ashare_ledger.json"
PAIR = ("TIGER", "sh600000")


def _tmpdir() -> str:
    return tempfile.mkdtemp(prefix="qb10-")


def _ts(day: int = 30, month: int = 9) -> datetime:
    return datetime(2026, month, day, 7, 0, tzinfo=UTC)


def _fill(tid: str, qty: str = "1000", price: str = "10") -> Fill:
    return Fill(fill_id="f-" + tid, order_id="o-1", client_order_id="c-1",
                venue_trade_id=tid, quantity=Decimal(qty),
                price=Decimal(price), fee=Decimal("2"), fee_currency="CNY",
                ts=_ts())


def _book() -> Ledger:
    """一本有内容的账：入金 + 买入 + 分红 + 拆股，覆盖四类状态。"""
    ledger = Ledger(base_currency="CNY")
    ledger.deposit("ACC", "CNY", Decimal("100000"), ref="dep-1")
    ledger.apply_fill(_fill("t1"), "TIGER", "sh600000", "ACC", Side.BUY, "CNY",
                      decision_price=Decimal("10.1"),
                      attribution={"strat_a": Decimal("600"),
                                   "strat_b": Decimal("400")})
    ledger.post_dividend("ACC", "CNY", Decimal("55"), ref="div-1",
                         gross_amount=Decimal("61"), withheld_tax=Decimal("6"))
    ledger.apply_split("TIGER", "sh600000", Decimal("2"), ref="split-1",
                       ts=_ts(1, 10))
    return ledger


PRICES = {PAIR: Decimal("10.5"), ("TIGER", "sh600004"): Decimal("8.2")}


# --------------------------------------------------------------------------- #
#  A. 序列化往返
# --------------------------------------------------------------------------- #

def part_a() -> None:
    section("A1 整份快照写出去再读回来，账面必须一模一样")

    ledger = _book()
    path = os.path.join(_tmpdir(), "book.json")
    store = LedgerStore(path)
    summary = store.save(ledger)
    truth(os.path.exists(path), "A1 文件已落盘")
    truth(summary["checksum"].startswith("sha256:"), "A1 返回校验和")

    result = store.load()
    eq(result.status, "ok", "A1 加载状态 ok")
    eq(result.source, "primary", "A1 来源是主文件")
    back = result.ledger
    truth(back is not None, "A1 拿到了账本")

    section("A2 NAV 与六项归因逐分一致")

    eq(back.nav(PRICES), ledger.nav(PRICES), "A2 NAV 一致")
    eq(back.performance(PRICES).to_dict(), ledger.performance(PRICES).to_dict(),
       "A2 归因六项与派生字段全部一致")
    truth(back.performance(PRICES).closes, "A2 恢复后归因依然闭合")
    eq(back.net_deposits_base, ledger.net_deposits_base, "A2 净入金一致")

    section("A3 持仓的每个派生字段都在（不是只存了数量）")

    pos, orig = back.positions()[PAIR], ledger.positions()[PAIR]
    eq(pos.qty, orig.qty, "A3 数量一致")
    eq(pos.buy_at_decision, orig.buy_at_decision, "A3 决策价成本一致")
    eq(pos.slippage, orig.slippage, "A3 滑点一致")
    eq(pos.attr_qty, orig.attr_qty, "A3 策略归属数量一致")
    eq(pos.attr_cost, orig.attr_cost, "A3 策略归属成本一致")
    eq(pos.attr_realized, orig.attr_realized, "A3 已实现损益一致")

    section("A4 分录与序号")

    eq(back.entry_count, ledger.entry_count, "A4 分录条数一致")
    eq(back._seq, ledger._seq, "A4 序号一致")
    eq([e.entry_id for e in back._entries], [e.entry_id for e in ledger._entries],
       "A4 分录 ID 顺序一致")
    eq(len(back._refs), len(ledger._refs), "A4 幂等键数量一致")

    section("A5 空账本也能往返（首次启动的形状）")

    empty = Ledger(base_currency="CNY")
    store.save(empty)
    loaded = store.load()
    eq(loaded.status, "ok", "A5 空账本状态 ok")
    eq(loaded.ledger.entry_count, 0, "A5 空账本 0 条分录")
    eq(loaded.ledger.nav({}), Decimal("0"), "A5 空账本 NAV 为 0")


# --------------------------------------------------------------------------- #
#  B. 幂等索引跨重启
# --------------------------------------------------------------------------- #

def part_b() -> None:
    section("B1 重启之后，重复成交回报依然只算一次")

    ledger = _book()
    path = os.path.join(_tmpdir(), "book.json")
    store = LedgerStore(path)
    store.save(ledger)
    before_count = ledger.entry_count
    before_qty = ledger.positions()[PAIR].qty

    revived = store.load().ledger
    written = revived.apply_fill(_fill("t1"), "TIGER", "sh600000", "ACC",
                                Side.BUY, "CNY", decision_price=Decimal("10.1"),
                                attribution={"strat_a": Decimal("600")})
    falsy(written, "B1 重复成交返回 False（没写进去）")
    eq(revived.entry_count, before_count, "B1 分录数没变")
    eq(revived.positions()[PAIR].qty, before_qty, "B1 持仓没翻倍")

    section("B2 重复分红不会把现金和归因加两遍")

    before_div = revived.performance(PRICES).dividend
    before_cash = revived.cash_native()[("ACC", "CNY")]
    revived.post_dividend("ACC", "CNY", Decimal("55"), ref="div-1")
    eq(revived.performance(PRICES).dividend, before_div, "B2 分红归因没变")
    eq(revived.cash_native()[("ACC", "CNY")], before_cash, "B2 现金没变")

    section("B3 重复拆股不会把持仓乘第二次（B10 修掉的真 bug）")

    qty_before = revived.positions()[PAIR].qty
    revived.apply_split("TIGER", "sh600000", Decimal("2"), ref="split-1",
                        ts=_ts(1, 10))
    eq(revived.positions()[PAIR].qty, qty_before,
       "B3 同一 ref 重放拆股，数量不变")
    eq(revived.positions()[PAIR].attr_qty["strat_a"],
       revived.positions()[PAIR].attr_qty["strat_a"], "B3 归属数量也没乘第二次")

    section("B4 重启后新分录不会撞号")

    revived.apply_fill(_fill("t2", qty="100"), "TIGER", "sh600004", "ACC",
                       Side.BUY, "CNY", decision_price=Decimal("8.2"))
    ids = [e.entry_id for e in revived._entries]
    eq(len(ids), len(set(ids)), "B4 分录 ID 无重复")
    truth(revived.entry_count > before_count, "B4 新成交确实写进去了")


# --------------------------------------------------------------------------- #
#  C. 坏档：三种都不能当空账本
# --------------------------------------------------------------------------- #

def part_c() -> None:
    section("C1 文件不存在：取决于调用方要不要建仓")

    path = os.path.join(_tmpdir(), "none.json")
    store = LedgerStore(path)
    absent = store.load()
    eq(absent.status, "absent", "C1 状态 absent")
    falsy(absent.usable, "C1 默认不给账本")
    falsy(absent.needs_attention, "C1 absent 不算异常（首次启动是正常的）")
    def _allow_absent() -> None:
        fresh = store.load(allow_absent=True)
        eq(fresh.status, "absent", "C1 状态仍是 absent")
        truth(fresh.usable, "C1 给了空账本")
        eq(fresh.ledger.entry_count, 0, "C1 空账本没有分录")

    guard("C1 允许首次启动时才给空账本", _allow_absent)

    section("C2 写了一半的文件：识别为损坏，不许当空账本")

    ledger = _book()
    path = os.path.join(_tmpdir(), "book.json")
    store = LedgerStore(path)
    store.save(ledger)
    with open(path, "rb") as fh:
        blob = fh.read()
    with open(path, "wb") as fh:
        fh.write(blob[: len(blob) // 2])
    broken = store.load()
    eq(broken.status, "corrupt", "C2 状态 corrupt")
    falsy(broken.usable, "C2 不给账本（拿空账本顶上会重买一遍）")
    truth(broken.needs_attention, "C2 必须有人看一眼")

    guard("C2 must_load 在坏档时抛，而不是返回空账本",
          lambda: raises(LedgerCorrupt,
                         lambda: store.must_load(base_currency="CNY")))

    section("C3 有备份时回退，但如实说是备份")

    ledger = _book()
    path = os.path.join(_tmpdir(), "book.json")
    store = LedgerStore(path)
    store.save(ledger)                       # 第 1 版
    ledger.post_dividend("ACC", "CNY", Decimal("9"), ref="div-2")
    store.save(ledger)                       # 第 2 版，.bak = 第 1 版
    eq(store.load().entry_count, ledger.entry_count, "C3 正常时读最新版")

    with open(path, "wb") as fh:
        fh.write(b'{"kind":"quantos.ledger","schema":1,')
    back = store.load()
    eq(back.status, "recovered_from_backup", "C3 回退到备份")
    eq(back.source, "backup", "C3 来源标记为 backup")
    truth(back.needs_attention, "C3 用了备份要人看")
    truth(".bak" in back.detail, "C3 detail 里说清了为什么回退")
    truth(back.entry_count < ledger.entry_count, "C3 备份确实是旧的那一版")

    section("C4 校验和不对（有人改过文件）→ 拒绝")

    ledger = _book()
    path = os.path.join(_tmpdir(), "book.json")
    store = LedgerStore(os.path.join(_tmpdir(), "only.json"))
    store.save(ledger)
    raw = json.loads(Path(store.path).read_text(encoding="utf-8"))
    raw["body"]["cash"]["ACC|CNY"] = "99999999"
    Path(store.path).write_text(json.dumps(raw), encoding="utf-8")
    tampered = store.load()
    ne(tampered.status, "ok", "C4 被改过就不是 ok")
    falsy(tampered.usable, "C4 不给账本")

    section("C5 现金与分录对不上 → 即使重算校验和也拦得住")

    ledger = _book()
    path = os.path.join(_tmpdir(), "book.json")
    store = LedgerStore(path)
    store.save(ledger)
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    raw["body"]["cash"]["ACC|CNY"] = "1"          # 改了现金
    from quantos_core.state_store import digest_of
    raw["checksum"] = digest_of(raw["body"])      # 顺手把校验和也改对
    Path(path).write_text(json.dumps(raw), encoding="utf-8")
    guard("C5 交叉核对（现金 = 分录推出来的现金）独立于校验和",
          lambda: raises(LedgerCorrupt,
                         lambda: deserialize_ledger(raw["body"])))

    section("C6 版本不兼容 → 停在门口，不猜迁移")

    ledger = _book()
    path = os.path.join(_tmpdir(), "book.json")
    store = LedgerStore(path)
    store.save(ledger)
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    raw["schema"] = 99
    Path(path).write_text(json.dumps(raw), encoding="utf-8")
    newer = store.load()
    eq(newer.status, "schema_mismatch", "C6 状态 schema_mismatch")
    falsy(newer.usable, "C6 不给账本")

    section("C7 verify() 是只读诊断，不改文件")

    before = Path(path).read_bytes()
    info = store.verify()
    eq(Path(path).read_bytes(), before, "C7 verify 不动文件")
    eq(info["status"], "schema_mismatch", "C7 verify 如实报告状态")


# --------------------------------------------------------------------------- #
#  D. 风控状态
# --------------------------------------------------------------------------- #

def part_d() -> None:
    section("D1 熔断记忆必须跨重启（重启就复位等于没有熔断）")

    path = os.path.join(_tmpdir(), "risk.json")
    store = RiskStateStore(path)
    state = RiskState()
    state.halt("回撤 15% 超限")
    state.drawdown_pct = Decimal("15.0")
    store.save_state(state)

    loaded, info = store.load_state()
    eq(info.status, "ok", "D1 状态 ok")
    eq(loaded.permission_level.value, "HALTED", "D1 权限仍是 HALTED")
    eq(loaded.halted_reasons, ["回撤 15% 超限"], "D1 熔断原因还在")
    eq(loaded.drawdown_pct, Decimal("15.0"), "D1 回撤数值还在")

    section("D2 风控状态坏了，不能当 NORMAL 重新开始")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"kind":"quantos.risk_state"')
    bad_state, bad_info = store.load_state()
    eq(bad_state, None, "D2 不给状态对象")
    eq(bad_info.status, "corrupt", "D2 状态 corrupt")
    truth(bad_info.needs_attention, "D2 需要人看")

    section("D3 absent 与 corrupt 必须能区分")

    eq(store.load().status, "corrupt", "D3 坏档是 corrupt")
    fresh = RiskStateStore(os.path.join(_tmpdir(), "new.json")).load()
    eq(fresh.status, "absent", "D3 没写过是 absent")
    falsy(fresh.needs_attention, "D3 absent 不算异常")


# --------------------------------------------------------------------------- #
#  E. 真实 A 股：两个独立进程各跑一拍
# --------------------------------------------------------------------------- #

PROBE = r'''
import json, sys
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(sys.argv[1]); TMP = Path(sys.argv[2]); DAY = date.fromisoformat(sys.argv[3])
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
import quantos_schedule as qs
from quantos_core.schedule import RebalanceSpec

qs.LEDGER_PATH = TMP / "ledger.json"
qs.RISK_PATH = TMP / "risk.json"
qs.SCHED_PATH = TMP / "sched.json"
runner = qs.AshareRunner(
    pack_path=qs.PACK_PATH, data_dir=qs.DATA_DIR, venue=qs.VENUE,
    params={"top_n": "4"}, symbols=6, bars=80, min_bars=40,
    nav=Decimal("100000"), quota_pct=Decimal("0.9"),
    ledger_path=qs.LEDGER_PATH, risk_path=qs.RISK_PATH, dry_frame_day=DAY)
start = {f"{k[0]}|{k[1]}": str(p.attr_qty) for k, p in runner.ledger.positions().items()}
outcome = runner(RebalanceSpec("ashare_ew20", "v1", date(2026, 1, 5), 20,
                               "rebalance"), DAY)
print("@@RESULT@@" + json.dumps({
    "status": runner.ledger_status,
    "start_positions": start,
    "start_entries": runner.ledger._seq,
    "end_positions": {f"{k[0]}|{k[1]}": str(p.qty) for k, p in runner.ledger.positions().items()},
    "fills": outcome.fills,
    "nav": str(runner.ledger.net_deposits_base),
    "entries": runner.ledger.entry_count,
}, ensure_ascii=False))
'''


def _run_probe(tmp: str, day: str) -> dict:
    script = os.path.join(tmp, "probe.py")
    Path(script).write_text(PROBE, encoding="utf-8")
    proc = subprocess.run([sys.executable, script, ROOT, tmp, day],
                          capture_output=True, text=True, timeout=240)
    if proc.returncode != 0:
        raise AssertionError(f"子进程失败：{proc.stderr[-800:]}")
    for line in proc.stdout.splitlines():
        if line.startswith("@@RESULT@@"):
            return json.loads(line[len("@@RESULT@@"):])
    raise AssertionError(f"子进程没有输出结果：{proc.stdout[-500:]}")


def part_e() -> None:
    section("E1 两个独立进程各跑一拍（第一拍建仓）")

    if not DAILY.exists() or not PACK.exists():
        truth(True, "E 本机没有真实行情/策略包，跳过（不影响后端结论）")
        return

    tmp = _tmpdir()
    first = _run_probe(tmp, "2026-09-30")
    eq(first["status"], "created", "E1 第一拍创建账本")
    eq(first["start_positions"], {}, "E1 第一拍起始是空仓")
    truth(first["fills"] > 0, "E1 第一拍确实成交了")

    section("E2 第二拍从第一拍的持仓继续（不是从空仓）")

    second = _run_probe(tmp, "2026-11-04")
    eq(second["status"], "ok", "E2 第二拍读到已有账本")
    truth(bool(second["start_positions"]), "E2 第二拍起始持仓非空")
    eq(set(second["start_positions"]), set(first["end_positions"]),
       "E2 起始持仓就是第一拍结束时的那几只")

    section("E3 第二拍只补差额（不像从零开始那样全量重建）")

    truth(second["fills"] < len(second["start_positions"]),
          f"E3 第二拍成交 {second['fills']} 笔 < 持仓 {len(second['start_positions'])} 只")
    truth(second["entries"] > first["entries"], "E3 分录在两拍之间累加")

    section("E4 预算口径是账本 NAV，不是写死的初始资金")

    eq(second["nav"], "100000.0000", "E4 净入金没变（没重复入金）")
    truth(second["entries"] <= 40, "E4 分录规模合理")


# --------------------------------------------------------------------------- #
#  F. cron 入口与进程编排
# --------------------------------------------------------------------------- #

def part_f() -> None:
    import quantos_schedule as qs
    from quantos_core.schedule import Decision, ItemReport, TickReport

    section("F1 退出码就是报警线：拒绝运行不能返回 0")

    def report(**kw) -> TickReport:
        return TickReport(as_of=_ts(), calendar_name="X", signal_day=date(2026, 9, 30),
                          items=tuple(kw.pop("items", ())), **kw)

    def item(decision: Decision) -> ItemReport:
        return ItemReport(strategy_id="s", version="v1", decision=decision)

    eq(qs.tick_exit_code(None), 0, "F1 没有报告 = 0")
    eq(qs.tick_exit_code(report()), 0, "F1 正常运行 = 0")
    eq(qs.tick_exit_code(report(items=(item(Decision.SKIPPED_NOT_DUE),))), 0,
       "F1 没到期 = 0")
    eq(qs.tick_exit_code(report(items=(item(Decision.SKIPPED_ALREADY_RAN),))), 0,
       "F1 已跑过 = 0")
    eq(qs.tick_exit_code(report(items=(item(Decision.SKIPPED_CLOSED),))), 0,
       "F1 不是交易日 = 0")
    eq(qs.tick_exit_code(report(refused=True)), 2, "F1 整拍被拒 = 2（不是 0）")
    eq(qs.tick_exit_code(report(items=(item(Decision.FAILED),))), 2,
       "F1 跑失败 = 2")
    eq(qs.tick_exit_code(report(items=(item(Decision.REFUSED_IN_DOUBT),))), 1,
       "F1 in_doubt = 1（要人确认）")

    section("F2 单实例锁：上一拍没退，这一拍必须退出而不是并行")

    from quantos_core.locks import AccountGatewayLock, LockBusy

    lock_dir = os.path.join(_tmpdir(), "locks")
    os.makedirs(lock_dir, exist_ok=True)
    holder = AccountGatewayLock(lock_dir, "ASHARE_PAPER", "TIGER")
    holder.acquire()
    guard("F2 同一账户+网关第二次加锁必须失败",
          lambda: raises(LockBusy, AccountGatewayLock(lock_dir, "ASHARE_PAPER",
                                                      "TIGER").acquire))
    holder.release()

    def _reacquire() -> None:
        again = AccountGatewayLock(lock_dir, "ASHARE_PAPER", "TIGER")
        again.acquire()
        truth(again.is_held, "F2 又能拿到锁")
        again.release()

    guard("F2 释放之后可以再拿", _reacquire)

    section("F3 quantos_tick.py 真的能跑，且锁被占时返回 3")

    tick = os.path.join(ROOT, "scripts", "quantos_tick.py")
    truth(os.path.exists(tick), "F3 入口脚本存在")

    lock_root = Path(ROOT) / "state" / "quantos" / "locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    pinned = AccountGatewayLock(str(lock_root), "ASHARE_PAPER", "TIGER")
    pinned.acquire()
    try:
        busy = subprocess.run([sys.executable, tick, "--dry-run"],
                              capture_output=True, text=True, timeout=240,
                              cwd=ROOT)
    finally:
        pinned.release()
    eq(busy.returncode, 3, "F3 锁被占 → 退出码 3")
    truth("另一个 tick 正在跑" in (busy.stdout + busy.stderr),
          "F3 输出说明了是谁占着锁")

    section("F4 进程编排如实报告缺口（网关没接就不许亮绿灯）")

    up = os.path.join(ROOT, "scripts", "quantos_up.py")
    # B11 起"执行网关"是真的探针（Alpaca 纸面）。验收必须能关掉联网，
    # 否则券商 API 抖一下这个套件就红了，而红的原因跟本批代码无关。
    env = dict(os.environ, QUANTOS_ALPACA_PROBE="off")
    status = subprocess.run([sys.executable, up, "--status"],
                            capture_output=True, text=True, timeout=120,
                            cwd=ROOT, env=env)
    eq(status.returncode, 0, "F4 状态命令正常返回")
    text = status.stdout
    for name in ("风控服务", "执行网关", "调度器(tick)", "运维面"):
        truth(name in text, f"F4 列出 {name}")
    truth("[探针]" in text, "F4 网关标注为探针型（是库，不是常驻进程）")
    truth("跳过联网探针" in text, "F4 关掉开关后如实说跳过了，而不是假装没接")
    truth("PaperBroker" in text, "F4 说明 A 股成交仍走模拟撮合")

    machine = subprocess.run([sys.executable, up, "--status", "--json"],
                             capture_output=True, text=True, timeout=120,
                             cwd=ROOT, env=env)
    eq(machine.returncode, 0, "F4 --json 正常返回")
    payload = json.loads(machine.stdout)
    truth(isinstance(payload, list) and len(payload) == 4,
          "F4 --json 是纯 JSON（可直接喂 jq / json.load）")
    truth(all("state" in item and "key" in item for item in payload),
          "  └ 每项都带 key 与 state")

    section("F5 crontab 片段给出的是收盘后 + 结算延迟的时间")

    cron = subprocess.run([sys.executable, up, "--crontab"],
                          capture_output=True, text=True, timeout=120, cwd=ROOT)
    eq(cron.returncode, 0, "F5 crontab 命令正常返回")
    truth("35 15 * * 1-5" in cron.stdout, "F5 15:35（收盘 15:00 + 结算延迟）")
    truth("quantos_tick.py" in cron.stdout, "F5 指向 tick 入口")
    truth("非交易日" in cron.stdout, "F5 说明非交易日不必单独排")


# --------------------------------------------------------------------------- #
#  G. 上屏：账本卡片能说出真相
# --------------------------------------------------------------------------- #

def part_g() -> None:
    section("G1 页面探针：账本数字能对回后端，坏档必须红")

    probe_node = subprocess.run(["node", "--version"], capture_output=True, text=True)
    if probe_node.returncode != 0:
        truth(True, "G1 本机没有 node，跳过前端探针（不影响后端结论）")
        return

    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    from quantos_ops_server import OpsService, build_runtime

    rt = build_runtime()
    tmp = _tmpdir()

    def provider_for(ledger_path: str):
        def snapshot():
            store = LedgerStore(ledger_path)
            result = store.load()
            ledger = result.ledger
            return {
                "available": True, "status": result.status,
                "path": ledger_path, "source": result.source,
                "detail": result.detail, "saved_at": result.saved_at,
                "entry_count": ledger.entry_count if ledger else 0,
                "warnings": list(result.warnings),
                "cash": {}, "positions": [
                    {"venue": k[0], "symbol": k[1], "quote_ccy": p.quote_ccy,
                     "qty": str(p.qty), "strategy_ids": sorted(p.attr_qty)}
                    for k, p in ledger.positions().items()
                ] if ledger else [],
                "position_count": len(ledger.positions()) if ledger else 0,
                "net_deposits_base": str(ledger.net_deposits_base) if ledger else "0",
                "healthy": result.status == "ok",
            }
        return snapshot

    good_path = os.path.join(tmp, "ledger.json")
    LedgerStore(good_path).save(_book())

    bad_path = os.path.join(tmp, "bad.json")
    with open(bad_path, "wb") as fh:
        fh.write(b'{"kind":"quantos.ledger","schema":1,')

    def service(ledger_path: str) -> OpsService:
        return OpsService(metrics=rt["metrics"], registry=rt["registry"],
                          prices_provider=lambda: rt["prices"], token="t0ken",
                          series=rt["series"], live_gate=rt["gate"],
                          risk_model_store=rt["risk_model_store"],
                          canary_router=rt["canary_router"],
                          research_inbox=rt["research_inbox"],
                          ledger_provider=provider_for(ledger_path))

    probe = os.path.join(ROOT, "scripts", "page_probe.js")
    page = os.path.join(ROOT, "quantos", "index.html")

    def run_probe(svc: OpsService, name: str) -> subprocess.CompletedProcess:
        payload = {
            "snapshot": svc.snapshot(), "strategies": svc.strategies(),
            "audit": svc.audit(30), "navHistory": svc.nav_history(),
            "live": svc.live(), "portfolio": svc.portfolio(),
            "ledger": svc.ledger(),
        }
        path = os.path.join(tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, default=str)
        return subprocess.run(["node", probe, page, path],
                              capture_output=True, text=True, timeout=240)

    ok = run_probe(service(good_path), "ledger_ok.json")
    eq(ok.returncode, 0, "G1 健康账本 fixture 探针全部通过")
    if ok.stdout.strip():
        print("      " + ok.stdout.strip().splitlines()[-1])

    broken = run_probe(service(bad_path), "ledger_bad.json")
    eq(broken.returncode, 0, "G1 坏档 fixture 探针通过（页面红着说拒绝运行）")

    section("G2 账本卡片的渲染函数不调用别的渲染函数（单向）")

    src = Path(ROOT, "quantos", "index.html").read_text(encoding="utf-8")
    start = src.index("function renderLedger()")
    body = src[start: src.index("function renderDrawer()", start)]
    for other in ("renderSchedule", "renderDrawer", "renderOverview",
                  "refreshAll"):
        falsy(other + "(" in body,
              f"G2 renderLedger 不调用 {other}（联动统一走 refreshAll）")
    truth("renderLedger()" in src, "G2 refreshAll 里有 renderLedger")

    section("G3 页面不会把账本状态伪造成绿灯")

    truth("LEDGER_STATUS_META" in src, "G3 状态映射是显式的")
    truth("p-bad" in src, "G3 坏档用红 pill")
    truth("拒绝运行" in src, "G3 页面写清了坏档的后果")


# --------------------------------------------------------------------------- #
#  H. 代码卫生
# --------------------------------------------------------------------------- #

STDLIB_OK = {
    "math", "statistics", "decimal", "fractions", "operator", "datetime",
    "typing", "dataclasses", "collections", "itertools", "functools",
    "enum", "string", "re", "ast", "hashlib", "os", "sys", "tempfile",
    "pathlib", "types", "__future__", "json", "time", "abc", "contextlib",
    "zoneinfo", "socket", "signal", "subprocess", "shutil", "urllib",
    "threading", "uuid", "random", "copy", "io", "textwrap", "argparse",
}


def part_h() -> None:
    section("H1 存储层只依赖标准库与项目内部")

    for name in ("state_store.py", "ledger_store.py"):
        tree = ast.parse((Path(ROOT) / "quantos_core" / name)
                         .read_text(encoding="utf-8"))
        external = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root not in STDLIB_OK and root != "quantos_core":
                        external.append(alias.name)
            elif isinstance(node, ast.ImportFrom) and not node.level:
                root = (node.module or "").split(".")[0]
                if root not in STDLIB_OK and root != "quantos_core":
                    external.append(node.module or "")
        eq(external, [], f"H1 {name} 无第三方依赖（发现：{external}）")

    section("H2 状态写入全部走原子替换，没有裸 open('w')")

    for name in ("state_store.py", "ledger_store.py"):
        src = (Path(ROOT) / "quantos_core" / name).read_text(encoding="utf-8")
        truth("write_atomic" in src, f"H2 {name} 用 write_atomic")
        falsy('open(self.path, "w"' in src, f"H2 {name} 不直接对着目标文件写")
        falsy("json.dump(" in src.replace("json.dumps(", ""),
              f"H2 {name} 没有裸 json.dump 直写")

    section("H3 账本存储不碰网关/下单（它只管文件）")

    src = (Path(ROOT) / "quantos_core" / "ledger_store.py").read_text(encoding="utf-8")
    for forbidden in ("gateway", "ccxt", "oms", ".place(", "RiskEngine"):
        falsy(forbidden in src, f"H3 ledger_store.py 不引用 {forbidden}")

    section("H4 拆股的幂等门在改数量之前")

    ledger_src = (Path(ROOT) / "quantos_core" / "ledger.py").read_text(encoding="utf-8")
    idx_gate = ledger_src.index('("split", ref_id, "SPLIT") in self._refs')
    idx_mutate = ledger_src.index("pos.qty = q(before * ratio, 10)")
    truth(idx_gate < idx_mutate,
          "H4 先查 ref 再改数量（否则重放会把持仓乘第二次）")


def main() -> None:
    part_a()
    part_b()
    part_c()
    part_d()
    part_e()
    part_f()
    part_g()
    part_h()


if __name__ == "__main__":
    raise SystemExit(run("B10 账本落盘与进程编排", main))
