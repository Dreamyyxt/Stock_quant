#!/usr/bin/env python
"""
QuantOS 2.0 — 一把跑完全部验收
=============================

顺序是有意的：先契约（B0），再数据（B1），再执行与故障（B2/T-205），
再运维中心（B3 + T-302 抽检），再实盘就绪（B4）、多场所（B5），最后多策略与 AI 研究（B6）。
后面依赖前面的正确性，前一套挂了就没必要看后面。

退出码 0 表示全部通过。可以直接挂到 CI 或每日自检。
"""

from __future__ import annotations

import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SUITES = [
    ("B0 契约与账本", "quantos_selftest.py"),
    ("B1 数据层与回放", "quantos_selftest_b1.py"),
    ("B2 风控独立化与幂等执行", "quantos_selftest_b2.py"),
    ("T-205 故障注入", "quantos_fault_inject.py"),
    ("B3 运维中心（指标/真实数据源/策略实例）", "quantos_selftest_b3.py"),
    ("T-302 接口与进程内抽检", "quantos_ops_check.py"),
    ("B4 实盘就绪（三级开关/日终对账/归因日报）", "quantos_selftest_b4.py"),
    ("B5 多场所（网关/能力矩阵/资金费/调度）", "quantos_selftest_b5.py"),
    ("B6 多策略与 AI 研究（风险预算/灰度/AI 隔离）", "quantos_selftest_b6.py"),
    ("B7 估计层与公司行动（协方差/分红/拆股/上屏）", "quantos_selftest_b7.py"),
    ("B8 策略运行时（契约/包导入/全链路）", "quantos_selftest_b8.py"),
    ("B9 交易日历与策略排期（锚点/幂等/时间口径）", "quantos_selftest_b9.py"),
    ("B10 账本落盘与进程编排（恢复/幂等/cron）", "quantos_selftest_b10.py"),
    ("B11 真实网关（纸面连通/幂等/撤单两步/上屏）", "quantos_selftest_b11.py"),
    ("B12 回测引擎（面板/撮合/无未来函数/指标）", "quantos_selftest_b12.py"),
    ("B13 因子模型（因子收益/载荷/Σ/自由度）", "quantos_selftest_b13.py"),
]


def main() -> int:
    print("QuantOS 2.0 验收")
    print("=" * 66)
    results = []
    for title, script in SUITES:
        path = os.path.join(_ROOT, "scripts", script)
        print(f"\n▶ {title}  ({script})")
        proc = subprocess.run([sys.executable, path], cwd=_ROOT,
                              capture_output=True, text=True)
        tail = [line for line in proc.stdout.strip().splitlines() if line.strip()]
        for line in tail[-3:]:
            print("  " + line)
        if proc.returncode != 0 and proc.stderr.strip():
            print("  stderr: " + proc.stderr.strip().splitlines()[-1])
        results.append((title, proc.returncode == 0, proc.returncode))

    print("\n" + "=" * 66)
    print("汇总")
    print("-" * 66)
    for title, ok, code in results:
        print(f"  [{'PASS' if ok else 'FAIL':>4}] {title}" + ("" if ok else f"  (exit {code})"))
    passed = sum(1 for _, ok, _ in results if ok)
    print("-" * 66)
    print(f"  {passed}/{len(results)} 套件通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
