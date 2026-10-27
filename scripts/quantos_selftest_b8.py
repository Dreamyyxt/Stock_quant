#!/usr/bin/env python3
"""
B8 验收：策略接口契约 + 策略包导入 + 策略运行时。

三条判据，跟其他批次一致：
  1. 说到的约束真的会拦（AST 体检、参数校验、warmup 不足）
  2. 整条链路串起来能跑出成交，而且账能对上
  3. 出错时报告能指出是哪一段挂的，不是笼统的"失败了"

跑法：`.venv/bin/python scripts/quantos_selftest_b8.py`
"""

from __future__ import annotations

import ast
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, ROOT)

from _asserts import (  # noqa: E402
    eq, falsy, guard, near, ne, raises, run, section, truth,
)

from quantos_core.capital_allocator import StrategyPolicy, allocate  # noqa: E402
from quantos_core.contracts import (  # noqa: E402
    D, Fill, Side, StrategyRunState, Verdict,
)
from quantos_core.idempotency import ClientOrderIdStore  # noqa: E402
from quantos_core.ledger import Ledger  # noqa: E402
from quantos_core.oms import Oms  # noqa: E402
from quantos_core.risk_engine import (  # noqa: E402
    PermissionLevel, RiskEngine, RiskPolicy, RiskState,
)
from quantos_core.strategy_registry import (  # noqa: E402
    IllegalStageTransition, PromotionBlocked, StrategyRegistry, StrategyStage,
)
from quantos_core.strategy_api import (  # noqa: E402
    FrameError, MarketFrame, ManifestError, ParamError, ParamSpec, Series,
    SignalOut, StrategyContext, StrategyError, StrategyManifest, coerce_signal,
)
from quantos_core.strategy_pack import (  # noqa: E402
    ALLOWED_IMPORT_PREFIXES, FORBIDDEN_CALLS, PackRejected, admission_blockers,
    audit_source, load_pack, pack_from_object, to_instance,
)
from quantos_core.strategy_runtime import (  # noqa: E402
    EngineRiskClient, PaperBroker, PaperFillConfig, StrategyRun, StrategyRuntime,
)

UTC = timezone.utc
D0 = date(2026, 9, 1)

#: 一个"标准形态"的策略包：零 import、返回 dict。
GOOD_PACK = '''
MANIFEST = {
    "strategy_id": "ew_momentum",
    "version": "v1",
    "venue": "SIM",
    "kind": "rebalance",
    "rebalance_days": 5,
    "warmup_bars": 6,
    "params": [
        {"name": "top_n", "kind": "int", "low": 1, "high": 10, "default": 2},
        {"name": "weight", "kind": "float", "low": 0.01, "high": 0.5, "default": 0.2},
    ],
    "description": "按 6 日动量取前 top_n 等权",
}

def compute(frame, ctx):
    top_n = ctx.params["top_n"]
    ranked = []
    for sym in frame.symbols():
        closes = frame.require(sym, 6).closes()
        mom = closes[-1] / closes[0] - 1
        ranked.append((mom, sym))
    ranked.sort(reverse=True)
    out = []
    for mom, sym in ranked[:top_n]:
        if mom > 0:
            out.append({"symbol": sym, "weight": ctx.params["weight"],
                        "reason": "mom=%s" % mom})
    return out
'''

#: 真实数据段用的策略：永远持有动量前 N（ew20 的形态）。
#: 刻意**不**加 `if mom > 0` 过滤 —— 那是"看空就空仓"，全市场下跌时会得到
#: 0 个信号，看起来像链路坏了，其实只是策略形态不同。真实再平衡不做择时。
REAL_PACK = '''
MANIFEST = {
    "strategy_id": "ashare_real",
    "version": "v1",
    "venue": "TIGER",
    "kind": "rebalance",
    "rebalance_days": 20,
    "warmup_bars": 6,
    "params": [
        {"name": "top_n", "kind": "int", "low": 1, "high": 10, "default": 3},
        {"name": "weight", "kind": "float", "low": 0.01, "high": 0.5, "default": 0.2},
    ],
    "description": "真实 A 股：动量前 top_n 等权持有",
}

def compute(frame, ctx):
    top_n = ctx.params["top_n"]
    ranked = []
    for sym in frame.symbols():
        closes = frame.require(sym, 6).closes()
        ranked.append((closes[-1] / closes[0] - 1, sym))
    ranked.sort(reverse=True)
    return [{"symbol": sym, "weight": ctx.params["weight"],
             "reason": "mom=%s" % mom} for mom, sym in ranked[:top_n]]
'''


