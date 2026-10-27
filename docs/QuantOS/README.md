# QuantOS 2.0 · 工程文档索引

> 本目录是 QuantOS 2.0 的**设计与推进中枢**。需求、差距、契约、路线图四份文档构成闭环：
> 需求编号 → 现状差距 → 契约定义 → 分期任务 → 代码与自测。

## 文档地图

| 文档 | 回答什么问题 | 谁该看 |
|---|---|---|
| [`00-系统要求.md`](00-系统要求.md) | 要建什么？为什么这么设计？ | 所有人；任何需求变更先改这里 |
| [`01-现状差距分析.md`](01-现状差距分析.md) | 现在已经有什么？还缺什么？ | 排期、判断"能不能复用" |
| [`02-服务数据契约.md`](02-服务数据契约.md) | 五个服务之间的字段、状态机、错误语义 | 写代码前必读，验收依据 |
| [`03-推进路线图.md`](03-推进路线图.md) | 先做什么后做什么？怎么算做完？ | 排期与验收 |

## 代码位置

| 产出 | 路径 | 状态 |
|---|---|---|
| 领域契约与状态机 | `quantos_core/contracts.py` | ✅ B0 |
| 资金分配与多策略净额 | `quantos_core/capital_allocator.py` | ✅ B0 |
| 风控三道防线与权限级 | `quantos_core/risk_engine.py` | ✅ B0 |
| 复式账本与五维归因（B7 起六项，含分红） | `quantos_core/ledger.py` | ✅ B0 / B7 加公司行动 |
| 事件存储（JSONL/SQLite） | `quantos_core/events.py` | ✅ B1 |
| 行情快照契约 | `quantos_core/market.py` | ✅ B1 |
| 成本模型（回测实盘共用） | `quantos_core/cost.py` + `config/cost_model.json` | ✅ B1 |
| 策略输出适配器 | `quantos_core/strategy_adapter.py` | ✅ B1 |
| 事件回放器 | `quantos_core/replay.py` | ✅ B1 |
| 领域对象线格式 | `quantos_core/codec.py` | ✅ B2 |
| 风控独立进程 + fail-closed 客户端 | `quantos_core/risk_service.py` | ✅ B2 |
| 幂等键持久化 | `quantos_core/idempotency.py` | ✅ B2 |
| 订单管理（风控→注册→状态机） | `quantos_core/oms.py` | ✅ B2 |
| UNKNOWN 对账闭环 | `quantos_core/reconciliation.py` | ✅ B2 |
| 账户×网关单活锁 | `quantos_core/locks.py` | ✅ B2 |
| 策略实例注册表 + 阶段机 + 审计 | `quantos_core/strategy_registry.py` | ✅ B3 |
| 指标采集与严重度分级 | `quantos_core/metrics.py` | ✅ B3 |
| 运维 HTTP 面（只绑回环） | `quantos_core/ops_server.py` | ✅ B3 |
| 运维运行时 + 故障注入 | `scripts/quantos_ops_server.py` | ✅ B3 / B4 加实盘与日终 |
| 接口/进程内抽检 | `scripts/quantos_ops_check.py` | ✅ B3 |
| 页面 DOM 探针（零依赖） | `scripts/page_probe.js` | ✅ B3 / B4 净值曲线 / B7 三块上屏 |
| 三级实盘开关 | `quantos_core/live_gate.py` | ✅ B4 |
| 日终四项对账 + 归因日报 + 净值时序 | `quantos_core/eod.py` | ✅ B4 |
| 公司行动对账（第五项）+ 六项归因 | `quantos_core/eod.py` | ✅ B7 |
| 场所网关抽象（归一化类型 + 错误五分类 + 提交对账） | `quantos_core/gateway.py` | ✅ B5 |
| 交易所能力矩阵与准入（含逐标的检查） | `quantos_core/venues.py` | ✅ B5 |
| CCXT 适配（注入式 transport + probe 实测） | `quantos_core/ccxt_gateway.py` | ✅ B5 |
| 资金费记账（结算边界 + 幂等 + 对账） | `quantos_core/funding.py` | ✅ B5 |
| 多日历调度（24/7 与股票时段隔离） | `quantos_core/scheduler.py` | ✅ B5 |
| Crypto 接入体检（准入/调度/结算/下单量化） | `scripts/quantos_crypto_check.py` | ✅ B5 |
| 风险模型与 Euler 风险分解 | `quantos_core/risk_budget.py` | ✅ B6 |
| 等风险贡献权重（Spinu 坐标下降） | `quantos_core/risk_budget.py` | ✅ B6 |
| 风险预算 → 额度换算 | `quantos_core/risk_budget.py` | ✅ B6 |
| 策略灰度路由与回滚 | `quantos_core/canary.py` | ✅ B6 |
| AI 研究通道（只读 + AST 隔离体检） | `quantos_core/ai_research.py` | ✅ B6 |
| 协方差估计（σ/ρ/收缩/正定性/自由度诊断） | `quantos_core/covariance.py` | ✅ B7 |
| 策略接口契约（MANIFEST / compute / 行情 frame / 信号形态） | `quantos_core/strategy_api.py` | ✅ B8 |
| 策略包导入（AST 体检 / 指纹 / 干跑 / 准入） | `quantos_core/strategy_pack.py` | ✅ B8 |
| 策略运行时（信号 → 意图 → 净额 → 风控 → 成交 → 账本） | `quantos_core/strategy_runtime.py` | ✅ B8 |
| 策略包导入体检命令 | `scripts/quantos_strategy_import.py` | ✅ B8 |
| 交易日历（数据驱动 + 规则对账 + 覆盖边界） | `quantos_core/calendar.py` | ✅ B9 |
| 策略排期（锚点固定 / claim 三段 / in_doubt 需人工） | `quantos_core/schedule.py` | ✅ B9 |
| 排期调度命令（calendar / status / plan / tick / resolve） | `scripts/quantos_schedule.py` | ✅ B9 |
| 校准过的 A 股交易日历（1999 个交易日 / 155 天休市，2018-07~2026-10） | `config/calendar/ashare.json` | ✅ B9 |
| 状态存储原语（原子写 + kind/schema/sha256 信封 + 备份回退） | `quantos_core/state_store.py` | ✅ B10 |
| 账本落盘与恢复（整份快照 / 幂等索引重建 / 现金与分录交叉核对） | `quantos_core/ledger_store.py` | ✅ B10 |
| cron 入口（一拍一进程 + 单实例锁 + 退出码分级） | `scripts/quantos_tick.py` | ✅ B10 |
| 进程编排（四进程状态 / 缺口如实上报 / crontab 片段） | `scripts/quantos_up.py` | ✅ B10 |
| 真实网关（Alpaca 纸面 / 纯 stdlib REST / 端点白名单 + 错误二分） | `quantos_core/alpaca_gateway.py` | ✅ B11 |
| 网关联调（probe 只读 / reconcile 对账 / roundtrip 真下单需 --yes） | `scripts/quantos_alpaca.py` | ✅ B11 |
| 风险模型体检（真实 A 股 / 合成数据双源） | `scripts/quantos_risk_check.py` | ✅ B7 |
| 策略组合体检（风险/等风险/预算/灰度/AI） | `scripts/quantos_portfolio_check.py` | ✅ B6 |
| 控制台 | `quantos/index.html` | ✅ B4 实盘就绪 + 归因日报；B7 加风险模型 / 灰度 / AI 收件箱；B9 加策略排期；B10 加账本；B11 加真实网关；已发布在线版（B5 多场所未上屏） |
| 既有实盘链路 | `main.py` / `core/` / `execution/` / `risk/` | 🟡 单券商单账户，待接契约层 |

