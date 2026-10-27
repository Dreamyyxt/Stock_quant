# 实盘就绪手册（LIVE READINESS）

> 适用版本：2026-10 改造后
> 目标：从老虎模拟盘切到真实资金账户，且行为可预测、可回滚、可对账。

---

## 0. 一句话原则

**模拟盘验证的是「策略能不能赚钱」，实盘验证的是「执行会不会亏钱」。**
下面所有改动都服务于第二件事：每一笔订单都必须有成交确认、有预算约束、有状态留痕。

---

## 1. 本次改造做了什么

| 问题 | 改造 |
|---|---|
| 止损卖出后同轮被调仓买回（BABA 2026-06-29 实证） | 止损独立成阶段，先执行先确认；触发止损的标进入冷却期，冷却期内目标权重强制归零 |
| 下单即认为成交，卖 78k 立刻买 76.7k（现金仅 12k） | 每笔订单轮询到终态；卖出未确认不进入买入阶段；买入受显式预算约束 |
| 风控状态全在内存，重启即失忆 | `RiskManager` 状态落盘到 `state/risk_state.json`（成本、峰值、组合峰值、冷却期） |
| 交易计数/挂单不落盘 | `state/order_state.json`（当日计数、已提交标的、挂单簿） |
| 交易所硬编码猜测（INTC 被判 NYSE） | 走券商合约查询，取不到时用白名单，未知标的直接报错不猜 |
| 守护进程 + 手动运行可并发下单 | 按账户加 `flock` 单实例锁，第二个进程直接退出 |
| 开盘 09:30 用延迟行情下限价单 | 调仓改 09:40 ET，并在执行层强制交易时间窗（09:40–15:45） |
| 凭证硬编码、私钥在仓库目录 | 凭证走环境变量 / `config/.env`，新增 `.gitignore` 防误提交 |
| 实盘无任何二次确认 | `TRADING_ENV=live` 必须同时提供 `LIVE_TRADING_CONFIRM` 才允许下单 |
| 调度错过就静默跳过（"missed by 8:30"） | `misfire_grace_time=900s` + `coalesce`，最多迟到 15 分钟补跑 |
| 行情异常时静默退化为静态权重 | 历史数据缺失时告警，不再静默 |
| 无告警通道 | `Notifier`：日志 + `state/alerts.jsonl` + Webhook + macOS 通知 |
| 日志单文件写满 3 个月 | 按天滚动，保留 30 天 |

---

## 2. 上线前必须人工确认的 5 件事

这些代码无法替你完成：

1. **老虎后台开通实盘权限**：美股交易权限、以及是否需要融资（margin）权限。
   本系统按「不超过净值」下单，不主动加杠杆；但美股 T+1 结算下，卖出款再买入
   需要账户本身允许（现金账户会受限）。
2. **实盘账户号与模拟账户号不同**。`TIGER_ACCOUNT` 必须换成实盘账户号。
   RSA 私钥可沿用同一个开发者账号下的密钥，但要确认该密钥已对实盘账户授权。
3. **确认最小下单单位与碎股**：本系统只下整数股，`min_order_notional` 默认 $200，
   低于该金额的调仓会被跳过。若资金量较小，需要下调该值。
4. **确认告警渠道可用**：设置 `ALERT_WEBHOOK_URL`（企业微信/飞书/Slack 均可），
   然后手动触发一次告警验证能收到。
5. **确认没有第二套程序在下单**：旧的 `live_monitor.sh`、`order_monitor.py`、
   `monitor_orders.py` 不要与主程序同时运行。

---

## 3. 切换步骤

### 3.1 准备凭证（不写进代码）

```bash
cp config/.env.example config/.env   # 若不存在则新建
```

`config/.env` 内容：

```bash
TRADING_ENV=paper                     # 先保持 paper
TIGER_ID=你的开发者ID
TIGER_ACCOUNT=模拟账户号
TIGER_PRIVATE_KEY_PATH=/绝对路径/config/private_key.pem
ALERT_WEBHOOK_URL=
```