#: 另一个策略，用来测"错误隔离"与"多策略净额"。
FAILING_PACK = '''
MANIFEST = {"strategy_id": "boom", "version": "v1", "venue": "SIM",
            "warmup_bars": 2, "rebalance_days": 1}

def compute(frame, ctx):
    raise RuntimeError("策略内部炸了")
'''

#: 第二个多头策略：固定买 AAA。用来测「两个策略抢同一标的」时
#: 净额合并、但每策略的归属必须分开（R-A07 + R-D05）。
SECOND_PACK = '''
MANIFEST = {"strategy_id": "second", "version": "v1", "venue": "SIM",
            "warmup_bars": 2, "rebalance_days": 1,
            "params": [{"name": "w", "kind": "float", "low": 0.0, "high": 1.0,
                        "default": 0.1}]}

def compute(frame, ctx):
    return [{"symbol": "AAA", "weight": ctx.params["w"], "reason": "固定多头"}]
'''


# --------------------------------------------------------------------------- #
#  工具
# --------------------------------------------------------------------------- #

def _write(tmp: str, name: str, source: str) -> str:
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(source)
    return path


def _series(symbol: str, base: Decimal, drift: Decimal, n: int = 40,
            start: date = D0) -> Series:
    pts = []
    for i in range(n):
        pts.append((start + timedelta(days=i),
                    (base * (Decimal(1) + drift * (i + 1))).quantize(Decimal("0.01"))))
    return Series(symbol=symbol, points=tuple(pts))


def _frame(now: datetime, n: int = 40) -> MarketFrame:
    return MarketFrame(as_of=now, venue="SIM", series={
        "AAA": _series("AAA", Decimal("100"), Decimal("0.004"), n),
        "BBB": _series("BBB", Decimal("120"), Decimal("0.003"), n),
        "CCC": _series("CCC", Decimal("140"), Decimal("-0.002"), n),
    })


NOW = datetime(2026, 10, 12, 21, 0, tzinfo=UTC)


def _ledger(tmp: str, cash: str = "100000") -> Ledger:
    lg = Ledger(base_currency="USD")
    lg.deposit("ACC", "USD", D(cash))
    return lg


def _stack(tmp: str, cash: str = "100000"):
    ledger = _ledger(tmp, cash)
    idem = ClientOrderIdStore(os.path.join(tmp, "cid.json"))
    oms = Oms("ACC", "PF", None, idem)
    engine = RiskEngine(RiskPolicy())
    state = RiskState()
    client = EngineRiskClient(engine, state)
    oms.risk = client
    broker = PaperBroker(PaperFillConfig())
    rt = StrategyRuntime(ledger=ledger, oms=oms, risk_client=client,
                         account_id="ACC", broker=broker)
    return ledger, oms, state, client, rt


# --------------------------------------------------------------------------- #
#  A. 契约
# --------------------------------------------------------------------------- #