跑全部验收：

```bash
.venv/bin/python scripts/quantos_verify.py     # 14 套件 / 1625 条断言
.venv/bin/python scripts/quantos_crypto_check.py   # Crypto 接入体检（不联网）
.venv/bin/python scripts/quantos_risk_check.py --source ashare --tail 400   # 真实 A 股估一次 σ
.venv/bin/python scripts/quantos_risk_check.py --demo --shrinkage 0.5      # 合成数据看收缩效果
.venv/bin/python scripts/quantos_strategy_import.py --pack research/strategy_packs/ashare_ew20.strat.py   # 策略包导入体检
.venv/bin/python scripts/quantos_schedule.py calendar --build   # 重建 A 股交易日历并对账
.venv/bin/python scripts/quantos_schedule.py status             # 每个策略下次什么时候跑
.venv/bin/python scripts/quantos_schedule.py tick                # 只判定不执行（默认安全）
.venv/bin/python scripts/quantos_schedule.py tick --execute      # 真跑一拍
.venv/bin/python scripts/quantos_schedule.py ledger              # 账本状态（可不可信、多少持仓）
.venv/bin/python scripts/quantos_tick.py --dry-run              # cron 入口演练（拿锁 + 判定）
.venv/bin/python scripts/quantos_up.py --status                  # 四进程各是什么状态
.venv/bin/python scripts/quantos_up.py --crontab                 # 打印 crontab 片段
.venv/bin/python scripts/quantos_alpaca.py probe                 # 只读探针：账户/时钟/回查端点
.venv/bin/python scripts/quantos_alpaca.py reconcile             # 场所持仓与现金 vs 本地账本
.venv/bin/python scripts/quantos_alpaca.py roundtrip --qty 1 --yes  # 真下单（纸面）：买入→核对→平仓
```

单独跑某一套：

```bash
.venv/bin/python scripts/quantos_selftest.py        # B0 契约与账本     48 条
.venv/bin/python scripts/quantos_selftest_b1.py     # B1 数据层与回放   93 条
.venv/bin/python scripts/quantos_selftest_b2.py     # B2 风控与幂等     73 条
.venv/bin/python scripts/quantos_fault_inject.py    # T-205 故障注入  7 场景 55 条
.venv/bin/python scripts/quantos_selftest_b3.py     # B3 运维中心      103 条
.venv/bin/python scripts/quantos_ops_check.py       # T-302 抽检        6 项
.venv/bin/python scripts/quantos_selftest_b4.py     # B4 实盘就绪       96 条
.venv/bin/python scripts/quantos_selftest_b5.py     # B5 多场所        208 条
.venv/bin/python scripts/quantos_selftest_b6.py     # B6 多策略与 AI   174 条
.venv/bin/python scripts/quantos_selftest_b7.py     # B7 估计层+公司行动 212 条
.venv/bin/python scripts/quantos_selftest_b8.py     # B8 策略运行时      142 条
.venv/bin/python scripts/quantos_ops_check.py --json # 同上，给 CI 用
```

起运维面（默认只绑 `127.0.0.1`）：