确认 `config/.env` 和 `*.pem` 都已被 `.gitignore` 覆盖：

```bash
git status --short | grep -E "\.env|\.pem"   # 应当无输出
```

### 3.2 模拟盘跑通新执行层（至少 5 个交易日）

```bash
.venv/bin/python main.py --mode health      # 连通性 / 账户 / 持仓 / 行情
.venv/bin/python main.py --mode dry         # 只看计划，不下单
.venv/bin/python main.py --mode rebalance   # 真实下单（模拟账户）
.venv/bin/python main.py --mode status      # 净值 / 持仓 / 冷却期 / 当日单数
```

验收标准（每条都要看日志）：

- [ ] `rebalance` 日志里出现 `Buy budget: $...`，且买入金额未超过该预算
- [ ] 卖出后日志出现成交确认（`order_result` 里 `status=filled`、`fill_price` 有值）
- [ ] 若卖单未成交，买入被跳过并产生 `Buy skipped` 告警，而不是继续买
- [ ] 同一标的同一天不可能出现两笔同方向订单（`Duplicate ... blocked`）
- [ ] 手动再开一个 `--mode rebalance` 会被单实例锁拒绝（退出码 3）
- [ ] 重启进程后 `--mode status` 仍能看到 `Portfolio peak` 与 `Stop-out cooldowns`

### 3.3 影子模式（建议 30 天，强烈推荐）

在拿到真实滑点数据之前不要投真钱。做法：

1. 保持 `TRADING_ENV=paper`，正常跑调度；
2. 每个交易日收盘后，把 `logs/rebalance_*.json` 的 `trades` 与券商当日实际盘口/成交对比；
3. 统计三个数：**未成交率**、**相对参考价的滑点**、**买卖价差成本**；
4. 如果未成交率 > 30% 或平均滑点 > 0.3%，先调整 `limit_slippage_pct` 与调仓时点，
   不要带着这个执行质量上实盘。

### 3.4 切到实盘（小资金）

```bash
# 1) 改 config/.env
TRADING_ENV=live
TIGER_ACCOUNT=实盘账户号
LIVE_TRADING_CONFIRM=I_UNDERSTAND_LIVE_CAPITAL_IS_AT_RISK

# 2) 验证拦截与放行都生效
.venv/bin/python main.py --mode status        # 应能读到实盘账户
.venv/bin/python main.py --mode dry           # 只出计划，不下单

# 3) 首次实盘只给 1% 资金，跑通一轮完整调仓
.venv/bin/python main.py --mode rebalance
```

首轮必须人工核对三件事：

- 券商 App 里的持仓与 `--mode status` 一致；
- `state/alerts.jsonl` 没有 `critical`；
- `logs/rebalance_*.json` 的 `execution_results` 与券商成交记录逐笔对得上。

### 3.5 接上常驻服务

```bash
# 安装 launchd（注意 plist 里的 TZ / 工作目录）
cp scripts/com.stockquant.trading.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.stockquant.trading.plist
launchctl list | grep stockquant

# 观察
tail -f logs/trading.log
```

`KeepAlive=true` 意味着崩溃会自动拉起；单实例锁保证不会因此产生双进程。

---

## 4. 日常运维

| 频率 | 动作 | 命令 |
|---|---|---|
| 每日开盘前 | 健康检查（09:00 ET 自动） | `main.py --mode health` |
| 每日 | 看 `state/alerts.jsonl` 是否有 critical | `tail state/alerts.jsonl` |
| 每周 | 对账：净值、持仓、现金 vs 券商 | `main.py --mode status` |
| 每月 | 检查 `logs/rebalance_*.json` 的成交率与滑点 | 手工统计 |
| 每月 | 复核 `state/risk_state.json` 的 `portfolio_peak` 是否合理 | 手工 |