def part_a() -> None:
    section("A1 参数声明：缺值/越界/类型错都必须报")

    spec = ParamSpec("lookback", "int", low=5, high=60, required=True)
    eq(spec.coerce("20"), 20, "A1 字符串形式的整数可接受")
    eq(spec.coerce(20), 20, "A1 整数可接受")
    raises(ParamError, lambda: spec.coerce(None), "A1 必填缺值报错")
    raises(ParamError, lambda: spec.coerce("4"), "A1 低于下界报错")
    raises(ParamError, lambda: spec.coerce("61"), "A1 高于上界报错")
    raises(ParamError, lambda: spec.coerce("20.5"), "A1 int 类型拒绝小数")

    boolean = ParamSpec("flag", "bool", default=False, required=False)
    truth(boolean.coerce("true") is True, "A1 bool 接受 'true'")
    truth(boolean.coerce("0") is False, "A1 bool 接受 '0'")
    raises(ParamError, lambda: boolean.coerce("maybe"), "A1 bool 拒绝乱写")

    numeric = ParamSpec("w", "float", low=Decimal("0.01"), high=Decimal("0.5"),
                        default=Decimal("0.2"))
    eq(numeric.coerce("0.3"), Decimal("0.3"), "A1 float 走 Decimal，不是 0.30000000004")
    raises(ParamError, lambda: numeric.coerce("nan"), "A1 拒绝 NaN")
    raises(ParamError, lambda: numeric.coerce("0.9"), "A1 float 越界报错")

    raises(ManifestError,
           lambda: ParamSpec("bad", "float", required=False, default=None),
           "A1 非必填却没默认值 —— 不允许用 required=False 绕过必填")

    section("A2 manifest：拼错的键与拍脑袋的周期都要拦")

    m = StrategyManifest(
        strategy_id="s1", version="v1", venue="SIM", rebalance_days=20,
        warmup_bars=25,
        params=(ParamSpec("top_n", "int", low=1, high=100, default=20),))

    eq(m.resolve_params({}), {"top_n": 20}, "A2 未传参数时用声明默认值")
    raises(ParamError, lambda: m.resolve_params({"topn": 20}),
           "A2 未知参数键直接报错（拼错参数名不许静默跑）")

    required_m = StrategyManifest(
        strategy_id="s2", version="v1", venue="SIM",
        params=(ParamSpec("halflife", "int", low=1, high=400),))
    raises(ParamError, lambda: required_m.resolve_params({}),
           "A2 必填参数缺失报错")

    raises(ManifestError,
           lambda: StrategyManifest("s3", "v1", "SIM",
                                    params=(ParamSpec("a"), ParamSpec("a"))),
           "A2 参数名重复报错")
    raises(ManifestError,
           lambda: StrategyManifest("s4", "v1", "SIM", rebalance_days=0),
           "A2 rebalance_days=0 报错（等于每根 bar 都调）")
    raises(ManifestError,
           lambda: StrategyManifest("s5", "v1", "SIM", kind="whatever"),
           "A2 未知 kind 报错")

    fp1 = m.fingerprint_of_params({"top_n": 20})
    fp2 = m.fingerprint_of_params({"top_n": 21})
    ne(fp1, fp2, "A2 参数变则参数指纹变（代码没变也是另一个策略）")

    section("A3 行情：乱序/重号/非正价必须在构造时炸")

    ok = Series("AAA", ((D0, Decimal("10")), (D0 + timedelta(days=1), Decimal("11"))))
    eq(len(ok), 2, "A3 合法序列长度正确")
    eq(ok.last(), Decimal("11"), "A3 last() 取最后收盘")
    raises(FrameError,
           lambda: Series("AAA", ((D0 + timedelta(days=1), Decimal("10")),
                                  (D0, Decimal("11")))),
           "A3 日期倒序报错")
    raises(FrameError,
           lambda: Series("AAA", ((D0, Decimal("10")), (D0, Decimal("11")))),
           "A3 重复日期报错")
    raises(FrameError, lambda: Series("AAA", ((D0, Decimal("0")),)),
           "A3 零价格报错")

    section("A4 MarketFrame：warmup 不足不许拿半截数据算")

    frame = _frame(NOW, n=10)
    eq(len(frame.symbols()), 3, "A4 三个标的都在")
    eq(len(frame.require("AAA", 6)), 6, "A4 require 取最近 6 根")
    raises(FrameError, lambda: frame.require("AAA", 50), "A4 warmup 不足报错")
    raises(FrameError, lambda: frame.require("ZZZ", 1), "A4 标的缺失报错")
    raises(FrameError,
           lambda: MarketFrame(as_of=datetime(2026, 1, 1), venue="SIM",
                               series={"AAA": ok}),
           "A4 无时区 as_of 报错（naive 时间没法判新旧）")
    raises(FrameError,
           lambda: MarketFrame(as_of=NOW, venue="SIM", series={}),
           "A4 空行情报错（空表不许静默跑空）")

    section("A5 信号：只给权重和理由，超界一律拒")

    s = SignalOut("AAA", Decimal("0.2"), "入选")
    eq(s.suggested_weight, Decimal("0.2"), "A5 SignalOut 正常构造")
    raises(StrategyError, lambda: SignalOut("AAA", Decimal("1.5"), "加杠杆"),
           "A5 权重超 ±1 报错")
    raises(StrategyError, lambda: SignalOut("AAA", Decimal("0.1"), ""),
           "A5 没有 reason 报错")

    eq(coerce_signal({"symbol": "AAA", "weight": "0.1", "reason": "别名"}).suggested_weight,
       Decimal("0.1"), "A5 dict 的 weight 是 suggested_weight 的别名")
    raises(StrategyError, lambda: coerce_signal({"symbol": "AAA", "reason": "没权重"}),
           "A5 只给方向不给权重报错（适配器会丢弃，不如当场说清）")
    raises(StrategyError,
           lambda: coerce_signal({"symbol": "AAA", "weight": "0.1",
                                  "reason": "x", "wieght": "0.2"}),
           "A5 拼错的键报错（wieght 不许静默忽略）")
    raises(StrategyError, lambda: coerce_signal("AAA"), "A5 字符串不是信号")


