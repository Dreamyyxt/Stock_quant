"""
QuantOS 2.0 — 回测引擎（B12 / T-1202 ~ T-1205）
==============================================

把历史日线翻译成事件流，喂给**同一个** `Replayer`，账务/风控/净额全部复用。

为什么不是"再写一个回测脚本"
----------------------------
`research/ashare_ew20/ew20_backtest.py` 已经能用，结论也经过多轮手工核对。
那为什么还要把它搬进平台？因为那套脚本的价值和它的风险是同一件事：
**它自己算账**。自己算的账没人能审计，也没法和实盘对账 —— 一旦回测和实盘
是两套代码，两边数字不一样时，你无法判断是策略失效还是记账错了。

所以这里的做法不是"移植算法"，而是**换掉记账的人**：

    bar 面板 ──► 事件流（market_snapshot / strategy_intent / cycle / fill）
                     │
                     ▼
                 Replayer（B1 就有，实盘回放走的是它）
                     │
                     ▼
              Ledger / RiskEngine / MarketDataBook

回测在这里只做两件事：**生产事件**、**模拟撮合**。其余一行都不重写。

这一批要证明的四件事
--------------------
1. **没有未来函数**（T-1203）。信号用 t 日收盘算，成交只发生在 t+1 开盘。
   这不是"我们小心地没用未来数据"，而是**结构上不可能**：
   - `_settle()` 读 `bar(i)` 的**开盘价**，且它在一天里**先执行**；
   - `_signals()` 读 `bar(i)` 的**收盘价**，且在**后执行**。
   当天成交发生在当天收盘之前，所以任何一天的成交都不可能用到当天收盘。
   反过来改顺序就是引入未来函数 —— 这段注释放在这里就是为了让人改的时候看见。
2. **会计恒等式成立**（T-1205）。每日 NAV = 现金 + Σ持仓×收盘价，账本归因六项闭合。
3. **A 股规则不打折**（T-1204）。整手、涨停不买、跌停不卖、停牌不成交、
   T+1、现金不足不成交。每条都有独立计数，跑完能看见各拦了多少次。
4. **可复现**。同输入两次跑 digest 相同。

日线上的诚实边界（写在这里，不藏在代码注释里）
----------------------------------------------
- **成交口径是"次日开盘价 + 滑点"**。不模拟限价排队、不模拟盘中滑点路径。
  日线上做不出这些，假装能做只会得到一个精确的假答案。
- **停牌按最近收盘价估值，但绝不在停牌日成交**。估值用旧价、成交用新价，
  两件事从不混；快照的 `as_of` 记的是**那个旧价真正发生的日子**，不是今天。
- **`frame` 只给策略最近 `window` 根 bar**（默认 max(warmup+10, 60)）。
  要更长历史就抬高 `manifest.warmup_bars`；超窗口会 `FrameError`，
  不会"用现有的凑一凑"。
- **风控的「行情陈旧」闸在回测里关闭**。它是给分钟级链路设计的；日频下
  任何停牌持仓都会让它全天误判。回测里由撮合层按"停牌不成交"接管，
  判定更严而不是更松。这条偏差写进 `result.notes`，不静默。
"""

from __future__ import annotations

import csv
import hashlib
import math
import os
from array import array
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from quantos_core.capital_allocator import StrategyPolicy
from quantos_core.contracts import (
    ContractViolation, D, Order, OrderState, OrderType, Side, q,
)
from quantos_core.cost import CostModel
from quantos_core.events import EventEnvelope, canonical_json
from quantos_core.performance import (
    PerformanceStats, average_nav, compute_stats, cost_drag, exposure_stats,
    yearly_returns,
)
from quantos_core.replay import ReplayConfig, ReplayResult, Replayer
from quantos_core.risk_engine import RiskPolicy
from quantos_core.strategy_adapter import AdapterConfig, adapt_signals, intent_to_payload
from quantos_core.strategy_api import (
    MarketFrame, Series, StrategyContext, StrategyError,
    StrategyManifest, check_strategy, coerce_signal,
)
from quantos_core.venues import all_admissions
from quantos_core.venues import profile as venue_profile

CN_TZ = timezone(timedelta(hours=8))
DEFAULT_VENUE = "xshg"
OPEN_TIME = time(9, 30)
CLOSE_TIME = time(15, 0)
NAN = float("nan")

_ZERO = Decimal(0)


# --------------------------------------------------------------------------- #
#  异常
# --------------------------------------------------------------------------- #

class BacktestError(RuntimeError):
    """回测基础设施错误（配置不合法、日历不够长）。不是策略的错。"""


class DataQualityError(BacktestError):
    """数据本身有问题，而任何兜底都会让结论失效 —— 必须停下来说清楚。"""


# --------------------------------------------------------------------------- #
#  bar 与面板
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Bar:
    """一根日线。价格已按 `adjust` 口径处理过（默认后复权）。"""

    day: date
    open: Decimal
    close: Decimal
    volume: Decimal
    amount: Decimal = _ZERO

    def __post_init__(self) -> None:
        if self.open <= 0 or self.close <= 0:
            raise DataQualityError(f"{self.day} 价格非正：{self.open}/{self.close}")

    @property
    def suspended(self) -> bool:
        """停牌判据：成交量为 0。注意**价格平坦不等于停牌** —— 涨跌停也平坦。"""
        return self.volume <= 0


def _dec(x: float) -> Decimal:
    """
    float → Decimal，按 6 位小数收敛。

    面板内部用 float 存（1879×794 个 Decimal 要吃几百 MB），但每一个进入
    账本/意图的数字都必须是 Decimal。这里是唯一的转换点，口径也就只有这一处：
    1e-6 对股价、对名义额都远小于任何有意义的分辨率。
    """
    if not math.isfinite(x):
        raise DataQualityError(f"价格/数量不是有限数：{x!r}")
    return Decimal(str(round(x, 6)))


