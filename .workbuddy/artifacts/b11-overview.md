# B11 · 真实网关（Alpaca 纸面）

> 验收：**14/14 套件、1625 条断言**通过（B11 新增 142 条）
> 真下单这一环第一次被真实检验 —— 但**只到纸面账户**

## 这一批解决的是哪道墙

B10 之后，整条链路只剩一处没被真实检验：**真下单**。

`PaperBroker` 是个诚实的假券商：立刻成交、从不拒单、从不超时。B2 之后写的
所有防线（幂等键、超时只查不重发、部分成交、对账不平）在它面前全是
「代码看起来对」。这一批把同一套 `VenueGateway` 接口接到一个真会拒绝、
真会排队、真会部分成交的地方。

为什么是纸面而不是直接上真钱：纸面有真撮合、真回报、真错误码，但没有真钱。
它正好能验证「网关这一层对不对」，而验证错一次的代价是 0 ——
fail-closed 不等于 fail-brave。

## 做了什么

| 文件 | 作用 |
|---|---|
| `quantos_core/alpaca_gateway.py` | 纯 stdlib REST 网关：transport 协议 / 端点白名单 / 错误二分 / 状态归一化 / 探针 |
| `scripts/quantos_alpaca.py` | `probe`（只读）/ `orders` / `reconcile` / `roundtrip`（真下单，需 `--yes`） |
| `scripts/quantos_selftest_b11.py` | 142 条断言，A~K 十一段，**全部离线**（FakeTransport） |
| `quantos_core/venues.py` | 新增 `alpaca` 能力画像 + `supports_fractional` |
| `ops_server` / `quantos_ops_server` / `index.html` | `GET /api/ops/gateway` + 网关卡片 |
| `scripts/quantos_up.py` | 执行网关从「未接入」占位 → 真探针（up / degraded / absent / down） |

## 三条实测出来的事实（文档没写，但决定了适配器的形状）

1. **`DELETE /v2/orders:by_client_order_id` 不存在。** 下单有 `client_order_id`
   维度，撤单却只认场所自己的 `order_id`。所以撤单必须两步：先回查换出 id，
   再 `DELETE /v2/orders/{id}` —— 中间存在「单子刚成交」的缝。
2. **重复的 `client_order_id` 返回 422，不是 409。** 和「数量非法」同一个状态码，
   只能靠 message 里的 `client_order_id` + `unique` 区分。把它当普通 422 拒绝，
   等于把「这笔单已经存在」误判成「这笔单没下出去」，然后上层就会重发。
3. **`done_for_day` 是当日单的终点，可能带着部分成交。** 我们的 `OrderState`
   没有「部分成交后被撤」这一档，映射成 `CANCELLED` + 保留 `filled_qty`。
   映射成 `EXPIRED` 会让 OMS 等残量，映射成 `PARTIALLY_FILLED` 会让它永远不终态。

## 硬证据（真实往返，不是断言）

```
参考价（last）  11.745
买入回报      FILLED（filled） 可见 732ms  成交 1@11.75
买入滑点      -4.26 bp
卖出回报      FILLED  成交 1@11.74  可见 2888ms
卖出滑点      -4.26 bp
场所持仓      空仓 ✓
场所现金      99999.99 USD
账本现金      99999.99000000 USD
```

场所现金与账本现金**分币不差**，持仓归零。这是 `PaperBroker` 永远给不出的证据。

## 立起来的五道闸（全部在发请求之前）

`reduce_only`（美股现货没这概念）· 名义额超单笔上限 · 限价+碎股 · 休市 · 账户
`trading_blocked` / 非 ACTIVE —— 每一条都断言 `POST` 次数为 0。

再加上端点守卫：只认纸面；真钱要 `QUANTOS_ALPACA_LIVE=1` **和** `allow_live=True`
同时在场；自定义 host（含 `127.0.0.1`）一律拒。凭据躺在 `.env` 里，一个 URL
拼写错误就会打到真钱账户 —— 这种错误必须在下单前断掉。

## 修掉的真 bug 与踩到的坑

1. **`load(allow_absent=True)` 返回的是空账本对象，不是 `None`。** 首次建仓判定写成
   `if result.ledger is not None: return` 就会把「没有账本」当成「账本是空的」，
   现金 0 —— 第一笔真实成交已经发生，记账本时才炸。判定必须看 `status == "absent"`。
2. **`quantos_up` 没把项目根加进 `sys.path`**，探针报 `No module named 'quantos_core'`，
   而那看起来像「网关未接入」—— 假阴性的典型样子。
3. **`--status --json` 不是纯 JSON**（先打人类可读几行再打 JSON），`| jq` 会炸。
4. **`loadLive()` 里 `pending = 6` 而实际有 8 条请求**，`refreshAll` 提前触发、
   `LIVE.busy` 提前置回。改成计数器。
5. **备注被画成告警条**：探针通过时「市场当前开盘」也是黄条。黄条一多就没人读，
   真正的告警会被埋掉。改成按健康度分样式。
6. **故障夹具把 `query_by_client_id_ok` 写反了**，导致「回查不可用」显示成「连不上」。

## 六条已知限制

1. **只到纸面账户。** 真钱端点有开关，但**没有任何真实资金联调的证据**。
2. **A 股链路没接网关。** `tick` 的成交仍走 `PaperBroker`；Alpaca 是美股，
   与 A 股股票池不是同一批标的，两者只是共用同一套网关契约。
3. **没有 WebSocket。** 全靠轮询；可见延迟实测 700~3700ms 波动。
4. **手续费按 0 记。** 佣金确实 0，但 SEC/TAF 费用在账户账单里存在，账本没记。
5. **没接进风控与 OMS 的实盘路径。** `place()` 是库，不是运行中的下单通道。
6. **行情用 IEX feed**（免费档），不是全市场 SIP 汇总价。

## 文档同步

- `03-推进路线图.md`：新增第 13 章（为什么 / 任务表 / 九条 DoD 证据 / 六条坑 / 六条限制），
  风险登记册补 6 行，章节顺延到 14/15
- `02-服务数据契约.md`：新增 **G.10 `GET /api/ops/gateway`** 与 10 条契约要点
- `README.md`：代码位置 2 行 + 3 条命令 + 计数
- `04-未完成清单.md`：1.1 从「没有真实网关」改成「已解除 + 还剩什么」，
  部署表补一行，第 6 节 B11 标完、B12 升到 P0

## 下一步

**B12 回测引擎**（用户点名的那件事）：把 `research/` 里手工做的样本外验证变成
平台的第 N 个功能，复用同一套 `Ledger` / 成本模型 / 风控 —— 这三样现在都齐了，
做回测不会返工。

真钱联调不是「再写一批代码」能解决的：需要 T-404 的两周小额观察期，以及把
`AlpacaGateway` 接进 `tick` 的下单链路。前者要时间，后者要真钱。