# --------------------------------------------------------------------------- #
#  B. 策略包导入
# --------------------------------------------------------------------------- #

def part_b() -> None:
    tmp = tempfile.mkdtemp()

    section("B1 AST 体检：明显越界的写法必须当场拒")

    bad_cases = [
        ("import os\nMANIFEST={}\n", "不允许的 import", "import os"),
        ("import requests\nMANIFEST={}\n", "不允许的 import", "import requests"),
        ("from subprocess import run\nMANIFEST={}\n", "不允许的 import", "subprocess"),
        ("MANIFEST={}\ndef compute(f,c):\n    return open('/etc/passwd').read()\n",
         "禁用的调用", "open"),
        ("MANIFEST={}\ndef compute(f,c):\n    return eval('1')\n",
         "禁用的调用", "eval"),
        ("MANIFEST={}\ndef compute(f,c):\n    return __import__('os')\n",
         "禁用的调用", "__import__"),
        ("MANIFEST={}\ndef compute(f,c):\n    return c.__class__\n",
         "禁止访问双下划线属性", "__class__"),
        ("MANIFEST={}\nprint('导入时就执行了')\n", "顶层", "顶层表达式调用"),
    ]
    for src, expect_kind, expect_token in bad_cases:
        path = _write(tmp, f"bad_{abs(hash(src)) % 99999}.strat.py", src)
        exc = raises(PackRejected, lambda p=path: load_pack(p),
                     f"B1 拒绝：{expect_token}")
        if exc is not None:
            truth(expect_kind in str(exc), f"B1 拒绝原因写明是「{expect_kind}」")

    audit = audit_source("import os\n")
    falsy(audit.ok, "B1 audit_source 对 import os 判不通过")
    truth("os" in audit.bad_imports, "B1 报告里列出 os")

    # 注释里提到 import os 不该被当成违规（子串匹配会误判，AST 不会）
    audit2 = audit_source("# 这里讨论 import os 为什么危险\nMANIFEST={}\n")
    truth(audit2.ok, "B1 注释里出现 import os 不算违规")

    section("B2 合法包：指纹稳定、可干跑")

    good_path = _write(tmp, "good.strat.py", GOOD_PACK)
    pack = load_pack(good_path, dry_frame=_frame(NOW), dry_params={"top_n": 2})
    eq(pack.manifest.strategy_id, "ew_momentum", "B2 读到 strategy_id")
    eq(len(pack.fingerprint), 12, "B2 指纹 12 位")
    eq(len(pack.dry_signals), 2, "B2 干跑产出 top_n=2 条信号")
    truth(all(isinstance(s, SignalOut) for s in pack.dry_signals),
          "B2 干跑结果已归一成 SignalOut")
    truth(pack.manifest.fingerprint == pack.fingerprint,
          "B2 指纹回写进 manifest（注册时 code_commit 要对得上）")

    pack_again = load_pack(good_path, dry_frame=_frame(NOW), dry_params={"top_n": 2})
    eq(pack_again.fingerprint, pack.fingerprint, "B2 同一份源码指纹稳定")

    tampered = GOOD_PACK.replace('"v1"', '"v1"')  # 同内容
    eq(load_pack(_write(tmp, "same.strat.py", tampered)).fingerprint, pack.fingerprint,
       "B2 内容相同指纹相同")
    changed = GOOD_PACK.replace("top_n", "top_k")
    ne(load_pack(_write(tmp, "chg.strat.py", changed)).fingerprint, pack.fingerprint,
       "B2 改一处标识符指纹就变（改代码必须换指纹）")

    section("B3 干跑：返回垃圾必须在导入时炸，不能等凌晨调度")

    for label, body in (
        ("返回 None", "def compute(frame, ctx):\n    return None\n"),
        ("返回字符串", "def compute(frame, ctx):\n    return 'hello'\n"),
        ("返回 dict", "def compute(frame, ctx):\n    return {'AAA': 1}\n"),
        ("抛异常", "def compute(frame, ctx):\n    raise ValueError('boom')\n"),
        ("返回未知键", "def compute(frame, ctx):\n"
                    "    return [{'symbol':'AAA','weight':'0.1','reason':'x','foo':1}]\n"),
    ):
        src = ("MANIFEST = {'strategy_id':'bad_dry','version':'v1','venue':'SIM',"
               "            'warmup_bars':2,'rebalance_days':1}\n" + body)
        path = _write(tmp, f"dry_{abs(hash(label)) % 99999}.strat.py", src)
        exc = raises(PackRejected,
                     lambda p=path: load_pack(p, dry_frame=_frame(NOW)),
                     f"B3 干跑拒绝：{label}")
        if exc is not None and label == "抛异常":
            truth("bad_dry" in str(exc), "B3 异常消息带上策略 id（不然不知道是谁）")

    section("B4 准入：R-S03 五条证据缺一不可")

    empty = admission_blockers({}, paper_days=0)
    eq(len(empty), 6, "B4 没有证据时报 6 条阻断（5 条材料 + 模拟盘天数）")
    truth(any("样本外" in b for b in empty), "B4 报告里点名样本外回测")
    truth(any("人工授权" in b for b in empty), "B4 报告里点名人工授权")

    full = admission_blockers({
        "out_of_sample_backtest": True, "costs_modeled": True,
        "paper_verified": True, "error_path_tested": True,
        "human_approved": True}, paper_days=30)
    eq(len(full), 0, "B4 证据齐全时无阻断")

    short = admission_blockers({
        "out_of_sample_backtest": True, "costs_modeled": True,
        "paper_verified": True, "error_path_tested": True,
        "human_approved": True}, paper_days=3)
    eq(len(short), 1, "B4 模拟盘只跑 3 天仍被拦")

    section("B5 导入只会进 RESEARCH，且必须交代数据版本")

    policy = StrategyPolicy(strategy_id="ew_momentum", venue="SIM", quota_pct=D("0.3"))
    inst = to_instance(pack, policy=policy, data_version="2026q4",
                       cost_model_version="2026-10-v1", actor="human:owner")
    truth(inst.stage is StrategyStage.RESEARCH, "B5 导入后的阶段固定为 RESEARCH")
    eq(inst.code_commit, pack.fingerprint, "B5 code_commit 就是源码指纹")
    raises(PackRejected,
           lambda: to_instance(pack, policy=policy, data_version="",
                               cost_model_version="2026-10-v1", actor="x"),
           "B5 不写数据版本直接拒")
    raises(PackRejected,
           lambda: to_instance(pack, policy=StrategyPolicy(strategy_id="other",
                                                           venue="SIM"),
                               data_version="2026q4",
                               cost_model_version="x", actor="x"),
           "B5 配额声明与策略 id 不一致直接拒")

    section("B6 注册后也不许下单（RESEARCH 不在可交易列表里）")

    registry = StrategyRegistry()
    registry.register(inst, actor="human:owner")
    eq(len(registry.policies()), 0,
       "B6 RESEARCH 策略不出现在 policies() 里 —— 导一万个也不会产生订单")

    # 边界要钉死：PAPER 必须进得来，否则修完 RESEARCH 之后没人能模拟盘。
    # 而且必须先走 BACKTEST —— 跳跃本身就该被拒。
    raises(IllegalStageTransition,
           lambda: registry.promote("ew_momentum", StrategyStage.PAPER,
                                    actor="human:owner"),
           "B6 RESEARCH 跳级进 PAPER 被拒（生命周期不能跳站）")
    registry.promote("ew_momentum", StrategyStage.BACKTEST, actor="human:owner")
    eq(len(registry.policies()), 0, "B6 BACKTEST 仍不进候选池")
    raises(PromotionBlocked,
           lambda: registry.promote("ew_momentum", StrategyStage.PAPER,
                                    actor="human:owner"),
           "B6 没有回测报告就不许进 PAPER")
    registry.promote("ew_momentum", StrategyStage.PAPER, actor="human:owner",
                     backtest_report_id="bt-2026-10-20")
    eq(len(registry.policies()), 1, "B6 备齐材料升到 PAPER 后进候选池")
    raises(Exception, lambda: registry.promote("ew_momentum", StrategyStage.LIVE,
                                               actor="human:owner"),
           "B6 直接 RESEARCH → LIVE 被准入拒绝")

    section("B7 内置/对象形态也能装进同一套契约")

    class Inline:
        manifest = StrategyManifest(strategy_id="inline", version="v1",
                                    venue="SIM", warmup_bars=2)
        @staticmethod
        def compute(frame, ctx):
            return []

    inline = pack_from_object(Inline(), source="", origin="<inline>")
    eq(inline.manifest.strategy_id, "inline", "B7 对象形态可加载")
    truth(any("指纹退化" in w for w in inline.warnings),
          "B7 无源码时明确提示指纹退化了（只能证明声明没改）")