class BarPanel:
    """
    多标的日线面板。内部按**全局交易日历对齐**，缺失为 NaN。

    为什么不用 `dict[date, Bar]`：1879 个交易日 × 794 只 ≈ 149 万个对象，
    Python 里是几百 MB 常驻内存。三条 `array('d')` 对齐到日历只要 ~40MB，
    而且"某天有没有 bar"变成一次 `isnan` 判断。
    """

    __slots__ = ("calendar", "symbols", "_open", "_close", "_volume", "_amount",
                 "_index", "adjusted", "skipped")

    def __init__(self, calendar: Sequence[date], symbols: Sequence[str],
                 opens: Mapping[str, array], closes: Mapping[str, array],
                 volumes: Mapping[str, array], amounts: Mapping[str, array],
                 *, adjusted: bool = True,
                 skipped: Sequence[str] = ()) -> None:
        self.calendar: Tuple[date, ...] = tuple(calendar)
        self.symbols: Tuple[str, ...] = tuple(symbols)
        self._open, self._close = opens, closes
        self._volume, self._amount = volumes, amounts
        self._index: Dict[date, int] = {d: i for i, d in enumerate(self.calendar)}
        self.adjusted = adjusted
        #: 被丢掉的标的原因（数据太短/解析失败）。幸存者偏差的一部分，必须可见。
        self.skipped: Tuple[str, ...] = tuple(skipped)

    # -- 构造 ------------------------------------------------------------ #

    @classmethod
    def from_rows(cls, rows: Mapping[str, Sequence[Tuple]], *,
                  adjusted: bool = True) -> "BarPanel":
        """
        测试/小规模用：`{symbol: [(day, open, close, volume[, amount]), ...]}`。

        生产走 `load()`。两条路最后落到同一套 array 结构，所以自测跑的就是
        生产代码，不是另一条分支。
        """
        if not rows:
            raise DataQualityError("面板没有任何标的")
        return cls._build(rows, adjusted, ())

    @classmethod
    def load(cls, data_dir: str, *, adjust: bool = True,
             min_bars: int = 60, max_names: Optional[int] = None,
             start: Optional[date] = None, end: Optional[date] = None,
             ) -> "BarPanel":
        """
        读 `state/ashare/daily/*.csv`
        （`date,open,close,high,low,volume,amount,chg_pct,adj_factor`）。

        `adjust=True` 用 `price × adj_factor`。这份数据 adj_factor 在最新一天是
        1.0、越往前越小，是**后复权**（分红再投资）。用它做回测意味着分红自动
        进了收益，不需要额外的公司行动事件，也不会在除权日凭空少一块钱。
        """
        if not os.path.isdir(data_dir):
            raise DataQualityError(f"数据目录不存在：{data_dir}")
        files = sorted(f for f in os.listdir(data_dir) if f.endswith(".csv"))
        if not files:
            raise DataQualityError(f"{data_dir} 里没有 csv")
        if max_names:
            files = files[:max_names]

        raw: Dict[str, List[Tuple[date, float, float, float, float]]] = {}
        skipped: List[str] = []
        for name in files:
            sym = name[:-4]
            rows: List[Tuple[date, float, float, float, float]] = []
            try:
                with open(os.path.join(data_dir, name), "r", encoding="utf-8-sig") as fh:
                    for rec in csv.DictReader(fh):
                        try:
                            day = date.fromisoformat(rec["date"])
                            if start and day < start:
                                continue
                            if end and day > end:
                                continue
                            adj = (float(rec["adj_factor"])
                                   if adjust and rec.get("adj_factor") else 1.0)
                            o = float(rec["open"]) * adj
                            c = float(rec["close"]) * adj
                            # 停牌行 volume 是空串 —— 那正是"今天没成交"。
                            vs = (rec.get("volume") or "").strip()
                            v = float(vs) if vs else 0.0
                            a = float(rec.get("amount") or 0) or 0.0
                        except (KeyError, ValueError):
                            continue
                        if not (o > 0 and c > 0):
                            continue
                        rows.append((day, o, c, v, a))
            except OSError as exc:
                skipped.append(f"{sym}(读失败:{exc.__class__.__name__})")
                continue
            if len(rows) < min_bars:
                skipped.append(f"{sym}({len(rows)}<{min_bars}根)")
                continue
            rows.sort(key=lambda r: r[0])
            raw[sym] = rows

        if not raw:
            raise DataQualityError(
                f"没有任何标的达到 {min_bars} 根 bar（丢弃 {len(skipped)} 个）")
        return cls._build(raw, adjust, skipped)

    @classmethod
    def _build(cls, raw: Mapping[str, Sequence[Tuple]], adjust: bool,
               skipped: Sequence[str]) -> "BarPanel":
        calendar = sorted({r[0] for rs in raw.values() for r in rs})
        idx = {d: i for i, d in enumerate(calendar)}
        symbols = sorted(raw)
        n = len(calendar)

        def col(k: int, default: float) -> Dict[str, array]:
            out: Dict[str, array] = {}
            for sym in symbols:
                arr = array("d", [NAN] * n)
                for r in raw[sym]:
                    if len(r) > k and r[k] is not None:
                        arr[idx[r[0]]] = float(r[k])
                    else:
                        # 缺成交额 ≠ 停牌；缺成交量 = 停牌（数据里就是空串）。
                        arr[idx[r[0]]] = 0.0 if k == 3 else default
                out[sym] = arr
            return out

        return cls(calendar, symbols, col(1, NAN), col(2, NAN), col(3, 0.0),
                   col(4, 0.0), adjusted=adjust, skipped=skipped)

    # -- 读取 ------------------------------------------------------------ #

    def __len__(self) -> int:
        return len(self.calendar)

    def index_of(self, day: date) -> Optional[int]:
        return self._index.get(day)

    def first_index_on_or_after(self, day: date) -> Optional[int]:
        """
        第一个 ≥ day 的交易日。用户给的日期常常是周末或节假日（"从 2024 年起"），
        为此报错只会让人反复试日期 —— 但它也**不许**悄悄往前找，
        那会把起点挪到未来数据里。
        """
        if not self.calendar:
            return None
        lo, hi = 0, len(self.calendar) - 1
        if day <= self.calendar[0]:
            return 0
        if day > self.calendar[-1]:
            return None
        while lo < hi:
            mid = (lo + hi) // 2
            if self.calendar[mid] < day:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def has_bar(self, symbol: str, i: int) -> bool:
        arr = self._open.get(symbol)
        return arr is not None and 0 <= i < len(arr) and math.isfinite(arr[i])

    def bar(self, symbol: str, i: int) -> Optional[Bar]:
        """第 i 天的 bar。没有数据（未上市/无行/越界）返回 None。"""
        if not self.has_bar(symbol, i):
            return None
        return Bar(day=self.calendar[i], open=_dec(self._open[symbol][i]),
                   close=_dec(self._close[symbol][i]),
                   volume=_dec(self._volume[symbol][i]),
                   amount=_dec(self._amount[symbol][i]))

    def close_with_date(self, symbol: str, i: int) -> Optional[Tuple[Decimal, date]]:
        """
        最近一次收盘 **以及它真正发生的日子**。

        返回日期是为了让停牌标的的行情快照能诚实地写 `as_of`：
        价格是三天前的，时间戳就得是三天前。用今天的时间戳标一个旧价，
        等于对风控说"这条行情很新"，而它恰恰是风控要抓的那种假新鲜。
        """
        arr = self._close.get(symbol)
        if arr is None:
            return None
        j = min(i, len(arr) - 1)
        while j >= 0:
            v = arr[j]
            if math.isfinite(v):
                return _dec(v), self.calendar[j]
            j -= 1
        return None

    def close_at(self, symbol: str, i: int) -> Optional[Decimal]:
        got = self.close_with_date(symbol, i)
        return got[0] if got else None

    def prev_close(self, symbol: str, i: int) -> Optional[Decimal]:
        """第 i 天**之前**的最近收盘（涨跌停基准）。停牌多日仍按自己上一次的收盘。"""
        return self.close_at(symbol, i - 1) if i > 0 else None

    def live_symbols(self, i: int) -> List[str]:
        """第 i 天**能成交**的标的：有 bar 且成交量 > 0。停牌的不算。"""
        out: List[str] = []
        for sym in self.symbols:
            arr = self._volume.get(sym)
            if self.has_bar(sym, i) and arr[i] > 0:
                out.append(sym)
        return out


