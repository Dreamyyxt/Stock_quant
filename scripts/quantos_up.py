#!/usr/bin/env python
"""
QuantOS 2.0 — 进程编排（B10 / T-1004）
=====================================

四个进程各干什么，以及**现在到底哪些是真的**：

| 进程        | 形态     | 现状                                              |
| ----------- | -------- | ------------------------------------------------- |
| 风控服务    | 常驻     | ✅ 真的在跑（Unix socket，`quantos_core.risk_service`） |
| 运维面      | 常驻     | ✅ 真的在跑（127.0.0.1 HTTP，`quantos_ops_server.py`）  |
| 调度器      | cron     | ✅ 真的在跑（`quantos_tick.py`，每交易日 15:35）        |
| 执行网关    | 常驻     | ❌ **没有**。`VenueGateway` 还只有 ABC + 注入式 CCXT，真实券商适配器是 B11 |

这个模块的第一原则是**不装**：网关没接就显示"未接入"，而不是起一个
什么都不做的空壳进程让表看起来满的。一张四个绿灯的表比一张两个绿灯
加两个红叉的表危险得多 —— 前者会让人以为"下单链路是通的"。

还有一处必须说清楚：**调度器那一拍目前用的是进程内风控引擎**
（`EngineRiskClient`），不是上面那个独立风控服务。独立服务是为生产准备的，
把它接进 tick 路径是 B8 留下的口子，这里只报状态，不假装已经接好。

用法
====
    .venv/bin/python scripts/quantos_up.py --status     # 四行状态，如实
    .venv/bin/python scripts/quantos_up.py --crontab    # 打印 crontab 片段
    .venv/bin/python scripts/quantos_up.py --start      # 起常驻的两个
    .venv/bin/python scripts/quantos_up.py --stop       # 停掉
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "state" / "quantos"
LOGS = ROOT / "logs"

PY = str(ROOT / ".venv" / "bin" / "python")
RISK_SOCK = RUN / "risk.sock"
#: 风控服务自己的状态文件。它与 tick 那条链路用的 `ashare_risk.json` 不是同一个
#: —— 后者是进程内引擎的熔断记忆，前者是独立服务的。名字分开，免得对不上账。
RISK_STATE = RUN / "risk_service.json"
OPS_PORT = 8787

OK, BAD, WARN, GAP = "🟢", "🔴", "🟡", "⚪"


@dataclass
class Proc:
    key: str
    name: str
    form: str                       # daemon / cron / absent
    cmd: List[str]
    pidfile: Path
    probe: Callable[[], Tuple[str, str]]


# --------------------------------------------------------------------------- #
#  探针：真实探测，不读配置猜
# --------------------------------------------------------------------------- #

def probe_risk() -> Tuple[str, str]:
    if not RISK_SOCK.exists():
        return "down", "socket 不存在"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(0.4)
    try:
        sock.connect(str(RISK_SOCK))
        return "up", f"{RISK_SOCK.name} 可连接"
    except OSError as exc:
        return "down", f"{type(exc).__name__}: {exc}"
    finally:
        sock.close()


def probe_ops() -> Tuple[str, str]:
    """
    探运维面。

    必须显式绕开代理：装了 `http_proxy` 的机器上，`urlopen` 会把
    `127.0.0.1` 也送进代理，代理回一个 502，看起来像"服务活着但坏了"。
    一个假绿灯和假红灯都会让人查错方向，所以这里直连。
    """
    url = f"http://127.0.0.1:{OPS_PORT}/api/ops/health"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=0.6) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        ok = bool(payload.get("ok", True))
        return ("up" if ok else "degraded"), f"端口 {OPS_PORT}"
    except urllib.error.HTTPError as exc:
        # 端口有东西在回话，但不是我们的健康检查 —— 别当成"在跑"
        return "wrong", f"端口 {OPS_PORT} 有响应但不健康（HTTP {exc.code}）"
    except urllib.error.URLError as exc:
        return "down", f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}"
    except (OSError, ValueError) as exc:
        return "down", f"{type(exc).__name__}: {exc}"


def probe_scheduler() -> Tuple[str, str]:
    """调度器不是常驻进程 —— 它"活着"的证据是排期文件里最近一次运行时间。"""
    path = RUN / "schedule.json"
    if not path.exists():
        return "idle", "还没有排期文件（从未 tick 过）"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return "down", f"排期文件读不出来：{exc}"
    entries = raw.get("entries") or []
    runs = 0
    last_at = ""
    in_doubt = []
    for item in entries:
        key = str(item.get("key") or f"{item.get('strategy_id')}@{item.get('version')}")
        runs += int(item.get("runs") or 0)
        stamp = str(item.get("last_run_at") or "")
        if stamp > last_at:
            last_at = stamp
        if item.get("claim_day"):
            in_doubt.append(key)
    if in_doubt:
        return "attention", f"卡住待确认 {len(in_doubt)} 条：{', '.join(in_doubt)}"
    if not runs:
        return "idle", "还没跑过"
    return "cron", f"累计 {runs} 拍，最近 {last_at or '-'}"


def probe_gateway() -> Tuple[str, str]:
    """执行网关：B11 才做。这里不给绿灯，也不起空壳。"""
    return "absent", "真实券商网关未接入（B11）；当前成交走 PaperBroker"


PROCS: List[Proc] = [
    Proc("risk", "风控服务", "daemon",
         [PY, "-m", "quantos_core.risk_service", "--socket", str(RISK_SOCK),
          "--state", str(RISK_STATE)],
         RUN / "risk-service.pid", probe_risk),
    Proc("gateway", "执行网关", "absent",
         [], RUN / "gateway.pid", probe_gateway),
    Proc("scheduler", "调度器(tick)", "cron",
         [PY, str(ROOT / "scripts" / "quantos_tick.py")],
         RUN / "tick.pid", probe_scheduler),
    Proc("ops", "运维面", "daemon",
         [PY, str(ROOT / "scripts" / "quantos_ops_server.py"),
          "--host", "127.0.0.1", "--port", str(OPS_PORT)],
         RUN / "ops-web.pid", probe_ops),
]

_MARK = {"up": OK, "cron": OK, "idle": WARN, "attention": BAD,
         "degraded": WARN, "wrong": BAD, "down": BAD, "absent": GAP}


# --------------------------------------------------------------------------- #
#  动作
# --------------------------------------------------------------------------- #

def cmd_status(args) -> int:
    rows = []
    for proc in PROCS:
        state, detail = proc.probe()
        rows.append((proc, state, detail))

    width = max(len(p.name) for p in PROCS)
    for proc, state, detail in rows:
        form = {"daemon": "常驻", "cron": "cron", "absent": "未接入"}[proc.form]
        print(f"{_MARK.get(state, '?')} {proc.name.ljust(width)}  "
              f"[{form}]  {detail}")

    live = sum(1 for _p, s, _d in rows if s in ("up", "cron"))
    gaps = sum(1 for _p, s, _d in rows if s == "absent")
    bad = sum(1 for _p, s, _d in rows if s in ("down", "attention", "wrong"))
    print(f"\n真实在跑 {live} / {len(rows)}" +
          (f"；未接入 {gaps} 个" if gaps else "") +
          (f"；异常 {bad} 个" if bad else ""))

    print("\n说明：")
    print("  · 调度器是 cron 驱动的一拍一进程，不是常驻；「活着」的证据是上次运行时间。")
    print("  · tick 目前仍用**进程内**风控引擎（EngineRiskClient），独立风控服务")
    print("    还没有接进下单链路 —— 这一格是绿灯只代表服务本身健康。")
    if gaps:
        print("  · 执行网关未接入：所有成交来自 PaperBroker，不是真实交易所。")
    if args.json:
        print("\n" + json.dumps(
            [{"key": p.key, "name": p.name, "form": p.form,
              "state": s, "detail": d} for p, s, d in rows],
            ensure_ascii=False, indent=1))

    if args.strict and (bad or gaps):
        return 1
    return 0


def cmd_start(args) -> int:
    LOGS.mkdir(parents=True, exist_ok=True)
    RUN.mkdir(parents=True, exist_ok=True)
    started, skipped = [], []
    for proc in PROCS:
        if proc.form != "daemon":
            continue
        state, _detail = proc.probe()
        if state in ("up", "degraded"):
            skipped.append((proc, state))
            continue
        log = open(LOGS / f"{proc.key}.log", "ab")
        child = subprocess.Popen(
            proc.cmd, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,   # 与当前终端解耦，关窗口不会带走
        )
        proc.pidfile.write_text(str(child.pid), encoding="utf-8")
        started.append((proc, child.pid))

    time.sleep(0.8)                        # 给 socket bind / port listen 一点时间
    for proc, pid in started:
        state, detail = proc.probe()
        print(f"{_MARK.get(state, '?')} 启动 {proc.name} pid={pid} → {state}  {detail}")
    for proc, state in skipped:
        print(f"{OK} {proc.name} 已在运行（{state}），不重复启动")
    if not started:
        print("\n没有需要启动的常驻进程。")
    return 0


def cmd_stop(args) -> int:
    stopped = []
    for proc in PROCS:
        if not proc.pidfile.exists():
            continue
        raw = proc.pidfile.read_text(encoding="utf-8").strip()
        try:
            pid = int(raw)
        except ValueError:
            proc.pidfile.unlink(missing_ok=True)
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            stopped.append((proc, pid))
        except ProcessLookupError:
            print(f"{WARN} {proc.name} pid={pid} 已经不在了，清掉 pidfile")
        except PermissionError as exc:
            print(f"{BAD} {proc.name} pid={pid} 没权限停：{exc}")
        finally:
            proc.pidfile.unlink(missing_ok=True)
    for proc, pid in stopped:
        print(f"{WARN} 已发送 SIGTERM  {proc.name} pid={pid}")
    if not stopped:
        print("没有记录在案的常驻进程。")
    return 0


def cmd_crontab(args) -> int:
    py = PY
    print(f"# QuantOS 2.0 —— 每交易日推进一拍（A 股 15:00 收盘 + 结算延迟）")
    print(f"35 15 * * 1-5  cd {ROOT} && {py} scripts/quantos_tick.py"
          f" >> logs/quantos_tick.log 2>&1")
    print()
    print("# 日终对账（收盘后跑一次，把当日归因与券商快照对平）")
    print(f"10 16 * * 1-5  cd {ROOT} && {py} scripts/quantos_ops_server.py --eod"
          f" >> logs/quantos_eod.log 2>&1")
    print()
    print("# 说明：")
    print("#   · 非交易日 tick 会自己返回 0（日历判定），不需要在 crontab 里排节假日。")
    print("#   · 退出码 1/2 会让 cron 发信，那就是要人看的时候。")
    print("#   · 15:35 不是随便挑的：收盘 15:00 + 30 分钟结算延迟，")
    print("#     早于这个点跑会用上一个交易日的数据。")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="QuantOS 进程编排")
    ap.add_argument("--status", action="store_true", help="看四个进程状态（默认动作）")
    ap.add_argument("--start", action="store_true", help="启动常驻进程")
    ap.add_argument("--stop", action="store_true", help="停止常驻进程")
    ap.add_argument("--crontab", action="store_true", help="打印 crontab 片段")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true",
                    help="有任何异常或未接入就以退出码 1 结束（CI 用）")
    args = ap.parse_args(argv)

    if args.start:
        return cmd_start(args)
    if args.stop:
        return cmd_stop(args)
    if args.crontab:
        return cmd_crontab(args)
    return cmd_status(args)


if __name__ == "__main__":
    raise SystemExit(main())
