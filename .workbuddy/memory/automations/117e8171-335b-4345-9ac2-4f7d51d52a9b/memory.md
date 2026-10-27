# TSLA ORB 影子盘 · 当日收盘结算（自动化执行记录）

## 关键操作经验（每轮先看这段）

- **必须先清过期盘中缓存**：`shadow_session.py::ensure_bars()` 只要
  `state/intraday_cache/TSLA_<date>.csv` 存在就直接复用，不会重抓。盘中跑过一次后
  缓存里是残缺 session，直接重跑永远拿不到完整日。做法：重命名成 `*.csv.partial.bak`
  再跑（保守可回滚）。
- **SIP 权限不足**：Alpaca 账户查询 recent SIP 数据返回 403
  （`subscription does not permit querying recent SIP data`），脚本自动降级 IEX，
  这是预期行为，不是故障。报告里需说明数据源是 IEX。
- 完整常规 session = 390 根 1 分钟 bar（09:30–15:59 ET），脚本阈值 385。
- 命令前缀必须 `set -a && source config/.env && set +a`，否则拿不到 ALPACA_* 凭据。

## 执行历史

| 日期 | 结果 |
| --- | --- |
| 2026-10-26 收盘结算 | 完整日 390 bars(IEX)；LONG 925 @395.59(10:20) → 391.61(session end)；-3,679.65(-1.01%)；净值 367,694.35 → 364,014.66 |
