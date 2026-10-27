#!/usr/bin/env python3
"""渲染 EW20（等权 + 20日再平衡）回测仪表盘 → research/ashare_ew20/index.html

只读三件套 + summary 里的容量网格 / 幸存者界，不重算回测。

Run:
    python research/ashare_ew20/render_ew20.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pandas as pd

STRAT = Path(__file__).resolve().parent       # research/ashare_ew20
PROJ = STRAT.parent.parent
OUT = STRAT / "outputs"
sys.path.insert(0, str(PROJ))

from reporting.render_dashboard import build_dashboard_data, render_dashboard  # noqa: E402


def inline_vendor(path: Path) -> None:
    """CDN 的 marked/DOMPurify 内联进页面，保证离线打开不白屏。"""
    html = path.read_text(encoding="utf-8")
    repl = {
        "https://cdn.jsdelivr.net/npm/marked@14.1.2/marked.min.js": PROJ / "reporting" / "vendor" / "marked.min.js",
        "https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js": PROJ / "reporting" / "vendor" / "purify.min.js",
    }
    for url, local in repl.items():
        tag = f'<script src="{url}"></script>'
        if tag in html and local.exists():
            html = html.replace(tag, f"<script>{local.read_text(encoding='utf-8')}</script>")
    path.write_text(html, encoding="utf-8")


def _sanitize(obj):
    """NaN/inf → None：json.dumps 输出裸 NaN 是非法 JSON，前端会整页白屏。"""
    import math
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def pct(x, digits: int = 2) -> str:
    return "--" if x is None else f"{x:,.{digits}f}%"


def num(x, digits: int = 2) -> str:
    return "--" if x is None else f"{x:,.{digits}f}"


def load_nav(path: Path) -> list[dict]:
    if not path.exists():
        return []
    df = pd.read_csv(path)
    return [{"date": str(d)[:10], "value": float(v)} for d, v in zip(df["date"], df["value"])]


def main() -> int:
    summary = json.loads((OUT / "ew20_summary.json").read_text(encoding="utf-8"))
    p = summary["summary"]
    meta = summary["meta"]
    bench = summary["benchmarks"]
    grid = summary.get("capacity_grid", [])
    surv = summary.get("survivorship_bound", [])
    yearly = summary.get("yearly_pct", {})
    _ = yearly  # 年度收益看 outputs/ew20_yearly.png

    report = build_dashboard_data(
        equity_csv=OUT / "ew20_equity.csv",
        trades_csv=OUT / "ew20_trades.csv",
        summary_json=OUT / "ew20_summary.json",
        language="zh",
        market="china_a",
    )

    # ---- 主图：指数对照 ----
    idx_curves = {}
    for key, fname in [("沪深300", "index_000300.csv"), ("中证500", "index_000905.csv"),
                       ("中证800", "index_000906.csv")]:
        f = PROJ / "state" / "ashare" / fname
        if not f.exists():
            continue
        df = pd.read_csv(f, parse_dates=["date"]).set_index("date")["close"].sort_index()
        df = df.loc[(df.index >= pd.Timestamp(p["start"])) & (df.index <= pd.Timestamp(p["end"]))]
        if df.empty:
            continue
        base = report["equity_curve"][0]["value"]
        idx_curves[key] = [{"date": d.strftime("%Y-%m-%d"), "value": round(float(v / df.iloc[0] * base), 2)}
                           for d, v in df.items()]

    colors = {"沪深300": "#9e9e9e", "中证500": "#bcbd22", "中证800": "#8c564b"}
    for m in report["modules"]:
        if m.get("type") == "overview_chart":
            m["markers"] = []
            m["overlay_series"] = [
                {"name": k, "stroke": colors.get(k, "#999"), "points": v}
                for k, v in idx_curves.items()
            ]
            m["stats"] = [
                {"label": "总收益", "value": pct(p["total_return_pct"]), "raw": p["total_return_pct"]},
                {"label": "年化", "value": pct(p["annual_return_pct"]), "raw": p["annual_return_pct"]},
                {"label": "最大回撤", "value": pct(p["max_drawdown_pct"]), "raw": p["max_drawdown_pct"]},
                {"label": "Sharpe", "value": num(p["sharpe"]), "raw": p["sharpe"]},
                {"label": "调仓次数", "value": f"{meta['rebalances']}"},
                {"label": "平均现金", "value": pct(meta["avg_cash_pct"]), "raw": meta["avg_cash_pct"]},
            ]
        elif m.get("type") == "trades_table":
            rows = m.get("rows", [])
            m["title"] = "回合交易（最近 120 段）"
            m["subtitle"] = (f"共 {len(rows):,} 段闭合回合（按标的从建仓到清零聚合）；"
                             "完整清单在 outputs/ew20_trades.csv")
            m["rows"] = rows[-120:]

    # ---- 容量网格表 ----
    grid_rows = [{
        "metric": f"{r['names']} 只 @ {r['capital_wan']:.0f} 万",
        "values": [
            {"main": pct(r["annual_pct"]), "raw": r["annual_pct"]},
            {"main": pct(r["total_pct"]), "raw": r["total_pct"]},
            {"main": pct(r["mdd_pct"]), "raw": r["mdd_pct"]},
            {"main": num(r["sharpe"]), "raw": r["sharpe"]},
            {"main": pct(r["cost_drag_pct_yr"]), "raw": r["cost_drag_pct_yr"]},
            {"main": pct(r["avg_cash_pct"]), "raw": r["avg_cash_pct"]},
            {"main": f"{r['turnover_yr']:.1f}x", "raw": r["turnover_yr"]},
        ],
    } for r in grid]

    surv_rows = [{
        "metric": f"{r['replace_per_year_pct']:.0f}%/年 · 价差 {r['relative_gap_pct']:.0f}%",
        "values": [
            {"main": pct(r["annual_drag_pct"]), "raw": r["annual_drag_pct"]},
            {"main": pct(r["adjusted_annual_pct"]), "raw": r["adjusted_annual_pct"]},
            {"main": f"{r['adjusted_total_pct']:,.1f}%", "raw": r["adjusted_total_pct"]},
        ],
    } for r in surv]



    bench_txt = "、".join(f"{k} {pct(v)}" for k, v in bench.items() if v is not None)
    modules = [
        {"type": "metric_table", "tab": "capacity", "title": "容量前沿：只数 × 资金（全部摩擦成本已含）",
         "subtitle": "收益随只数单调上升——等权溢价主要来自流动性较差的尾部小票；"
                     "资金不足的伤害主要走「整手买不起 → 现金闲置」这条路，而不是手续费。",
         "columns": ["配置", "年化", "总收益", "最大回撤", "Sharpe", "成本拖累/年", "平均现金", "年换手"],
         "rows": grid_rows},
        {"type": "text", "tab": "capacity", "title": "怎么读这张表",
         "text": (
             "- **只数是第一杠杆**：全池 794 只年化 18.8%，砍到 200 只只剩 14.7%，砍到 50 只只剩 6.2%。"
             "「按流动性选前 N 名」每砍一刀都在把收益最高的那部分小票砍掉。\n"
             "- **资金是第二杠杆**：全池@100万 只有 70% 仓位（30% 现金闲置），年化掉到 14.0%；"
             "1000 万以上现金闲置降到 4% 以内。\n"
             "- **成本拖累只有 0.4–1.2%/年**，远低于直觉——因为 20 日再平衡的换手本来就低，"
             "且 2000 元以下的小额漂移单不下（下这种单只会被 5 元佣金下限磨死）。\n"
             "- 要吃满这条曲线，**1 手最低资金门槛约 570 万**（今天的价格）；"
             "1000 万是当前价位下比较舒服的起点。"
         )},
        {"type": "metric_table", "tab": "survivorship", "title": "幸存者偏差：能做的是给界，不是测量",
         "subtitle": "池子是今天还活着的 794 只中证800 成分股；退市/调出标的的行情拿不到（接口返回空表），"
                     "时点成分股也没有。下表是假设驱动的敏感度：每年被调出 d 比例、这部分每年少赚 h。",
         "columns": ["假设（调出比例 · 相对价差）", "年化拖累", "调整后年化", "调整后总收益"],
         "rows": surv_rows},
        {"type": "text", "tab": "survivorship", "title": "基准锚点",
         "text": (
             f"- 本策略（含全部成本）：总 {pct(p['total_return_pct'])}，年化 {pct(p['annual_return_pct'])}。\n"
             f"- 同窗口指数买入持有：{bench_txt}。指数本身含真实成分股更替（无幸存者偏差），"
             f"但它是市值加权——所以两者的差 = 等权溢价 + 小盘倾斜 + 幸存者偏差，三者分不开。\n"
             "- **能把三者拆开的唯一办法是拿到历史时点成分股**（中证指数公司官网提供历史调样公告）。"
             "在那之前，任何\"年化 18.8%\"都应该按表里的拖累自行打折后再预期。"
         )},
        {"type": "text", "tab": "notes", "title": "执行口径",
         "text": (
             "- 信号：每 20 个交易日收盘出单（网格锚定 2019-01-18）；成交：次日开盘价。\n"
             "- 费用：佣金 2.5bp（单笔最低 5 元）+ 印花税 5bp（卖出）+ 过户费 0.1bp + 滑点 5bp。\n"
             "- 约束：100 股整手（科创板 200 股）；涨停不买、跌停不卖（挂到下一日）；停牌跳过。\n"
             "- 再平衡：只下金额 ≥ 2000 元的调仓单；卖出先于买入。\n"
             "- 数据：中证800 现任成分股 794 只日线（2018-01 起），不复权价 + 接口日收益还原含股息价。"
         )},
        {"type": "text", "tab": "notes", "title": "与上一轮结论的关系",
         "text": (
             "- 上一轮解析口径（收盘腿、零成本、每期强制换仓）算出等权全池 +305%；"
             "本引擎按可下单口径（开盘腿、全摩擦、持仓连续）算出 +261%。**两者不矛盾**："
             "差额就是\"把研究数字变成能下单的策略\"要付的代价。\n"
             "- 引擎已与解析口径交叉验证：零成本设定下差异 4.1pp（93 期均摊每期 0.4bp），"
             "来自停牌/涨跌停挡单与成分集的细微差别。\n"
             "- 流动性前 200 名反而比全池差 4pp/年 —— 这一条直接否掉了\"做个流动性好一点的"
             "子集\"的直觉：等权溢价不住在流动性好的地方。"
         )},
        {"type": "text", "tab": "notes", "title": "局限（按影响排序）",
         "text": (
             "1. 幸存者偏差未量化（见\"幸存者偏差\"页）。\n"
             "2. 日线撮合：无盘口冲击，滑点 5bp 是对小资金的假设；资金大了要重测。\n"
             "3. 8 年样本只覆盖一轮牛熊；等权溢价在 2017 年前的风格里未必存在。\n"
             "4. 无行业/市值中性化，等权组合天然带小盘暴露——回撤 -26% 主要来自这个暴露。\n"
             "5. ST 股按 10% 涨跌停处理（实际 ±5%），偏宽松。"
         )},
        {"type": "text", "tab": "notes", "title": "免责声明",
         "text": ("⚠️ 以上内容由 AI 基于公开信息整理生成，仅供参考，不构成任何投资建议或个股推荐。"
                  "投资有风险，决策需谨慎。")},
    ]
    report["modules"] = [m for m in report["modules"] if m.get("type") not in ("metric_table",)] + modules
    report["ui"] = {
        **report.get("ui", {}),
        "subtitle": "中证800 等权 + 20日再平衡 · 可上线基线（含成本/整手/涨跌停）",
        "tabs": [
            {"id": "overview", "label": "净值与基准"},
            {"id": "capacity", "label": "容量前沿"},
            {"id": "survivorship", "label": "幸存者偏差"},
            {"id": "notes", "label": "口径与局限"},
        ],
        "active_tab": "overview",
        "language": "zh",
    }

    report = _sanitize(report)
    out = render_dashboard(report, output_path=STRAT / "index.html")
    inline_vendor(out)
    print(f"[ok] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
