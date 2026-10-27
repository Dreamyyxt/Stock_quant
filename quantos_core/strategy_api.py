"""
策略接口契约 —— 生产平面唯一认的形态。

为什么需要这一层
================
B0~B7 把「意图之后」的链路做完了：净额 → 风控 → 订单 → 成交 → 账本 → 归因。
但**意图是凭空出现的**。`strategy_registry` 只存元数据（版本、commit、回测报告），
`strategy_adapter` 只做格式转换，`replay` 要外面喂 handler。
真正产生信号的策略一直躺在 `research/` 里，跟契约链没有关系。

这个模块补的就是那一刀：**一个策略必须长什么样，才能被系统当成策略。**

四条硬约束（违反任何一条都直接报错，不给"宽容模式"）
====================================================
1. **只产信号，不产订单**（R-A06）。`SignalOut` 里没有任何金额、数量、账户字段。
   策略想下 15% 仓位，只能表达"这个标的我要 0.15 的权重"，至于 0.15 换成多少钱、
   能不能下、下多少，全部由资金分配与风控决定。少一个字段，就少一次绕过风控的机会。
2. **不自己取数**。行情由 `MarketFrame` 传进来。策略里出现 `open()` / 网络请求
   都是违规（由 `strategy_pack` 的 AST 体检把关）。理由：自己取数的策略没法回放，
   回放不了就没法证明"回测和实盘算的是同一件事"（R-S06）。
3. **参数必须声明**。`ParamSpec` 里没写的键传进来就是报错。拼错参数名（`lookbak`
   写成 `lookback`）不会静默用默认值跑一年，而是当场失败。
4. **warmup 不够就不算**。`MarketFrame.require()` 拿不到足够 bar 就抛 `FrameError`，
   绝不"用现有的凑一凑"—— 凑出来的信号是最危险的一种错误：数字看着正常。

版本与指纹
==========
`StrategyManifest.fingerprint` 由 `strategy_pack` 从源码算出来，写进
`StrategyInstance.code_commit`。同版本号不同指纹 = 代码被改过，注册时会被拒。
这条挡的是"改了两行代码，版本号没变，然后说回测报告仍然有效"。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

__all__ = [
    "StrategyError", "ParamError", "FrameError", "ManifestError",
    "ParamSpec", "Series", "MarketFrame", "StrategyContext", "SignalOut",
    "StrategyManifest", "check_strategy", "signal_payload", "coerce_signal",
]


class StrategyError(RuntimeError):
    """策略层的错误基类。所有子类都代表"这个策略不能用"，不是"这次算错了"。"""


class ParamError(StrategyError):
    """参数不合规：未声明、越界、类型不对。"""


class FrameError(StrategyError):
    """行情不够或不合规。注意这不是"降级"，是拒绝计算。"""


class ManifestError(StrategyError):
    """manifest 本身写错了 —— 这种错必须在导入时炸，不能等到跑起来。"""


# --------------------------------------------------------------------------- #
#  参数声明
# --------------------------------------------------------------------------- #

_KINDS = ("float", "int", "bool", "str")


@dataclass(frozen=True)
class ParamSpec:
    """
    一个可调参数的完整声明。

    `required=True` 且没有 `default` 时，调用方必须显式给值。
    刻意**不**给"随便一个默认值"的便利：策略的默认参数是一个投资判断，
    不是一个工程默认值，工程替用户拍板就是把风险藏起来。
    """

    name: str
    kind: str = "float"
    low: Optional[Decimal] = None
    high: Optional[Decimal] = None
    default: Any = None
    required: bool = True
    desc: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise ManifestError("ParamSpec.name 不可为空")
        if self.kind not in _KINDS:
            raise ManifestError(f"参数 {self.name} 的 kind={self.kind!r} 不支持（只认 {_KINDS}）")
        if self.low is not None and self.high is not None and D(self.low) > D(self.high):
            raise ManifestError(f"参数 {self.name} 的 low > high，区间是空的")
        if self.required and self.default is None:
            # 允许 required + default 同时存在（默认值仍然要走 coerce 校验）。
            pass
        if not self.required and self.default is None:
            raise ManifestError(
                f"参数 {self.name} 非必填却没给默认值 —— 缺值时策略拿不到数，"
                "这跟必填是一回事，别用 required=False 绕过去")

    # -- 取值 ------------------------------------------------------------ #

    def coerce(self, raw: Any) -> Any:
        """把外部传入的值转成声明的类型，并执行区间校验。"""
        if raw is None:
            if self.default is None:
                raise ParamError(f"参数 {self.name} 必填，收到 None")
            raw = self.default

        if self.kind == "bool":
            if isinstance(raw, bool):
                return raw
            text = str(raw).strip().lower()
            if text in ("1", "true", "yes", "on"):
                return True
            if text in ("0", "false", "no", "off"):
                return False
            raise ParamError(f"参数 {self.name} 不是布尔值：{raw!r}")

        if self.kind == "str":
            if not isinstance(raw, str):
                raise ParamError(f"参数 {self.name} 必须是字符串：{raw!r}")
            if self.required and not raw.strip():
                raise ParamError(f"参数 {self.name} 不能是空串")
            return raw

        if self.kind == "int":
            try:
                value: Any = int(raw)
            except (TypeError, ValueError):
                raise ParamError(f"参数 {self.name} 不是整数：{raw!r}")
            if isinstance(raw, float) and not float(raw).is_integer():
                raise ParamError(f"参数 {self.name} 收到 {raw!r}，int 类型不接受小数")
            self._check_range(Decimal(value))
            return value

        # float：对外统一 Decimal，避免 0.1+0.2 那类误差进到策略里。
        try:
            value_d = Decimal(str(raw))
        except (InvalidOperation, TypeError, ValueError):
            raise ParamError(f"参数 {self.name} 不是数字：{raw!r}")
        if not value_d.is_finite():
            raise ParamError(f"参数 {self.name} 不是有限数：{raw!r}")
        self._check_range(value_d)
        return value_d

    def _check_range(self, value: Decimal) -> None:
        if self.low is not None and value < D(self.low):
            raise ParamError(f"参数 {self.name}={value} 低于下界 {self.low}")
        if self.high is not None and value > D(self.high):
            raise ParamError(f"参数 {self.name}={value} 高于上界 {self.high}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "kind": self.kind,
            "low": None if self.low is None else str(self.low),
            "high": None if self.high is None else str(self.high),
            "default": None if self.default is None else str(self.default),
            "required": self.required, "desc": self.desc,
        }


# --------------------------------------------------------------------------- #
#  行情
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Series:
    """
    单标的的收盘序列：`(date, close)` 升序、日期唯一、价格为正。

    为什么只给收盘价而不是完整 OHLCV：策略层现在只需要收盘，
    给多了等于鼓励策略用"未来才知道"的字段（比如当根 bar 的最高价）。
    要用更多字段时再加，并同时补回放口径 —— 加字段比改语义便宜。
    """

    symbol: str
    points: Tuple[Tuple[date, Decimal], ...]

    def __post_init__(self) -> None:
        if not self.symbol:
            raise FrameError("Series.symbol 不可为空")
        prev: Optional[date] = None
        for day, close in self.points:
            if not isinstance(day, date):
                raise FrameError(f"{self.symbol}: 日期项不是 date：{day!r}")
            if prev is not None and day <= prev:
                raise FrameError(
                    f"{self.symbol}: 日期必须严格升序且唯一（{prev} -> {day}）"
                    "，乱序会让收益算反号")
            prev = day
            if D(close) <= 0:
                raise FrameError(f"{self.symbol}: {day} 收盘价 {close} 非正")

    def __len__(self) -> int:
        return len(self.points)

    def closes(self) -> Tuple[Decimal, ...]:
        return tuple(close for _day, close in self.points)

    def last_date(self) -> date:
        if not self.points:
            raise FrameError(f"{self.symbol} 没有任何数据点")
        return self.points[-1][0]

    def last(self) -> Decimal:
        if not self.points:
            raise FrameError(f"{self.symbol} 没有任何数据点")
        return self.points[-1][1]

    def tail(self, bars: int) -> "Series":
        if bars <= 0:
            raise FrameError(f"tail(bars={bars}) 必须为正")
        return Series(symbol=self.symbol, points=self.points[-bars:])


@dataclass(frozen=True)
class MarketFrame:
    """
    一次计算能看到的全部行情。**只读**，且只包含 `as_of` 及之前的数据。

    构造时不做截断——截断是调用方的责任（`as_of` 与 `series` 必须自洽）。
    这是刻意的：如果这里偷偷按 as_of 过滤，策略作者会以为"传进来的就是全量"，
    而在回放时拿到含未来数据的序列却毫无察觉。
    """

    as_of: datetime
    venue: str
    series: Mapping[str, Series]

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None:
            raise FrameError("MarketFrame.as_of 必须是带时区的时间")
        if not self.venue:
            raise FrameError("MarketFrame.venue 不可为空")
        if not self.series:
            raise FrameError("MarketFrame 没有任何标的 —— 空行情等于没行情，不许静默跑空")

    def symbols(self) -> Tuple[str, ...]:
        return tuple(sorted(self.series))

    def require(self, symbol: str, bars: int) -> Series:
        """
        取标的最近 `bars` 根。不足即抛错。

        `bars` 是硬门槛：策略声明 warmup=60 就必须有 60 根，
        少一根也不许算。半截窗口算出来的指标不会报错，只会悄悄偏。
        """
        if bars <= 0:
            raise FrameError(f"require(bars={bars}) 必须为正")
        s = self.series.get(symbol)
        if s is None:
            raise FrameError(f"行情里没有 {symbol}（现有：{', '.join(self.symbols())}）")
        if len(s) < bars:
            raise FrameError(
                f"{symbol} 只有 {len(s)} 根 bar，策略要求 {bars} 根"
                "（warmup 不足时不许用半截数据算）")
        return s.tail(bars)

    def latest_prices(self) -> Dict[str, Decimal]:
        """
        各标的最后一个价格。

        **注意**：这个价格是 `as_of` 当天的收盘/最新，不是"现在"。
        调用方要自己保证 `as_of` 没过期（运行时由行情陈旧检查负责）。
        """
        return {sym: s.last() for sym, s in self.series.items()}


# --------------------------------------------------------------------------- #
#  策略输出
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SignalOut:
    """
    策略的输出：一个标的、一个目标权重、一句理由。

    刻意与 `strategy_adapter.adapt_signals` 的 duck typing 字段对齐
    （symbol / direction / suggested_weight / score / reason），
    这样策略包不需要 import 任何生产模块，也能被适配器接住。

    `suggested_weight` 是**相对策略自身额度**的权重，不是占 NAV 的比例。
    这一层语义差很容易搞混：策略说 0.15，实际占 NAV 是 0.15 × 策略配额。
    """

    symbol: str
    suggested_weight: Decimal
    reason: str
    score: Decimal = Decimal(0)
    direction: int = 0

    def __post_init__(self) -> None:
        if not self.symbol:
            raise StrategyError("SignalOut.symbol 不可为空")
        w = D(self.suggested_weight)
        if not w.is_finite():
            raise StrategyError(f"{self.symbol}: 权重不是有限数（{self.suggested_weight}）")
        if abs(w) > 1:
            raise StrategyError(
                f"{self.symbol}: 权重 {w} 超过 ±1 —— 单标的权重上界是 1，"
                "超过部分说明策略自己想加杠杆，加杠杆归资金管理层管，不归策略")
        if not self.reason or not str(self.reason).strip():
            raise StrategyError(
                f"{self.symbol}: 没有 reason 的信号不接受 —— "
                "事后无法回答『系统为什么做这笔交易』（R-G02）")
        if self.direction not in (-1, 0, 1):
            raise StrategyError(f"{self.symbol}: direction 只能是 -1/0/1，收到 {self.direction}")


#: 包只写 dict 时允许的键。比 SignalOut 宽容一点点（weight 是 suggested_weight 的别名），
#: 但多一个键就报错 —— 否则拼错的 `wieght` 会静默变成 0 权重，看起来像是"策略看空"。
_SIGNAL_ALIASES = {
    "symbol": "symbol",
    "suggested_weight": "suggested_weight",
    "weight": "suggested_weight",
    "reason": "reason",
    "score": "score",
    "direction": "direction",
}


def coerce_signal(item: Any) -> SignalOut:
    """
    把策略返回值统一成 `SignalOut`。

    支持两种写法，**推荐 dict**：

    ```python
    return [{"symbol": "600000", "weight": "0.05", "reason": "入选"}]   # 零 import，推荐
    return [SignalOut("600000", Decimal("0.05"), "入选")]               # 也行
    ```

    为什么推荐 dict：策略包一旦 import `quantos_core`，就多了一条顺着 import
    摸到生产对象的通道。dict 是纯数据，跨进程、跨语言、跨仓库都能用，
    也不会让"研究=生产同一套逻辑"退化成"研究和生产共用一份实现"。
    """
    if isinstance(item, SignalOut):
        return item
    if not isinstance(item, Mapping):
        raise StrategyError(
            f"信号必须是 SignalOut 或 dict，收到 {type(item).__name__}")
    unknown = sorted(set(item) - set(_SIGNAL_ALIASES))
    if unknown:
        raise StrategyError(
            f"信号里有不认识的键 {unknown}（允许：{sorted(_SIGNAL_ALIASES)}）")
    if "symbol" not in item:
        raise StrategyError("信号缺少 symbol")
    if "suggested_weight" not in item and "weight" not in item:
        raise StrategyError(
            f"{item.get('symbol')}: 信号缺少 suggested_weight —— "
            "只给方向的信号无法决定金额，适配器会把它丢掉")
    weight = item.get("suggested_weight", item.get("weight"))
    return SignalOut(
        symbol=str(item["symbol"]),
        suggested_weight=D(weight),
        reason=str(item.get("reason") or ""),
        score=D(item.get("score", 0) or 0),
        direction=int(item.get("direction", 0) or 0),
    )


def signal_payload(signals: Iterable[SignalOut]) -> Dict[str, Any]:
    """信号 → 可落盘/可进事件的字典。用于审计『这个意图是哪个信号下的』。"""
    return {"signals": [{
        "symbol": s.symbol,
        "suggested_weight": str(D(s.suggested_weight)),
        "score": str(D(s.score)),
        "direction": int(s.direction),
        "reason": s.reason,
    } for s in signals]}


# --------------------------------------------------------------------------- #
#  上下文
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class StrategyContext:
    """
    策略计算时能看到的一切（除行情以外）。

    刻意**不**给 `nav`：给了之后策略会开始算"我有多少钱，所以买 300 股"，
    于是仓位就脱离了资金管理。策略只该关心权重。
    """

    as_of: datetime
    params: Mapping[str, Any]
    positions: Mapping[str, Decimal] = field(default_factory=dict)
    #: 本策略自己的持仓（symbol → 数量），供需要知道"我现在有没有"的策略使用。
    trading_days_left: Optional[int] = None

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None:
            raise StrategyError("StrategyContext.as_of 必须是带时区的时间")
        if self.params is None:
            raise StrategyError("StrategyContext.params 不可为 None（无参数请传空 dict）")


# --------------------------------------------------------------------------- #
#  Manifest
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class StrategyManifest:
    """
    策略的自述文件。导入时全部字段都会被校验，没有一个字段"可以留空再说"。

    `rebalance_days` 不是装饰：它告诉调度器"这个策略多久才需要算一次"。
    缺了它，日频策略会被当成分钟频反复调用，然后同一个信号被反复下单
    （虽然有幂等键兜着，但那是兜底，不该常态发生）。
    """

    strategy_id: str
    version: str
    venue: str
    #: "rebalance" 到期才调仓；"signal" 每天都可以给信号。
    kind: str = "rebalance"
    rebalance_days: int = 20
    warmup_bars: int = 60
    params: Tuple[ParamSpec, ...] = ()
    author: str = ""
    description: str = ""
    #: 由 strategy_pack 从源码算出；手工写的 manifest 允许留空（内置策略）。
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.strategy_id:
            raise ManifestError("manifest.strategy_id 不可为空")
        if not self.version:
            raise ManifestError("manifest.version 不可为空")
        if not self.venue:
            raise ManifestError("manifest.venue 不可为空")
        if self.kind not in ("rebalance", "signal"):
            raise ManifestError(f"manifest.kind={self.kind!r} 不支持（rebalance / signal）")
        if int(self.rebalance_days) < 1:
            raise ManifestError("rebalance_days 至少为 1（0 等于每根 bar 都调，别这么干）")
        if int(self.warmup_bars) < 1:
            raise ManifestError("warmup_bars 至少为 1")
        names = [p.name for p in self.params]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ManifestError(f"参数名重复：{dupes} —— 后一个会静默覆盖前一个")

    # -- 参数 ------------------------------------------------------------ #

    def param_specs(self) -> Dict[str, ParamSpec]:
        return {p.name: p for p in self.params}

    def resolve_params(self, raw: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        """
        外部参数 → 校验后的参数字典。

        未知键**直接报错**。静默忽略未知键的代价是：用户把 `halflife`
        写成 `half_life`，跑了一年才发现这个参数从来没生效过。
        """
        raw = dict(raw or {})
        specs = self.param_specs()
        unknown = sorted(set(raw) - set(specs))
        if unknown:
            raise ParamError(
                f"策略 {self.strategy_id} 不认识参数 {unknown}（已声明：{sorted(specs) or '无'}）")
        missing = sorted(n for n, p in specs.items()
                         if p.required and n not in raw and p.default is None)
        if missing:
            raise ParamError(f"策略 {self.strategy_id} 缺少必填参数 {missing}")
        return {n: specs[n].coerce(raw.get(n)) for n in specs}

    def fingerprint_of_params(self, params: Mapping[str, Any]) -> str:
        """参数指纹。用于回答『还是同一个策略吗』：代码没变、参数变了，就是另一个策略。"""
        parts = [f"{k}={params[k]}" for k in sorted(params)]
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy_id": self.strategy_id, "version": self.version,
            "venue": self.venue, "kind": self.kind,
            "rebalance_days": self.rebalance_days,
            "warmup_bars": self.warmup_bars,
            "author": self.author, "description": self.description,
            "fingerprint": self.fingerprint,
            "params": [p.to_dict() for p in self.params],
        }


# --------------------------------------------------------------------------- #
#  校验入口
# --------------------------------------------------------------------------- #

def check_strategy(obj: Any) -> Tuple[StrategyManifest, Any]:
    """
    把一个对象当策略检一遍，返回 (manifest, compute)。

    只认两样东西：一个 `manifest`（StrategyManifest 或能转成它的 dict），
    一个 `compute(frame, ctx)`。刻意不做基类继承 —— 策略包要能不 import
    任何生产模块就能写，继承会逼它 import 我们这个包。
    """
    raw_manifest = getattr(obj, "manifest", None)
    if raw_manifest is None:
        raw_obj = getattr(obj, "MANIFEST", None)
        if raw_obj is None:
            raise ManifestError("策略缺少 manifest / MANIFEST")
        if isinstance(raw_obj, StrategyManifest):
            manifest = raw_obj
        elif isinstance(raw_obj, Mapping):
            manifest = _manifest_from_dict(raw_obj)
        else:
            raise ManifestError(f"MANIFEST 类型不支持：{type(raw_obj).__name__}")
    elif isinstance(raw_manifest, StrategyManifest):
        manifest = raw_manifest
    else:
        raise ManifestError("策略的 manifest 属性必须是 StrategyManifest")

    compute = getattr(obj, "compute", None)
    if not callable(compute):
        raise ManifestError(
            f"策略 {manifest.strategy_id} 缺少 compute(frame, ctx) —— "
            "没有计算入口的策略等于没有策略")
    return manifest, compute


def _manifest_from_dict(raw: Mapping[str, Any]) -> StrategyManifest:
    """dict → StrategyManifest。缺字段就报错，不做"能给个默认就给"的填补。"""
    if not isinstance(raw, Mapping):
        raise ManifestError("MANIFEST 必须是 dict")
    allowed = {"strategy_id", "version", "venue", "kind", "rebalance_days",
               "warmup_bars", "params", "author", "description", "fingerprint"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ManifestError(f"MANIFEST 有不认识的键 {unknown}（拼错了？允许的键：{sorted(allowed)}）")

    specs = []
    for item in raw.get("params") or ():
        if isinstance(item, ParamSpec):
            specs.append(item)
        elif isinstance(item, Mapping):
            specs.append(ParamSpec(**{k: v for k, v in item.items()
                                     if k in ("name", "kind", "low", "high",
                                              "default", "required", "desc")}))
        else:
            raise ManifestError(f"params 项类型不支持：{type(item).__name__}")

    missing = [k for k in ("strategy_id", "version", "venue") if not raw.get(k)]
    if missing:
        raise ManifestError(f"MANIFEST 缺少必填键 {missing}")

    kwargs: Dict[str, Any] = {
        "strategy_id": str(raw["strategy_id"]),
        "version": str(raw["version"]),
        "venue": str(raw["venue"]),
        "params": tuple(specs),
    }
    for key in ("kind", "author", "description", "fingerprint"):
        if key in raw and raw[key] is not None:
            kwargs[key] = str(raw[key])
    for key in ("rebalance_days", "warmup_bars"):
        if key in raw and raw[key] is not None:
            kwargs[key] = int(raw[key])
    return StrategyManifest(**kwargs)


def D(value: Any) -> Decimal:
    """本模块内部统一入口。放在末尾避免和 dataclass 定义顺序打架。"""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))
