#!/usr/bin/env python3
"""
策略包导入体检 —— 一条命令回答"这个收集来的策略能不能进平台"。

用法
====
    # 用真实 A 股日线做干跑（默认取 state/ashare/daily）
    .venv/bin/python scripts/quantos_strategy_import.py --pack research/strategy_packs/ashare_ew20.strat.py

    # 改参数再验一遍
    .venv/bin/python scripts/quantos_strategy_import.py --pack x.strat.py --set top_n=10 --set lookback=60

    # 只给结论，不要明细（给 CI / 批量导入用）
    .venv/bin/python scripts/quantos_strategy_import.py --pack x.strat.py --json

为什么要单独一个命令
====================
`load_pack()` 是库，库的问题是"没人主动调就不会跑"。收集来的策略需要一个
**一眼能看懂是过还是不过**的入口，否则会变成"先扔进目录，以后再验"，
而"以后再验"通常等于"上真钱之前没验"。

退出码
======
0 通过（可以进 RESEARCH 阶段）/ 1 体检不通过 / 2 用法或数据问题
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from quantos_core.strategy_api import MarketFrame, Series  # noqa: E402
from quantos_core.strategy_pack import (  # noqa: E402
    PackRejected, load_pack,
)

DEFAULT_DATA = ROOT / "state" / "ashare" / "daily"


def load_csv_series(data_dir: Path, limit: int, bars: int,
                    min_bars: int) -> Tuple[Dict[str, Series], Optional[date]]:
    """
    从日线 CSV 读 `bars` 根收盘价。

    口径：`close` 列约定为**前复权**价（见 docs/QuantOS/02 附录 G.1）。
    读到 `open=0` 的停牌行直接跳过 —— 那种行的收盘价是补出来的，
    拿它算收益等于凭空插入一段"价格没动"的假历史。
    """
    files = sorted(data_dir.glob("*.csv"))[:limit]
    series: Dict[str, Series] = {}
    last_day: Optional[date] = None
    for path in files:
        points: List[Tuple[date, Decimal]] = []
        try:
            with open(path, encoding="utf-8") as fh:
                fh.readline()
                for line in fh:
                    parts = line.rstrip("\n").split(",")
                    if len(parts) < 6:
                        continue
                    opn, close = parts[1], parts[2]
                    if opn in ("0", "0.0", ""):
                        continue          # 停牌行
                    try:
                        points.append((date.fromisoformat(parts[0]),
                                       Decimal(close)))
                    except Exception:                       # noqa: BLE001
                        continue
        except OSError:
            continue
        if len(points) < min_bars:
            continue
        series[path.stem] = Series(path.stem, tuple(points[-bars:]))
        day = points[-1][0]
        last_day = day if last_day is None or day > last_day else last_day
    return series, last_day


def build_frame(data_dir: Path, venue: str, limit: int, bars: int,
                min_bars: int) -> MarketFrame:
    if not data_dir.is_dir():
        raise SystemExit(f"数据目录不存在：{data_dir}\n"
                         "（策略包本身不取数，行情必须由外部喂进来）")
    series, last_day = load_csv_series(data_dir, limit, bars, min_bars)
    if not series or last_day is None:
        raise SystemExit(f"{data_dir} 里没有可用的行情（需要至少 {min_bars} 根 bar）")
    # A 股收盘 15:00（北京时间）。用 UTC 表示，避免无时区时间被判为无效。
    as_of = datetime(last_day.year, last_day.month, last_day.day,
                     7, 0, tzinfo=timezone.utc)
    return MarketFrame(as_of=as_of, venue=venue, series=series)


def parse_sets(pairs: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for item in pairs:
        if "=" not in item:
            raise SystemExit(f"--set 需要 key=value 形式，收到 {item!r}")
        key, value = item.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="策略包导入体检")
    ap.add_argument("--pack", required=True, help="策略包路径（*.strat.py）")
    ap.add_argument("--data-dir", default=str(DEFAULT_DATA),
                    help="日线 CSV 目录（默认 state/ashare/daily）")
    ap.add_argument("--venue", default="TIGER", help="场所（必须与包内 MANIFEST 一致）")
    ap.add_argument("--symbols", type=int, default=8, help="取多少只标的做干跑")
    ap.add_argument("--bars", type=int, default=120, help="每只取多少根 bar")
    ap.add_argument("--min-bars", type=int, default=30, help="少于这么多根的标的直接跳过")
    ap.add_argument("--set", action="append", default=[], metavar="K=V",
                    help="覆盖参数，可重复")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    args = ap.parse_args()

    params = parse_sets(args.set)
    pack_path = Path(args.pack)
    if not pack_path.is_file():
        print(f"策略包不存在：{pack_path}", file=sys.stderr)
        return 2

    frame = build_frame(Path(args.data_dir), args.venue, args.symbols,
                        args.bars, args.min_bars)

    try:
        pack = load_pack(str(pack_path), dry_frame=frame, dry_params=params)
    except PackRejected as exc:
        if args.json:
            print(json.dumps({"ok": False, "reasons": [str(exc)]},
                             ensure_ascii=False, indent=1))
        else:
            print("❌ 导入被拒\n")
            for line in str(exc).split("；"):
                print(f"  · {line}")
        return 1

    payload = pack.to_dict()
    payload.update({
        "ok": True,
        "resolved_params": {k: str(v) for k, v in
                            pack.manifest.resolve_params(params).items()},
        "frame": {"as_of": frame.as_of.isoformat(), "venue": frame.venue,
                  "symbols": list(frame.symbols())},
        "signals": [{"symbol": s.symbol, "weight": str(s.suggested_weight),
                     "score": str(s.score), "reason": s.reason}
                    for s in pack.dry_signals],
    })

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        return 0

    m = pack.manifest
    print("✅ 体检通过\n")
    print(f"  策略      {m.strategy_id} {m.version}（{m.author or '未署名'}）")
    print(f"  指纹      {pack.fingerprint}  ← 注册时会写成 code_commit")
    print(f"  场所      {m.venue}    调仓周期 {m.rebalance_days} 天    "
          f"warmup {m.warmup_bars} 根")
    if m.description:
        print(f"  说明      {m.description}")
    print(f"  参数      {payload['resolved_params']}")
    print(f"  行情      {frame.as_of:%Y-%m-%d} · {len(frame.symbols())} 只 · "
          f"{args.venue}")
    print(f"\n  干跑信号 {len(pack.dry_signals)} 条：")
    for s in pack.dry_signals[:20]:
        print(f"    {s.symbol:12s} 权重 {s.suggested_weight:>6}  {s.reason}")
    if len(pack.dry_signals) > 20:
        print(f"    …… 还有 {len(pack.dry_signals) - 20} 条")
    if pack.warnings:
        print("\n  ⚠️  告警：")
        for w in pack.warnings:
            print(f"    · {w}")
    print("\n  下一步：这个包只能进 RESEARCH 阶段。"
          "要上模拟盘必须走 BACKTEST→PAPER，材料齐全（R-S03）；")
    print("           直接提到 LIVE 会被 `StrategyRegistry.promote()` 拒 —— 这是设计。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