```bash
.venv/bin/python scripts/quantos_ops_server.py --once                 # 打印一次快照
.venv/bin/python scripts/quantos_ops_server.py --once --eod           # 日终对账 + 归因日报
.venv/bin/python scripts/quantos_ops_server.py --once --fault stale-market
.venv/bin/python scripts/quantos_ops_server.py --port 8787            # 起 HTTP 服务
curl -s http://127.0.0.1:8787/api/ops/snapshot | head -c 400
curl -s http://127.0.0.1:8787/api/ops/nav-history
curl -s http://127.0.0.1:8787/api/ops/live
curl -s http://127.0.0.1:8787/api/ops/portfolio    # 风险模型 / 灰度 / AI 收件箱
```

可用故障：`stale-market` / `risk-halt` / `unknown-order` / `silent-strategy` /
`eod-mismatch` / `live-not-armed` / `stale-model` / `unbooked-dividend`。

### 控制台有两种形态，别搞混

控制台 `quantos/index.html` 是单文件页面，同时被用在两个场景：

| 形态 | 打开方式 | 数据来源 | 实盘就绪区块会显示什么 |
|---|---|---|---|
| **本地运维**（看真数据） | 起 `quantos_ops_server.py` 后开 `http://127.0.0.1:8787/` | `/api/ops/*` 实时拉取 | 三盏锁的真实状态 + 对账结果 |
| **在线分享**（给别人看结构） | 资料库链接 `https://www.workbuddy.cn/space/d/d1xCrrZhxVjrCktWwbLWG4` | 连不上本机 → 退回演示数据 | **「需要连接运维服务后才能查看」**，不编造绿灯 |

在线版是公网可开的，但**它永远看不到你的真实仓位**——运维面只绑回环地址，
这是刻意的。在线版的价值是"能在手机上翻看结构、演示给别人"，不是"远程盯盘"。
真要看实时状态，必须在跑运维服务的那台机器上开。

演示模式下实盘就绪区块刻意不推测状态：猜错一次绿灯，就会让人以为可以下真钱单。

---

## 当前进度看板

| 批次 | 主题 | 状态 | 说明 |
|---|---|---|---|
| **B0** | 契约与账本骨架 | ✅ 完成 2026-10-26 | T-001~T-005 交付，48 条断言 |
| **B1** | 数据层与事件回放 | ✅ 完成 2026-10-26 | T-101~T-105 交付，93 条断言 |
| **B2** | 风控独立化 + 幂等执行 | ✅ 完成 2026-10-26 | T-201~T-205 交付，73 条 + 7 个故障场景 |
| B3 | 运维中心后端 | ✅ 完成 2026-10-26 | T-301~T-303 交付，103 条 + 抽检 6 项一致 + 解掉整标的阻断 |
| B4 | 小规模实盘 + 日终对账 | ✅ 代码完成 2026-10-26 | T-401~T-403 交付，96 条；T-404 两周观察期需真钱真时间 |
| B5 | Crypto 接入 | ✅ 代码完成 2026-10-26 | T-501a/b~T-504 交付，208 条；真实交易所联调待凭据与 `probe()` |
| B6 | 多策略与 AI 研究 | ✅ 代码完成 2026-10-27 | T-601~T-603 交付，174 条；风险预算/灰度回滚/AI 隔离 |
| B7 | 估计层与公司行动 | ✅ 代码完成 2026-10-27 | T-701~T-703 交付，212 条；协方差估计/分红拆股对账/三块上屏 |
| B8 | 策略运行时与导入契约 | ✅ 代码完成 2026-10-27 | T-801~T-803 交付，142 条；策略接口/包导入/全链路运行时 |

### B0 交付明细

| 任务 | 产出 | 验证方式 |
|---|---|---|
| T-001 | `contracts.py`：10 个领域对象 + 订单状态机（10 状态 / 显式迁移表） | S1 共 11 条断言 |
| T-002 | `capital_allocator.py`：固定配额 + 波动率缩放 + 跨策略净额 + 在途扣除 | S2 共 10 条断言 |
| T-003 | `risk_engine.py`：三道防线 + 三级权限 + 处置剧本 + 未知订单对账 | S3 共 15 条断言 |
| T-004 | `ledger.py`：双分录 + NAV + 五维归因 + 对账 | S4 共 10 条断言 |
| T-005 | `quantos_selftest.py`：确定性端到端链路 | S5 共 2 条断言 |

### B0 已明确验证的六条关键行为

1. 两策略反向意图 → **只产生一笔净额订单**（趋势 +15780 / 均值回归 -7890 → 净 +7890）。
2. 在途订单被扣进差额，**同一意图不会在两次调度里下单两次**。
3. `confidence` 从 0.01 改到 0.99，**下单量完全不变**（需求 R-A06 的机器验证）。
4. 订单超时只迁到 `UNKNOWN`，且状态机里**没有任何回到"重新提交"的边**。
5. 风控超单笔限额只返回 `REDUCE_REQUIRED + cap`，**自己不改进单量**；账本归因五项之和恒等于净收益。
6. 入金/出金不改变净收益；现金差 1 元也会在报告里暴露。

### B3 交付明细

| 任务 | 产出 | 验证方式 |
|---|---|---|
| T-301 | `metrics.py`：`Metric{key,label,value,unit,severity,threshold,detail,source,component}` + `OpsSnapshot{worst,counts,root_causes,components}` + p95 执行延迟 | B3-A 段断言 |
| T-302 | 页面接 `/api/ops/*` 双数据源；`scripts/page_probe.js` 零依赖 DOM 探针；`scripts/quantos_ops_check.py` 接口 vs 进程内抽检 | B3-D/E 段 + 抽检 6/6 |
| T-303 | `strategy_registry.py`：`RESEARCH→BACKTEST→PAPER→LIVE` 阶段机、三道 LIVE 闸门、审计可回放可校验 | B3-B 段断言 |
| — | `ops_server.py`：只绑 `127.0.0.1`、写操作要 `X-Ops-Token`、无 CORS、请求体 64KB 上限 | B3-C 段断言 |
| — | `scripts/quantos_ops_server.py`：`build_runtime()` + `--once/--fault/--token/--port` | 故障注入 4 类可复现 |