# --------------------------------------------------------------------------- #
#  撮合规则（A 股）
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ArenaRules:
    """
    交易所撮合规则里"日线上怎么判定"的那部分（涨跌停宽度、停顿判据）。

    整手/最小金额**不在这里**：它们来自 `venues.profile("xshg")` 的能力矩阵，
    这样回测和实盘用的是同一份声明，不会各按各的规则取整。
    """

    limit_main: Decimal = Decimal("0.10")
    limit_growth: Decimal = Decimal("0.20")
    limit_tol: Decimal = Decimal("0.002")

    @staticmethod
    def lot_size(symbol: str) -> Decimal:
        """
        一手多少股。**按代码段分档**，不是按场所分档 —— 科创板（688）是 200 股起。

        能力矩阵里 `Capabilities.symbol_rules` 是按**标的**索引的，没法表达
        "所有 688 开头"这种模式；所以场所级只给默认值（100），代码段分档放这里。
        两者在 `DailyBacktest._lot_step` 里合流，并互相校验。
        """
        return Decimal("200") if symbol.lower().startswith("sh688") else Decimal("100")

    def limit_width(self, symbol: str) -> Decimal:
        s = symbol.lower()
        return self.limit_growth if (s.startswith("sh688") or s.startswith("sz30")) \
            else self.limit_main

    def at_limit_up(self, symbol: str, open_px: Decimal, prev_close: Decimal) -> bool:
        """开盘即涨停：买不到。容差吸收复权与四舍五入。"""
        return open_px >= prev_close * (1 + self.limit_width(symbol) - self.limit_tol)

    def at_limit_down(self, symbol: str, open_px: Decimal, prev_close: Decimal) -> bool:
        """开盘即跌停：卖不掉。"""
        return open_px <= prev_close * (1 - self.limit_width(symbol) + self.limit_tol)


# --------------------------------------------------------------------------- #
#  配置
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class BacktestConfig:
    """
    `cost_model` **没有默认值**：成本参数必须来自 `config/cost_model.json`。
    给它一个内置默认，等于允许"回测一套费率、实盘另一套"，而那种差异
    在结果里看不出来 —— 只会在实盘第一个月吃掉收益。
    """

    cost_model: CostModel
    strategy_policies: Sequence[StrategyPolicy]
    account_id: str = "QBT-0001"
    venue: str = DEFAULT_VENUE
    initial_cash: Decimal = Decimal("10000000")
    base_currency: str = "CNY"
    risk_policy: RiskPolicy = field(default_factory=RiskPolicy)
    #: "research" = 不参与日损/回撤熔断；"prod" = 完全按 RiskPolicy，会被 5% 回撤停住。
    risk_profile: str = "research"
    #: 每个场所的最小下单金额（组合层用，低于则判 NOOP 而不产生碎单）。
    min_notional: Mapping[str, Decimal] = field(default_factory=dict)
    strategy_params: Mapping[str, Any] = field(default_factory=dict)
    warmup_bars: int = 0
    start: Optional[date] = None
    #: 挂单最多存活几根 bar，超过就撤（相当于一张能挂 10 个交易日的日单）。
    max_pending_bars: int = 10
    #: 给策略的 frame 窗口（根）。小于 manifest.warmup_bars 会被拒。
    frame_window: int = 0
    #: 策略每轮最多看到多少只标的（None = 当日全部可成交标的）。
    max_frame_symbols: Optional[int] = None
    rules: ArenaRules = field(default_factory=ArenaRules)

    def __post_init__(self) -> None:
        if D(self.initial_cash) <= 0:
            raise BacktestError("initial_cash 必须为正")
        if self.risk_profile not in ("research", "prod"):
            raise BacktestError(f"risk_profile={self.risk_profile!r} 不支持")
        if self.max_pending_bars < 1:
            raise BacktestError("max_pending_bars 至少为 1")
        if not self.strategy_policies:
            raise BacktestError("至少要一个 StrategyPolicy，否则净额层不知道钱归谁")

    def replay_config(self) -> ReplayConfig:
        """
        派生回放配置。两处刻意偏离，都进 `result.notes`：

        - `order_type=MARKET`：日线上模拟不了限价排队，只能市价按开盘价成交。
        - 行情陈旧闸关闭：见模块 docstring 最后一条。
        """
        policy = replace(self.risk_policy, stale_market_data_action="allow")
        return ReplayConfig(
            account_id=self.account_id,
            strategy_policies=tuple(self.strategy_policies),
            risk_policy=policy,
            cost_model=self.cost_model,
            base_currency=self.base_currency,
            order_type=OrderType.MARKET,
            min_notional=dict(self.min_notional),
        )


