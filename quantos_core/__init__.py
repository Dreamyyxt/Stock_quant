"""
QuantOS 2.0 核心层
==================

五服务之间的数据契约、资金分配、风控、账本、事件与执行基础设施。
对应 `docs/QuantOS/02-服务数据契约.md`，需求编号见 `00-系统要求.md`。

模块分层（严格单向，禁止反向 import）
------------------------------------
L0 领域层（不依赖任何本项目模块）
    contracts.py          领域对象 + 订单状态机 + 幂等键构造
    ledger.py             复式记账、NAV、五维归因、对账

L1 计算层（只依赖 L0）
    capital_allocator.py  额度分配 + 多策略净额 + OrderIntent 构造
    risk_engine.py        三道防线、三级权限、HALT 处置剧本、UNKNOWN 判定
    cost.py               费用/滑点模型（回测与实盘共用一份参数文件）
    market.py             行情快照契约（缺价显式 None）

L2 基础设施层（依赖 L0/L1）
    codec.py              领域对象线格式（进程间 / 落盘快照 / 事件日志共用）
    events.py             事件存储（JSONL / SQLite，单调 seq、幂等追加、尾部自愈）
    strategy_adapter.py   现有策略输出 → StrategyIntent（不重写策略逻辑）
    replay.py             事件回放器（确定性重建决策链与账本）
    idempotency.py        幂等键持久化分配器
    oms.py                订单管理（风控→注册→状态机的唯一合法顺序）
    reconciliation.py     UNKNOWN 订单对账闭环
    locks.py              账户×网关单活锁
    strategy_registry.py  策略实例 + 生命周期闸门 + 可回放审计（T-303）

L3 运维层（依赖 L2）
    metrics.py            指标采集（每条指标带 severity / threshold / source）
    ops_server.py         只绑回环的运维 HTTP 面（token 保护写操作）
    risk_service.py       风控独立进程 + fail-closed 客户端

为什么分层这么死板
------------------
`risk_service` 需要 `codec`，`oms` 也需要 `codec`，而 `codec` 只依赖 `contracts`。
如果哪天有人让 `codec` 去 import `oms`，就会出现"为了一个函数把整个执行层拖进
风控进程"的循环依赖 —— 那种项目通常在第三个月开始没人敢改风控。

验收
----
    scripts/quantos_selftest.py      B0 契约与账本        48 条
    scripts/quantos_selftest_b1.py   B1 数据层与回放      93 条
    scripts/quantos_selftest_b2.py   B2 风控与幂等        73 条
    scripts/quantos_fault_inject.py  故障注入（T-205）    7 个场景
    scripts/quantos_selftest_b3.py   B3 运维中心         103 条（含前端渲染探针）
    scripts/quantos_ops_check.py     T-302 接口与进程内抽检 6 项
    scripts/quantos_verify.py        一把全跑
"""

from quantos_core import contracts          # noqa: F401  L0
from quantos_core import ledger             # noqa: F401  L0
from quantos_core import capital_allocator  # noqa: F401  L1
from quantos_core import risk_engine        # noqa: F401  L1
from quantos_core import cost               # noqa: F401  L1
from quantos_core import market             # noqa: F401  L1
from quantos_core import codec              # noqa: F401  L2
from quantos_core import events             # noqa: F401  L2
from quantos_core import strategy_adapter   # noqa: F401  L2
from quantos_core import replay             # noqa: F401  L2
from quantos_core import idempotency        # noqa: F401  L2
from quantos_core import oms                # noqa: F401  L2
from quantos_core import reconciliation     # noqa: F401  L2
from quantos_core import locks              # noqa: F401  L2
from quantos_core import strategy_registry  # noqa: F401  L2
from quantos_core import metrics            # noqa: F401  L3
from quantos_core import ops_server         # noqa: F401  L3
from quantos_core import risk_service       # noqa: F401  L3

__version__ = "0.3.0"