---

## 5. 回滚

任何异常，按顺序：

```bash
# 1) 停服务
launchctl unload ~/Library/LaunchAgents/com.stockquant.trading.plist

# 2) 改回模拟盘
TRADING_ENV=paper  TIGER_ACCOUNT=模拟账户号（config/.env）

# 3) 若已有实盘持仓，先手工确认是否需要平仓，再恢复调度
```

**注意**：`state/risk_state.json` 记录的是「账户无关」的风控参数。切换账户前建议备份并
清空，否则实盘会继承模拟盘的组合峰值，导致回撤熔断被误触发：

```bash
cp state/risk_state.json state/risk_state.paper.bak
rm state/risk_state.json
```

---

## 6. 目前仍然是已知缺口（不要假装它不存在）

1. **行情是 15 分钟延迟数据**。没有实时行情订阅，限价单在快速行情下仍可能不成交。
   这不是 bug，是数据权限问题；要么买行情，要么接受更高的未成交率。
2. **回测跑的不是实盘策略**。`backtest/backtester.py` 是独立实现（等权、固定 -15% 止损、
   月度、无 overlay），与实盘的「固定权重 + RSI/52 周 tilt + 日频」不是一回事。
   在拿它做任何资金决策之前，需要先把回测对齐实盘逻辑。
3. **策略本身没有样本外验证**。当前 8 只固定权重高度集中在科技（MSFT/GOOGL/AAPL/NVDA/AMD
   合计 70%），`BABA` 是中概 ADR，有额外政策风险。这是配置选择，不是代码问题。
4. **wash sale 未处理**。美股 30 天内亏损卖出再买回会触发 wash sale，冷却期 10 天
   并不足以规避，税务口径需要单独处理。
5. **无拆股/分红/公司行为处理**。持仓数量在拆股后需要人工核对。

---

## 7. 故障注入结果（B2 DoD，2026-10-26）

`scripts/quantos_fault_inject.py`，7 个场景 55 条断言全部通过。
判定标准不是"跑得通"，而是每个故障要么**正确恢复**、要么**安全停止**，不允许静默错误。

| # | 场景 | 注入方式 | 观察到的行为 | 结论 |
|---|---|---|---|---|
| 1 | 风控进程崩溃 | 对独立风控进程发 `SIGKILL` | 后续检查返回 `REJECT` + `risk_service_unavailable` + `freeze_new_orders`；不创建订单对象 | 安全停止 |
| 2 | 下单超时 | 主动触发 `order_timeout` | 只迁 `UNKNOWN` 并冻结标的；重试拿回同一 `client_order_id`，订单数仍为 1 | 正确恢复 |
| 3 | 重复成交回报 | 同一 `venue_trade_id` 连投 3 次 | 账本只记 1 次，持仓只增 1 股 | 正确恢复 |
| 4 | 部分成交后超时 | 先 1 股成交，交易所实为 2.5 股 | 对账只补差额 1.5 股；重复对账不再补 | 正确恢复 |
| 5 | 进程重启 | 导出快照 → 新进程恢复 | 有快照：复用不重发；**忘恢复：拒发并转 `needs_reconciliation`** | 安全停止 |
| 6 | 磁盘满 | 写入抛 `ENOSPC` | `EventStoreFull` 显式抛出，`seq` 不推进；恢复后序号连续；半行被截断 | 安全停止 |
| 7 | 交易所失联 60s | 前两轮查询超时，第三轮恢复 | 前两轮 `wait` 保持 `UNKNOWN`，恢复后 `adopt` 收敛，持仓与账本一致 | 正确恢复 |

### 这一轮暴露并修掉的两个真实缺陷