### B3 已明确验证的四条关键行为

1. 注入 `stale-market` 后 `overall=critical`，`root_causes` 直接指到 `market` 与 `reconcile` 两个环节，不是一句「系统异常」。
2. 页面 6 项数字（NAV / 名义敞口 / 回撤 / 策略实例数 / 待对账订单 / 订单总数）与进程内对象逐项一致；且在线 NAV 与演示常量**不相等**（证明真的换了数据源）。
3. 策略升 `LIVE` 必须同时过三道闸：人工 actor、真走过 `PAPER`、观察期 ≥ 7 天。任一不过都拒绝，**并且拒绝也落审计**。
4. `verify_against_audit()` 能从审计事件重建全部实例状态并逐字段比对；篡改内存状态会被发现。

### B4 交付明细

| 任务 | 产出 | 验证方式 |
|---|---|---|
| T-401 | `live_gate.py`：`LiveGate` 三级武装（账户 / 策略 / 单笔上限）+ `Confirmation{actor,reason,echo}` + 12h 到期 + `revoke` 一按就关 + `status().missing` 缺项清单 | B4-A 段 24 条 |
| T-402 | `eod.py`：`EodReconciler` 四项对账（现金/持仓/挂单/成交）+ `BrokerStatement` 契约 + `StatementUnavailable` fail-closed + 报告落盘 | B4-B 段 20 条 |
| T-403 | `eod.py`：`NavSeries` 按日 upsert + `DailyAttribution` 五项日报 + 两条独立校验（汇率=0 / 手续费≤0） | B4-C 段 21 条 |
| — | `ops_server.py`：`/api/ops/nav-history`、`/api/ops/live` 两个只读端点 | B4-D 段 11 条 |
| — | `metrics.py`：`live.ready.<acct>` / `live.reconcile.<acct>`，未接开关报 `not_configured` | B4-D 段断言 |
| — | `quantos/index.html`：净值曲线用真实日终快照；点不足显示占位，不拿演示曲线顶上 | 页面探针 26 条（有真点）/ 25 条（无点） |
| T-404 | 小额实盘 2 周观察期 | ⏳ 需真钱真时间，代码无法代劳 |

### B4 已明确验证的四条关键行为

1. **少一级确认就无法下单**：只武装账户级 → 拒；账户+策略缺单笔上限 → 拒；三级齐但对账未清零 → 拒。且一次列出全部缺项。
2. **AI 不能武装实盘、回显填错就打不开、授权 12 小时自动失效、撤销一按就关**（关闭必须比打开容易）。
3. **拿不到券商账单会判为"不干净"**，不是"对上了"；对账一不干净，已武装的实盘账户立刻被拦（`blocked_layer=reconciliation`）。
4. **五项归因之和等于当日净收益**；同时独立校验「单币种汇率项必须精确为 0」「手续费必须 ≤ 0」——这两条才是真能抓错的那两条。

---

### B5 交付明细

| 任务 | 产出 | 验证方式 |
|---|---|---|
| T-501a | `gateway.py`：归一化 `VenueOrder`/`VenuePosition`/`VenueBalance`/`FundingPayment` + 错误五分类（`ambiguous` 语义）+ `submit_with_reconcile`（超时只查不重发）+ `normalize_state`（未知状态词 → UNKNOWN） | B5-A 段 25 条 / B5-B 段 26 条 |
| T-501b | `ccxt_gateway.py`：注入式 transport（核心包不 import ccxt）、场所方言映射、数量价格取整、`OrderNotFound`→None 与查询失败区分、资金费符号翻转、`probe()` 部分实测如实上报 | B5-C 段 40 条 |
| T-502 | `venues.py`：`Capabilities` + `SymbolRules` + 五条阻断线 + 逐标的准入检查 + `quantize_qty/price` 方向约束 + 五家声明档案 | B5-D 段 40 条 |
| T-503 | `funding.py`：结算边界（8h UTC 0/8/16）+ 方向符号 + 计提与已结算分离 + 按 `(场所,标的,边界)` 幂等 + 修正结算只补差额 + `FundingReconciler` | B5-E 段 42 条 |
| T-504 | `scheduler.py`：`EquitySession`（时区/节假日/提前收盘/周末）+ `CryptoSession`（维护窗口）+ lane 异常隔离 + 休市不补跑 + `boundaries_crossed` 补账 | B5-F 段 35 条 |
| — | `scripts/quantos_crypto_check.py`：准入矩阵、lane 状态、今日结算、下单量化 | 手工运行 |

### B5 已明确验证的四条关键行为

1. **`place_calls` 恒为 1 或 0，永不为 2**：连续 3 次超时、回查也超时、回查鉴权失败——每一种都只调一次下单。`UNKNOWN` 是合法终态，不是"待重试"。
2. **能力不足的场所一个请求都发不出去**：缺客户端单号或缺按单号回查，`submit_with_reconcile` 直接抛 `CapabilityError`，`place_calls == 0`。
3. **资金费重跑 100 次只入账 1 次**；交易所修正金额时只补差额（6 → 6.5 记 0.5），审计能看出"当时记了 6、后来补了 0.5"。
4. **美股收盘不影响 Crypto，且休市后开盘只跑一次**：周一开盘跑 1 次而不是补出周末的 2880 次；一条 lane 抛 `SystemExit` 另一条照常运行。

