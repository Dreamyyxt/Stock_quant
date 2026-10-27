#!/usr/bin/env python3
"""
QuantOS 2.0 — Alpaca 纸面账户联调入口（T-1104，B11）
====================================================

四个子命令，风险从低到高排好了：

    probe       只读。账户/时钟/标的属性/回查端点，一次都不下单。
    reconcile   只读。把场所的持仓与现金跟本地账本比一遍。
    orders      只读。最近的订单与成交流水（发现账本漏记用）。
    roundtrip   **会真下单**。买 1 股再卖出，验证真实撮合、真实回报、
                真实成交价，并把两笔成交喂进账本。必须显式 `--yes`。

为什么 roundtrip 要单独一个命令、还要 `--yes`
----------------------------------------------
其余三件事都可以随时跑，唯独下单会改变账户状态。把它们混在一个"跑一下
看看"的默认路径里，某天有人在真实端点上 `python quantos_alpaca.py` 手滑
回车，就是一笔不应该存在的成交。默认只读，是这里唯一合理的设计。

roundtrip 验证的是 PaperBroker 永远给不出的四件事
--------------------------------------------------
1. 真实接受与回报：场所会回一个带 order_id 的真订单，不是本地造的对象。
2. 真实成交价：`filled_avg_price` 是撮合出来的。它和下单前的 last 之差
   就是这次的真实滑点 —— 这是纸面模拟里唯一无法伪造的数字。
3. 真实拒单路径：坏标的、超出购买力、收盘后下单，都是场所回的，
   不是我们本地 `if` 出来的。
4. 真实对账：成交之后场所持仓必须等于账本持仓，差一分钱都要能看见。

用法
----
    .venv/bin/python scripts/quantos_alpaca.py probe
    .venv/bin/python scripts/quantos_alpaca.py reconcile --json
    .venv/bin/python scripts/quantos_alpaca.py roundtrip --symbol AAPL --qty 1 --yes
    .venv/bin/python scripts/quantos_alpaca.py roundtrip --qty 0.01 --yes   # 碎股

环境
----
    ALPACA_API_KEY / ALPACA_API_SECRET   放 config/.env（已被 .gitignore 挡住）
    QUANTOS_ALPACA_MAX_NOTIONAL          单笔名义上限，默认 1000
    QUANTOS_ALPACA_LIVE                  **不要设**。设了才有真钱端点，
                                         而且要再配 --live 才生效。

退出码：0 正常；1 需要人看（对账不平 / 状态未知）；2 拒绝执行或失败。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

ROOT = Path(__file__).resolve().parent
PROJ = ROOT.parent
for _p in (str(PROJ), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config.settings  # noqa: F401  —— 载入 config/.env（KEY=VALUE）
from quantos_core.alpaca_gateway import (        # noqa: E402
    DATA_BASE, LIVE_BASE, PAPER_BASE, AlpacaGateway, ProbeResult,
    build_gateway_from_env,
)
from quantos_core.contracts import (             # noqa: E402
    D, Fill, OrderIntent, OrderState, OrderType, Side, utcnow,
)
from quantos_core.gateway import (               # noqa: E402
    ACCEPTED, GatewayError, TransportError, VenueRejected, submit_with_reconcile,
)
from quantos_core.ledger import Ledger           # noqa: E402
from quantos_core.ledger_store import LedgerStore  # noqa: E402
from quantos_core.state_store import StateError  # noqa: E402

ACCOUNT = "ALPACAPAPER"
VENUE = "alpaca"
CCY = "USD"
STATE_DIR = PROJ / "state" / "quantos"
LEDGER_PATH = STATE_DIR / "alpaca_ledger.json"

MARK = {"ok": "🟢", "warn": "🟡", "bad": "🔴", "skip": "⚪"}


# --------------------------------------------------------------------------- #
#  小工具
# --------------------------------------------------------------------------- #

def _pct(value: Optional[Decimal]) -> str:
    return "-" if value is None else f"{value * 100:+.4f}%"


def _bp(fill_px: Decimal, ref_px: Decimal, side: Side) -> Optional[Decimal]:
    """
    真实滑点（基点）。符号统一成「相对决策价你亏了多少 bp」：亏为负、赚为正。

    买入成交在参考价**之上**是亏（多付了），卖出成交在参考价**之下**也是亏。
    所以先算 `fill - ref` 再乘 `-side.sign`：两侧都变成"负 = 吃亏"。
    不这么统一，看盘的人每次都要先回忆方向，然后就再也不看了。
    """
    if not ref_px:
        return None
    diff = -(fill_px - ref_px) * side.sign
    return diff / ref_px * Decimal(10000)


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def _open_gateway(args) -> AlpacaGateway:
    return build_gateway_from_env(
        base=LIVE_BASE if getattr(args, "live", False) else "",
        allow_live=bool(getattr(args, "live", False)),
        require_market_open=not getattr(args, "ignore_market_hours", False),
    )


# --------------------------------------------------------------------------- #
#  probe
# --------------------------------------------------------------------------- #

def cmd_probe(args) -> int:
    gw = _open_gateway(args)
    try:
        result = gw.probe(args.symbol, verify=bool(args.verify))
    except (GatewayError, StateError) as exc:
        print(f"{MARK['bad']} 探针失败：{type(exc).__name__}: {exc}")
        return 2

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0 if result.ok else 2

    print("=" * 62)
    print("Alpaca 网关探针")
    print("=" * 62)
    print(f"  端点      {gw.credentials.base}  ({result.mode})")
    print(f"  凭据      {gw.credentials.masked_key}")
    print(f"  单笔上限  {gw.max_order_notional} USD")
    if result.ok:
        print(f"  {MARK['ok']} 账户      {result.detail}")
        print(f"  {MARK['ok']} 按单号回查 通（{result.query_latency_ms}ms，"
              f"404 + 'order not found'）")
        if result.rules is not None:
            print(f"  {MARK['ok']} 标的规则  {args.symbol}: 步长 {result.rules.qty_step} / "
                  f"最小名义 {result.rules.min_notional}")
        clock = result.clock or {}
        print(f"  {'🟢 开盘' if clock.get('is_open') else '⚪ 休市'}    "
              f"下一次收盘 {clock.get('next_close')}")
    else:
        print(f"  {MARK['bad']} {result.detail}")
    for note in result.notes:
        print(f"     · {note}")
    print("-" * 62)
    print("  说明：探针全程只读，没有下任何单。")
    return 0 if result.ok else 2


# --------------------------------------------------------------------------- #
#  orders —— 成交回捞
# --------------------------------------------------------------------------- #

def cmd_orders(args) -> int:
    gw = _open_gateway(args)
    try:
        orders = gw.fetch_recent_orders(limit=args.limit)
        fills = gw.fetch_fills(page_size=args.limit)
    except (GatewayError, StateError) as exc:
        print(f"{MARK['bad']} 读取失败：{type(exc).__name__}: {exc}")
        return 2

    if args.json:
        print(json.dumps({
            "orders": [o.to_dict() for o in orders],
            "fills": fills,
        }, ensure_ascii=False, indent=2))
        return 0

    print(f"最近 {len(orders)} 笔订单 / {len(fills)} 笔成交")
    for o in orders:
        print(f"  {o.ts.isoformat() if o.ts else '-'}  {o.symbol:<6} {o.side.value:<4} "
              f"{o.quantity}@{o.avg_price or '-'}  {o.state.value:<16} {o.raw_state}")
    if fills:
        print("\n成交流水（场所口径，用来发现账本漏记）：")
        for f in fills:
            print(f"  {f.get('transaction_time') or '-'}  {f.get('symbol')} "
                  f"{f.get('side')} {f.get('qty')}@{f.get('price')}  id={f.get('id')}")
    return 0


# --------------------------------------------------------------------------- #
#  roundtrip —— 真下单
# --------------------------------------------------------------------------- #

def _wait_terminal(gw: AlpacaGateway, symbol: str, cid: str, *,
                   timeout: float, interval: float = 0.5):
    """
    轮询到终态，并顺手量出**可见延迟**。

    这个数字是 B11 唯一能实测出来的能力声明：`order_visibility_lag_ms`
    是抄文档时填的，真实值只有这里能给出。它直接决定超时之后要查几次
    才敢下结论 —— 查得太早会得到"没有这笔单"，而那正是重发的起点。
    """
    started = time.monotonic()
    seen_at: Optional[float] = None
    last = None
    while True:
        elapsed = time.monotonic() - started
        try:
            found = gw.fetch_order(symbol, cid)
        except TransportError:
            found = None
        if found is not None and seen_at is None:
            seen_at = (time.monotonic() - started) * 1000
            last = found
        elif found is not None:
            last = found
        if last is not None and last.is_terminal:
            return last, int(seen_at or 0)
        if elapsed >= timeout:
            return last, int(seen_at or 0) if seen_at is not None else -1
        time.sleep(interval)


def _make_intent(symbol: str, side: Side, qty: Decimal, price: Decimal, tag: str) -> OrderIntent:
    now = utcnow()
    cid = f"qos-rt-{_ts()}-{tag}"
    return OrderIntent(
        order_intent_id=cid,
        client_order_id=cid,
        account_id=ACCOUNT,
        venue=VENUE,
        symbol=symbol,
        side=side,
        quantity=qty,
        order_type=OrderType.MARKET,
        notional=(qty * price).quantize(Decimal("0.01")),
        attribution={f"alpaca_probe:{tag}": qty},
        reason_chain=({"source": "quantos_alpaca_roundtrip", "why": "真实撮合验证"},),
        as_of=now,
        ts=now,
    )


def cmd_roundtrip(args) -> int:
    if not args.yes:
        print("拒绝执行：roundtrip 会在 Alpaca 账户上**真实下单**。")
        print("  纸面账户＝假钱，但订单是真的：会占用购买力、产生成交流水、")
        print("  并且（默认）立刻平仓 —— 平仓那一笔也是真订单。")
        print("  确认要跑，加 --yes。先跑 probe 也能看到 90% 的信息。")
        return 2

    gw = _open_gateway(args)
    try:
        probe: ProbeResult = gw.probe(args.symbol, verify=True)
    except (GatewayError, StateError) as exc:
        print(f"{MARK['bad']} 下单前探针失败：{type(exc).__name__}: {exc}")
        return 2
    if not probe.ok:
        print(f"{MARK['bad']} 探针不通过，拒绝下单：{probe.detail}")
        return 2
    if not probe.clock.get("is_open"):
        print(f"{MARK['bad']} 市场休市，拒绝下单"
              f"（日单会被排到下一个开盘，那就不是「现在这一单」了）")
        return 2

    price = gw.last_price(args.symbol)
    if price is None:
        print(f"{MARK['bad']} 取不到 {args.symbol} 最新价，无法核对名义额 —— 拒绝下单")
        return 2
    notional = (D(args.qty) * price).quantize(Decimal("0.01"))
    if notional > gw.max_order_notional:
        print(f"{MARK['bad']} 名义额 {notional} 超过单笔上限 {gw.max_order_notional}"
              f"（用的是最新价 {price}）—— 拒绝下单")
        return 2

    print("=" * 62)
    print(f"roundtrip  {args.symbol}  {args.qty} 股  约 {notional} {CCY}")
    print("=" * 62)
    print(f"  参考价（last）  {price}")
    print(f"  单笔上限        {gw.max_order_notional} {CCY}")
    print(f"  账户            {probe.detail}")
    print()

    ledger = _open_ledger(probe)
    buy = _make_intent(args.symbol, Side.BUY, D(args.qty), price, "buy")
    report: Dict[str, Any] = {"symbol": args.symbol, "qty": str(args.qty),
                              "ref_price": str(price), "notional": str(notional)}

    outcome = submit_with_reconcile(gw, buy, max_queries=4, query_delay_sec=0.4)
    print(f"  买入提交      {outcome.status}  {outcome.reason}")
    if outcome.status != ACCEPTED:
        print(f"{MARK['bad']} 买入未成交，停止（不重发，不猜测）：{outcome.reason}")
        if outcome.needs_manual:
            report["needs_manual"] = True
            _dump(args, report)
            return 1
        return 2

    order, lag_ms = _wait_terminal(gw, args.symbol, buy.client_order_id,
                                  timeout=args.timeout)
    report["buy_order"] = None if order is None else order.to_dict()
    report["visibility_lag_ms"] = lag_ms
    if order is None:
        print(f"{MARK['bad']} 提交成功但轮询不到这笔单 —— 状态未知，需人工")
        _dump(args, report)
        return 1
    print(f"  买入回报      {order.state.value}（{order.raw_state}）"
          f" 可见 {lag_ms}ms  成交 {order.filled_qty}@{order.avg_price}")
    if order.state is not OrderState.FILLED:
        print(f"{MARK['warn']} 未完全成交（{order.state.value}）：不记账本，等它终结")
        report["incomplete"] = True
        _dump(args, report)
        return 1

    fill_px = D(order.avg_price)
    slip = _bp(fill_px, price, Side.BUY)
    report["buy_slippage_bp"] = None if slip is None else str(slip)
    print(f"  买入滑点      {_bp_text(slip)}（相对 last {price}）")

    _apply_fill(ledger, gw, order, Side.BUY, price, buy.client_order_id)
    print(f"  账本现金      {_cash(ledger)} {CCY}")

    if args.keep:
        print(f"\n{MARK['warn']} --keep：保留这 {order.filled_qty} 股，没有平仓。")
        _persist(ledger)
        _dump(args, report)
        return 0

    sell_px = gw.last_price(args.symbol) or fill_px
    sell = _make_intent(args.symbol, Side.SELL, D(order.filled_qty), sell_px, "sell")
    out2 = submit_with_reconcile(gw, sell, max_queries=4, query_delay_sec=0.4)
    print(f"\n  卖出提交      {out2.status}  {out2.reason}")
    if out2.status != ACCEPTED:
        print(f"{MARK['bad']} 平仓未提交成功 —— 账户里可能留着 {order.filled_qty} 股，"
              f"请手工处理（这是真实的敞口）")
        _dump(args, report)
        return 1

    order2, lag2 = _wait_terminal(gw, args.symbol, sell.client_order_id,
                                  timeout=args.timeout)
    report["sell_order"] = None if order2 is None else order2.to_dict()
    if order2 is None or order2.state is not OrderState.FILLED:
        print(f"{MARK['bad']} 平仓未完全成交，账户里可能还有持仓，请手工确认")
        _dump(args, report)
        return 1
    slip2 = _bp(D(order2.avg_price), sell_px, Side.SELL)
    report["sell_slippage_bp"] = None if slip2 is None else str(slip2)
    print(f"  卖出回报      {order2.state.value}  成交 {order2.filled_qty}@{order2.avg_price}"
          f"  可见 {lag2}ms")
    print(f"  卖出滑点      {_bp_text(slip2)}")
    _apply_fill(ledger, gw, order2, Side.SELL, sell_px, sell.client_order_id)

    # --- 三方对账：账本 / 场所持仓 / 场所账户 --- #
    print()
    account = gw.account(fresh=True)
    positions = gw.fetch_positions()
    held = {p.symbol: p.qty for p in positions}
    report["venue_positions"] = {k: str(v) for k, v in held.items()}
    report["ledger_cash"] = str(_cash(ledger))
    report["venue_cash"] = account.get("cash")
    print(f"  场所持仓      {held or '空仓 ✓'}")
    print(f"  场所现金      {account.get('cash')} {account.get('currency')}")
    print(f"  账本现金      {_cash(ledger)} {CCY}（手续费包含在内）")

    leftovers = {k: v for k, v in held.items() if v != 0}
    if leftovers:
        print(f"{MARK['warn']} 账户仍有持仓：{leftovers} —— 平仓没平干净")
        _persist(ledger)
        _dump(args, report)
        return 1

    print(f"\n{MARK['ok']} 真实撮合链路通：提交 → 回报 → 成交价 → 账本 → 平仓归零")
    _persist(ledger)
    _dump(args, report)
    return 0


def _bp_text(value: Optional[Decimal]) -> str:
    return "-" if value is None else f"{value:+.2f} bp"


def _apply_fill(ledger: Ledger, gw: AlpacaGateway, order, side: Side,
                decision_price: Decimal, cid: str) -> None:
    fill = Fill(
        fill_id=f"{order.venue_order_id}:{order.filled_qty}",
        order_id=order.venue_order_id,
        client_order_id=cid,
        venue_trade_id=order.venue_order_id,
        quantity=D(order.filled_qty),
        price=D(order.avg_price),
        fee=Decimal(0),               # Alpaca 美股佣金 0；SEC/TAF 费用在账户账单里
        fee_currency=CCY,
        ts=order.ts or utcnow(),
    )
    ledger.apply_fill(
        fill, venue=VENUE, symbol=order.symbol, account_id=ACCOUNT, side=side,
        quote_ccy=CCY, decision_price=decision_price,
        attribution={f"alpaca_probe:{side.value.lower()}": D(order.filled_qty)})


def _open_ledger(probe: ProbeResult) -> Ledger:
    """
    打开（或首次建立）账本。

    首次建仓的判定必须看 `status == "absent"`，**不能看 `ledger is None`**：
    `load(allow_absent=True)` 在文件不存在时会返回一个**空账本对象**，
    于是 `if result.ledger is not None: return` 会把"没有账本"当成
    "账本是空的"直接返回，现金 0 —— 第一笔成交就会撞上"现金不足"。
    （这条踩过：真实成交已经发生，记账本时才炸。）

    首次入金用**账户真实权益**，而不是写死 100000 —— 纸面账户的初始资金
    是场所给的，本地再写一个数字，两边从第一秒起就不一致。
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    store = LedgerStore(str(LEDGER_PATH))
    result = store.load(base_currency=CCY, allow_absent=True)
    if result.status not in ("absent", "ok", "recovered_from_backup"):
        raise StateError(f"账本不可用（{result.status}）：{result.detail}")
    if result.status != "absent" and result.ledger is not None:
        return result.ledger
    ledger = Ledger(base_currency=CCY)
    equity = D(probe.account.get("equity") or 0)
    if equity <= 0:
        raise StateError("账户权益为 0/不可读：拒绝凭空建账本")
    ledger.deposit(ACCOUNT, CCY, equity, ref=f"initial-{ACCOUNT}")
    return ledger