1. **订单状态机缺 `SUBMITTED → FILLED` 边**。快速成交或 WS 漏掉 ack 时，成交回报会被判非法迁移而**丢失成交记录**。已补齐 `SUBMITTED → PARTIALLY_FILLED / FILLED`，理由：成交回报本身就是"交易所已接受此单"的证据。
2. **`resolve_unknown_order` 把 `partially_filled` 判成全部成交**。原因是子串匹配 `"filled"`。这会让一个只成交 60% 的订单在账本里变成满仓。已改为先排除 `partial`。

### 仍然没有覆盖的故障（上线前必须补）

- **券商侧不按 `client_order_id` 去重**：本地再严也只能保证"我们不重复发"，不能保证"券商不重复执行"。需要在真实环境逐场所验证（B5 能力矩阵）。
- **数据库不可用**：目前 `EventStore` 是本地文件，无远程数据库依赖，因此该故障尚不适用；接 Postgres 后需补。
- **时钟跳变**：NTP 大幅回拨会让 `as_of` 判陈旧/未来失效，尚未做单调时钟兜底。

---

## 8. 可观测性验证（B3 DoD，2026-10-26）

B2 证明的是「故障发生时系统不会做错事」；B3 补的是**「故障发生时人能不能立刻知道是哪一环」**。
这两件事必须都成立 —— 一个安静地安全停止、但没人知道它停了的系统，等于不可用。

`scripts/quantos_ops_server.py --once --fault <name>` 注入后，看 `overall` / `headline` / `root_causes`：

| # | 注入故障 | `overall` | `headline` 指向 | `critical` 条目 |
|---|---|---|---|---|
| 0 | 无（基线） | `warn` | `feed · OKX 通道 degraded` | 0 |
| 1 | `stale-market`（行情簿整体停在 10 分钟前） | `critical` | `market · TIGER 最旧快照已 600.1s 未更新` | `market.latency.TIGER` / `market.stale_symbols` |
| 2 | `risk-halt`（权限降为 HALTED + 回撤 6.2%） | `critical` | `risk · 回撤 6.2%` | `risk.drawdown` / `risk.permission` |
| 3 | `unknown-order`（订单超时未对账） | `critical` | `orders · TIGERMAIN-PF-TLT-…-0003` | `orders.unknown` / `reconcile.pending` / `risk.unknown_symbols` |
| 4 | `silent-strategy`（模拟盘策略心跳停 8 分钟） | `critical` | `strategy · crypto_trend 最近一次心跳 480s 前` | `strategy.heartbeat.crypto_trend` |

真实输出示例（场景 1）：

```
overall  = critical
headline = market · TIGER 最旧快照已 600.1s 未更新（另有 4 项）
 · [critical] market.latency.TIGER   ← market     | TIGER 最旧快照已 600.1s 未更新
 · [critical] market.stale_symbols   ← market     | TIGER:QQQ, TIGER:SPY, TIGER:TLT
 · [warn]     feed.status.OKX        ← feed       | OKX 通道 degraded：重连中（第 2 次）
 · [warn]     feed.status.TIGER      ← feed       | 行情推送停滞 10 分钟
 · [warn]     strategy.reduce_only   ← strategy   | mean_reversion
```

**基线必须健康，否则故障注入没有意义。** 这一条是验证时才发现并修掉的：
`build_runtime()` 原先在默认场景里就挂着一笔待对账订单，导致**任何一次** `--once`
都是 `overall=critical`、headline 永远是 `orders · …`。那样的话注入哪个故障看起来都一样，
"能定位"这件事等于没被验证过。现在基线最差只到 `warn`（OKX 通道降级 + 一个仅减仓策略），
待对账订单只在 `--fault unknown-order` 时才造出来 —— 每个故障的 headline 都指向自己的组件。

> 这条经验同样适用于生产：**长期挂着未处理告警的系统等于没有告警。**
> 看到 `critical` 的第一件事是处理它，而不是习惯它。

**日常巡检怎么做**（不需要看日志）：

