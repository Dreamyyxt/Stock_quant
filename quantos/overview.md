# QuantOS 2.0 · 基金管理终端（交付说明）

## 交付物

| 类型 | 内容 |
| --- | --- |
| 在线页面 | https://workbuddy.link/p/d1xCrrZhxVjrCktWwbLWG4 （任何人可打开） |
| 协作编辑 | https://www.workbuddy.cn/space/d/d1xCrrZhxVjrCktWwbLWG4 |
| 本地源文件 | `quantos/index.html`（单文件，133KB，零外链） |
| 云端数据表 | 「QuantOS 审计台账」`7ZJXAfyWFnBeP3fot5pkew`（已挂在该页面节点下） |

## 页面结构（4 个模块）

1. **组合总览** — 今日待处理队列（逾期标红 + 一键处理）置顶；NAV / 今日盈亏 / 名义敞口 / 当前回撤 / 策略运行 5 张 KPI；8 周净值曲线（内联 SVG，含初始净值基准线）；资金分配（股票池 5 万 / Crypto 池 3 万 / 现金缓冲 2 万，含使用率）；系统健康 6 项。
2. **策略中心** — Research→Backtest→Paper→Live 生命周期；5 个策略卡片（配额/已投/盈亏/最近信号/风险贡献 + 暂停·恢复·仅减仓·提交上线审批）；交易意图汇总表（目标权重、信心度、触发依据，标注「不下发」）。
3. **风控与组合** — 交易权限三级切换（NORMAL / REDUCE_ONLY / HALTED，HALTED 二次确认且明确「不等于市价清仓」）；三道防线（订单级/策略级/基金级，命中次数与处置动作）；敞口环形图；资金配额与风险贡献条；阈值配置表单（保存即写审计，若新回撤线低于当前回撤会二次确认并触发熔断）。
4. **运维与审计** — 生产平面健康 6 项；订单状态机流水（含「状态未知」与「部分成交」，点开看决策依据）；云端审计台账（真实读写）；数据管理（导出 JSON / 导入恢复 / 清空重置）。

## 数据流

- 审计台账：页面 `db.query` 分页拉取（`startCursor`/`hasMore` 三重保险）→ 渲染；任何人工操作（暂停策略、处理告警、切权限、改阈值、提交审批）`db.addRecord` 追加一条记录。
- 失败降级：无 SDK 或写入失败 → localStorage 暂存 + 顶部显示「离线模式」+ toast 提示，恢复后自动补写。
- 本地状态（策略开关、阈值、告警已读）：`localStorage['wb_quantos_v2_state']`，可导出/导入 JSON。

## 校验结果

- `lint_schema.py`：MINDX_LINT_OK
- `lint_database_sdk_usage.py`：MINDX_DBSDK_LINT_OK
- `lint_page_quality.py`：MINDX_PAGE_QUALITY_OK（PQ001 / PQ005 带 ack 放行）
  - PQ001 误报：检测器用启发式剥离正则字面量，把 JS 字符串里的 `</div>` 误判为正则起始，吞掉 2 个右括号，导致「括号未配平」。`node --check` 已确认语法有效。
  - PQ005 误报：所有 innerHTML 动态插值均经 `esc()` 转义、数字经 `Number/toFixed` 处理；检测器要求整段右值被 `escapeHtml()` 整体包裹，与拼接 HTML 标记的写法天然不兼容。
- 自建 DOM 探针冒烟：20 项渲染断言通过（KPI 数值、待处理 5 项、净值 SVG、策略卡、意图表、三道防线、环图、订单流水、权限/同步状态）。
- 线上产物已回拉比对，8 处数据库绑定标记与全部模块文案完整（平台仅在元素上注入了 `data-page-node-id`）。

## 待接入（原型 → 生产）

1. 策略/仓位/成交/净值目前是演示数据，需接生产平面 API（SSE 或轮询）替换 `defaultState()`。
2. 审计台账可直接复用；建议再加 `signals`、`orders/fills`、`risk_events` 三张表，与页面同构。
3. 股票实盘注意：IBKR + LEAN 不支持 Apple Silicon ARM Docker 本地实盘，生产节点用 x86 Linux；IB Gateway 需周期性图形化重认证，不能承诺永久无人值守。