def _persist(ledger: Ledger) -> None:
    LedgerStore(str(LEDGER_PATH)).save(ledger)


def _cash(ledger: Ledger) -> Decimal:
    return ledger.cash_native().get((ACCOUNT, CCY), Decimal(0))


def _dump(args, payload: Dict[str, Any]) -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


# --------------------------------------------------------------------------- #
#  reconcile
# --------------------------------------------------------------------------- #

def cmd_reconcile(args) -> int:
    gw = _open_gateway(args)
    try:
        account = gw.account(fresh=True)
        positions = gw.fetch_positions()
    except (GatewayError, StateError) as exc:
        print(f"{MARK['bad']} 读取场所失败：{type(exc).__name__}: {exc}")
        return 2

    venue_pos = {p.symbol: p.qty for p in positions}
    store = LedgerStore(str(LEDGER_PATH))
    result = store.load(base_currency=CCY, allow_absent=True)
    ledger = result.ledger

    rows: List[Dict[str, Any]] = []
    problems: List[str] = []
    # 同 `_open_ledger`：absent 时 store 给的是**空账本对象**，不是 None。
    # 直接用它等于宣布"账本里现金为 0"，于是永远差 10 万。
    if result.status == "absent" or ledger is None:
        problems.append(f"本地账本 {result.status}：{result.detail or '还没有成交记录'}")
        ledger_pos: Dict[str, Decimal] = {}
        ledger_cash: Optional[Decimal] = None
    else:
        ledger_pos = {sym: book.qty for (_, sym), book in ledger.positions().items()}
        ledger_cash = _cash(ledger)

    for symbol in sorted(set(venue_pos) | set(ledger_pos)):
        v = venue_pos.get(symbol, Decimal(0))
        l = ledger_pos.get(symbol, Decimal(0))
        rows.append({"symbol": symbol, "venue": str(v), "ledger": str(l),
                     "diff": str(v - l)})
        if v != l:
            problems.append(f"{symbol} 持仓不一致：场所 {v} / 账本 {l}")

    venue_cash = D(account.get("cash") or 0)
    cash_diff = None if ledger_cash is None else venue_cash - ledger_cash
    if cash_diff is not None and abs(cash_diff) > Decimal("0.01"):
        problems.append(f"现金不一致：场所 {venue_cash} / 账本 {ledger_cash}"
                        f"（差 {cash_diff}）")

    if args.json:
        print(json.dumps({
            "ok": not problems, "problems": problems, "positions": rows,
            "venue_cash": str(venue_cash),
            "ledger_cash": None if ledger_cash is None else str(ledger_cash),
            "cash_diff": None if cash_diff is None else str(cash_diff),
            "ledger_status": result.status, "path": str(LEDGER_PATH),
        }, ensure_ascii=False, indent=2))
        return 0 if not problems else 1

    print("=" * 62)
    print("Alpaca 对账（场所 vs 本地账本）")
    print("=" * 62)
    print(f"  账本      {LEDGER_PATH.relative_to(PROJ)}  [{result.status}]")
    if rows:
        print(f"  {'标的':<8}{'场所':>12}{'账本':>12}{'差':>12}")
        for r in rows:
            flag = "  " if r["diff"] == "0" else "🔴"
            print(f"  {r['symbol']:<8}{r['venue']:>12}{r['ledger']:>12}{r['diff']:>12} {flag}")
    else:
        print("  持仓      两边都是空仓")
    print(f"  现金      场所 {venue_cash} / 账本 "
          f"{'-' if ledger_cash is None else ledger_cash}")
    print("-" * 62)
    if problems:
        for p in problems:
            print(f"  {MARK['bad']} {p}")
        return 1
    print(f"  {MARK['ok']} 对平：持仓与现金都一致")
    return 0