```bash
.venv/bin/python scripts/quantos_ops_server.py --once            # 一眼看 overall
curl -s http://127.0.0.1:8787/api/ops/snapshot | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["overall"], d["headline"])'
.venv/bin/python scripts/quantos_ops_check.py                    # 接口与进程内是否一致
```

**三条读指标时的纪律**：

1. `not_configured` **不是健康**。它表示"这个探针没接"，而不是"这项正常"。看到它就等于看到一个待办。
2. `ok` 只代表**进程内能看到的事**。券商侧真实延迟、交易所侧挂单状态，指标看不出来，只能靠对账。
3. 页面上的曲线在实时模式下**可能是演示形状**（无历史净值序列），页面已显式标注。运维决策看 `root_causes`，不看曲线。

---

## 9. 实盘就绪检查（B4 DoD，2026-10-26）

B3 解决「出问题看得见」；B4 解决「**真钱出去之前有没有人真的按过开关**」。

### 9.1 上线前的四级硬件条件

```
① 三级武装（T-401）      账户级 → 策略级 → 单笔上限，缺一级就下不了单
② 对账清零（T-402）      现金 / 持仓 / 挂单 / 成交 四项与券商一致
③ 归因可拆（T-403）      当日盈亏能被五项拆干净，且汇率项在单币种下为 0
④ 观察期满（T-404）      小额实盘 2 周：无重复订单、无对账差异、回撤在预算内
```

①②③ 是代码保证的（96 条断言），**④ 只能人工执行**。

### 9.2 每天开盘前必须做的三件事

```bash
# 1) 看实盘资格：missing 为空才继续
curl -s http://127.0.0.1:8787/api/ops/live | python3 -m json.tool | grep -A6 missing

# 2) 跑昨天日终：四项差异为 0 才继续
.venv/bin/python scripts/quantos_ops_server.py --once --eod

# 3) 重新武装（授权 12 小时到期，跨日必须重来）
python3 - <<'PY'
from quantos_core.live_gate import LiveGate, Confirmation
# actor 必须 human:*，reason ≥ 6 字，echo 必须逐字等于对象名
PY
```

**第 3 步的摩擦是设计出来的，不是麻烦。** 每天重按一次，才能保证
「今天真的有人决定要开」，而不是「上周有人开过，之后就没人管了」。

### 9.3 什么时候必须立刻撤销

| 现象 | 动作 |
|---|---|
| 日终出现**成交**差异（本地有券商无 / 券商有本地无） | 立刻 `revoke(account)`，查清之前不许再开 |
| 出现**挂单**差异（券商挂着本地不知道） | 立刻撤销 + 查交易所，可能有未知成交 |
| `live.reconcile` 变 `critical` | 实盘已自动被拦，人要做的是查原因而不是绕过它 |
| `orders.unknown` > 0 且超过 5 分钟未收敛 | 撤销 + 跑对账 |

撤销在任何时候都不需要确认，也不影响纸面运行：

```python
gate.revoke("TIGERMAIN", by="human:owner", why="日终成交差异待查")
```

### 9.4 两条不要做的事

1. **不要为了"跑起来"把对账判干净的逻辑放宽。** `clean = statement_ok and not diffs`
   里的 `statement_ok` 是这条链上最容易被删的一行 —— 删掉它，"取数失败"就变成了
   "零差异"，而这两件事在净值上看完全一样。
2. **不要把授权有效期调长。** 12 小时的意思是"今天开今天用"。
   改成一周，等于把每天一次的判断变成每周一次，风险窗口放大 7 倍，收益一分不多。

### 9.5 验证方式

```bash
.venv/bin/python scripts/quantos_selftest_b4.py     # 96 条
.venv/bin/python scripts/quantos_ops_server.py --once --fault eod-mismatch     # 对账不干净立刻拦实盘
.venv/bin/python scripts/quantos_ops_server.py --once --fault live-not-armed   # 少一级就无法下单
```
