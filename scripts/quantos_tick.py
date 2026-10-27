#!/usr/bin/env python
"""
QuantOS 2.0 — 一拍一进程（B10 / T-1004）
=======================================

给 cron / launchd 调用的入口。为什么不让调度器常驻自己定时：

1. **常驻进程会被杀**，而"杀在哪一步"决定了账本是否落后于现实。一拍一进程
   把状态全部放在盘上（账本 + 排期 claim），杀掉之后下一拍从盘上继续，
   没有"内存里还有半拍"这种中间态。
2. **cron 的失败是可见的**：退出码非 0 就是失败，日志、告警、监控都能接。
   常驻进程的 `while True: sleep(60)` 崩了，只要没人看，就等于"今天没跑"。
3. **单实例由锁保证**，不由"进程是否还在"保证。上一拍卡住不退出时，
   这一拍拿到 `LockBusy` 直接退出（退出码 3），不会两个进程同时动同一个账户。

用法
====
    # 每交易日 15:35 推一拍（A 股收盘 15:00 + 结算延迟）
    35 15 * * 1-5  cd /path/to/Stock_quant && .venv/bin/python scripts/quantos_tick.py >> logs/quantos_tick.log 2>&1

    # 只想看会不会跑（不落单、不写账本）
    .venv/bin/python scripts/quantos_tick.py --dry-run

退出码
======
    0  正常：跑了，或没到期（含"今天不是交易日"）
    1  跑完了但有需要人工处理的记录（in_doubt / 连续失败）
    2  拒绝运行：配置错、日历未校准、账本不可用
    3  另一个 tick 正在跑（锁被占）

1 和 2 都会让 cron 的邮件/告警响起来 —— 这是故意的：`REFUSED_IN_DOUBT`
不该安静地滑过去。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from quantos_core.locks import AccountGatewayLock, LockBusy   # noqa: E402
import quantos_schedule as qs                                  # noqa: E402

LOCK_ROOT = ROOT / "state" / "quantos" / "locks"


def _say(message: str) -> None:
    """cron 日志里带时间戳，事后才能对齐"那天为什么没跑"。"""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{stamp}] {message}", flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="QuantOS 一拍一进程（cron 入口）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只判定不执行（仍然会拿锁，验证锁是否通）")
    ap.add_argument("--now", default="", help="ISO 时间；不给用当前时刻")
    ap.add_argument("--nav", default="100000", help="首次建仓的初始资金")
    ap.add_argument("--anchor", default="")
    ap.add_argument("--set", action="append", default=[], metavar="K=V")
    ap.add_argument("--no-lock", action="store_true",
                    help="跳过单实例锁（只给测试用；生产上等于放弃互斥）")
    args = ap.parse_args(argv)

    os.makedirs(LOCK_ROOT, exist_ok=True)
    lock = AccountGatewayLock(str(LOCK_ROOT), qs.ACCOUNT, qs.VENUE)
    owner = None
    if not args.no_lock:
        try:
            owner = lock.acquire()
        except LockBusy as exc:
            # 拿到锁 = 另一个 tick 还活着。这次直接走，下一拍再来。
            _say(f"另一个 tick 正在跑，本拍跳过：{exc}")
            return 3
        _say(f"拿到锁 pid={owner.pid if owner else '?'}")

    tick_argv = ["tick", "--nav", args.nav]
    if args.now:
        tick_argv += ["--now", args.now]
    if args.anchor:
        tick_argv += ["--anchor", args.anchor]
    for pair in args.set:
        tick_argv += ["--set", pair]
    if not args.dry_run:
        tick_argv.append("--execute")

    started = time.time()
    try:
        rc = qs.main(tick_argv)
    finally:
        if owner is not None:
            lock.release()
    elapsed = time.time() - started

    _say(f"{'演练' if args.dry_run else '执行'}结束 rc={rc} 耗时 {elapsed:.1f}s")
    if rc == 0:
        return 0
    if rc == 1:
        _say("有需要人工处理的记录：跑 `quantos_schedule.py status` 看是哪一条")
        return 1
    _say("拒绝运行：先处理上面的报错，再等下一拍")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
