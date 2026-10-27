# Stock_quant 项目长期约定

## 目录结构（2026-10-26 起，用户明确要求）

- 研究/回测一律放 `research/<策略名>/`，产物写该目录的 `outputs/`，仪表盘 HTML 写策略目录根。
  - 现有：`research/tesla_orb`（TSLA 分钟线 ORB + 真引擎回放）、`research/ashare_ew20`
    （中证800 等权20日再平衡，可上线基线 + 本地影子盘 `paper_trade.py`）、
    `research/ashare_xsec`（横截面因子检验）、`research/ashare_etf_510300`。
- 数据只进 `state/`（带 fetch_audit.json 审计）；跨策略工具脚本放 `scripts/`；
  文档放 `docs/`（目录约定见 `docs/STRUCTURE.md`）。
- 研究脚本顶部统一路径写法：`ROOT=脚本目录`、`PROJ=ROOT.parent.parent`（state/reporting 在项目根）、
  `OUT=ROOT/"outputs"`。不要再往项目根堆文件。

## 环境与坑

- Python：`.venv/bin/python`（3.13）。沙箱 shim 下 `Path.mkdir(exist_ok=True)` 会误报
  FileExistsError，用 `os.makedirs(..., exist_ok=True)`。
- westock CLI（~/.local/bin/westock）：批量 kline 一次最多 20 只但内部拆 2 个子请求，
  第二个必撞频控且 rc=0 静默少给 → BATCH=10 + 间隔 8s + 缺号重试；分钟线仅近 1 月/跨度≤10 天。
- A 股日线口径：不复权 + 接口 change_pct 反推含股息价；复权因子 clip [0.5,2.0]；
  停牌行 open=0 补成收盘价、volume=NaN。
- 仪表盘：CDN 依赖已内联在 `reporting/vendor/`；`_sanitize()` 兜 NaN（裸 NaN 是非法 JSON 会白屏）；
  交付前必须 headless Chrome 截图自检。
- **Chrome headless 窗口宽度下限 500px**：`--window-size=390` 会被夹到 500，390px 窄屏
  没法用命令行验证（iframe 包也不行，`--dump-dom` 只返回主文档）。窄屏核对用 500px 截图。
- 页面截图自检：起 `scripts/quantos_ops_server.py --html <临时副本>`，副本里把
  `var ui = { view: 'overview' }` 改成 `'ops'` —— 在 `setTimeout` 里点 `[data-nav="ops"]`
  在 virtual-time 下不可靠。

## QuantOS 架构（B0~B7 全部代码完成，`scripts/quantos_verify.py` 10/10 套件 1068 条）

- 铁律：策略只能发意图；超时**绝不重试**先查交易所；`HALTED ≠ 市价清仓`；
  **fail-closed**（缺数据报错/拒绝，绝不填默认值）；**宁可显示缺口，也不显示假绿灯**。
- `quantos_core/`：contracts / allocator / risk_engine / ledger / events / market / cost /
  replay / risk_service / idempotency / oms / reconciliation / locks / strategy_registry /
  metrics / ops_server / live_gate / eod / gateway / venues / ccxt_gateway / funding /
  scheduler / risk_budget / canary / ai_research / **covariance**。
- 运维面 `quantos/index.html` 默认读 `/api/ops/*`（只绑 127.0.0.1），取不到才退回演示并显式标注。
  渲染层规矩：渲染函数**互不调用**，联动走 `refreshAll()`；补充数据请求失败不计入离线判定。
- 归因是**六项**（trading/slippage/fee/dividend/funding/fx），`fx` 是残差 ——
  所以恒等式闭合证明不了没漏记，真能抓错的是「单币种 fx 必须精确为 0」「手续费 ≤ 0」。
- 协方差估计：`annualization` 必填无默认；对齐用日期**交集不填充**；
  `dof_ratio` 分母是待估参数 `n+n(n-1)/2` 而非标的数；`raw_corr` 必须与收缩后一起显示并落盘。

## A 股研究结论（2026-10-26）

- 选股因子（rev/mom 十分位）相对等权全池的超额仅 ~20pp/7.7年，随机组合零分布中位数就有 +265%。
- **等权 + 20日再平衡本身就是最大的 alpha**（小盘倾斜 + 波动率收割）。
- 可上线口径（开盘成交、全摩擦、整手、涨跌停、1000万）：全池 794 只年化 18.78%、MDD -26.1%、
  Sharpe 0.97；只数是第一杠杆（200只→14.7%，50只→6.2%）；流动性前 200 名反而跑输全池。
- 幸存者偏差无法测量（退市股行情不可得、无时点成分股），只能给界（年拖累 ≈ 调出率×价差，
  见 ew20 summary 的 survivorship_bound）。
- TSLA ORB：引擎与回测对账一致（838笔方向零差异）；模拟盘走影子模式。
- A 股模拟交易：老虎不能交易 A 股；用 `research/ashare_ew20/paper_trade.py` 本地影子盘
  （status/init/plan/settle），账本在 `state/ashare/ew20_paper_book.json`。
