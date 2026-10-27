#!/usr/bin/env python3
"""渲染 A 股横截面回测的 HTML 仪表盘。

只读三件套 + 基准曲线 + 因子统计，不重新算回测——账目只有一处，避免"页面和
文件对不上"。

Run:
    python scripts/render_ashare_xsec.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

STRAT = Path(__file__).resolve().parent      # research/ashare_xsec
ROOT = STRAT.parent.parent                   # 项目根：state/ 在这里
OUT = STRAT / "outputs"
sys.path.insert(0, str(ROOT))
from reporting.render_dashboard import build_dashboard_data, render_dashboard  # noqa: E402

PREFIX = {"rev20": "ashare_rev20", "mom120": "ashare_mom120"}
LABEL = {"rev20": "20日反转", "mom120": "120日动量"}
COLOR = {"rev20": "#f23645", "mom120": "#4c78a8", "equal_weight": "#54a24b", "csi300": "#9e9e9e"}


def load_nav(path: Path, col: str = "value") -> list[dict]:
    if not path.exists():
        return []
    df = pd.read_csv(path)
    return [{"date": str(d)[:10], "value": float(v)} for d, v in zip(df["date"], df[col])]


def pct(x, digits: int = 2) -> str:
    return "--" if x is None else f"{x:,.{digits}f}%"


def num(x, digits: int = 3) -> str:
    return "--" if x is None else f"{x:,.{digits}f}"


def inline_vendor(path: Path) -> None:
    """把 CDN 上的 marked / DOMPurify 内联进页面。

    模板默认从 jsdelivr 拉这两个库。离线打开时脚本 404，渲染函数在第一行就抛异常，
    页面只剩下一个空白骨架——线上一挂 CDN 就白屏，这种交付不合格。
    把库内联进来之后，index.html 单文件可离线打开。
    """
    html = path.read_text(encoding="utf-8")
    repl = {
        "https://cdn.jsdelivr.net/npm/marked@14.1.2/marked.min.js": ROOT / "reporting" / "vendor" / "marked.min.js",
        "https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js": ROOT / "reporting" / "vendor" / "purify.min.js",
    }
    for url, local in repl.items():
        tag = f'<script src="{url}"></script>'
        if tag in html and local.exists():
            html = html.replace(tag, f"<script>{local.read_text(encoding='utf-8')}</script>")
    path.write_text(html, encoding="utf-8")


def _sanitize(obj):
    """把 NaN / inf 换成 None。

    report 最终被 json.dumps 嵌入页面，Python 默认允许输出裸 NaN，但那是**非法 JSON**，
    前端 JSON.parse 会抛语法错误、整页白屏。在源头兜住，不依赖上游一定干净。
    """
    import math
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def main() -> int:
    stats_path = OUT / "ashare_xsec_factor_stats.json"
    bench_path = OUT / "ashare_xsec_benchmarks.csv"
    for p in [stats_path, bench_path] + [OUT / f"{v}_summary.json" for v in PREFIX.values()]:
        if not p.exists():
            raise SystemExit(f"[FATAL] 缺文件 {p}，先跑 ashare_xsec_backtest.py")

    extra_stats = json.loads(stats_path.read_text(encoding="utf-8"))
    bench = pd.read_csv(bench_path)

    robust_path = ROOT / "state" / "ashare" / "robustness.json"
    robust = json.loads(robust_path.read_text(encoding="utf-8")) if robust_path.exists() else {}

    sums = {}
    for key, prefix in PREFIX.items():
        sums[key] = json.loads((OUT / f"{prefix}_summary.json").read_text(encoding="utf-8"))

    ew_nav = load_nav(bench_path, "equal_weight")
    idx_nav = load_nav(bench_path, "csi300")

    # ---- 基准的汇总指标 -------------------------------------------------
    def curve_metrics(values: list[float]) -> dict:
        if len(values) < 2:
            return {}
        s = pd.Series(values)
        ret = s / s.iloc[0] - 1.0
        dd = (s / s.cummax() - 1.0).min()
        daily = s.pct_change().dropna()
        sharpe = float(daily.mean() / daily.std() * (252 ** 0.5)) if daily.std() > 0 else None
        n = len(s)
        years = n / 252.0
        ann = ((s.iloc[-1] / s.iloc[0]) ** (1.0 / years) - 1.0) * 100 if years > 0 and s.iloc[0] > 0 else None
        return {
            "total": float(ret.iloc[-1] * 100),
            "annual": float(ann) if ann is not None else None,
            "mdd": float(abs(dd) * 100),
            "sharpe": sharpe,
        }

    ew_m = curve_metrics([p["value"] for p in ew_nav]) if ew_nav else {}
    idx_m = curve_metrics([p["value"] for p in idx_nav]) if idx_nav else {}

    holdout = extra_stats.get("holdout", {})
    ic = extra_stats.get("ic", {})

    report = build_dashboard_data(
        equity_csv=OUT / f"{PREFIX['rev20']}_equity.csv",
        trades_csv=OUT / f"{PREFIX['rev20']}_trades.csv",
        summary_json=OUT / f"{PREFIX['rev20']}_summary.json",
        language="zh",
        market="china_a",
    )

    rev_nav = report["equity_curve"]
    mom_nav = load_nav(OUT / f"{PREFIX['mom120']}_equity.csv")

    # ---- 主图：加对照线，去掉逐笔买卖标记（几千笔，画上去只会糊成一片） ----
    for m in report["modules"]:
        if m.get("type") == "overview_chart":
            m["markers"] = []
            m["overlay_series"] = [
                {"name": LABEL["mom120"], "stroke": COLOR["mom120"], "points": mom_nav},
                {"name": "等权全池", "stroke": COLOR["equal_weight"], "points": ew_nav},
                {"name": "沪深300", "stroke": COLOR["csi300"], "points": idx_nav},
            ]
            m["stats"] = [
                {"label": "总收益", "value": pct(sums["rev20"]["summary"]["total_return_pct"]),
                 "raw": sums["rev20"]["summary"]["total_return_pct"]},
                {"label": "年化", "value": pct(sums["rev20"]["summary"]["annual_return_pct"]),
                 "raw": sums["rev20"]["summary"]["annual_return_pct"]},
                {"label": "最大回撤", "value": pct(sums["rev20"]["summary"]["max_drawdown_pct"]),
                 "raw": -abs(sums["rev20"]["summary"]["max_drawdown_pct"] or 0)},
                {"label": "Sharpe", "value": num(sums["rev20"]["summary"]["sharpe"]),
                 "raw": sums["rev20"]["summary"]["sharpe"]},
                {"label": "交易笔数", "value": f"{sums['rev20']['summary']['total_trades']:,}"},
                {"label": "胜率", "value": pct(sums["rev20"]["summary"]["win_rate_pct"]),
                 "raw": sums["rev20"]["summary"]["win_rate_pct"]},
            ]
        elif m.get("type") == "trades_table":
            rows = m.get("rows", [])
            m["title"] = "交易明细（最近 120 笔）"
            m["subtitle"] = f"共 {len(rows):,} 笔闭合交易，页面只展示最近 120 笔；完整清单在 trades.csv"
            m["rows"] = rows[-120:]
        elif m.get("type") == "metric_table":
            # 覆盖默认表而不是追加，避免上次那种"两张指标表"
            m["title"] = "多空对照与基准"
            m["columns"] = ["指标", LABEL["rev20"], LABEL["mom120"], "等权全池", "沪深300"]
            m["rows"] = [
                {"metric": "总收益", "values": [
                    {"main": pct(sums[k]["summary"]["total_return_pct"]),
                     "raw": sums[k]["summary"]["total_return_pct"]} for k in ("rev20", "mom120")
                ] + [{"main": pct(ew_m.get("total")), "raw": ew_m.get("total")},
                     {"main": pct(idx_m.get("total")), "raw": idx_m.get("total")}]},
                {"metric": "年化", "values": [
                    {"main": pct(sums[k]["summary"]["annual_return_pct"]),
                     "raw": sums[k]["summary"]["annual_return_pct"]} for k in ("rev20", "mom120")
                ] + [{"main": pct(ew_m.get("annual")), "raw": ew_m.get("annual")},
                     {"main": pct(idx_m.get("annual")), "raw": idx_m.get("annual")}]},
                {"metric": "最大回撤", "values": [
                    {"main": pct(sums[k]["summary"]["max_drawdown_pct"]),
                     "raw": -abs(sums[k]["summary"]["max_drawdown_pct"] or 0)}
                    for k in ("rev20", "mom120")
                ] + [{"main": pct(ew_m.get("mdd")), "raw": -abs(ew_m.get("mdd") or 0)},
                     {"main": pct(idx_m.get("mdd")), "raw": -abs(idx_m.get("mdd") or 0)}]},
                {"metric": "Sharpe", "values": [
                    {"main": num(sums[k]["summary"]["sharpe"]),
                     "raw": sums[k]["summary"]["sharpe"]} for k in ("rev20", "mom120")
                ] + [{"main": num(ew_m.get("sharpe")), "raw": ew_m.get("sharpe")},
                     {"main": num(idx_m.get("sharpe")), "raw": idx_m.get("sharpe")}]},
                {"metric": "胜率", "values": [
                    {"main": pct(sums[k]["summary"]["win_rate_pct"]),
                     "raw": sums[k]["summary"]["win_rate_pct"]} for k in ("rev20", "mom120")
                ] + [{"main": "--"}, {"main": "--"}]},
                {"metric": "交易笔数", "values": [
                    {"main": f"{sums[k]['summary']['total_trades']:,}"} for k in ("rev20", "mom120")
                ] + [{"main": "--"}, {"main": "--"}]},
                {"metric": "横截面 IC", "values": [
                    {"main": num(ic.get(k, {}).get("mean_ic"), 4),
                     "raw": ic.get(k, {}).get("mean_ic")} for k in ("rev20", "mom120")
                ] + [{"main": "--"}, {"main": "--"}]},
                {"metric": "IC 置换 p 值", "values": [
                    {"main": num(ic.get(k, {}).get("p_perm"), 3)} for k in ("rev20", "mom120")
                ] + [{"main": "--"}, {"main": "--"}]},
                {"metric": "封存段收益", "values": [
                    {"main": pct(holdout.get(k, {}).get("return_pct")),
                     "raw": holdout.get(k, {}).get("return_pct")} for k in ("rev20", "mom120")
                ] + [{"main": pct(holdout.get("ew_bench", {}).get("return_pct")),
                      "raw": holdout.get("ew_bench", {}).get("return_pct")},
                     {"main": pct(holdout.get("csi300", {}).get("return_pct")),
                      "raw": holdout.get("csi300", {}).get("return_pct")}]},
            ]

    win = extra_stats.get("window", {})
    uni = extra_stats.get("universe", {})
    cfg = extra_stats.get("config", {})
    rev_ic, mom_ic = ic.get("rev20", {}), ic.get("mom120", {})
    rev_y = extra_stats.get("yearly", {}).get("rev20", {})
    rev_excess = (sums["rev20"]["summary"]["total_return_pct"] or 0) - (ew_m.get("total") or 0)

    modules = []
    if robust:
        surf = robust.get("factor_surface", [])
        det = robust.get("detail", {})
        rows = []
        for r in surf:
            name = r["factor"]
            d = det.get(name, {})
            null = d.get("random_null", {})
            x3 = d.get("cost_sensitivity", {}).get("x3", {})
            ho = d.get("holdout", {})
            verdict = d.get("verdict", "--")
            rows.append({
                "metric": f"{name}",
                "values": [
                    {"main": pct(r.get("total_pct")), "raw": r.get("total_pct")},
                    {"main": pct(r.get("annual_pct")), "raw": r.get("annual_pct")},
                    {"main": pct(abs(r.get("mdd_pct") or 0)), "raw": -abs(r.get("mdd_pct") or 0)},
                    {"main": num(r.get("sharpe"), 2), "raw": r.get("sharpe")},
                    {"main": f"{null.get('percentile_vs_null', 0) * 100:.1f}%"},
                    {"main": pct(x3.get("total_pct")), "raw": x3.get("total_pct")},
                    {"main": pct(ho.get("total_pct")), "raw": ho.get("total_pct")},
                    {"main": "通过" if verdict == "PASS" else "不通过"},
                ],
            })
        modules.append({
            "type": "metric_table",
            "tab": "audit",
            "title": "稳健性审计矩阵（11 个候选 × 4 类测谎）",
            "subtitle": ("随机分位 = 在同一批标的中随机挑选同样只数、持有同样天数，重复 300 次后"
                         "策略所处的位置；超过 99% 才算通过，11 个检验一起做门槛还要更高。"
                         "本表为解析口径（开盘进、开盘出、等权、不计整手与涨跌停），"
                         "略优于主策略引擎的成交口径 —— 主策略那一栏的数字更保守。"),
            "columns": ["因子", "总收益", "年化", "最大回撤", "Sharpe",
                        "随机分位", "3×成本", "封存段", "判定"],
            "rows": rows,
        })
        bench_ew = robust.get("benchmark_equal_weight_open", {})
        modules.append({
            "type": "text",
            "tab": "audit",
            "title": "这张表怎么说",
            "text": (
                f"- **等权全池（同一口径）{pct(bench_ew.get('total_pct'))}**。"
                f"随机挑 79 只、持 20 日的零分布中位数是 "
                f"{pct(det.get('rev20', {}).get('random_null', {}).get('null_median_total_pct'))}。\n"
                f"- 也就是说：**在这个池子里，随便买一篮子股票的七年收益，就足以解释掉"
                f"策略的绝大部分涨幅**。因子本身贡献的那一段，落在噪声的范围内。\n"
                f"- 反转族（rev5–rev60）表现比动量族平滑，但只有 rev10、rev40 勉强越过 99 分位；"
                f"相邻参数之间上下跳动，这是\"针\"不是\"坡\"，典型的噪声形态。\n"
                f"- 动量族（mom60–mom250）收益更高，但最大回撤 30%+，"
                f"且横截面 IC 为 {num(extra_stats.get('ic', {}).get('mom120', {}).get('mean_ic'), 4)}（方向相反），"
                f"说明它赚的不是\"强者恒强\"，而是少数极端赢家带来的右尾。\n"
                f"- 3 倍成本下全部仍为正 —— 不是因为策略强，而是因为**收益里 beta 占比太高**，"
                f"成本根本压不住。这一条单独看会造成严重误判。"
            ),
        })

    modules += [
        {
            "type": "text",
            "tab": "notes",
            "title": "结论",
            "text": (
                f"**两个因子都没有跑出可交易的超额；真正的发现在基准那一栏。**\n\n"
                f"- 20 日反转：总收益 {pct(sums['rev20']['summary']['total_return_pct'])}，"
                f"年化 {pct(sums['rev20']['summary']['annual_return_pct'])}，"
                f"最大回撤 {pct(sums['rev20']['summary']['max_drawdown_pct'])}。\n"
                f"- 120 日动量：总收益 {pct(sums['mom120']['summary']['total_return_pct'])}。\n"
                f"- **等权全池基准：{pct(ew_m.get('total'))}** —— 策略只领先 {rev_excess:+.2f}pp（7.7 年）。\n"
                f"- **沪深300 买入持有：{pct(idx_m.get('total'))}**。\n\n"
                f"三个数字放一起，结论很直接：**在 A 股，\"等权 + 每 20 个交易日再平衡\"本身就是"
                f"最厚的那层 alpha（{pct(ew_m.get('total'))} vs 指数 {pct(idx_m.get('total'))}），"
                f"比任何选股因子都强**。它来自两个机制：把权重从小盘股偏弱的市值加权里解放出来，"
                f"以及高波动下的再平衡溢价（波动率收割）。\n\n"
                f"而选股这一层，反转的横截面 IC 是 {num(rev_ic.get('mean_ic'), 4)}"
                f"（置换 p = {num(rev_ic.get('p_perm'), 3)}，t = {num(rev_ic.get('t_stat'), 2)}），"
                f"看似显著，但它对应到只做多十分位的组合上，只剩下 "
                f"{rev_excess:+.2f}pp。动量的 IC 是 {num(mom_ic.get('mean_ic'), 4)}，方向相反。"
            ),
        },
        {
            "type": "text",
            "tab": "notes",
            "title": "口径与假设（每条都会影响数字）",
            "text": (
                f"- 标的池：中证 800（沪深300 + 中证500）**当前**成分股，实际入池 "
                f"{uni.get('fetched', '--')} 只（请求 {uni.get('requested', '--')} 只，其余被数据体检判废）。\n"
                f"- 窗口：{win.get('start', '--')} → {win.get('end', '--')}，"
                f"每 {cfg.get('rebalance_bars', 20)} 个交易日调仓，持有期相同，**样本不重叠**。\n"
                f"- 每期取十分位（约 {int(round(cfg.get('quantile', 0.1) * 100))}% 的标的）等权买入，"
                f"初始资金 {cfg.get('initial_cash', 0):,.0f} 元。\n"
                f"- 信号在第 t 日收盘后算出，第 t+1 日**开盘价**成交；T+1 当日买入当日不可卖。\n"
                f"- 100 股整手（科创板最低 200 股）；开盘即涨停不买、跌停不卖并顺延到下一日；"
                f"停牌不可交易。\n"
                f"- 成本：佣金 2.5bp 双边 + 印花税 5bp（仅卖出）+ 过户费 0.1bp 双边 = "
                f"单次往返 {cfg.get('round_trip_cost_bp', 10.2)}bp。\n"
                f"- 价格：接口前复权序列是坏的（除息日会出现假暴跌），所以用不复权价 + 接口日收益率"
                f"反推含股息复权价，见 fetch_ashare_universe.py 的说明。\n"
                f"- 基准：等权全池与策略**完全同口径**——信号日次日开盘等权买入全部可交易标的、"
                f"持有 20 个交易日后开盘卖出，每一个信号日都调仓、不计成本；沪深300 为买入持有。"
            ),
        },
        {
            "type": "text",
            "tab": "notes",
            "title": "偏差与限制",
            "text": (
                "1. **幸存者偏差（最重的一条）**：标的池取的是今天还在中证 800 里的股票，"
                "等于提前剔掉了这 8 年里退市、被调出、市值崩掉的样本。结果方向偏乐观，"
                "不能当预期收益。\n"
                "2. **单边样本**：接口只能回溯 2000 根日线（约 8.2 年），只覆盖一轮完整牛熊，"
                "任何\"这个因子长期有效\"的结论都超出数据能支持的范围。\n"
                "3. **整手导致的高价股缺失**：预算按等权分配，单价过高导致买不到一手的标的被跳过，"
                "组合因此轻微偏向低价股，这是一个无法消除的机械性偏移。\n"
                "4. **日线撮合**：开盘价成交假设没有冲击成本，也没有排队；"
                "真实交易 80 只股票的单笔金额虽小，但同日同向的冲击仍会被低估。\n"
                "5. **涨跌停与停牌**：只在开盘价上判断，盘中触板后打开的情况没有建模。"
                f"反转策略本期被涨停挡住 {extra_stats.get('frictions', {}).get('rev20', {}).get('skipped_limit_up', 0)} 次、"
                f"跌停顺延 {extra_stats.get('frictions', {}).get('rev20', {}).get('blocked_limit_down', 0)} 次。"
            ),
        },
        {
            "type": "text",
            "tab": "notes",
            "title": "本轮修掉的三个 bug（都会直接改变结论）",
            "text": (
                "1. **等权基准少算一半调仓**。旧实现每期结束后跳到 `exit+1`，"
                "恰好越过下一个信号日，94 期只执行了 47 期；同时成交价用的是信号日**当天开盘**"
                "（信号来自当天收盘，属前视）。两处叠加把基准从 +305% 压低到 +90%，"
                "**超额收益被虚高约 215pp**。\n"
                "2. **复权还原会把整只票压成 0 价**。接口偶发一条错误的 change_pct，"
                "乘进 cumprod 后该日之前的所有价格归零，整只票被判废——800 只里有 25 只"
                "因此隐身。现在对单日复权因子做了 ±50% 的物理约束。\n"
                "3. **停牌行 `open=0`**。停牌日在接口里是 open=high=low=0、close 沿用前收，"
                "直接进回测等于在 0 元成交。现在补成收盘价并把 volume 置 NaN，引擎会正确挡掉。\n\n"
                "顺带一条数据层的教训：批量接口在频控下会**静默少给**（要 20 只只回 10 只，"
                "返回码仍是 0）。脚本现在对缺号单独重试，并把失败如实写进审计文件。"
            ),
        },
        {
            "type": "text",
            "tab": "notes",
            "title": "下一步（按性价比排序）",
            "text": (
                "1. **把基准当策略来做**。等权 + 20 日再平衡拿到 +305%，"
                "而所有选股因子的边际都不超过 +20pp。先把这条基线做实（含成本、含整手、含涨跌停），"
                "它既是基准，也是一条能直接上线的策略。\n"
                "2. **补时点成分股**。没有历史每期真实的中证 800 名单，幸存者偏差无法量化；"
                "这是当前最大的单一未知项。\n"
                "3. **要做选股，先做行业与市值中性化**。现在的十分位是全市场排序，"
                "结果里混着大量行业和市值的暴露，分不清哪部分是真信号。\n"
                "4. 8 年数据判不了因子生死。任何\"这个因子长期有效\"的说法都超出数据支持范围。"
            ),
        },
        {
            "type": "text",
            "tab": "notes",
            "title": "免责声明",
            "text": ("⚠️ 以上内容由 AI 基于公开信息整理生成，仅供参考，不构成任何投资建议或个股推荐。"
                     "投资有风险，决策需谨慎。"),
        },
    ]

    report["modules"].extend(modules)
    report["ui"] = {
        **report["ui"],
        "subtitle": "A 股横截面选股回测 · 中证800 · 预注册双假设",
        "tabs": [
            {"id": "overview", "label": "20日反转（主策略）"},
            {"id": "audit", "label": "稳健性审计"},
            {"id": "notes", "label": "结论与口径"},
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
