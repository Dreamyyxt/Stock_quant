#!/usr/bin/env python3
"""A 股日线数据体检：把接口返回的残次品挑出来。

为什么要这一步
--------------
westock 接口在频控下会返回**残缺甚至拼接错乱**的表（实测：`sh600809` 2018-11 到
2019-01 的价格掉到 0.38 元、单日涨 2033%，而真实价格约 40 元）。这类数据不会报错，
只会安静地把回测收益放大一个数量级。所以必须有一道体检闸门。

检查项
------
1. 价格非正（前复权出现负价 / 0 价）——直接判废
2. 单日涨跌幅超过该板块涨跌停限制 + 3pp（主板 10%、创业板/科创板 20%）
   ——真实行情不可能发生，判废
3. 同一天重复行、日期非递增 ——判废
4. 有效 bar 少于 300 根 ——样本不足，判废（但记录为 short 而不是 bad）

用法
----
    python scripts/qa_ashare_data.py            # 只体检 + 出报告
    python scripts/qa_ashare_data.py --quarantine   # 把不合格文件移到 rejected/

产物：state/ashare/qa_report.json
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DAILY = ROOT / "state" / "ashare" / "daily"
REJECTED = ROOT / "state" / "ashare" / "rejected"
REPORT = ROOT / "state" / "ashare" / "qa_report.json"

LIMIT_MAIN = 0.10
LIMIT_GROWTH = 0.20
TOL = 0.03           # 涨跌停 + 3pp 容差（新股首日、退市整理期不在此池内）
MIN_BARS = 300


def limit_width(code: str) -> float:
    return LIMIT_GROWTH if (code.startswith("sh688") or code.startswith("sz30")) else LIMIT_MAIN


def check(code: str, df: pd.DataFrame) -> tuple[str, list[str]]:
    """返回 (状态, 问题列表)。状态 ∈ ok / short / bad。"""
    issues: list[str] = []
    if df["date"].duplicated().any():
        issues.append("duplicate_dates")
    if not df["date"].is_monotonic_increasing:
        issues.append("dates_not_sorted")

    price_cols = ["open", "close", "high", "low"]
    if df[price_cols].isna().any().any():
        issues.append("nan_price")
    if (df[price_cols] <= 0).any().any():
        issues.append("non_positive_price")

    r = df["close"].pct_change()
    lim = limit_width(code)
    viol = int((r.abs() > lim + TOL).sum())
    if viol:
        issues.append(f"illegal_daily_moves:{viol}")

    # 当日 open 偏离 close 超过涨跌停幅度，说明 open/close 不同源
    dev = int(((df["open"] / df["close"] - 1.0).abs() > lim + TOL).sum())
    if dev:
        issues.append(f"open_close_mismatch:{dev}")

    if len(df) < MIN_BARS:
        return "short", issues
    return ("bad" if issues else "ok"), issues


def main(quarantine: bool = False) -> int:
    files = sorted(DAILY.glob("*.csv"))
    if not files:
        raise SystemExit(f"[FATAL] {DAILY} 为空")

    ok, short, bad = [], [], []
    detail: dict[str, list[str]] = {}
    for path in files:
        code = path.stem
        df = pd.read_csv(path, parse_dates=["date"])
        for c in ["open", "close", "high", "low"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        status, issues = check(code, df)
        if status == "ok":
            ok.append(code)
        elif status == "short":
            short.append(code)
        else:
            bad.append(code)
            detail[code] = issues

    if quarantine and bad:
        REJECTED.mkdir(parents=True, exist_ok=True)
        for code in bad:
            src = DAILY / f"{code}.csv"
            if src.exists():
                shutil.move(str(src), str(REJECTED / f"{code}.csv"))

    total = len(files)
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total": total,
        "ok": len(ok),
        "short_history": len(short),
        "bad": len(bad),
        "bad_pct": round(len(bad) / total * 100.0, 2),
        "bad_codes": bad,
        "bad_detail": detail,
        "short_codes": short,
        "quarantined": bool(quarantine and bad),
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[qa] 共 {total} 只：合格 {len(ok)}，历史过短 {len(short)}，判废 {len(bad)}"
          f"（{len(bad) / total * 100:.1f}%）")
    for code, issues in list(detail.items())[:15]:
        print(f"      {code}: {', '.join(issues)}")
    if quarantine and bad:
        print(f"[qa] 已隔离到 {REJECTED}")
    print(f"[qa] 报告 {REPORT}")
    return 0 if len(ok) >= 100 else 1


if __name__ == "__main__":
    import sys
    raise SystemExit(main(quarantine="--quarantine" in sys.argv))
