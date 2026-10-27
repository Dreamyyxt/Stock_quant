# 项目目录结构约定

> 2026-10-26 起生效。之后所有新生成的文件必须按本约定落位，不要再往项目根目录堆。

## 总览

```
Stock_quant/
├── main.py                    # 实盘/模拟盘入口（TSLA 日内引擎）
├── config/  core/  intraday/  execution/  risk/  scheduler/  screener/
│                              # 实盘引擎各层（别动结构）
├── strategies/                # 引擎内的策略类（OrbTslaSignals 等）
├── backtest/                  # 引擎配套回测器
├── reporting/                 # 三件套导出 + HTML 仪表盘模板（共享）
│   └── vendor/                # marked/DOMPurify 内联库（离线打开依赖）
├── scripts/                   # 跨策略共享脚本（数据抓取、运维、监控）
├── research/                  # ★ 所有研究型回测按策略归档
│   ├── tesla_orb/             # TSLA 开盘区间突破（分钟线）
│   │   ├── tsla_breakout_backtest.py    # 主回测
│   │   ├── engine_replay_tsla.py        # 真引擎逐日回放
│   │   ├── tsla_{lab,refine,cost}.py    # 参数/成本研究
│   │   ├── fetch_tsla_sessions.py       # 数据抓取
│   │   └── outputs/                     # equity/trades/summary/图/仪表盘
│   ├── ashare_ew20/           # A股 中证800 等权+20日再平衡（可上线基线）
│   │   ├── ew20_backtest.py
│   │   └── outputs/
│   ├── ashare_xsec/           # A股 横截面因子检验（rev20/mom120 + 稳健性）
│   │   ├── ashare_xsec_backtest.py
│   │   ├── ashare_robustness.py         # 11候选×4类测谎矩阵
│   │   ├── ashare_factor_lab.py         # 105检验因子工厂
│   │   ├── render_ashare_xsec.py        # 仪表盘渲染 → index.html
│   │   ├── index.html
│   │   └── outputs/
│   └── ashare_etf_510300/     # 510300 单标的日频（mom_5d 检验 + 落地回测）
│       ├── rev5_510300_backtest.py
│       └── outputs/
├── state/                     # ★ 数据层（所有策略共用，不放代码）
│   ├── ashare/                # 中证800 日线 csv、指数、审计、robustness.json
│   ├── tsla_1min.csv          # TSLA 分钟线缓存
│   └── ...
├── docs/                      # 文档（本文件、研究框架、上线清单）
└── logs/                      # 运行日志
```

## 规则

1. **新回测** → `research/<策略名>/<前缀>_backtest.py`，产物一律写该目录的
   `outputs/`。策略名用市场+方法，如 `ashare_ew20`、`tesla_orb`。
2. **路径常量**（每个研究脚本顶部统一写法）：
   ```python
   ROOT = Path(__file__).resolve().parent      # 策略目录；产物写 ROOT/outputs
   PROJ = ROOT.parent.parent                   # 项目根：state/ 与 reporting/ 在这里
   OUT  = ROOT / "outputs"
   ```
   数据读 `PROJ/state/...`，共享库 `from reporting.xxx import ...`，输出 `OUT/...`。
3. **数据**只进 `state/`，带审计文件（fetch_audit.json）。抓取脚本放 `scripts/`。
4. **仪表盘** HTML 写策略目录根（如 `research/ashare_xsec/index.html`），
   不再放项目根；渲染前先 headless 截图自检，CDN 依赖用 `reporting/vendor/` 内联。
5. **shared 工具**（多策略都会用）才放 `scripts/`；策略专属脚本跟策略走。
6. 跑法：`python research/ashare_ew20/ew20_backtest.py`（任意 cwd 均可，
   脚本内部用绝对路径锚定）。