# --------------------------------------------------------------------------- #
#  结果
# --------------------------------------------------------------------------- #

@dataclass
class BacktestResult:
    config: BacktestConfig
    manifest: Optional[StrategyManifest]
    stats: PerformanceStats
    series: List[Tuple[date, Decimal]] = field(default_factory=list)
    yearly: List[Dict[str, Any]] = field(default_factory=list)
    replay: Optional[ReplayResult] = None
    #: 各类阻断/跳过的次数。跑完能看见每条规则各拦了多少。
    blocked: Dict[str, int] = field(default_factory=dict)
    cost: Dict[str, Any] = field(default_factory=dict)
    exposure: Dict[str, Any] = field(default_factory=dict)
    trades: List[Dict[str, str]] = field(default_factory=list)
    trades_truncated: bool = False
    #: 必须让用户看见的口径说明与偏差。
    notes: List[str] = field(default_factory=list)
    digest: str = ""

    @property
    def final_nav(self) -> Optional[Decimal]:
        return self.series[-1][1] if self.series else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "digest": self.digest,
            "strategy": self.manifest.to_dict() if self.manifest else None,
            "venue": self.config.venue,
            "risk_profile": self.config.risk_profile,
            "initial_cash": str(q(self.config.initial_cash, 2)),
            "final_nav": (str(q(self.final_nav, 2))
                          if self.final_nav is not None else None),
            "performance": self.stats.to_dict(),
            "yearly": self.yearly,
            "blocked": dict(sorted(self.blocked.items())),
            "cost": self.cost,
            "exposure": self.exposure,
            "replay": self.replay.summary() if self.replay else None,
            "trades": len(self.trades),
            "trades_truncated": self.trades_truncated,
            "notes": self.notes,
        }


# --------------------------------------------------------------------------- #
#  驱动器
# --------------------------------------------------------------------------- #

