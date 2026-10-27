"""
策略包模板 —— 复制这个文件，改成你自己的逻辑就能导入。

导入命令
========
    .venv/bin/python scripts/quantos_strategy_import.py --pack 你的策略.strat.py

契约只有三条
============
1. 顶层有 `MANIFEST`（dict），字段见下面注释，缺一个都会在导入时炸。
2. 顶层有 `compute(frame, ctx)`，返回**信号列表**。
3. 每个信号是 dict：`{"symbol": ..., "weight": ..., "reason": ...}`。

不要做的事（会被 AST 体检当场拒掉）
==================================
· `import os` / `sys` / `subprocess` / `requests` / 任何网络或文件 IO
· `open()` / `eval()` / `exec()` / `__import__()` / `getattr(x, "__class__")`
· 在顶层写 `print(...)` 或调用函数（导入即执行会把调度器搞乱）
· 自己算金额、算股数、决定下单 —— 策略只给权重，钱和单子归别人管

能用的东西
==========
math / statistics / decimal / fractions / operator / datetime / typing /
dataclasses / collections / itertools / functools / enum / string / re
"""

from decimal import Decimal

MANIFEST = {
    # 唯一标识，之后不能改（改了就是另一个策略，历史盈亏接不上）
    "strategy_id": "my_strategy",
    # 语义化版本。代码变了版本要变，否则指纹对不上会被拒
    "version": "v1",
    # 交易场所。必须和行情 frame 的 venue 一致，不一致运行时直接报"场所不匹配"
    "venue": "TIGER",
    # rebalance=到期才调仓；signal=每天都可能给信号
    "kind": "rebalance",
    # 多少天调一次。日频填 1，周频 5，月频 20
    "rebalance_days": 20,
    # 最少需要多少根 bar 才能算。不足时 frame.require() 会抛错，不会拿半截数据凑
    "warmup_bars": 25,
    # 可调参数。low/high 是硬边界，越界报错；
    # required=True 且不给 default 表示必须显式传值（比如风险预算）
    "params": [
        {"name": "top_n", "kind": "int", "low": 1, "high": 100, "default": 20,
         "desc": "取动量前几名"},
        {"name": "weight", "kind": "float", "low": 0.001, "high": 0.20,
         "default": 0.05, "desc": "单标的权重（相对本策略配额，不是占 NAV）"},
        {"name": "lookback", "kind": "int", "low": 5, "high": 250, "default": 20,
         "desc": "动量窗口（交易日）"},
    ],
    "author": "your-name",
    "description": "模板：按 lookback 日动量取前 top_n 等权",
}


def compute(frame, ctx):
    """
    frame: 只读行情。`frame.symbols()` 拿标的，`frame.require(sym, n)` 拿最近 n 根。
           取不到会抛 FrameError —— 这是故意的，缺数据时**不许**算。
    ctx:   `ctx.params`（已校验）、`ctx.positions`（本策略当前持仓数量）、`ctx.as_of`

    返回信号列表。没有信号就返回 `[]`，**不要**返回 None。
    """
    lookback = int(ctx.params["lookback"])
    top_n = int(ctx.params["top_n"])
    weight = ctx.params["weight"]

    ranked = []
    for symbol in frame.symbols():
        # require 会检查 warmup：不足就抛错，而不是拿现有的凑
        series = frame.require(symbol, lookback + 1)
        closes = series.closes()
        momentum = Decimal(closes[-1]) / Decimal(closes[0]) - Decimal(1)
        ranked.append((momentum, symbol))

    # 同分时按代码排序，保证两次跑出来的结果完全一致（可复现）
    ranked.sort(key=lambda item: (-item[0], item[1]))

    out = []
    for momentum, symbol in ranked[:top_n]:
        out.append({
            "symbol": symbol,
            "weight": weight,
            "reason": "mom%d=%s" % (lookback, round(float(momentum), 4)),
        })
    return out