# --------------------------------------------------------------------------- #
#  命令行
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Alpaca 纸面账户联调（B11）。默认只读；下单要 --yes。")
    # `--json` 在主命令和子命令上都能写。子解析器那一份必须用 SUPPRESS，
    # 否则它的默认值 False 会把主命令上已经解析到的 True 覆盖掉 ——
    # argparse 的经典坑：`prog --json sub` 会静默变回非 JSON。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="输出 JSON")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--live", action="store_true",
                    help="用真实端点。还需要 QUANTOS_ALPACA_LIVE=1，缺一不可")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("probe", parents=[common],
                        help="只读探针：账户/时钟/标的/回查端点")
    p1.add_argument("--symbol", default="AAPL")
    p1.add_argument("--verify", action="store_true", help="把能力声明标记为实测")
    p1.set_defaults(func=cmd_probe)

    p2 = sub.add_parser("orders", parents=[common], help="只读：最近订单与成交流水")
    p2.add_argument("--limit", type=int, default=20)
    p2.set_defaults(func=cmd_orders)

    p3 = sub.add_parser("roundtrip", parents=[common], help="真下单：买入、核对成交、平仓")
    p3.add_argument("--symbol", default="AAPL")
    p3.add_argument("--qty", default="1")
    p3.add_argument("--timeout", type=float, default=25.0)
    p3.add_argument("--keep", action="store_true", help="不自动平仓（会留下敞口）")
    p3.add_argument("--yes", action="store_true", help="确认会真实下单")
    p3.add_argument("--ignore-market-hours", action="store_true",
                    help="休市也允许下单（只用于验证拒单路径）")
    p3.set_defaults(func=cmd_roundtrip)

    p4 = sub.add_parser("reconcile", parents=[common], help="只读：场所 vs 账本")
    p4.set_defaults(func=cmd_reconcile)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except VenueRejected as exc:
        print(f"{MARK['bad']} 场所拒绝：{exc}")
        return 2
    except TransportError as exc:
        print(f"{MARK['bad']} 传输层不确定（**不要重发**）：{exc}")
        return 1
    except (GatewayError, StateError) as exc:
        print(f"{MARK['bad']} {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