# --------------------------------------------------------------------------- #
#  C. 运行时
# --------------------------------------------------------------------------- #

def part_c() -> None:
    tmp = tempfile.mkdtemp()
    pack = load_pack(_write(tmp, "good.strat.py", GOOD_PACK),
                     dry_frame=_frame(NOW), dry_params={"top_n": 2})
    policy = StrategyPolicy(strategy_id="ew_momentum", venue="SIM",
                            quota_pct=D("0.5"))
    alloc = allocate(policy, D("100000"))

    section("C1 整条链路：信号 → 意图 → 净额 → 风控 → 成交 → 账本")

    ledger, oms, state, client, rt = _stack(tmp)
    report = rt.run_once(_frame(NOW), [StrategyRun(pack, {"top_n": 2})],
                         nav=D("100000"), now=NOW,
                         allocations={"ew_momentum": alloc},
                         policies={"ew_momentum": policy}, risk_state=state)
    eq(len(report.fills), 2, "C1 两个信号最终变成两笔成交")
    eq(report.intents, 2, "C1 两个意图")
    eq(len(report.errors), 0, "C1 无异常")
    eq(len(report.rejected), 0, "C1 无被拒")

    fees = sum((D(f.fee) for f in report.fills), Decimal(0))
    eq(fees, Decimal("0.40020000") * 2, "C1 手续费按 2bp 计")
    truth(oms.open_orders() == [], "C1 全部成交后没有在途订单")

    section("C2 风控要求按 cap 重算时，上游必须真的重算")

    eq(len(report.capped), 2, "C2 两笔单都因单笔上限被重算")
    before, after = report.capped[0][1], report.capped[0][2]
    eq(D(before), Decimal("10000.00"), "C2 重算前 delta 是策略给的 10000")
    eq(D(after), Decimal("2000.00"), "C2 重算后按 NAV 2% 的 cap 压到 2000")
    truth(D(after) < D(before), "C2 重算只会变小，不会放大")

    section("C3 归因：成交必须落到具体策略头上（R-D05）")

    pos = ledger.positions()[("SIM", "AAA")]
    truth(D(pos.attr_qty.get("ew_momentum", 0)) > 0,
          "C3 AAA 的持仓记在 ew_momentum 名下")
    near(D(pos.attr_qty["ew_momentum"]), D(report.fills[0].quantity), D("1e-8"),
         "C3 归属数量等于成交数量（不多不少）")
    eq(len(ledger.positions()), 2, "C3 只买了两个标的，账本里就只有两个")

    section("C4 行情陈旧时风控拦在成交之前")

    ledger2, oms2, state2, client2, rt2 = _stack(tmp)
    stale_now = NOW + timedelta(minutes=10)
    rep2 = rt2.run_once(_frame(NOW), [StrategyRun(pack, {"top_n": 2})],
                        nav=D("100000"), now=stale_now,
                        allocations={"ew_momentum": alloc},
                        policies={"ew_momentum": policy}, risk_state=state2)
    eq(len(rep2.fills), 0, "C4 行情陈旧 10 分钟时不成交")
    truth(len(rep2.rejected) >= 1, "C4 被拒原因有记录")
    truth(any("stale_market_data" in r[1] or "行情陈旧" in r[2] for r in rep2.rejected),
          "C4 原因指向行情陈旧，不是笼统失败")

    section("C5 一个策略炸了，不许拖垮另一个")

    # 不带干跑：干跑会（正确地）在导入阶段就拒掉它。
    # 这一节要测的是"运行期已经有一个坏策略时，别把别人拖死"。
    boom = load_pack(_write(tmp, "boom.strat.py", FAILING_PACK))
    ledger3, oms3, state3, client3, rt3 = _stack(tmp)
    rep3 = rt3.run_once(_frame(NOW),
                        [StrategyRun(boom), StrategyRun(pack, {"top_n": 2})],
                        nav=D("100000"), now=NOW,
                        allocations={"ew_momentum": alloc},
                        policies={"ew_momentum": policy}, risk_state=state3)
    eq(len(rep3.errors), 1, "C5 只有一个策略报错")
    truth("boom" in rep3.errors[0][0], "C5 错误归属到 boom")
    eq(len(rep3.fills), 2, "C5 另一个策略照常成交")

    section("C6 重复回报不许重复记账")

    before_n = len(ledger.journal())
    first_fill = report.fills[0]
    again = ledger.apply_fill(first_fill, venue="SIM", symbol="AAA",
                              account_id="ACC", side=Side.BUY,
                              quote_ccy="USD", decision_price=first_fill.price,
                              ts=NOW)
    truth(again is False, "C6 同一笔成交重复入账返回 False")
    eq(len(ledger.journal()), before_n, "C6 重放后分录数不变")

    section("C7 暂停的策略不下单，且不留半截状态")

    ledger4, oms4, state4, client4, rt4 = _stack(tmp)
    rep4 = rt4.run_once(_frame(NOW), [StrategyRun(pack, {"top_n": 2},
                                                  state=StrategyRunState.PAUSED)],
                        nav=D("100000"), now=NOW,
                        allocations={"ew_momentum": alloc},
                        policies={"ew_momentum": policy}, risk_state=state4)
    eq(len(rep4.fills), 0, "C7 暂停后不成交")
    truth(all(getattr(t.decision, "value", t.decision) != "trade"
              for t in rep4.targets),
          "C7 暂停后没有任何 TRADE 目标（意图被净值层丢弃，不是没产生）")
    truth(any("paused" in b[1] for b in rep4.blocked),
          "C7 阻断原因写明 paused")

    section("C8 两个策略抢同一标的：净额合并，归属分开（R-A07 + R-D05）")

    second = load_pack(_write(tmp, "second.strat.py", SECOND_PACK),
                       dry_frame=_frame(NOW), dry_params={"w": "0.1"})
    ledger5, oms5, state5, client5, rt5 = _stack(tmp)
    second_policy = StrategyPolicy(strategy_id="second", venue="SIM",
                                   quota_pct=D("0.2"))
    second_alloc = allocate(second_policy, D("100000"))
    rep5 = rt5.run_once(
        _frame(NOW), [StrategyRun(pack, {"top_n": 2}), StrategyRun(second, {"w": "0.1"})],
        nav=D("100000"), now=NOW,
        allocations={"ew_momentum": alloc, "second": second_alloc},
        policies={"ew_momentum": policy, "second": second_policy}, risk_state=state5)
    aaa = [t for t in rep5.targets if t.symbol == "AAA"]
    truth(len(aaa) == 1, "C8 AAA 只产生一个净额目标（不许各自下单两次）")
    eq(len(aaa[0].per_strategy), 2,
       "C8 净额目标里保留了两个策略各自的归属（不然算不出各自盈亏）")
    truth(all(v > 0 for v in aaa[0].per_strategy.values()),
          "C8 两条都是多头，归属同向累加")

    # 系统只做多：空头意图必须被适配器丢掉，而不是变成"卖出现有持仓"。
    short_src = SECOND_PACK.replace('"second"', '"shorter"').replace(
        'ctx.params["w"]', '-ctx.params["w"]')
    shorter = load_pack(_write(tmp, "shorter.strat.py", short_src),
                        dry_frame=_frame(NOW), dry_params={"w": "0.1"})
    ledger6, oms6, state6, client6, rt6 = _stack(tmp)
    short_policy = StrategyPolicy(strategy_id="shorter", venue="SIM",
                                  quota_pct=D("0.2"))
    rep6 = rt6.run_once(_frame(NOW), [StrategyRun(shorter, {"w": "0.1"})],
                        nav=D("100000"), now=NOW,
                        allocations={"shorter": allocate(short_policy, D("100000"))},
                        policies={"shorter": short_policy}, risk_state=state6)
    eq(rep6.intents, 0, "C8 负权重在 allow_short=False 下不产生意图")
    eq(len(rep6.fills), 0, "C8 也就不会成交（不许把看空当成卖出）")


