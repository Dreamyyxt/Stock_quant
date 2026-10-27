#!/usr/bin/env python3
"""抓取 A 股横截面标的池日线（前复权），落盘为每股一个 CSV。

用途：给 A 股横截面因子检验（cross-section lab）准备数据。
数据源：westock CLI（腾讯自选股行情接口）。

设计要点
--------
1. 标的池取中证 800（沪深300 + 中证500）当前成分股。**这是幸存者偏差的来源**：
   今天还在指数里的股票，天生就是"活下来并且做大了"的那批。回测结果因此偏乐观，
   脚本在审计里显式记一笔，报告里必须披露，不能拿它当预期收益。
2. 批次拉取，每批 25 只 × 2000 根，失败批次重试 2 次并把失败代码写进审计。
3. **剔除当日未收盘 bar**：盘中拉数据时最后一根是半根 K 线，进回测就是未来函数的反面
   （用不完整的当日收盘价做决策）。按 Asia/Shanghai 当天日期整行删掉。
4. 幂等：默认跳过已存在的 CSV；`--refresh` 才重拉。
5. **不复权**（`--fq nofq`），这是踩坑之后的决定：接口的 qfq 序列不是单调缩放，
   而是带台阶的——实测 600519 在 2018-10-29（真实跌停 -10.00%）被 qfq 算成 -18.14%，
   600809 的 2018-11~2019-01 段更是掉到 0.38 元、单日涨 2033%。用 qfq 做因子的结果是
   凭空多出一堆假暴跌。不复权的代价是除权除息日的价格缺口被当成真实下跌（A 股年化
   股息率约 1.5–2%），这是**已知且可披露**的偏差，比不可预测的假数据好得多。
   同时保存接口给的 `change_pct`，用它交叉校验本地算出来的收益率。

产物
----
state/ashare/daily/<code>.csv      每股一份（date,open,high,low,close,volume,amount）
state/ashare/fetch_audit.json      审计：请求/成功/失败/剔除的当日 bar
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "state" / "ashare" / "daily"
AUDIT = ROOT / "state" / "ashare" / "fetch_audit.json"
WESTOCK = Path.home() / ".local" / "bin" / "westock"

POOLS = {"sh000300": "沪深300", "sh000905": "中证500"}
BATCH = 10          # 一次只发一个内部子请求。接口把 20 只拆成 2 个子请求，第二个
                    # 必然撞 LOCAL_RATE_LIMITED —— 于是每批稳定丢一半，且不报错。
LIMIT = 2000
RETRIES = 4
THROTTLE = 8.0      # 批间隔（秒）；实测 6 秒仍会被拒，8 秒基本稳定
ADJUST = "nofq"     # 不复权。理由见文件头第 5 条


def run(cmd: list[str]) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} -> rc={r.returncode}\n{r.stderr[:800]}")
    return r.stdout


def pool_codes() -> tuple[list[str], dict[str, str], dict[str, str]]:
    """返回 (去重后的代码列表, code->name, code->所属指数)。"""
    codes: list[str] = []
    names: dict[str, str] = {}
    member: dict[str, str] = {}
    for idx, label in POOLS.items():
        out = run([str(WESTOCK), "index", "constituent", idx, "--limit", "0"])
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) < 2:
                continue
            code, name = cells[0], cells[1]
            if not (code.startswith("sh") or code.startswith("sz") or code.startswith("bj")):
                continue
            if code not in names:
                codes.append(code)
                names[code] = name
                member[code] = label
    return codes, names, member


def parse_batch(md: str) -> dict[str, pd.DataFrame]:
    """解析批量 K 线 Markdown 表，返回 code -> DataFrame。

    表头形如 | code | date | open | last | high | low | volume | amount | exchange | change_pct |
    `last` 是收盘价；`exchange` 实际是换手率，这里丢弃。
    """
    frames: dict[str, list[dict]] = {}
    header: list[str] | None = None
    for line in md.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if header is None:
            if cells and cells[0] == "code" and "date" in cells:
                header = cells
            continue
        if len(cells) != len(header):
            continue
        if set(cells[0]) <= {"-", " "}:
            continue
        row = dict(zip(header, cells))
        code = row["code"]
        if not code:
            continue
        frames.setdefault(code, []).append(row)

    out: dict[str, pd.DataFrame] = {}
    for code, rows in frames.items():
        df = pd.DataFrame(rows)
        keep = ["date", "open", "last", "high", "low", "volume", "amount", "change_pct"]
        for c in keep:
            if c not in df.columns:
                df[c] = None
        df = df[keep].rename(columns={"last": "close", "change_pct": "chg_pct"})
        for c in ["open", "close", "high", "low", "volume", "amount", "chg_pct"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date", "close"]).sort_values("date")
        # 剔除半根 bar 与异常行
        df = df[df["close"] > 0]
        out[code] = df.reset_index(drop=True)
    return out


def sanitize_prices(df: pd.DataFrame) -> pd.DataFrame:
    """清理停牌行。

    A 股停牌日在行情接口里表现为 `open=high=low=0`、`close` 沿用前收、`volume=0`。
    这种行直接喂进回测，等于在"价格为 0"的地方成交，或者让开平价比值变成无穷。
    处理方式：价格补成收盘价，同时把 volume 置为 NaN —— 这样引擎的
    `isfinite(volume)` 检查会把它当不可交易日挡掉，语义正确。
    close<=0 的行无法修复，直接删。
    """
    out = df.copy()
    c = pd.to_numeric(out["close"], errors="coerce")
    out = out.loc[np.isfinite(c) & (c > 0)].reset_index(drop=True)
    if out.empty:
        return out
    for col in ("open", "high", "low"):
        v = pd.to_numeric(out[col], errors="coerce")
        bad = ~np.isfinite(v) | (v <= 0)
        if bad.any():
            out.loc[bad, col] = out.loc[bad, "close"]
            out.loc[bad, "volume"] = np.nan
    return out


def to_adjusted(df: pd.DataFrame) -> pd.DataFrame:
    """用接口给的日收益率把不复权价还原成「含股息的复权价」。

    为什么绕这一圈：接口的 qfq 序列本身是坏的（带台阶），但它的 `change_pct`
    是**正确**的含权收益率——实测 600519 在 2024-06-19 除息日 raw 收益 -1.35%，
    change_pct 却是 +0.70%，差额 2.05% 正好是当年股息率。所以只要拿 change_pct
    复合出净值，再反推每日复权因子，就能得到一条自洽的复权价：

        close_adj[t] = close_raw[t] * factor[t]
        close_adj[t]/close_adj[t-1] = (1 + r_raw) * (factor[t]/factor[t-1]) = 1 + chg[t]

    因子锚定最后一天 = 1（即最新一日复权价 = 真实价）。
    """
    out = df.copy().reset_index(drop=True)
    r_raw = out["close"].pct_change()
    chg = out["chg_pct"] / 100.0
    # 缺 change_pct 的行按 raw 收益处理
    chg = chg.where(chg.notna(), r_raw)
    step = (1.0 + chg) / (1.0 + r_raw)
    # 关键防线：接口偶尔给出坏行（停牌日 change_pct 错位、新股首日无涨跌幅限制），
    # 单条 step=0 就会把 cumprod 之前的所有价格压成 0，整只票被判废。
    # 现实里没有任何公司行为能让一天的复权因子变化超过 ±50%，超出即视为脏行。
    step = step.replace([np.inf, -np.inf], np.nan).fillna(1.0).clip(0.5, 2.0)
    factor = step.cumprod()
    factor = factor / factor.iloc[-1]
    for c in ["open", "close", "high", "low"]:
        out[c] = out[c] * factor
    out["adj_factor"] = factor
    out["chg_pct"] = chg * 100.0
    return out


def limit_width(code: str) -> float:
    return 0.20 if (code.startswith("sh688") or code.startswith("sz30")) else 0.10


def drop_illegal_moves(code: str, df: pd.DataFrame, max_drop: int = 5) -> tuple[pd.DataFrame, int]:
    """剔除单日涨跌幅超出涨跌停 +2pp 的行。

    这类行绝大多数是真实的公司行为（长期停牌复牌、ST 摘帽、退市整理期首日），
    少数是新股上市首日（无涨跌幅限制）。它们只有寥寥几根，**删行**比删整只票合理：
    删整只会让标的池系统性偏向"没出过事"的股票，那才是真偏差。
    超过 max_drop 根说明这只票的序列本身不可信，返回 -1 交给上层整只剔除。
    """
    chg = df["chg_pct"] / 100.0
    bad = chg.abs() > limit_width(code) + 0.02
    n = int(bad.sum())
    if n == 0:
        return df, 0
    if n > max_drop:
        return df, -1
    return df.loc[~bad].reset_index(drop=True), n


def is_sane(code: str, df: pd.DataFrame) -> tuple[bool, str]:
    """写盘前先拦一道。

    过了 to_adjusted 之后，`chg_pct` 就是含权日收益，所以只剩两类硬伤：
    (a) 价格非正 / 日期重复 —— 表被拼坏了；
    (b) |单日收益| 超过涨跌停 + 2pp —— 长期停牌复牌、ST 变盘、退市整理期之类，
        这类缺口是真实的，但会污染 20/120 日动量，这里整只剔除并如实计数。
    """
    if df.empty:
        return False, "empty"
    cols = ["open", "close", "high", "low"]
    if (df[cols] <= 0).any().any():
        return False, "non_positive_price"
    if df["date"].duplicated().any():
        return False, "duplicate_dates"
    chg = df["chg_pct"] / 100.0
    if (chg.abs() > limit_width(code) + 0.02).any():
        return False, "gap_or_corporate_action"
    return True, ""


def fetch_batch(codes: list[str]) -> tuple[dict[str, pd.DataFrame], str]:
    """拉一批；返回 (解析结果, 错误描述)。

    注意：接口频控时 CLI 仍然 rc=0，只把 `[LOCAL_RATE_LIMITED]` 写在正文里。
    所以不能只看 returncode，必须看解析结果是否为空。
    """
    last_err = ""
    for attempt in range(RETRIES + 1):
        try:
            md = run([str(WESTOCK), "kline", ",".join(codes),
                      "--period", "day", "--limit", str(LIMIT), "--fq", ADJUST])
            frames = parse_batch(md)
            if frames:
                return frames, ""
            head = " ".join(md.split())[:200]
            last_err = f"empty payload: {head}"
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)[:300]
        if attempt < RETRIES:
            time.sleep(THROTTLE * (attempt + 1) * 2)   # 12s / 24s / 36s 退避
    return {}, last_err


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="忽略本地缓存重拉")
    ap.add_argument("--batch", type=int, default=BATCH)
    ap.add_argument("--codes", default="", help="只补这些代码（逗号分隔），配合 --refresh 用于修补坏数据")
    ap.add_argument("--include-today", action="store_true",
                    help="收盘后再跑时纳入当天 bar（默认剔除）；覆盖率<90%% 自动回滚")
    args = ap.parse_args()

    if not WESTOCK.exists():
        print(f"[FATAL] 找不到 westock CLI: {WESTOCK}", file=sys.stderr)
        return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    today = pd.Timestamp(datetime.now().date())

    codes, names, member = pool_codes()
    print(f"[pool] 中证800 成分股去重后 {len(codes)} 只（沪深300 {sum(1 for v in member.values() if v == '沪深300')} / "
          f"中证500 {sum(1 for v in member.values() if v == '中证500')}）")

    todo = [c for c in codes if args.refresh or not (OUT_DIR / f"{c}.csv").exists()]
    if args.codes:
        only = [c.strip() for c in args.codes.split(",") if c.strip()]
        todo = [c for c in only if c in set(codes)]
        print(f"[only] 定向补数 {len(todo)} 只")
    print(f"[cache] 已有 {len(codes) - len(todo)} 只，本次需拉 {len(todo)} 只")

    ok, failed, dropped_today, illegal_dropped = 0, [], 0, 0
    rejected: dict[str, str] = {}
    t0 = time.time()
    for i in range(0, len(todo), args.batch):
        chunk = todo[i:i + args.batch]
        frames, err = fetch_batch(chunk)
        if not frames:
            failed.extend(chunk)
            print(f"[batch {i // args.batch + 1}] 失败: {err[:160]}", flush=True)
            time.sleep(THROTTLE * 2)
            continue
        missing = [c for c in chunk if c not in frames]
        # 接口会「部分成功」：一批 10 只只回来 6 只，rc 仍然是 0、正文里也不报错。
        # 不补的话样本会静默少掉一半（这次实测 620 只里丢了 151 只），
        # 而且丢的还集中在中证500 后半段 —— 直接变成隐性选择偏差。
        for attempt in range(3):
            if not missing:
                break
            time.sleep(THROTTLE * (attempt + 1) * 1.5)
            more, _ = fetch_batch(missing)
            if more:
                frames.update(more)
                missing = [c for c in missing if c not in frames]
        failed.extend(missing)
        for code, df in frames.items():
            before = len(df)
            # 默认剔除"当天"bar：盘中跑时最后一根是半根 K 线，进回测等于用不完整
            # 收盘价做决策。收盘后（--include-today）才保留，且事后有覆盖率校验兜底。
            df = df[df["date"] <= today] if args.include_today else df[df["date"] < today]
            dropped_today += before - len(df)
            if len(df) < 300:                      # 太短的历史没法算因子
                rejected[code] = "short_history"
                continue
            # 频控下接口会**静默少给**（茅台一次只返回 497 根），这种当作可重试失败
            if len(df) < 1900 and df["date"].min() <= pd.Timestamp("2018-09-01"):
                failed.append(code)
                continue
            df = sanitize_prices(df)               # 停牌行 open=0 修成收盘价 + volume=NaN
            if len(df) < 300:
                rejected[code] = "short_history"
                continue
            df = to_adjusted(df)                   # 用接口日收益还原含股息复权价
            df, dropped = drop_illegal_moves(code, df)
            if dropped < 0:
                rejected[code] = "too_many_illegal_moves"
                print(f"      判废 {code}: too_many_illegal_moves", flush=True)
                continue
            illegal_dropped += dropped
            sane, why = is_sane(code, df)
            if not sane:                           # 残缺/错位数据直接丢，不留脏数据
                rejected[code] = why
                print(f"      判废 {code}: {why}", flush=True)
                continue
            df.to_csv(OUT_DIR / f"{code}.csv", index=False, float_format="%.4f")
            ok += 1
        done = min(i + args.batch, len(todo))
        print(f"[batch {i // args.batch + 1}/{(len(todo) + args.batch - 1) // args.batch}] "
              f"{done}/{len(todo)} 累计成功 {ok} 失败 {len(failed)} "
              f"耗时 {time.time() - t0:.0f}s", flush=True)
        time.sleep(THROTTLE)      # 频控：批间隔

    # 收盘后纳入当天 bar 的安全阀：数据源没更新完时只有一部分股票有当日行，
    # 这种"半日横截面"会让当天信号凭空偏向有数据的那批股票 —— 宁可退回去不做。
    today_coverage = None
    if args.include_today:
        files = sorted(OUT_DIR.glob("*.csv"))
        with_today = []
        for path in files:
            dates = pd.read_csv(path, usecols=["date"])["date"]
            if (pd.to_datetime(dates) == today).any():
                with_today.append(path)
        today_coverage = round(len(with_today) / max(1, len(files)), 4)
        if today_coverage < 0.90:
            print(f"[guard] 当日 bar 覆盖率 {today_coverage:.1%} < 90%，"
                  f"判定数据源未更新完 → 回滚剔除当日行", flush=True)
            for path in with_today:
                frame = pd.read_csv(path)
                frame = frame[pd.to_datetime(frame["date"]) != today]
                frame.to_csv(path, index=False, float_format="%.4f")
            today_coverage = 0.0
        else:
            print(f"[guard] 当日 bar 覆盖率 {today_coverage:.1%}，"
                  f"已纳入 {today.date()}", flush=True)

    on_disk = len(list(OUT_DIR.glob("*.csv")))
    audit = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "pools": POOLS,
        "requested": len(codes),
        "saved": on_disk,
        "saved_this_run": ok,
        "cached_before": len(codes) - len(todo),
        "failed": failed,
        "failed_count": len(failed),
        "rejected": rejected,
        "rejected_count": len(rejected),
        "dropped_incomplete_today_bars": dropped_today,
        "today_bar_coverage": today_coverage,
        "dropped_illegal_move_rows": illegal_dropped,
        "limit_per_symbol": LIMIT,
        "adjust": f"{ADJUST}+api_return_readjust",
        "survivorship_warning": "标的池为当前中证800成分股，含幸存者偏差",
        "names": {c: names[c] for c in codes},
        "membership": member,
    }
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n[done] 落盘 {on_disk} / 请求 {len(codes)}；本次成功 {ok}，可重试失败 {len(failed)}，"
          f"永久剔除 {len(rejected)}，剔除当日 bar {dropped_today} 根")
    if rejected:
        reasons: dict[str, int] = {}
        for why in rejected.values():
            reasons[why] = reasons.get(why, 0) + 1
        print(f"[qa] 剔除原因: {reasons}")
    if failed:
        print(f"[warn] 待重试: {','.join(failed[:20])}{' ...' if len(failed) > 20 else ''}")
    print(f"[audit] {AUDIT}")
    return 0 if on_disk > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