---

### B7 交付明细

| 任务 | 产出 | 验证方式 |
|---|---|---|
| T-701 | `covariance.py`：`PriceSeries` / `EstimationConfig`（`annualization` 必填）/ `estimate()`（交集对齐 → 简单收益 → EWMA 加权矩 → 年化 → clip → 收缩 → 正定性探针 → 诊断）/ `CovarianceEstimate`（含 `raw_corr`、`dof_ratio`、`corr_stderr()`）/ `RiskModelStore`（原子替换 + 年龄） | B7-A 段 95 条 |
| T-702 | `ledger.py`：`post_dividend` / `post_interest` / `apply_split` + `was_written()` 幂等门；`eod.py`：`DiffKind.CORPORATE_ACTION` + `DeclaredAction` + 第五项对账（严重度排在持仓/现金之前）+ `DailyAttribution.dividend`；`NavPoint.dividend` 兼容老快照 | B7-B 段 84 条 |
| T-703 | `metrics.py` 三块新指标 + `_portfolio_facts()`；`ops_server.py` 只读端点 `/api/ops/portfolio`；`quantos/index.html` 三张卡片（风险模型 / 灰度 / AI 收件箱）+ 归因六项 | B7-C 段 21 条 + 页面探针 87 条 |
| — | `scripts/quantos_risk_check.py`（真实 A 股 / 合成数据双源体检 + 复权口径实证） | 手工运行 |
| — | 端到端：估计 → `to_risk_model` → `portfolio_risk` 不变量；公司行动后归因仍闭合 | B7-D 段 12 条 |

### B8 交付明细

| 任务 | 产出 | 验证方式 |
|---|---|---|
| T-801 | `strategy_api.py`：`ParamSpec`（声明式参数，越界/类型/未知键全报错）/ `Series` / `MarketFrame`（`require()` 是硬门槛）/ `SignalOut` + `coerce_signal`（dict 与对象两种写法）/ `StrategyManifest`（含 `resolve_params`、参数指纹） | B8-A 段 62 条 |
| T-802 | `strategy_pack.py`：AST 体检（import 白名单 + 禁用调用 + dunder + 顶层语句）/ `sha256` 指纹 / 强制干跑 / `admission_blockers`（R-S03 六项）/ `to_instance` 恒为 RESEARCH / `install` | B8-B 段 45 条 |
| T-803 | `strategy_runtime.py`：`run_once()` 串起 信号→意图→净额→风控→成交→账本；`PaperBroker`；`EngineRiskClient`（进程内适配器）；`RunReport`（信号/意图/目标/成交/被拒/限额重算/阻断/异常分列） | B8-C 段 35 条 |
| — | `scripts/quantos_strategy_import.py`（导入体检命令）+ `research/strategy_packs/_template.strat.py`（模板） | B8-D 段真实数据 |
| — | 零依赖：三个模块只 import 标准库（AST 断言） | B8-E 段 3 条 |

### B8 已明确验证的四条关键行为

1. **策略包零 import 也能被接住。** 第一版要求返回 `SignalOut`，结果是每个包都得 `import quantos_core`，而 AST 白名单又不允许 —— 体检当场把这个设计矛盾顶出来。现在推荐返回 dict，`SignalOut` 只是可选写法。
2. **导入不等于能下单。** `policies()` 以前只按 `run_state` 过滤，不看 `stage`，于是一个刚导入的 RESEARCH 策略会带着配额进资金分配器。现在只收 PAPER/LIVE；`RESEARCH → PAPER` 跳级被 `IllegalStageTransition` 拒。
3. **风控说"按 cap 重算"时上游真的重算了。** `REDUCE_REQUIRED` 以前没人接，所有超过单笔上限的仓位永远建不起来，报告上只显示"被拒"，看起来像风控在正常工作。现在 `target.capped(cap)` 重算一次（只一次，不循环试探边界）。
4. **成本必须在净值上看得见。** 一次两笔成交：净值 100000 → 99997.1996，差额正好是手续费 0.8004 + 滑点 2.0000。如果这个差额是 0，说明 `apply_fill` 根本没跑到，而不是"没有成本"。

---

### B7 已明确验证的四条关键行为

1. **自由度比的分母是待估参数个数**（`n + n(n-1)/2`），不是标的个数。同一份数据：3 标的 160 观测按标的算是 53.3（宽裕），按参数算是 26.67；5 标的 40 观测按标的是 8.0（还行），按参数算是 2.67（不可信并告警）。
2. **收缩前后一起给**。QQQ/SPY 样本 0.866 → 收缩后 0.622，位移 -0.244，而 ρ 分辨率只有 0.0791 —— 三对相关性全被移动超过统计噪声。页面同时标「超分辨率」，因为此时数字的主要成分是 δ 这个人为选择，不是市场事实。
3. **分红不单列就会被 fx 残差吞掉**。除权日现金 +1000、市值 -1000，net 不变；分红不入账时 `trading` 掉 1000、`fx` 变成 +1000，报表显示「汇率损益 +1000」而真正原因消失 —— **归因恒等式依然闭合**。拆股同理只改数量不改金额，否则凭空造出 50% 收益。
4. **三块能力缺数据一律降级，不编绿灯**。`/api/ops/portfolio` 404 时，风险模型/灰度/收件箱各自显示「看不到」，右上角不挂 `p-ok`。这个请求**不计入离线判定** —— 一次页面改版不该把所有实时指标一起打掉。验收脚本另外静态断言三个渲染函数互不调用（互调就是递归入口）。