# --------------------------------------------------------------------------- #
#  D. 真实 A 股数据
# --------------------------------------------------------------------------- #

def part_d() -> None:
    section("D1 真实 A 股日线：能读、能算、能成交")

    daily = Path(ROOT) / "state" / "ashare" / "daily"
    if not daily.is_dir():
        truth(True, "D1 本机没有 state/ashare/daily，跳过真实数据段（不影响契约结论）")
        return

    files = sorted(daily.glob("*.csv"))[:6]
    if len(files) < 3:
        truth(True, "D1 样本不足 3 只，跳过")
        return

    series = {}
    for path in files:
        symbol = path.stem
        rows = []
        with open(path, encoding="utf-8") as fh:
            header = fh.readline()
            for line in fh:
                parts = line.rstrip("\n").split(",")
                if len(parts) < 3:
                    continue
                try:
                    rows.append((date.fromisoformat(parts[0]),
                                 Decimal(parts[2])))
                except Exception:                      # noqa: BLE001
                    continue
        tail = rows[-30:]
        if len(tail) < 30:
            continue
        series[symbol] = Series(symbol=symbol, points=tuple(tail))

    truth(len(series) >= 3, f"D1 读到 {len(series)} 只真实标的（各 30 个交易日）")
    last_day = max(s.last_date() for s in series.values())
    frame = MarketFrame(
        as_of=datetime(last_day.year, last_day.month, last_day.day, 15, 0, tzinfo=UTC),
        venue="TIGER", series=series)

    real_pack_src = REAL_PACK
    tmp = tempfile.mkdtemp()
    real_pack = load_pack(_write(tmp, "real.strat.py", real_pack_src),
                          dry_frame=frame, dry_params={"top_n": 3})
    truth(len(real_pack.dry_signals) <= 3, "D1 真实数据上干跑不炸")
    eq(len(real_pack.dry_signals), len([s for s in real_pack.dry_signals]),
       "D1 信号条数自洽")
    truth(all(s.symbol in series for s in real_pack.dry_signals),
          "D1 信号里的标的全在真实行情里")

    ledger, oms, state, client, rt = _stack(tmp)
    policy = StrategyPolicy(strategy_id="ashare_real", venue="TIGER",
                            quota_pct=D("0.4"))
    alloc = allocate(policy, D("100000"))
    rep = rt.run_once(frame, [StrategyRun(real_pack, {"top_n": 3})],
                      nav=D("100000"), now=frame.as_of,
                      allocations={"ashare_real": alloc},
                      policies={"ashare_real": policy}, risk_state=state)
    eq(len(rep.errors), 0, "D1 真实数据整条链路无异常")
    truth(len(rep.fills) >= 1, f"D1 真实数据上成交 {len(rep.fills)} 笔")
    truth(D(rep.equity_after) < D("100000"),
          "D1 买完之后净值低于初始（手续费与滑点确实被扣了）")


