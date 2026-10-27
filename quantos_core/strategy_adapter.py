"""
QuantOS 2.0 — 策略输出适配器（T-104）
====================================

把现有策略（`strategies/base_strategy.py` 的 `Signal`：score / direction /
suggested_weight）翻译成契约层的 `StrategyIntent`，**不重写策略逻辑**。

三条纪律
--------
1. **不重算权重**：`suggested_weight` 原样搬进 `target_weight`（相对策略额度）。
   适配器一旦开始"顺手调一下权重"，回测与实盘就必然分叉（R-S06）。
2. **confidence 只透传**：`score` 被夹到 0..1 当 confidence，仅用于展示与审计。
   它不参与任何金额计算（R-A06）—— 这一点由 `capital_allocator` 保证。
3. **必须可追溯**：每个 intent 都带 `signal_ids`，能一路回查到产生它的信号。

为什么还需要 `to_payload / from_payload`
----------------------------------------
策略进程写事件、回放进程读事件，两边必须用同一个编解码。放在本文件是因为
这里最清楚 intent 的字段语义，避免回放器与适配器各写一套导致字段漂移。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from quantos_core.contracts import (
    ContractViolation, D, Signal, SignalKind, StrategyIntent, q,
)

UTC = timezone.utc


def _aware(dt: datetime, name: str) -> datetime:
    if not isinstance(dt, datetime):
        raise TypeError(f"{name} 必须是 datetime")
    if dt.tzinfo is None:
        raise ValueError(f"{name} 必须带时区")
    return dt.astimezone(UTC)


def _clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return max(low, min(high, value))


# --------------------------------------------------------------------------- #
#  适配配置
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class AdapterConfig:
    """一次适配所需的静态信息（都是策略侧已知、与行情无关的）。"""

    strategy_id: str
    strategy_version: str
    venue: str
    #: 信号有效期。超过即失效，组合层不会拿旧信号下单（R-S04）。
    ttl: timedelta = timedelta(hours=4)
    #: 低于此绝对权重视为"不持有"，避免产生一堆 0.0001 的碎意图。
    min_abs_weight: Decimal = Decimal("0.0001")
    #: 是否允许做空。现有策略只做多，默认关闭 —— 关闭时负权重按 0 处理。
    allow_short: bool = False

    def __post_init__(self):
        if not self.strategy_id or not self.venue:
            raise ContractViolation("AdapterConfig 的 strategy_id/venue 不可为空")


@dataclass(frozen=True)
class AdaptedBatch:
    """适配结果：信号原样保留，便于审计"这个意图是哪个信号下的"。"""

    signals: Tuple[Signal, ...]
    intents: Tuple[StrategyIntent, ...]
    dropped: Tuple[str, ...]          # 被丢弃的信号原因（symbol:reason）

    def __len__(self) -> int:
        return len(self.intents)


# --------------------------------------------------------------------------- #
#  适配主逻辑
# --------------------------------------------------------------------------- #

def adapt_signals(signals: Sequence[Any], config: AdapterConfig,
                  as_of: datetime, ts: Optional[datetime] = None) -> AdaptedBatch:
    """
    `signals` 可以是 `strategies.base_strategy.Signal`，也可以是任何带
    `symbol / direction / suggested_weight / score / reason` 属性的对象
    （duck typing，避免 quantos_core 反向依赖 strategies 包）。
    """
    as_of = _aware(as_of, "as_of")
    ts = _aware(ts or as_of, "ts")

    # ---- 第一步：按 symbol 汇总权重（同一标的多条信号求和，夹到 ±1） ---- #
    per_symbol: Dict[str, Decimal] = {}
    scores: Dict[str, Decimal] = {}
    reasons: Dict[str, List[str]] = {}
    dropped: List[str] = []

    for sig in signals:
        symbol = str(getattr(sig, "symbol", "") or "").strip()
        if not symbol:
            dropped.append("<empty>:no_symbol")
            continue
        weight = D(getattr(sig, "suggested_weight", 0) or 0)
        direction = int(getattr(sig, "direction", 0) or 0)
        if weight == 0 and direction != 0:
            # 老策略偶尔只给方向不给权重；此时不猜金额，直接丢弃并留痕。
            dropped.append(f"{symbol}:direction_without_weight")
            continue
        if weight < 0 and not config.allow_short:
            dropped.append(f"{symbol}:short_disabled")
            continue
        # 显式写 0 是**平仓指令**，不是"没有意见"。这两件事必须分开：
        # 前者要求把已有持仓卖光，后者要求什么都别做。混在一起的结果是
        # 策略一旦买进就永远卖不掉 —— 组合只会加仓，永远不会退出一个标的。
        # 微小但非零的权重仍然丢弃（那是碎意图，不是指令）。
        if weight != 0 and abs(weight) < D(config.min_abs_weight):
            dropped.append(f"{symbol}:below_min_weight")
            continue

        per_symbol[symbol] = per_symbol.get(symbol, Decimal(0)) + weight
        score = D(getattr(sig, "score", 0) or 0)
        scores[symbol] = max(scores.get(symbol, Decimal(0)), score)
        reason = str(getattr(sig, "reason", "") or "unspecified")
        reasons.setdefault(symbol, []).append(reason)

    # ---- 第二步：每标的生成 Signal + StrategyIntent -------------------- #
    out_signals: List[Signal] = []
    out_intents: List[StrategyIntent] = []

    for symbol in sorted(per_symbol):
        weight = _clamp(per_symbol[symbol], Decimal(-1), Decimal(1))
        # `weight == 0` 不再被丢弃：它是"目标权重清零"的显式指令。
        # 幂等的关键在于——净额层看到 target=0、current=有持仓，就会算出
        # 一笔卖出；而没有持仓时 delta 也是 0，自然 NOOP，不会产生碎单。

        reason_code = "+".join(sorted(set(reasons.get(symbol, ["unspecified"]))))
        # 0 权重归到 EXIT_LONG：它的语义就是"退出多头"，与负数在只做多模式下等价。
        kind = SignalKind.ENTER_LONG if weight > 0 else SignalKind.EXIT_LONG

        sig = Signal.build(
            strategy_id=config.strategy_id,
            strategy_version=config.strategy_version,
            symbol=symbol, venue=config.venue, kind=kind,
            strength=q(abs(weight), 6), reason_code=reason_code,
            as_of=as_of, ts=ts, ttl=config.ttl,
        )
        # confidence 只做展示：把 score 夹进 0..1，不参与任何金额计算。
        confidence = _clamp(scores.get(symbol, Decimal(0)), Decimal(0), Decimal(1))

        intent = StrategyIntent(
            intent_id=f"{config.strategy_id}:{symbol}:{int(as_of.timestamp())}",
            strategy_id=config.strategy_id,
            strategy_version=config.strategy_version,
            symbol=symbol, venue=config.venue,
            target_weight=q(weight, 8), confidence=q(confidence, 6),
            reason=reason_code, signal_ids=(sig.signal_id,),
            as_of=as_of, ts=ts, expires_at=ts + config.ttl,
        )
        out_signals.append(sig)
        out_intents.append(intent)

    return AdaptedBatch(signals=tuple(out_signals), intents=tuple(out_intents),
                        dropped=tuple(sorted(dropped)))


# --------------------------------------------------------------------------- #
#  事件编解码（策略进程写 / 回放器读，必须同一套）
# --------------------------------------------------------------------------- #

def intent_to_payload(intent: StrategyIntent) -> Dict[str, Any]:
    return {
        "intent_id": intent.intent_id,
        "strategy_id": intent.strategy_id,
        "strategy_version": intent.strategy_version,
        "symbol": intent.symbol,
        "venue": intent.venue,
        "target_weight": intent.target_weight,
        "confidence": intent.confidence,
        "reason": intent.reason,
        "signal_ids": list(intent.signal_ids),
        "as_of": intent.as_of,
        "ts": intent.ts,
        "expires_at": intent.expires_at,
        "reduce_only_hint": intent.reduce_only_hint,
    }


def intent_from_payload(payload: Mapping[str, Any]) -> StrategyIntent:
    try:
        return StrategyIntent(
            intent_id=str(payload["intent_id"]),
            strategy_id=str(payload["strategy_id"]),
            strategy_version=str(payload["strategy_version"]),
            symbol=str(payload["symbol"]),
            venue=str(payload["venue"]),
            target_weight=D(payload["target_weight"]),
            confidence=D(payload.get("confidence", 0)),
            reason=str(payload.get("reason", "")),
            signal_ids=tuple(str(x) for x in payload["signal_ids"]),
            as_of=payload["as_of"],
            ts=payload["ts"],
            expires_at=payload["expires_at"],
            reduce_only_hint=bool(payload.get("reduce_only_hint", False)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractViolation(f"StrategyIntent payload 非法: {payload!r}") from exc


def intent_from_event(env) -> StrategyIntent:
    """从事件信封还原 intent。时间字段已由 EventStore 还原成 datetime。"""
    return intent_from_payload(env.payload)


__all__ = [
    "AdapterConfig", "AdaptedBatch", "adapt_signals",
    "intent_to_payload", "intent_from_payload", "intent_from_event",
]