---

## B1 / B2 / B3 / B4 / B5 / B6 已解除的遗留项

| 遗留限制 | 现状 |
|---|---|
| 风控仍是**库调用**，不是独立进程 | ✅ 已解除（B2）：`risk_service.py` 独立进程，客户端 fail-closed |
| 超时重试可能重复下单 | ✅ 已解除（B2）：持久化幂等键 + 同义意图复用 + 快照恢复 |
| 订单 UNKNOWN 无后续处理 | ✅ 已解除（B2）：`reconciliation.py` 冻结→查询→补录/撤销→解冻 |
| 策略暂停时**整标的阻断** | ✅ 已解除（B3）：`NettingInput.current_by_strategy` 按策略算差额。PAUSED 冻结自身存量、REDUCE_ONLY 只平不反、**无意图不清仓**、无归属残差冻结 |
| 控制台只有演示常量 | ✅ 已解除（B3）：默认读 `/api/ops/*`，取不到才回退演示并**显式标注** |
| 没有**实盘开关**，改代码就能碰真钱 | ✅ 已解除（B4）：三级各自武装 + 三道确认 + 12 小时到期 + 撤销一按就关；AI 不能武装 |
| 对账只有现金和持仓两项 | ✅ 已解除（B4）：加上**挂单**与**成交**，且拿不到账单判为不干净 |
| 实时模式**没有历史净值序列** | ✅ 已解除（B4）：`NavSeries` 按日落盘，页面画真曲线；点不够就显示占位而不是假曲线 |
| 券商侧是否真按 `client_order_id` 去重，本地保证不了 | 🟡 部分解除（B5）：`venues.py` 把「能发单号」和**「能按单号回查」**都变成准入条件，缺任一条直接禁止自动交易。剩下的部分（场所是否真的去重）只能实盘验证 |
| 指标只覆盖进程内可见的事实 | 🟡 部分解除（B5）：`admit()` 会把「能力声明未实测」「可见延迟超出对账预算」显式说出来，不再靠 `ok` 掩盖。券商/交易所侧仍无法直接观测 |
| 多市场无法在一个基金视角下管 | 🟡 部分解除（B5）：账本本身是场所无关的（`SymbolKey = (venue, symbol)`），资金费已能入账；**但 Crypto 与股票的日界不同**（24/7 vs 交易日），日终与归因目前共用一个 UTC 日界 |
| **波动率与相关性全靠外部输入** | ✅ 已解除（B7）：`covariance.py` 从价格序列估 σ 与 ρ，含收缩、正定性探针、自由度诊断与落盘。**但**「给定假设怎么摊」和「假设会不会变」是两件事，后者仍未解决（见限制表） |
| 对账**不含公司行动** | ✅ 已解除（B7）：加了第五项 `CORPORATE_ACTION`（分红/拆股/融资利息），且严重度排在持仓/现金**之前** —— 它是根因，先报现金差异等于让运维修症状 |
| 多策略同向时风险被**重复计算** | ✅ 已解除（B6）：风险只在**净额**上算，再按净额占比切回策略，`Σ策略贡献 ≡ σ` 是断言。跨标的靠相关矩阵扣掉共同因子（朴素相加会高估 15%）。**附注**：同一标的下「各自算再相加」与净额算法数值相同（线性），所以只用单标的场景永远测不出这个 bug |
| 新版本只能整版上线，没有灰度 | ✅ 已解除（B6）：`canary.py` 按 `strategy_version` 分流，10% 试跑、一次回滚、自动化只能降不能升、扶正必须人工且必须先跑满 100% |
| AI 产出可能被直接写进生产策略 | ✅ 已解除（B6）：`ai_research.py` 在**结构上**不 import 任何生产对象（AST 断言），并硬拒 `quota_pct` / `armed` 等生产控制键（递归检查） |

---

## 仍然存在的限制（不要假装它不存在）