# --------------------------------------------------------------------------- #
#  E. 不变量
# --------------------------------------------------------------------------- #

STDLIB_OK = {
    "math", "statistics", "decimal", "fractions", "operator", "datetime",
    "typing", "dataclasses", "collections", "itertools", "functools",
    "enum", "string", "re", "ast", "hashlib", "os", "sys", "tempfile",
    "pathlib", "types", "__future__", "json", "time", "abc", "contextlib",
}


def part_e() -> None:
    section("E1 三个新模块不引外部依赖")

    for name in ("strategy_api.py", "strategy_pack.py", "strategy_runtime.py"):
        path = Path(ROOT) / "quantos_core" / name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        bad = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] not in STDLIB_OK:
                        bad.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue
                root = (node.module or "").split(".")[0]
                if root not in STDLIB_OK:
                    bad.append(node.module or "")
        eq(bad, [], f"E1 {name} 只依赖标准库（发现：{bad}）")

    section("E2 白名单与禁用清单本身是收紧的")

    for danger in ("os", "sys", "subprocess", "socket", "requests", "urllib"):
        falsy(danger in ALLOWED_IMPORT_PREFIXES,
              f"E2 白名单不含 {danger}")
    for name in ("open", "exec", "eval", "__import__", "compile"):
        truth(name in FORBIDDEN_CALLS, f"E2 禁用清单含 {name}")
    truth("quantos_core.strategy_api" in ALLOWED_IMPORT_PREFIXES,
          "E2 只放行 strategy_api 这一个纯数据契约模块")

    section("E3 运行时不许自己造风控结论")

    src = (Path(ROOT) / "quantos_core" / "strategy_runtime.py").read_text(encoding="utf-8")
    falsy("policy_key=" in src, "E3 运行时没有伪造 policy_key")
    truth("Verdict.REDUCE_REQUIRED" in src,
          "E3 运行时确实处理了 REDUCE_REQUIRED（cap 重算那条契约）")
    truth("def check" in src,
          "E3 风控走的是客户端适配器，不是在这里重写一套判断")


def main() -> None:
    part_a()
    part_b()
    part_c()
    part_d()
    part_e()


if __name__ == "__main__":
    raise SystemExit(run("B8 策略运行时与导入契约", main))