class DailyBacktest:
    """
    逐 bar 推进同一个 `Replayer`。

    一天的顺序是**固定的**，这是"没有未来函数"的结构保证：

        1. _settle(i)   用**今天开盘**撮合昨天收盘时下的单
        2. _signals(i)  用**今天收盘**（及之前）的数据算信号，产生新单
        3. _nav(i)      用今天收盘价给全部持仓估值，记一点 NAV
        4. _fund(i)     用刚记的 NAV 推进回撤/日内亏损（仅 prod 口径）

    1 在 2 之前：当天成交发生在当天收盘之前，所以成交不可能用到当天收盘。
    反过来改顺序就是引入未来函数。
    """

    def __init__(self, config: BacktestConfig, panel: BarPanel, strategy: Any,
                 *, max_trades: int = 400) -> None:
        self.cfg = config
        self.panel = panel
        self.manifest, self.compute = check_strategy(strategy)
        self.caps = venue_profile(config.venue)
        self.rules = config.rules
        self.replayer = Replayer(config.replay_config())
        self.engine = self.replayer.engine

        window = config.frame_window or max(self.manifest.warmup_bars + 10, 60)
        if window < self.manifest.warmup_bars:
            raise BacktestError(
                f"frame_window={window} 小于策略声明的 warmup_bars="
                f"{self.manifest.warmup_bars}，策略会在第一根就 FrameError")
        self._window = window

        self._seq = 0
        self._trade_seq = 0
        self._blocked: Counter = Counter()
        self._trades: List[Dict[str, str]] = []
        self._max_trades = max_trades
        self._truncated = False
        self._series: List[Tuple[date, Decimal]] = []
        self._exposure: List[Tuple[date, Decimal, Decimal]] = []
        self._pending_since: Dict[str, int] = {}
        self._last_nav: Decimal = D(config.initial_cash)
        self._peak_nav: Decimal = D(config.initial_cash)
        self._bought_today: Dict[str, Decimal] = {}
        self._notes: List[str] = []
        #: 策略被成功调用过几次 / 抛异常几次。用来识别"整个回测一次都没跑成"——
        #: 那种情况下产物是一条 100 万到 100 万的平线，绝不能当结果交付。
        self._strategy_calls = 0
        self._strategy_failures = 0
        self._run_params = self.manifest.resolve_params(config.strategy_params)

    # ------------------------------------------------------------------ #
    #  事件生产
    # ------------------------------------------------------------------ #

    def _emit(self, event_type: str, payload: Mapping[str, Any], ts: datetime) -> None:
        self._seq += 1
        self.replayer.apply(EventEnvelope(
            seq=self._seq, event_id=f"bt-{self._seq:08d}",
            event_type=event_type, ts=ts, as_of=ts, payload=payload))

    def _day_open(self, day: date) -> datetime:
        return datetime.combine(day, OPEN_TIME, tzinfo=CN_TZ)

    def _day_close(self, day: date) -> datetime:
        return datetime.combine(day, CLOSE_TIME, tzinfo=CN_TZ)

    # ------------------------------------------------------------------ #
    #  1. 撮合（今天开盘）
    # ------------------------------------------------------------------ #

    def _settle(self, i: int, day: date) -> None:
        ts = self._day_open(day)
        for order in sorted(self.replayer.pending_orders(), key=lambda o: o.order_id):
            remaining = D(order.remaining_qty)
            if remaining > 0:
                self._settle_one(order, i, day, ts, remaining)

    def _settle_one(self, order: Order, i: int, day: date, ts: datetime,
                    remaining: Decimal) -> None:
        sym = order.symbol
        bar = self.panel.bar(sym, i)
        if bar is None or bar.suspended:
            # 停牌：挂单继续等（明天可能复牌），不是撤单。
            self._blocked["suspend"] += 1
            self._expire_if_stale(order, i, day)
            return

        prev_close = self.panel.prev_close(sym, i)
        if prev_close is not None:
            if order.side is Side.BUY and self.rules.at_limit_up(sym, bar.open, prev_close):
                self._blocked["limit_up"] += 1
                self._expire_if_stale(order, i, day)
                return
            if order.side is Side.SELL and self.rules.at_limit_down(sym, bar.open, prev_close):
                self._blocked["limit_down"] += 1
                self._expire_if_stale(order, i, day)
                return

        # T+1：今天买的今天不能卖。回测正常不会触发（成交在开盘、信号在收盘），
        # 但这条断言留着 —— 万一以后有人改了成交时点，这里会拦住而不是悄悄过账。
        #
        # 注意这里**必须**走 _expire_if_stale：早期版本直接 return，导致这类单子
        # 永远挂着、每天重试一次，计数被刷成几千，而真实原因被埋在噪音里。
        if order.side is Side.SELL:
            position = self._position_qty(sym)
            sellable = position - self._bought_today.get(sym, _ZERO)
            if sellable <= 0:
                # 两种完全不同的原因，不能混在一个计数里：
                # 持仓为 0 = 这笔卖单没有标的可卖（上游账实不符）；
                # 持仓 >0 但都被今天买进锁住 = 真的 T+1。
                self._blocked["t1_locked" if position > 0 else "sell_without_position"] += 1
                self._expire_if_stale(order, i, day)
                return
            if remaining > sellable:
                remaining = sellable

        qty = remaining
        if order.side is Side.BUY:
            # 整手：买入必须整手。卖出允许零股（真实规则：零股必须一次卖完）。
            lot = self._lot_step(sym)
            qty = (remaining / lot).to_integral_value(rounding=ROUND_FLOOR) * lot
            if qty <= 0:
                self._blocked["lot_too_small"] += 1
                self._cancel(order, ts, "qty_below_one_lot")
                return

        est = self.cfg.cost_model.estimate(
            self.cfg.venue, order.side, qty, bar.open, nav=self._last_nav)
        px = est.expected_price

        trimmed_for_cash = False
        if order.side is Side.BUY:
            cash = self._cash()
            if qty * px + est.total_fee > cash:
                # 现金不够就按整手缩量。不"凭空补钱"，也不把单子挂成必然失败。
                lot = self._lot_step(sym)
                afford = ((cash - est.total_fee) / px).to_integral_value(
                    rounding=ROUND_FLOOR)
                qty = (afford / lot).to_integral_value(rounding=ROUND_FLOOR) * lot
                self._blocked["cash_short"] += 1
                trimmed_for_cash = True
                if qty <= 0:
                    self._cancel(order, ts, "insufficient_cash")
                    return
                est = self.cfg.cost_model.estimate(
                    self.cfg.venue, order.side, qty, bar.open, nav=self._last_nav)
                px = est.expected_price

        self._trade_seq += 1
        self._emit("fill", {
            "client_order_id": order.client_order_id,
            "venue_trade_id": f"bt-trade-{self._trade_seq:08d}",
            "fill_id": f"bt-fill-{self._trade_seq:08d}",
            "quantity": str(qty), "price": str(px),
            "fee": str(est.total_fee), "fee_currency": self.cfg.base_currency,
            "ts": ts, "quote_ccy": self.cfg.base_currency, "liquidity": "TAKER",
        }, ts)

        if order.side is Side.BUY:
            self._bought_today[sym] = self._bought_today.get(sym, _ZERO) + qty
        if len(self._trades) < self._max_trades:
            self._trades.append({
                "day": day.isoformat(), "symbol": sym, "side": order.side.value,
                "qty": str(qty), "price": str(q(px, 4)),
                "fee": str(q(est.total_fee, 2)),
                "notional": str(q(qty * px, 2)),
            })
        else:
            self._truncated = True

        if qty < remaining:
            # 两种"没吃完"必须分开记，否则计数会把一件事伪装成另一件事：
            #   现金不够 → 真的只买得起这么多（资金约束，会持续存在）；
            #   整手取整 → 下单量里的零头本来就不合法，不是"部分成交"。
            # 混在一个 partial 里，等权组合每天每只股票的零头都会变成一条
            # "部分成交"，把真正的资金约束淹没在几千条噪音里。
            if trimmed_for_cash:
                self._cancel(order, ts, "partial_residual")
                self._blocked["partial"] += 1
            else:
                self._cancel(order, ts, "lot_residual")
                self._blocked["lot_rounding"] += 1

    def _expire_if_stale(self, order: Order, i: int, day: date) -> None:
        since = self._pending_since.setdefault(order.client_order_id, i)
        if i - since >= self.cfg.max_pending_bars:
            self._cancel(order, self._day_open(day), "unfilled_timeout")
            self._pending_since.pop(order.client_order_id, None)

    def _cancel(self, order: Order, ts: datetime, reason: str) -> None:
        self._emit("order_cancel",
                   {"client_order_id": order.client_order_id, "reason": reason}, ts)
        # 撤单**必须**核对结果。迁移表里 `SUBMITTED → CANCELLED` 不合法，
        # 如果没先收到 ack 就撤，这次撤单会被静默丢弃，那张单子就永远挂在
        # 在途里、每天重试一次 —— 而计数会把这个 bug 伪装成"规则拦了很多次"。
        if order.state is not OrderState.CANCELLED:
            self._blocked[f"cancel_failed:{reason}"] += 1
        self._blocked[f"cancel:{reason}"] += 1

    # ------------------------------------------------------------------ #
    #  2. 信号与下单（今天收盘）
    # ------------------------------------------------------------------ #

    def _is_signal_day(self, i: int, start_i: int) -> bool:
        if self.manifest.kind != "rebalance":
            return True
        step = max(1, int(self.manifest.rebalance_days))
        return (i - start_i) % step == 0

    def _build_frame(self, i: int, day: date) -> MarketFrame:
        """
        只含 `as_of` 及之前、且只保留最近 `window` 根。**截断是调用方的责任**
        （`MarketFrame` 刻意不自动截断，见其 docstring），所以必须在这里做对。

        只放"今天能成交"的标的：停牌股放进来会让策略对它下单，而那些单
        永远成交不了，只会白占在途额度并堆一堆 `suspend` 计数。
        """
        lo = max(0, i - self._window + 1)
        symbols = self.panel.live_symbols(i)
        if self.cfg.max_frame_symbols and len(symbols) > self.cfg.max_frame_symbols:
            symbols = symbols[:self.cfg.max_frame_symbols]
        series: Dict[str, Series] = {}
        for sym in symbols:
            pts: List[Tuple[date, Decimal]] = []
            for j in range(lo, i + 1):
                if not self.panel.has_bar(sym, j):
                    continue                     # 停牌日没 bar，跳过（不是补价）
                pts.append((self.panel.calendar[j], self.panel.close_at(sym, j)))
            if len(pts) >= 2:
                series[sym] = Series(symbol=sym, points=tuple(pts))
        if not series:
            raise DataQualityError(f"{day} 没有任何可成交标的，建不出 frame")
        return MarketFrame(as_of=self._day_close(day), venue=self.cfg.venue,
                           series=series)

    def _signals(self, i: int, day: date) -> None:
        ts = self._day_close(day)

        # ---- 1. 算信号（只依赖面板，不依赖行情表） ------------------------ #
        signals: Tuple[Any, ...] = ()
        try:
            frame = self._build_frame(i, day)
        except DataQualityError as exc:
            # 建 frame 失败是**引擎侧**的问题（数据缺口），记下来并跳过这一天。
            self._blocked[f"frame:{type(exc).__name__}"] += 1
            self._notes.append(f"{day.isoformat()} 策略/行情出错："
                               f"{type(exc).__name__}: {exc}")
            return

        try:
            ctx = StrategyContext(as_of=frame.as_of, params=self._run_params,
                                  positions=self._positions_by_symbol())
            signals = tuple(coerce_signal(s) for s in (self.compute(frame, ctx) or ()))
            self._strategy_calls += 1
        except (StrategyError, ContractViolation) as exc:
            self._blocked[f"frame:{type(exc).__name__}"] += 1
            self._notes.append(f"{day.isoformat()} 策略/行情出错："
                               f"{type(exc).__name__}: {exc}")
            self._strategy_failures += 1
            signals = ()
        except Exception as exc:                      # noqa: BLE001
            # 策略自己的 bug（KeyError / TypeError / 手滑）同样不许把整轮回测炸掉，
            # 但也**绝不静默**：计进 blocked，写进 notes。如果每一次调用都失败，
            # `_finish` 会直接拒绝交付 —— 否则产物是一条 100 万到 100 万的平线，
            # 那是本批最不该出现的东西：一个看起来正常的假绿灯。
            self._blocked[f"frame:{type(exc).__name__}"] += 1
            self._notes.append(f"{day.isoformat()} 策略/行情出错："
                               f"{type(exc).__name__}: {exc}")
            self._strategy_failures += 1
            signals = ()

        # ---- 2. 发行情快照：只发会被用到的标的 ----------------------------- #
        # 全池每天都发的话，794 只 × 1879 天 ≈ 150 万个事件，慢到没法调参；
        # 而估值/风控真正需要的只有：持仓 ∪ 在途 ∪ 今天要下单的。
        for sym in self._needed_symbols(i, signals):
            got = self.panel.close_with_date(sym, i)
            if got is None:
                continue
            px, price_day = got
            when = self._day_close(price_day)
            self._emit("market_snapshot", {
                "venue": self.cfg.venue, "symbol": sym,
                # `last` 必须是 Decimal：MarketSnapshot 拒绝字符串和 0 价，
                # 这条严格性正是它存在的意义，回测不许绕过。
                "as_of": when, "received_at": when, "last": px,
                "source": "backtest", "sequence": self.panel.index_of(price_day),
            }, ts)

        if not signals:
            return

        # ---- 3. 信号 → 意图事件 ------------------------------------------- #
        batch = adapt_signals(signals, AdapterConfig(
            strategy_id=self.manifest.strategy_id,
            strategy_version=self.manifest.version,
            venue=self.cfg.venue), as_of=ts, ts=ts)
        for intent in batch.intents:
            self._emit("strategy_intent", intent_to_payload(intent), ts)
        if batch.dropped:
            self._blocked["intent_dropped"] += len(batch.dropped)

        # ---- 4. 决策轮：净额 → 风控 → 下单 --------------------------------- #
        self._emit("cycle", {"now": ts}, ts)

        # ---- 5. 交易所确认 -------------------------------------------------- #
        # 没有 ack 的订单停在 SUBMITTED，而迁移表里 `SUBMITTED → CANCELLED`
        # 不合法 —— 于是"撤不掉"会变成静默的每日重试。真实链路里券商也会先
        # 回一个"已接受"，这里必须补上，否则回测的事件流本身就是残缺的。
        for order in self.replayer.pending_orders():
            if order.state is OrderState.SUBMITTED:
                self._emit("order_ack", {"client_order_id": order.client_order_id}, ts)

    def _needed_symbols(self, i: int, signals: Sequence[Any]) -> List[str]:
        need = set(self._positions_by_symbol())
        for order in self.replayer.pending_orders():
            need.add(order.symbol)
        for sig in signals:
            need.add(getattr(sig, "symbol", ""))
        need.discard("")
        return sorted(need)

    # ------------------------------------------------------------------ #
    #  3. 估值与基金级风控
    # ------------------------------------------------------------------ #

    def _mark_prices(self, i: int) -> Dict[Tuple[str, str], Decimal]:
        """
        估值价表：**直接从面板取当日收盘**，不读行情表。

        为什么不读 `MarketDataBook`：行情快照只在调仓日发（那是执行层需要的东西），
        拿它给每天都估值会让非调仓日的 NAV 原地不动 —— 曲线变成 20 天一级的阶梯，
        波动率被压小、回撤被削平、夏普虚高。这类错误不会报错，只会让一个
        漂亮的假数字出现在报告里。

        停牌标的用最近一次收盘（`close_at` 自带回退），这是行业惯例；
        但成交永远不会发生在这些标的上，两件事不混。
        """
        out: Dict[Tuple[str, str], Decimal] = {}
        missing: List[str] = []
        for (_venue, sym), pos in self.replayer.ledger.positions().items():
            if D(pos.qty) == 0:
                continue
            px = self.panel.close_at(sym, i)
            if px is None:
                missing.append(sym)
                continue
            out[(self.cfg.venue, sym)] = px
        if missing:
            raise DataQualityError(
                f"{self.panel.calendar[i]} 有持仓但完全没有价格：{sorted(missing)}"
                "（不许用 0 或 1 兜底，那会让 NAV 变成一个编出来的数字）")
        return out

    def _nav(self, i: int, day: date) -> Decimal:
        prices = self._mark_prices(i)
        nav = self.replayer.ledger.nav(prices)
        self._series.append((day, q(nav, 2)))
        gross = self.replayer.ledger.gross_exposure(prices) if prices else _ZERO
        self._exposure.append((day, q(gross, 2), q(nav, 2)))
        return nav

    def _fund_state(self, nav: Decimal) -> None:
        if self.cfg.risk_profile != "prod":
            return
        self._peak_nav = max(self._peak_nav, nav)
        self.engine.update_fund_state(
            self.replayer.risk_state, nav,
            peak_nav=self._peak_nav, day_start_nav=self._last_nav)

    # ------------------------------------------------------------------ #
    #  工具
    # ------------------------------------------------------------------ #

    def _lot_step(self, symbol: str) -> Decimal:
        """
        一手多少股。**两个来源**：能力矩阵给场所默认值，`ArenaRules` 给代码段分档
        （科创板 688 是 200 股起）。两者必须相容，否则说明有一处口径写错了。
        """
        venue_step = self.caps.rules_for(symbol).qty_step
        segment = self.rules.lot_size(symbol)
        if segment % venue_step != 0:
            raise BacktestError(
                f"{symbol} 的分档整手 {segment} 与场所声明 {venue_step} 不相容")
        return segment

    def _cash(self) -> Decimal:
        return self.replayer.ledger.cash_native().get(
            (self.cfg.account_id, self.cfg.base_currency), _ZERO)

    def _position_qty(self, symbol: str) -> Decimal:
        pos = self.replayer.ledger.positions().get((self.cfg.venue, symbol))
        return D(pos.qty) if pos else _ZERO

    def _positions_by_symbol(self) -> Dict[str, Decimal]:
        out: Dict[str, Decimal] = {}
        for (venue, sym), pos in self.replayer.ledger.positions().items():
            if venue == self.cfg.venue and D(pos.qty) != 0:
                out[sym] = D(pos.qty)
        return out

    # ------------------------------------------------------------------ #
    #  主循环
    # ------------------------------------------------------------------ #

    def run(self) -> BacktestResult:
        cal = self.panel.calendar
        if not cal:
            raise BacktestError("面板没有交易日")
        start_i = self.cfg.warmup_bars
        if self.cfg.start:
            got = self.panel.first_index_on_or_after(self.cfg.start)
            if got is None:
                raise BacktestError(
                    f"start={self.cfg.start} 晚于最后一根 bar"
                    f"（{cal[-1]}），没有可评估的区间")
            if self.panel.calendar[got] != self.cfg.start:
                self._notes.append(
                    f"start={self.cfg.start} 不是交易日，实际起点取 "
                    f"{self.panel.calendar[got]}（只往后找，不往前）")
            start_i = max(start_i, got)
        if start_i >= len(cal):
            raise BacktestError(
                f"warmup_bars={self.cfg.warmup_bars} 超过日历长度 {len(cal)}")

        self._emit("deposit", {
            "account_id": self.cfg.account_id, "currency": self.cfg.base_currency,
            "amount": str(D(self.cfg.initial_cash)),
        }, self._day_close(cal[start_i]))

        for i in range(start_i, len(cal)):
            day = cal[i]
            self._bought_today = {}
            self._settle(i, day)                 # 今天开盘：撮合昨天的单
            # 决策发生在**今天收盘**，所以先按今天收盘价把 NAV 算出来。
            #
            # 早期版本把 `_nav` 放在 `_signals` 之后，风控拿的是**昨天**的 NAV。
            # 于是一笔"把持仓卖光"的卖单会被算成"单笔占 NAV 101%"，被风控
            # 拒绝 —— 组合只进不出，而报告上看不出任何异常，只是"策略不太换手"。
            # 今天收盘价在今天收盘时是已知的，这不是偷看未来。
            nav = self._nav(i, day)
            self._last_nav = nav
            self._fund_state(nav)
            if self._is_signal_day(i, start_i):
                self._signals(i, day)
        return self._finish()

    # ------------------------------------------------------------------ #
    #  收尾
    # ------------------------------------------------------------------ #

    def _finish(self) -> BacktestResult:
        # 一次都没成功调用过策略，却要交出一份"净值稳如泰山"的报告 —— 这是
        # 最坏的失败模式：没有报错、没有缺口、只有一条平线。宁可不交。
        if self._strategy_failures and not self._strategy_calls:
            first = next((n for n in self._notes if "策略/行情出错" in n), "")
            raise BacktestError(
                f"策略在全部 {self._strategy_failures} 次调用里都抛异常，"
                f"这次回测没有任何信号（拒绝产出一条平线的假结果）。首个错误：{first}")
        replay = self.replayer.snapshot()
        stats = compute_stats(self._series)
        avg_nav = average_nav(self._series) or self._last_nav
        # 最后一天的估值价同样走面板（与 `_nav` 同一口径），不用行情表。
        last_i = len(self.panel) - 1
        report = replay.ledger.performance(self._mark_prices(last_i))
        # 账本里 fee 与 slippage 都是**负数表示损失**（归因六项能直接相加）。
        # 报表要给的是"花了多少钱"，所以在这里翻一次符号，且只翻这一次。
        fee_cost = -D(report.fee)
        slip_cost = -D(report.slippage)
        drag = cost_drag(fee_cost, avg_nav, stats.years)
        total_cost = fee_cost + slip_cost

        def _bps(x: Decimal) -> Optional[str]:
            if not avg_nav:
                return None
            return str(q(D(x) / D(avg_nav) * Decimal(10000), 3))

        cost = {
            "fee_total": str(q(fee_cost, 2)),
            "slippage_total": str(q(slip_cost, 2)),
            "total_cost": str(q(total_cost, 2)),
            "fee_bps_of_avg_nav": _bps(fee_cost),
            "total_bps_of_avg_nav": _bps(total_cost),
            "annual_drag_pct": None if drag is None else round(drag * 100, 3),
            "fee_entries": sum(1 for e in replay.ledger.journal() if e["kind"] == "FEE"),
            "attribution_closes": report.closes,
            "trading_pnl": str(q(report.trading, 2)),
            "net_pnl": str(q(report.net_pnl, 2)),
        }
        notes = list(dict.fromkeys(self._notes + self._config_notes()))
        result = BacktestResult(
            config=self.cfg, manifest=self.manifest, stats=stats,
            series=list(self._series), yearly=yearly_returns(self._series),
            replay=replay, blocked=dict(self._blocked), cost=cost,
            exposure=exposure_stats(self._exposure),
            trades=list(self._trades), trades_truncated=self._truncated,
            notes=notes,
        )
        result.digest = _digest(result)
        return result

    def _config_notes(self) -> List[str]:
        """口径说明。每一条都是"读结果时必须知道的前提"。"""
        out = [
            "成交口径：信号日收盘出单 → 次日开盘价 + 滑点成交（日线上不模拟限价排队）。",
            "价格口径：" + ("后复权（adj_factor），分红视为再投资"
                          if self.panel.adjusted else "不复权（除权日的价格缺口会算成真实亏盈）"),
            f"策略只能看到最近 {self._window} 根 bar（frame 窗口），"
            f"更长历史请抬高 manifest.warmup_bars。",
            "回测与实盘共用 Replayer / Ledger / CostModel；回测只负责生产事件与模拟撮合。",
            "风控的「行情陈旧」闸在回测里关闭：日频下任何停牌持仓都会让它全天误判；"
            "改由撮合层按「停牌不成交」接管。",
        ]
        if self.cfg.risk_profile == "research":
            out.append("风控口径 research：不参与日损/回撤熔断，结论不代表生产风控下的结果。")
        else:
            out.append("风控口径 prod：完全按 RiskPolicy，含 5% 回撤 / 1% 日损熔断"
                       "（触发后永久停单，本批未做自动恢复）。")
        if self.manifest.kind == "rebalance":
            out.append(f"调仓节奏：每 {self.manifest.rebalance_days} 个交易日一次（来自策略 manifest）。")
        # 这条不是套话：A 股场所（xshg）目前声明「无法按客户端订单号回查」，
        # 结构准入会拒它。回测只借它的规则矩阵（整手/涨跌停/最小金额），
        # 所以结论可以说，但"这套配置能不能直接上实盘"必须写在报告里。
        try:
            admitted = {a.venue: a.allowed for a in all_admissions()}
        except Exception:                                   # noqa: BLE001
            admitted = {}
        if admitted.get(self.cfg.venue) is False:
            out.append(f"场所 {self.cfg.venue} 未通过实盘结构准入（原因见实盘能力矩阵），"
                       f"本次回测只使用它的交易规则，不代表该场所已可上线。")
        return out


# --------------------------------------------------------------------------- #
#  指纹
# --------------------------------------------------------------------------- #

def _digest(result: BacktestResult) -> str:
    """
    指纹只覆盖**确定性内容**：NAV 序列、成交、阻断计数、账本流水。
    特意不含时间戳与 `notes`（改文案不该让两次回测"不一致"）。
    """
    payload = {
        "series": [[d.isoformat(), str(nav)] for d, nav in result.series],
        "trades": result.trades,
        "blocked": dict(sorted(result.blocked.items())),
        "ledger": result.replay.ledger.journal() if result.replay else [],
        "fills": result.replay.fills if result.replay else 0,
        "cycles": result.replay.cycles if result.replay else 0,
        "venue": result.config.venue,
        "initial_cash": str(result.config.initial_cash),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


__all__ = [
    "ArenaRules", "BacktestConfig", "BacktestError", "BacktestResult", "Bar",
    "BarPanel", "DataQualityError", "DailyBacktest", "DEFAULT_VENUE",
]