| 限制 | 具体影响 | 解除批次 |
|---|---|---|
| **反向意图的成交归属是按比例分摊** | 同一净持仓无法表示「A 多 7500、B 空 6000」，归属按绝对值占比分摊。策略级 PnL 是可解释的近似值，不是真实分账。**B6 的风险归因按净额占比切，同样是近似** —— 它解决的是「风险不重复计算」，不是「真实分账」 | 子账户（未排期） |
| 补录成交是 **synthetic** | `venue_trade_id` 形如 `recon:{cid}:{累计量}`。B4 的成交项对账已能**把它抓出来**（本地有、券商无），但抓出来之后仍需人工判断，不会自动抹平。B5 未改变这一点 | 需要券商成交流水接口 |
| 券商侧是否真的按 `client_order_id` 去重 | 🟡 B5 已把「能发单号」和「能按单号回查」变成硬性准入条件，救不回来的场所直接禁止自动交易。但**场所内部是否去重**只能在实盘里验证 | 实盘 + 对账 |
| 事件存储只有 JSONL / SQLite | 已抽象 `EventStore` 接口，Postgres 后端未实现（单机单账户暂不需要） | 需要多进程共享时 |
| 回放器只处理 11 类事件 | 未知事件类型只计数不处理（`result.unknown_events`），新增事件类型时必须同步扩回放器，否则回放会静默丢步骤 | 持续 |
| 汇率损益仍用残差法 | 单一计价货币下可断言为 0（日报里已做独立校验）；多币种需改显式记账。B5 的资金费已支持 `fx_to_base`，但**归因里的 fx 仍是残差** | 多币种实盘时 |
| 日终对账**是只读的** | `EodReconciler` 只报差异，不自动改账本、不自动改订单。「自动抹平差异」是对账系统最危险的功能，刻意不做 | 有争议时人工修 |
| 公司行动**只覆盖三项** | 分红 / 拆股 / 融资利息已入账并对账（B7）。**权证行权、并购换股、供股、借券费没有纳入** —— 声明里出现未知类型时会报「未知公司行动」，但那只是报出来，不会自动处理 | 遇到具体品种前 |
| 分红税**按账单倒推** | `gross_amount` / `withheld_tax` 都来自券商账单的拆分，没有按各市场税率规则计算（美股 30% / 爱尔兰 15% / 港股不同）。账单拆错，账本就跟着错 | 需要跨市场税务时 |
| 净值时序**只在跑日终时写点** | 没跑日终就没有当天净值点，曲线会断。这是"必须有日终任务在跑"的硬依赖，不是可以忘的 | 用 launchd 定时跑 |
| 运维面**无鉴权、无 TLS** | 只绑回环 + `X-Ops-Token` 写保护。同机其他进程可读、可暴力猜 token。这是「单机单人」定位下的有意取舍，不是遗漏 | 多租户/远程运维时 |
| 指标**只覆盖进程内可见的事实** | 券商侧真实延迟、交易所侧挂单状态只能靠对账间接反映。B5 的 `admit()` 会把能力未实测显式说出来，但**不要把 `ok` 读成「交易所一切正常」** | 持续 |
| `not_configured` **不等于健康** | 未接探针时返回 `not_configured` 而非 `ok`（fail-closed 的运维版本）。`worst()` 会把未配置计为需关注，不要为了"全绿"去把它改成 ok | 持续（这是不变量） |
| **Crypto 能力矩阵全是声明值** | 五家交易所的步长/精度/最小额抄自文档，`verified_at` 为空。`quantos_crypto_check.py` 里刻意标 ⚠️ 而不是 ✅。拿它当真参数用，就是拿别人博客当参数交易 | `probe()` + 真实凭据 |
| **`supports_query_by_client_id` 测不出来** | `probe()` 只能读 `market()` 元数据。要验证"超时后能否按单号查回来"，必须真实下单一轮。所以它永远留在 `unverified` 里 | 真实店铺小额实测 |
| **交易所原生 WS 行情未接** | T-501 描述里含 WS，实际只做 REST + 可注入 transport；行情仍是 B1 的轮询快照。加密行情延迟敏感，这一条迟早要补 | 需要低延迟行情时 |
| **Crypto 与股票共用一个日界** | 幂等键含日期、日终与归因按 UTC 日切。加密 24/7 意味着"日"是人为约定。若以后要按交易所结算日算账，需重新定义日界 | 正式多市场记账时 |
| **资金费符号只按 CCXT 统一口径处理** | 适配器把 CCXT 的"现金流方向"取反成"费用方向"。若某场所 `info` 层另有约定，第一笔真实结算会由 `FundingReconciler` 抓出金额差异，但**方向错要人看** | 首次真实结算后核对 |
| **估计出的 σ/ρ 只代表历史窗口** | B7 有了估计层，但「给定假设怎么摊」与「假设会不会变」是两件事。模型 > 30 天判不可信，**而 30 天这个阈值没有依据**，没有证据说 29 天一定安全。滚动重估与 ρ 敏感性分析未做 | 需要持续研究 |
| **协方差参数个数是 O(n²)** | n 标的要估 `n + n(n-1)/2` 个参数；794 只 A 股需要 315,271 个参数而观测数只有 O(T)。实际只能压到几十只或改用因子结构。**因子模型未实现** | 组合标的数上升时 |
| **页面探针是 DOM 桩，不是浏览器** | `page_probe.js` 只验证「渲染出来的数字等于后端 JSON」，能抓数字漂移和漏字段，**抓不到 CSS 布局问题**。布局另用 headless Chrome 截图人工核对（1440 / 500 两个宽度）；390px 真机宽度靠命令行验证不了 | 持续（需要真机抽检） |
| **风险预算与固定配额是两套口径** | 两者都产出 `quota_pct`，谁生效由配置决定。同时用两套算同一个策略，两边都会说自己对。`budgets_from_policies()` 全 0 时返回空字典，就是这个切换开关 | 持续（不要同时开） |
| **策略级风险归因在净额为 0 时无意义** | 对锁组合的占比无法按净额切，给 0 并告警。真实风险（基差、两腿时差）未建模 | 需要基差模型时 |
| **灰度只到版本级，不到参数级** | 同一版本内改参数（lookback 20→18）没法灰度，只能整版灰度或整版回滚 | 需要参数级灰度时 |
| **`CanaryRouter` 是纯内存的** | 进程重启后路由表丢失，本该靠审计日志重建 —— 而**重建函数本次没实现**。重启后必须先 `register_stable` 再 `open_canary` | 重启逻辑落定前 |
| **AI 隔离靠 import 图，不靠运行时权限** | AST 扫描保证的是「本模块不自带写生产的能力」，**不是**「AI 永远无法影响生产」。别的模块拿到 registry 再传进研究流程，扫描管不到 | 需要强隔离时 |
| **`ResearchDraft.prepared_by` 只是字符串前缀** | `human:owner` 不是密码学签名。防误触，不防伪造 | 需要真实签名时 |

---

## 二十七处必须保持在单行上的不变量

改动代码时，这几条一旦被破坏，前面所有测试都可能还绿而系统已经不安全：

