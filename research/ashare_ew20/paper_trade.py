#!/usr/bin/env python3
"""paper_trade.py — EW20 本地影子盘（paper trading）

为什么不接券商模拟盘：老虎证券不能交易 A 股（只有沪深港通）；A 股券商模拟盘
需要单独开户与凭据，本脚本不碰任何账户。影子盘用同一套引擎口径在本地记账：

  - 信号：每个调仓日（2019-01-18 起每 20 个交易日）收盘后出单
  - 成交：次日开盘价，按回测同一套费用模型（佣金2.5bp/5元下限 + 印花5bp
    + 过户0.1bp + 滑点5bp），100 股整手
  - 数据：state/ashare/daily（收盘后更新），所以"结算"永远是 T+1 的

用法：
    python paper_trade.py status                      # 现在处于周期哪一步
    python paper_trade.py init --capital 10000000     # 开一本 1000 万的影子账
    python paper_trade.py plan                        # 生成下一次调仓订单
    python paper_trade.py settle --date 2026-10-28    # 数据到位后按开盘价成交

账本：state/ashare/ew20_paper_book.json；成交回执：research/ashare_ew20/outputs/ew20_paper_fills.csv

⚠️ 以上内容由 AI 基于公开信息整理生成，仅供参考，不构成任何投资建议或个股推荐。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent       # research/ashare_ew20
PROJ = ROOT.parent.parent
OUT = ROOT / "outputs"
BOOK = PROJ / "state" / "ashare" / "ew20_paper_book.json"
sys.path.insert(0, str(PROJ))
sys.path.insert(0, str(ROOT))

import ew20_backtest as E                    # noqa: E402  复用同一套引擎常量

REBALANCE = E.REBALANCE_BARS
EVAL_START = pd.Timestamp(E.EVAL_START)


# --------------------------------------------------------------------------- #
def load_panel() -> E.Panel:
    return E.load_panel()


def cycle_position(cal: pd.DatetimeIndex) -> dict:
    """当前日历在 20 日网格里的位置。信号日 = 网格日收盘；执行日 = 网格日+1 开盘。"""
    start = int(cal.searchsorted(EVAL_START))
    sigs = list(range(start, len(cal), REBALANCE))
    past = [s for s in sigs if s <= len(cal) - 1]
    last_sig = past[-1]
    next_sig = last_sig + REBALANCE
    n_since = (len(cal) - 1) - last_sig

    def _est_date(n_days: int) -> str:
        """数据末日之后的位置只能估：按工作日推算，节假日未剔除。"""
        d = np.busday_offset(np.datetime64(cal[-1].date()), n_days, roll="forward")
        return str(np.datetime64(d, "D"))

    if next_sig < len(cal):
        next_signal = str(cal[next_sig].date())
        next_exec = str(cal[next_sig + 1].date())
    else:
        k = REBALANCE - n_since
        next_signal = f"≈{_est_date(k)}（估算，节假日未剔除）"
        next_exec = f"≈{_est_date(k + 1)}（估算）"
    return {
        "last_signal": str(cal[last_sig].date()),
        "bars_since_signal": n_since,
        "next_signal": next_signal,
        "next_exec": next_exec,
        "cycle_day": f"{n_since}/{REBALANCE}",
        "n_periods_done": len(past),
    }


def plan_window(cal: pd.DatetimeIndex, sig_idx: int) -> tuple[str, bool]:
    """信号对应的执行日。

    数据里已经有信号日的下一根 bar（回放/补跑）→ 取真实日期；
    数据正好停在信号日（收盘后现场跑的正常情形）→ 只能是下一个交易日，标注估算。
    """
    if sig_idx + 1 < len(cal):
        return str(cal[sig_idx + 1].date()), True
    d = np.busday_offset(np.datetime64(cal[-1].date()), 1, roll="forward")
    return f"≈{np.datetime64(d, 'D')}（估算，节假日未剔除）", False


def target_portfolio(panel: E.Panel, n_names: int | None, capital: float,
                     sig_idx: int) -> tuple[list[dict], dict]:
    """按信号日收盘数据算目标持仓（以次日开盘估股数，实盘以实际开盘价微调）。"""
    n_names = n_names or None          # 账本里存的 0 表示全池
    cal = panel.calendar
    vol = panel.volume
    amount = panel.amount
    liq = amount.rolling(E.Cfg.liq_window, min_periods=E.Cfg.liq_window // 2).mean()
    px_close = panel.close.iloc[sig_idx]
    tradable = (vol.iloc[sig_idx] > 0) & np.isfinite(px_close.values)
    cand = np.where(tradable & np.isfinite(liq.iloc[sig_idx].values))[0]
    codes = list(panel.open.columns)
    if n_names is not None and len(cand) > n_names:
        order = cand[np.argsort(-liq.iloc[sig_idx].values[cand])]
        cand = order[:n_names]

    # 用最近收盘价估每只一手要多少钱，算出最低资金门槛
    lot_cost = {codes[j]: float(px_close.iloc[j]) * E.lot_size(codes[j]) for j in cand}
    min_capital = sum(v for v in lot_cost.values() if np.isfinite(v) and v > 0)

    # 等权目标股数：以执行日开盘价为参考（数据里还没有执行日时，退到信号日收盘价，
    # 比信号日开盘价更接近次日开盘）
    if sig_idx + 1 < len(cal):
        px_exec = panel.open.iloc[sig_idx + 1]
    else:
        px_exec = panel.close.iloc[sig_idx]
    per = capital * (1 - 0.005) / len(cand)
    rows = []
    for j in cand:
        code = codes[j]
        px = float(px_exec.iloc[j])
        if not np.isfinite(px) or px <= 0:
            continue
        size = int(per / px) // E.lot_size(code) * E.lot_size(code)
        if size < E.lot_size(code):
            continue
        rows.append({
            "symbol": code,
            "name": panel.names.get(code, code),
            "est_price": round(px, 3),
            "shares": size,
            "est_value": round(size * px, 2),
            "weight_pct": round(size * px / capital * 100, 3),
        })
    rows.sort(key=lambda r: -r["est_value"])
    info = {
        "n_targets": len(cand),
        "n_affordable": len(rows),
        "min_capital_1lot_each": round(min_capital, 0),
        "capital": capital,
        "est_invested_pct": round(sum(r["est_value"] for r in rows) / capital * 100, 2),
    }
    return rows, info


# --------------------------------------------------------------------------- #
def cmd_status(args: argparse.Namespace) -> int:
    panel = load_panel()
    cal = panel.calendar
    pos = cycle_position(cal)
    print(f"数据末日: {cal[-1].date()}（当日 bar 收盘后才有）")
    print(f"调仓周期: {pos['cycle_day']}  上次信号 {pos['last_signal']} → 执行次日开盘")
    print(f"下次信号: {pos['next_signal']} 收盘 → {pos['next_exec']} 开盘成交")
    if BOOK.exists():
        book = json.loads(BOOK.read_text(encoding="utf-8"))
        print(f"影子账本: 现金 {book['cash']:,.0f} / 持仓 {len(book['holdings'])} 只 / "
              f"净值 {book.get('equity', 0):,.0f}（{book.get('as_of', '--')}）")
        pend = book.get("pending_orders") or []
        if pend:
            print(f"待成交订单: {len(pend)} 笔（run settle --date <数据日> 结算）")
    else:
        print("影子账本: 未建立（run init）")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    BOOK.parent.mkdir(parents=True, exist_ok=True)
    book = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "capital": args.capital,
        "cash": args.capital,
        "holdings": {},          # symbol -> {"shares": int, "cost": float}
        "equity": args.capital,
        "as_of": None,
        "pending_orders": [],
        "fills": 0,
        "config": {"n_names": args.names, "rebalance": REBALANCE,
                   "eval_start": str(EVAL_START.date())},
    }
    BOOK.write_text(json.dumps(book, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[init] 影子账本已建：资金 {args.capital:,.0f}，目标 {args.names or '全池'} 只 → {BOOK}")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    if not BOOK.exists():
        print("[plan] 先 run init"); return 1
    book = json.loads(BOOK.read_text(encoding="utf-8"))
    panel = load_panel()
    cal = panel.calendar
    pos = cycle_position(cal)   # 仅状态展示；订单执行日走 plan_window
    sigs = [i for i in range(int(cal.searchsorted(EVAL_START)), len(cal), REBALANCE)]
    last_sig = sigs[-1]
    rows, info = target_portfolio(panel, book["config"]["n_names"],
                                  args.capital or book["capital"], last_sig)
    exec_day, exec_known = plan_window(cal, last_sig)
    stale = book.get("pending_signal_date") not in (None, str(cal[last_sig].date()))
    print(f"信号日 {cal[last_sig].date()}（收盘后生效）→ 执行日 {exec_day} 开盘")
    if stale:
        print(f"    旧挂单按 {book.get('pending_signal_date')} 信号生成，本次整单覆盖")
    print(f"目标 {info['n_targets']} 只，按当前价整手可成交 {info['n_affordable']} 只，"
          f"预计投入 {info['est_invested_pct']}%，一手最低资金门槛 "
          f"{info['min_capital_1lot_each']:,.0f} 元")
    book["pending_orders"] = [
        {"symbol": r["symbol"], "side": "buy", "shares": r["shares"],
         "plan_for": exec_day, "est_price": r["est_price"]}
        for r in rows
    ]
    book["pending_signal_date"] = str(cal[last_sig].date())
    BOOK.write_text(json.dumps(book, ensure_ascii=False, indent=2), encoding="utf-8")
    os.makedirs(OUT, exist_ok=True)
    orders_path = OUT / "ew20_paper_orders.csv"
    pd.DataFrame(book["pending_orders"]).to_csv(orders_path, index=False, encoding="utf-8-sig")
    print(f"[plan] {len(book['pending_orders'])} 笔买单已写入账本，等 {exec_day} 数据到位后结算")
    print(f"[plan] 订单清单: {orders_path}")
    return 0


def cmd_settle(args: argparse.Namespace) -> int:
    if not BOOK.exists():
        print("[settle] 先 run init"); return 1
    book = json.loads(BOOK.read_text(encoding="utf-8"))
    pend = book.get("pending_orders") or []
    if not pend:
        print("[settle] 没有待成交订单"); return 0
    panel = load_panel()
    cal = panel.calendar
    if pd.Timestamp(args.date) not in cal:
        print(f"[settle] {args.date} 不在交易日历里（数据还没落地？）"); return 1
    idx = int(cal.get_loc(pd.Timestamp(args.date)))
    op = panel.open.iloc[idx]
    vol = panel.volume.iloc[idx]
    cost = E.CostModel()
    fills, skipped = [], []
    for o in pend:
        code = o["symbol"]
        px = float(op.get(code, np.nan))
        if not np.isfinite(px) or not np.isfinite(vol.get(code, np.nan)) or vol.get(code, 0) <= 0:
            skipped.append(code); continue
        value = o["shares"] * px
        fee = cost.fee(value, "buy")
        if value + fee > book["cash"]:
            # 钱不够就整手往下减
            lot = E.lot_size(code)
            sh = o["shares"]
            while sh >= lot and sh * px + cost.fee(sh * px, "buy") > book["cash"]:
                sh -= lot
            if sh < lot:
                skipped.append(code); continue
            o["shares"] = sh
            value = sh * px
            fee = cost.fee(value, "buy")
        book["cash"] -= value + fee
        h = book["holdings"].setdefault(code, {"shares": 0, "cost": 0.0})
        h["shares"] += o["shares"]
        h["cost"] += value + fee
        fills.append({**o, "fill_price": round(px, 4), "fee": round(fee, 2),
                      "value": round(value, 2), "date": args.date})
        book["fills"] += 1
    book["pending_orders"] = []
    mv = 0.0
    cl = panel.close.ffill().iloc[idx]
    for code, h in book["holdings"].items():
        px = float(cl.get(code, np.nan))
        mv += h["shares"] * (px if np.isfinite(px) else h["cost"] / max(1, h["shares"]))
    book["equity"] = round(book["cash"] + mv, 2)
    book["as_of"] = args.date
    BOOK.write_text(json.dumps(book, ensure_ascii=False, indent=2), encoding="utf-8")

    os.makedirs(OUT, exist_ok=True)
    fills_path = OUT / "ew20_paper_fills.csv"
    pd.DataFrame(fills).to_csv(fills_path, index=False, mode="a",
                               header=not fills_path.exists())
    print(f"[settle] {args.date} 成交 {len(fills)} 笔，跳过 {len(skipped)} 笔"
          f"（停牌/缺价: {skipped[:5]}{'...' if len(skipped) > 5 else ''}）")
    print(f"[settle] 现金 {book['cash']:,.0f} / 持仓 {len(book['holdings'])} 只 / "
          f"净值 {book['equity']:,.0f}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="EW20 本地影子盘")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="周期位置与账本状态")
    p = sub.add_parser("init", help="建立影子账本")
    p.add_argument("--capital", type=float, required=True)
    p.add_argument("--names", type=int, default=0, help="0=全池")
    p = sub.add_parser("plan", help="生成下一次调仓订单")
    p.add_argument("--capital", type=float, default=None)
    p = sub.add_parser("settle", help="数据到位后按开盘价结算待成交订单")
    p.add_argument("--date", required=True)
    args = ap.parse_args()

    return {"status": cmd_status, "init": cmd_init,
            "plan": cmd_plan, "settle": cmd_settle}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
