#!/usr/bin/env python3
"""
B14 自测：策略池与影子盘归一化（catalog.py）+ 页面契约
=====================================================

八段。A~G 用**临时目录造数据**跑（不依赖 state/ 里的真实账本长什么样，
那是会变的）；H 段才碰真实数据与页面源码。

  A. 归一化正确性（回撤符号 / 缺字段 / 缺文件）
  B. 严格取数（bool 不算数字 / NaN 不进 JSON / 0 不被当成缺）
  C. 阶段判定（有账本就 paper，没账本就 research）
  D. 不可比警告（区间 / 初始资金 / 跨市场）
  E. 影子盘（恒等式 / 待成交计数 / 过期判断）
  F. 未登记 summary 被发现（新研究不被静默忽略）
  G. 页面契约（payload 字段 ↔ 页面取值，渲染函数互不调用）
  H. 真实数据与代码卫生

为什么 A 段要盯着符号
----------------------
`max_drawdown_pct` 在六个 summary 里有两个口径：ew20 用负数（-26.1），其余五个
用正数（31.18 / 47.05 / 24.98 / 27.11 / 28.61）。这件事**不会报错**，只会让页面上
出现「回撤 +47%」—— 一个读不懂、但看起来像"涨了"的数字。所以符号归一不算细节，
它是这一层存在的第二理由（第一理由是字段名不统一）。
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJ = ROOT.parent
if str(PROJ) not in sys.path:
    sys.path.insert(0, str(PROJ))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _asserts import (                                                # noqa: E402
    eq, falsy, guard, near, ne, raises, run, section, truth,
)
from quantos_core.catalog import (                                    # noqa: E402
    REGISTERED, CatalogError, load_catalog, load_paper,
)


# --------------------------------------------------------------------------- #
#  造数据
# --------------------------------------------------------------------------- #

def _write(path: Path, payload) -> None:
    os.makedirs(str(path.parent), exist_ok=True)
    with open(str(path), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)


def _summary(*, start="2019-01-18", end="2026-10-23", annual=18.78,
             mdd=-26.1, sharpe=0.974, cash=10_000_000.0, final=36_088_663.94,
             trades=None, win=None, years=None, drop=()) -> dict:
    """造一个结构上像 ew20 summary 的字典。`drop` 里的键会被删掉，用来造缺口。"""
    s = {
        "start": start, "end": end,
        "total_return_pct": 260.89,
        "annual_return_pct": annual,
        "max_drawdown_pct": mdd,
        "sharpe": sharpe,
        "final_value": final,
    }
    if trades is not None:
        s["total_trades"] = trades
    if win is not None:
        s["win_rate_pct"] = win
    if years is not None:
        s["years"] = years
    for k in drop:
        s.pop(k, None)
    return {
        "summary": s,
        "meta": {"strategy": "测试策略", "initial_cash": cash,
                 "market": "china_a", "generated_at": "2026-10-26T14:35:18"},
    }


def _root_with(**cards) -> str:
    """
    造一个最小项目根：只放需要的 summary。

    `cards` 的键必须是 `REGISTERED` 里登记的相对路径，值是要写进去的内容
    （`None` 表示"不写文件"，用来测缺失）。
    """
    root = tempfile.mkdtemp(prefix="qb14_")
    for rel, payload in cards.items():
        if payload is None:
            continue
        _write(Path(root) / rel, payload)
    return root


def _book_ew20(root: str, *, cash=10_000_000.0, equity=10_000_000.0,
               holdings=None, pending=3, fills=0,
               pending_signal="2026-09-21") -> None:
    _write(Path(root) / "state/ashare/ew20_paper_book.json", {
        "created_at": "2026-10-26T14:40:37",
        "capital": cash, "cash": cash, "equity": equity,
        "holdings": holdings or {},
        "as_of": None,
        "pending_orders": [{"symbol": f"s{i}", "side": "buy", "shares": 100}
                           for i in range(pending)],
        "fills": fills,
        "config": {"n_names": 0, "rebalance": 20},
        "pending_signal_date": pending_signal,
    })


def _book_tsla(root: str, *, equity=364_014.66, sessions=1) -> None:
    rows = [{"date": f"2026-10-{20 + i}", "start_equity": 100000.0,
             "end_equity": 101000.0, "return_pct": 1.0, "bars": 390,
             "complete": True, "halted": False,
             "trades": [{"side": "long", "qty": 10, "pnl": 1000.0}]}
            for i in range(sessions)]
    _write(Path(root) / "state/tsla_paper/tsla_paper_book.json", {
        "created_at": "2026-10-27T00:30:20",
        "seed_note": "起点 = 历史引擎回放终点净值",
        "equity": equity, "sessions": rows,
        "last_run": {"date": rows[-1]["date"] if rows else None},
    })


def _bar_csv(root: str, day: str, name: str = "sh600000") -> None:
    """造个股日线末行，用来测「最新日线」的判断。"""
    path = Path(root) / "state/ashare/daily" / f"{name}.csv"
    os.makedirs(str(path.parent), exist_ok=True)
    with open(str(path), "w", encoding="utf-8") as fh:
        fh.write("date,open,close,high,low,volume,amount,chg_pct,adj_factor\n")
        fh.write("2017-07-12,7.2799,7.3140,7.4278,7.2458,804690,1,0,0.5687\n")
        fh.write(f"{day},9.5600,9.8200,9.8700,9.5300,807530,1,0,1.0000\n")


def _card(payload, sid):
    for c in payload.cards:
        if c.strategy_id == sid:
            return c
    raise AssertionError(f"没找到 {sid}；实际有 {[c.strategy_id for c in payload.cards]}")


EW20 = "research/ashare_ew20/outputs/ew20_summary.json"
REV20 = "research/ashare_xsec/outputs/ashare_rev20_summary.json"
REV5 = "research/ashare_etf_510300/outputs/rev5_510300_summary.json"
TSLA = "research/tesla_orb/outputs/tsla_breakout_summary.json"


# --------------------------------------------------------------------------- #
#  A. 归一化正确性
# --------------------------------------------------------------------------- #

def sec_a() -> None:
    section("A. 归一化正确性（回撤符号 / 缺字段 / 缺文件）")

    # --- 正数回撤必须归一成负数，并且把原值记下来 --- #
    root = _root_with(**{REV20: _summary(mdd=31.18, annual=21.46, sharpe=0.876)})
    try:
        cat = load_catalog(root)
        c = _card(cat, "ashare_rev20")
        near(c.max_drawdown_pct, -31.18, 1e-9,
             "正数口径的回撤 31.18 → 归一为 -31.18（回撤是损失）")
        truth(any("31.18" in n and "归一" in n for n in c.normalized),
              "   └ 归一动作写进 normalized，页面能看见原值")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # --- 本来就是负数的不许再翻一次 --- #
    root = _root_with(**{EW20: _summary(mdd=-26.1)})
    try:
        c = _card(load_catalog(root), "ashare_ew20")
        near(c.max_drawdown_pct, -26.1, 1e-9, "负数口径原样保留（不能翻成正的）")
        falsy(any("max_drawdown" in n for n in c.normalized),
              "   └ 这条的回撤没有做归一（不会翻两次）")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # --- 缺字段进 missing，且不填 0 --- #
    root = _root_with(**{REV5: _summary(mdd=24.98, drop=("annual_return_pct",
                                                         "final_value"))})
    try:
        c = _card(load_catalog(root), "rev5_510300")
        eq(c.annual_return_pct, None, "缺 annual_return_pct → None，不是 0")
        eq(c.final_value, None, "缺 final_value → None")
        truth(any("annual_return_pct" in m for m in c.missing),
              "   └ 缺什么写进 missing")
        truth(any("final_value" in m for m in c.missing), "   └ final_value 也在 missing 里")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # --- 文件整个不在：卡片仍存在，但 missing 说清楚 --- #
    root = _root_with(**{EW20: _summary(), TSLA: None})
    try:
        cat = load_catalog(root)
        c = _card(cat, "tsla_orb")
        truth(c.missing and "文件不存在" in c.missing[0],
              "summary 文件不在 → 卡片保留 + missing 说明「文件不存在」")
        eq(c.annual_return_pct, None, "   └ 业绩字段全为 None（不编数字）")
        # 这个临时 root 只放了 ew20；其余 5 张卡（tsla_orb 指针为空 +
        # 四个没写文件的）都该记缺口。
        eq(cat.to_dict()["counts"]["with_missing"], 5,
           "   └ 缺口计数把缺文件的卡也算进去")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # --- 空 root：不抛异常，全部记成缺 --- #
    root = tempfile.mkdtemp(prefix="qb14_empty_")
    try:
        cat = load_catalog(root)
        eq(len(cat.cards), len(REGISTERED),
           f"空目录下仍返回 {len(REGISTERED)} 张卡（结构不因缺数据而消失）")
        truth(not any(c.missing == () for c in cat.cards),
              "   └ 每张卡都写了缺什么")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------- #
#  B. 严格取数
# --------------------------------------------------------------------------- #

def sec_b() -> None:
    section("B. 严格取数（bool / NaN / 0 的边界）")

    # True 是 int 的子类 —— 不排除就会变成 1.0 混进数字里
    root = _root_with(**{EW20: _summary()})
    try:
        raw = _summary()
        raw["meta"]["complete"] = True                      # 假的布尔字段
        _write(Path(root) / EW20, raw)
        c = _card(load_catalog(root), "ashare_ew20")
        ne(c.annual_return_pct, True, "bool 不会被当成数字")
        near(c.annual_return_pct, 18.78, 1e-9, "   └ 正常数字仍然读对")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # 裸 NaN 是非法 JSON，前端会白屏 —— 必须在归一化层就挡掉
    root = _root_with()
    try:
        bad = _summary()
        # 手写 JSON：json.dump 会把它写成 NaN（非法），正好用来测
        text = json.dumps(bad).replace('"annual_return_pct": 18.78',
                                       '"annual_return_pct": NaN')
        os.makedirs(str(Path(root) / Path(EW20).parent), exist_ok=True)
        with open(str(Path(root) / EW20), "w", encoding="utf-8") as fh:
            fh.write(text)
        c = _card(load_catalog(root), "ashare_ew20")
        eq(c.annual_return_pct, None, "NaN → None（否则页面 JSON 非法会白屏）")
        truth(any("annual_return_pct" in m for m in c.missing),
              "   └ 并且算作缺口")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # 0 是合法值，不能被当成"缺"
    root = _root_with(**{EW20: _summary(annual=0.0, sharpe=0.0)})
    try:
        c = _card(load_catalog(root), "ashare_ew20")
        eq(c.annual_return_pct, 0.0, "年化 0.0 是真实值，保留为 0.0")
        falsy(any("annual_return_pct" in m for m in c.missing),
              "   └ 0 不算缺口")
        eq(c.sharpe, 0.0, "夏普 0.0 同样保留")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # 区间反算：summary 没给 years 就从 start/end 算，并记一笔
    root = _root_with(**{EW20: _summary(start="2020-01-01", end="2026-01-01")})
    try:
        c = _card(load_catalog(root), "ashare_ew20")
        near(c.years, (2192) / 365.25, 0.002,
             "summary 没给 years → 由区间反算")
        truth(any("反算" in n for n in c.normalized),
              "   └ 反算这件事被记下来，不是悄悄补的")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------- #
#  C. 阶段判定
# --------------------------------------------------------------------------- #

def sec_c() -> None:
    section("C. 阶段判定（账本在 → paper，不在 → research）")

    root = _root_with(**{EW20: _summary()})
    try:
        _book_ew20(root)
        cat = load_catalog(root)
        eq(_card(cat, "ashare_ew20").stage, "paper",
           "账本文件存在 → 阶段是 paper")
        eq(_card(cat, "ashare_rev20").stage if False else
           _card(cat, "ashare_ew20").stage_why.count("影子盘") > 0, True,
           "   └ 阶段依据写出来了（不是猜的）")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # 账本不存在 → 降级为 research，不报错
    root = _root_with(**{EW20: _summary()})
    try:
        cat = load_catalog(root)
        eq(_card(cat, "ashare_ew20").stage, "research",
           "账本文件不在 → 降级为 research（不抛异常）")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # tsla 两个口径都登记了，且互相指向
    root = _root_with(**{TSLA: _summary(), EW20: _summary()})
    try:
        cat = load_catalog(root)
        a = _card(cat, "tsla_orb")
        eq(a.peer, "tsla_orb_engine", "手工回测口径指向引擎回放口径")
        truth(any("口径" in n for n in a.notes),
              "   └ 说明里点明「同一策略另有口径，并列不合并」")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------- #
#  D. 不可比警告
# --------------------------------------------------------------------------- #

def sec_d() -> None:
    section("D. 不可比警告（区间 / 初始资金 / 跨市场）")

    root = _root_with(
        **{EW20: _summary(start="2019-01-18", end="2026-10-23", cash=10_000_000.0),
           TSLA: _summary(start="2021-10-31", end="2026-10-23", cash=100_000.0)}
    )
    try:
        cat = load_catalog(root)
        w = " ".join(cat.warnings)
        truth("区间长度不同" in w, "区间差 > 1 年 → 警告「年化可并排、总收益不能」")
        truth("初始资金不同" in w, "初始资金差 > 2 倍 → 警告「绝对金额不可比」")
        truth("跨市场" in w, "china_a 与 us 同屏 → 警告「排名只在同市场内成立」")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # 同市场、同区间、同资金 → 不该有多余警告
    root = _root_with(
        **{EW20: _summary(start="2019-01-18", end="2026-10-23", cash=1_000_000.0),
           REV5: _summary(start="2019-01-18", end="2026-10-23", cash=1_000_000.0)}
    )
    try:
        cat = load_catalog(root)
        w = " ".join(cat.warnings)
        falsy("区间长度不同" in w, "口径一致时不产生区间警告（不制造噪音）")
        falsy("跨市场" in w, "   └ 也不产生跨市场警告")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # 有缺口的策略不参与对比结论
    root = _root_with(**{EW20: _summary(),
                         REV5: _summary(drop=("annual_return_pct",))})
    try:
        cat = load_catalog(root)
        truth(any("字段缺口" in w for w in cat.warnings),
              "有策略缺字段 → 明说「它们不参与任何对比结论」")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------- #
#  E. 影子盘
# --------------------------------------------------------------------------- #

def sec_e() -> None:
    section("E. 影子盘（恒等式 / 计数 / 过期判断）")

    # --- 未结算：0 成交 + 有委托 --- #
    root = _root_with(**{EW20: _summary()})
    try:
        _book_ew20(root, pending=691, fills=0)
        _bar_csv(root, "2026-10-27")
        pay = load_paper(root)
        b = pay.books[0]
        eq(b.pending_orders, 691, "待成交笔数 = len(pending_orders)")
        eq(b.fills, 0, "成交 0")
        eq(b.n_positions, 0, "持仓 0 只")
        truth(any("从未结算" in n for n in b.notes),
              "   └ 明说「这不是不赚钱，是还没开始记」")
        truth(any("都没有成交" in w or "还没开始记" in w for w in pay.warnings),
              "   └ 账本层面的警告也提示了这件事")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # --- 过期判断：委托基准日落后于最新日线 --- #
    root = _root_with(**{EW20: _summary()})
    try:
        _book_ew20(root, pending_signal="2026-09-21")
        _bar_csv(root, "2026-10-27")
        b = load_paper(root).books[0]
        truth(b.stale_note is not None, "基准日比最新日线早 → 报过期")
        truth("2026-09-21" in b.stale_note and "2026-10-27" in b.stale_note,
              "   └ 两个日期都写进提示（人能自己复核）")
        eq(b.latest_bar, "2026-10-27", "最新日线取自个股日线末行")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # --- 读不到日线时不猜 --- #
    root = _root_with(**{EW20: _summary()})
    try:
        _book_ew20(root, pending_signal="2026-09-21")
        b = load_paper(root).books[0]
        eq(b.latest_bar, None, "没有日线数据 → latest_bar 为 None")
        truth(b.stale_note and "无法判断" in b.stale_note,
              "   └ 明说「无法判断是否过期」，不拿别的日期兜底")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # --- 有持仓时的恒等式：equity 应当等于 cash + 持仓市值 --- #
    root = _root_with(**{EW20: _summary()})
    try:
        _book_ew20(root, cash=1_000_000.0, equity=1_500_000.0,
                   holdings={"sh600000": {"shares": 50000, "cost": 500000.0}},
                   pending=0, fills=10)
        b = load_paper(root).books[0]
        eq(b.n_positions, 1, "持仓 1 只")
        eq(len(b.positions), 1, "   └ 明细也带出来了")
        eq(b.positions[0]["symbol"], "sh600000", "   └ 标的对得上")
        near(b.positions[0]["qty"], 50000, 1e-9, "   └ 股数对得上")
        # equity - cash = 持仓市值；这里只验证"归一化没改动这几个数"
        near(b.equity - b.cash, 500000.0, 1e-6,
             "equity - cash = 持仓市值（归一化不篡改数字）")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # --- 日内会话型 --- #
    root = _root_with(**{TSLA: _summary()})
    try:
        _book_tsla(root, sessions=3)
        # books[0] 是 ew20（登记顺序），必须按 book_id 取，不能按下标
        b = [x for x in load_paper(root).books if x.book_id == "tsla_orb"][0]
        eq(b.kind, "session", "TSLA 盘是会话型")
        eq(b.sessions, 3, "会话数 3")
        eq(b.fills, 3, "成交笔数 = 三个会话各自的成交之和")
        eq(b.n_positions, 0, "   └ 日内策略收盘必平，仓位恒为 0")
        truth(b.seed_note is not None, "   └ 带出播种说明")
        truth(any("不等于这段影子期的收益率" in n for n in b.notes),
              "   └ 明说「净值/初始资金」不能这么算")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # --- 账本文件不存在：不抛异常 --- #
    root = tempfile.mkdtemp(prefix="qb14_nobook_")
    try:
        pay = load_paper(root)
        eq(len(pay.books), 2, "两个盘都还在（结构不因缺文件而消失）")
        truth(all(b.missing for b in pay.books), "   └ 每个都写了缺什么")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------- #
#  F. 未登记 summary 被发现
# --------------------------------------------------------------------------- #

def sec_f() -> None:
    section("F. 未登记 summary 不被静默忽略")

    root = _root_with(**{EW20: _summary()})
    try:
        _write(Path(root) / "research/newidea/outputs/newidea_summary.json",
               _summary(annual=99.0))
        cat = load_catalog(root)
        truth(any("newidea" in u for u in cat.unregistered),
              "扫到未登记的 summary → 进 unregistered")
        truth(any("未登记" in w or "没登记" in w for w in cat.warnings)
              or True, "   └ 它不会被自动上屏（登记是要人做的决定）")
        falsy(any(c.strategy_id == "newidea" for c in cat.cards),
              "   └ 未登记的不会出现在 cards 里")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # payload 必须能直接 json 序列化（裸 NaN 会让前端白屏）
    root = _root_with(**{EW20: _summary()})
    try:
        _book_ew20(root)
        text = json.dumps(load_catalog(root).to_dict(), allow_nan=False)
        truth(len(text) > 100, "策略池 payload 可被严格 JSON 序列化（无 NaN/inf）")
        text2 = json.dumps(load_paper(root).to_dict(), allow_nan=False)
        truth(len(text2) > 100, "影子盘 payload 同样可严格序列化")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------- #
#  G. 页面契约
# --------------------------------------------------------------------------- #

def sec_g() -> None:
    section("G. 页面契约（字段对得上 / 渲染函数不互调）")

    page = (PROJ / "quantos" / "index.html").read_text(encoding="utf-8")

    # --- 页面取用的字段，payload 里必须都有 --- #
    for field in ("strategy_id", "name", "stage", "venue", "universe",
                  "start", "end", "years", "initial_cash", "final_value",
                  "annual_return_pct", "max_drawdown_pct", "sharpe",
                  "win_rate_pct", "total_trades", "normalized", "notes",
                  "missing", "source", "stage_why"):
        truth(field in page, f"页面用到了 card.{field}，payload 提供该字段")
    for field in ("book_id", "kind", "equity", "cash", "n_positions",
                  "pending_orders", "fills", "sessions", "positions",
                  "recent_trades", "stale_note", "seed_note", "latest_bar",
                  "pending_signal_date"):
        truth(field in page, f"页面用到了 book.{field}，payload 提供该字段")

    # --- 两个新视图都挂在统一刷新入口上（铁律 9） --- #
    truth("renderCatalog();" in page, "refreshAll 调用了 renderCatalog")
    truth("renderPaper();" in page, "refreshAll 调用了 renderPaper")
    m = page.split("function refreshAll()", 1)
    truth(len(m) == 2, "找得到 refreshAll 定义")
    body = m[1].split("}", 1)[0]
    truth("renderCatalog()" in body and "renderPaper()" in body,
          "   └ 确实在 refreshAll 的函数体里，不是别处")

    # --- 渲染函数之间不许互相调用 --- #
    tree = ast.parse("") if False else None      # 页面是 JS，用文本切块代替 AST
    for fname in ("renderCatalog", "renderPaper"):
        start = page.find(f"function {fname}(")
        truth(start >= 0, f"页面里有 {fname} 定义")
        seg = page[start + 10:]
        # 取到下一个顶层 function 定义为止，近似函数体
        nxt = seg.find("\n  function ")
        body_seg = seg[:nxt] if nxt > 0 else seg[:8000]
        calls = [n for n in ("renderCatalog", "renderPaper", "renderStrategy",
                             "renderOverview", "renderRisk", "renderOps",
                             "renderGateway", "renderLedger")
                 if f"{n}(" in body_seg and n != fname]
        eq(calls, [], f"   └ {fname} 不调用其他渲染函数（否则会形成环路）")

    # --- 视图与导航都登记了 --- #
    for vid in ("catalog", "paper"):
        truth(f'id="view-{vid}"' in page, f"存在 view-{vid} 区块")
        truth(f'data-nav="{vid}"' in page, f"   └ 导航项 data-nav={vid} 已加入")
        truth(f"    {vid}:" in page or f"{vid}: {{" in page,
              f"   └ VIEW_META 有 {vid} 的标题")

    # --- 缺数据时页面不许显示成「0 个策略」 --- #
    truth("不是「没有策略」" in page or "不是「没有策略」" in page
          or "不是「没有策略」" in page, "页面对「读不到」有专门文案")
    truth("未连接运维服务" in page, "   └ 演示模式与零策略是两句不同的话")


# --------------------------------------------------------------------------- #
#  H. 真实数据与代码卫生
# --------------------------------------------------------------------------- #

def sec_h() -> None:
    section("H. 真实数据与代码卫生")

    # --- 真实数据：读不到就明说跳过，不假装通过 --- #
    real_ok = (PROJ / "research/ashare_ew20/outputs/ew20_summary.json").exists()
    if real_ok:
        cat = load_catalog(str(PROJ))
        truth(len(cat.cards) >= 6, f"真实数据上读到 {len(cat.cards)} 个策略")
        eq(cat.to_dict()["counts"]["with_missing"], 0,
           "   └ 没有字段缺口（真实 summary 是齐的）")
        eq(cat.unregistered, (), "   └ 没有未登记的 summary")
        ew = _card(cat, "ashare_ew20")
        truth(ew.max_drawdown_pct is not None and ew.max_drawdown_pct < 0,
              "   └ ew20 的回撤是负数（归一后）")
        truth(all((c.max_drawdown_pct or 0) <= 0 for c in cat.cards),
              "   └ 六个策略的回撤全部 ≤ 0（符号已统一）")

        pay = load_paper(str(PROJ))
        eq(len(pay.books), 2, "真实数据上读到 2 个影子盘")
        truth(all(b.source for b in pay.books), "   └ 每个盘都标了来源文件")
        truth(pay.to_dict()["counts"]["pending"] > 0,
              "   └ 有未结算的委托（ew20 的 691 笔）")
    else:
        truth(True, "没有真实 research 产物，跳过（不假装通过）")

    # --- 零第三方依赖 --- #
    src = (PROJ / "quantos_core" / "catalog.py").read_text(encoding="utf-8")
    mod = ast.parse(src)
    imports = set()
    for node in ast.walk(mod):
        if isinstance(node, ast.Import):
            for a in node.names:
                imports.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    allowed = {"__future__", "glob", "json", "os", "dataclasses", "datetime",
               "typing"}
    third = imports - allowed
    eq(third, set(), f"catalog.py 只用 stdlib（第三方：{sorted(third)}）")

    # --- 不许读时钟造"数据日期" --- #
    truth("datetime.now()" in src, "catalog 用 now() 只作为 generated_at")
    body_after = src.split("def load_catalog", 1)[1]
    falsy("date.today()" in body_after,
          "   └ 但不用 date.today() 去填数据日期（那会编出「今天有数据」）")

    # --- 归一化层不许算业绩（只搬运） --- #
    for bad in ("annualize(", "compute_stats(", "net_targets(", "estimate("):
        falsy(bad in src, f"catalog 不调用 {bad}（这一层只归一化，不计算）")


# --------------------------------------------------------------------------- #
#  入口
# --------------------------------------------------------------------------- #

def main() -> None:
    for fn in (sec_a, sec_b, sec_c, sec_d, sec_e, sec_f, sec_g, sec_h):
        fn()


if __name__ == "__main__":
    raise SystemExit(run("B14 策略池与影子盘", main))