1. `OrderState.UNKNOWN` 的迁移表里**不能出现 `CREATED` / `SUBMITTED`** —— 否则就有了"未知订单重新提交"的路径。
2. `Oms.submit` 的顺序必须是 **幂等键 → 风控 → 注册订单**。反过来（先注册再问风控）会引入"风控拒绝后回滚订单"的失败窗口。
3. `RiskServiceClient.check` **不得抛异常**。任何"让调用方自己处理风控异常"的改动，等于把 fail-closed 改成 fail-open。
4. `NettingInput.current_by_strategy` **为空或未提供时必须走整标的算法**（`use_strategy_level = bool(by_strategy)`）。空字典被当成"已提供"会算出错误的净额。
5. `per` 里的 `target_weight=0` **不得被过滤** —— 0 是清仓的显式表达，过滤掉等于该平的仓不平。
6. `OpsHttpServer` **不得绑 `0.0.0.0`**；`StrategyRegistry._reject` **必须先写审计再抛异常**。前者是隐私边界，后者是"为什么没升上去"的唯一线索。
7. `LiveGate` 的对账状态**默认必须是不干净**（`self._reconciliation` 里查不到就当作 False）。默认成 True 等于把"忘了对账"变成"对过账了"。
8. `LiveGate.authorize` **不得短路**：所有检查算完再下结论。短路会让运维"改一个刷新一次才发现还缺一个"，而缺项清单本身就是这个功能的产出。
9. `EodReport.clean` **必须同时要求 `statement_ok`**。`clean = not diffs` 会把"拿不到账单"算成"对平了"，这是本模块最危险的一行。
10. `EodReconciler` **只读**：不得自动改账本、不得自动改订单状态。自动抹平差异等于把对账系统变成最危险的功能。
11. `submit_with_reconcile` 里 **`place()` 只允许出现一次**。任何"超时后重试 place"的改动，都等于把 B2 的幂等键白做——交易所看来那是一笔新订单。
12. `translate_exception` 对**未分类异常必须返回 `TransportError`**（ambiguous=True）。改成 `VenueRejected` 会让上层安心重发，而重发是真实亏损。
13. 准入必须**同时要求** `supports_client_order_id` 与 `supports_query_by_client_id`。只要求前者，等于造出"看起来有幂等键、超时后救不回来"的错觉。
14. `quantize_qty` **只能向下**、`quantize_price` 买入向下卖出向上。任一方向反过来，都会让实际成交越过风控算出来的边界。
15. `FundingBook.settle` 的幂等键必须是**结算边界**，不能是"执行次数"或落库时间；`FundingAccrual` 的计提结果**永远不进账本**。
16. `portfolio_risk` 的参数**必须收净额**（参数名就叫 `net`）。传毛额进来会得到一个偏大的数字，而且同向重复计算从那里开始 —— 这个错误不会报错，只会让风控额度看起来还有余量。
17. `route()` **不得重新归一化**：某版本本轮没发意图时，它的份额不许补给另一个版本。补给等于让灰度版承担全额风险，报表上却仍写着「只灰度了 10%」。`reduce_only_hint=True` 的意图**不得缩放**，缩放会把清仓变成半清仓。
18. `auto_adjust()` **不得有提高权重的分支**，且判定值校验必须在状态判断**之前**。前者让自动化永远无法加风险，后者防止「状态不对 + 判定值拼错」被静默放过。`ai_research.py` **不得 import 任何生产模块** —— 这是 T-603 的 DoD，由 `assert_isolated()` 的 AST 扫描守着。
24. `policies()` **必须按 `stage` 过滤**（只收 PAPER/LIVE），`tradable` 只管 `run_state` —— 两个维度不能混。混了之后一个刚导入的研究策略就会带着配额进资金分配器。
25. 策略信号 **只能带权重，不得带金额/数量/账户**；`StrategyContext` **不得提供 `nav`**。策略一旦知道有多少钱，仓位就脱离资金管理，风控也就失去了分母。
26. `compute()` 返回 `None` **必须被拒**。空列表表示"今天没信号"，`None` 表示"忘了 return"，两者语义不同，不能同一个分支处理。
27. 策略包 **不得 import 任何生产模块**（唯一例外 `quantos_core.strategy_api`，纯数据契约）。AST 体检是**门槛不是沙箱** —— 真正的隔离需要独立进程与资源限额，本批不做也不假装做了。
19. `EstimationConfig.annualization` **不得有默认值**（股票 252 / Crypto 365，用错差约 20%）；`dof_ratio` 的分母**必须是待估参数个数** `n + n(n-1)/2`，不是标的个数。后者是同一份数据在两种口径下给出相反结论（26.67 vs 53.3）的那一处。
20. `CovarianceEstimate.raw_corr` **不得省略**，落盘（`to_dict`）也必须带。收缩能把 ρ 从 0.87 压到 0.62，只留收缩后的值等于把人为选择说成观测事实。
21. `apply_split` **不得改任何金额字段**（尤其 `buy_at_decision`）。拆股只改 `qty` / `attr_qty` / `avg_cost`，改金额等于凭空造收益，而且没有任何一笔成交能解释它。
22. 派生字段（入金/出金/手续费/资金费/分红累计）的累加**必须先过 `Ledger.was_written(entry)`**。去重只管住了现金，派生字段无条件累加会让净值凭空偏掉，而 **fx 残差会把差额吸收掉、恒等式照样闭合**。
23. `DiffKind.CORPORATE_ACTION` 的严重度**必须排在 `POSITION` / `CASH` 之前**；归因里**分红必须单列**，不得留给 fx 残差。前者防止运维修症状，后者防止真正的原因从报表里消失。
