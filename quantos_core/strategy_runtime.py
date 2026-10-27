"""
策略运行时 —— 把已经建好的各层串成一条单向链。

为什么需要这一层
================
B0~B7 每一层单独测都过，但它们之间**从来没有被同一次调用串起来过**：
净额层不知道意图从哪来，OM
S 不知道目标从哪来，账本不知道成交从哪来。
所以"整条链路是对的"这个结论，一直是靠各段分测拼出来的印象。

这一层只做一件事：按固定顺序调它们，并保证**任何一段失败都不被下一段掩盖**。

    行情 frame
      → 策略 compute（只产信号）
      → adapt_signals（信号 → 意图）
      → net_targets（意图 → 净目标 → 差额）
      → oms.prepare / submit（风控在这里拦）
      → PaperBroker 成交
      → oms.on_fill + ledger.apply_fill（带策略归属）
      → 归因

五条不在这里做的事
==================
1. **不重算风险**。风控只认 `RiskEngine`，这里不复制一份"差不多"的判断。
2. **不吞拒绝**。被风控拒的订单进 `report.rejected`，含 policy_key 原文，
   不许变成"今天没成交"。
3. **不自动重试**。超时的处理在 OMS 里（先查交易所），运行时不做第二次提交。
4. **不提升阶段**。RESEARCH 阶段的策略不会被 `policies()` 交出来，
   所以即使硬塞进运行时也没有额度、不会成交 —— 这是准入的第二道锁。
5. **不假装它是生产**。`PaperBroker` 用"决策价 ± 固定滑点 + 固定费率"成交，
   这不是撮合模拟，只是让账本能动起来。真实撮合属于网关层（B9）。

关于风控客户端
==============
生产环境的 `Oms` 应当接 `RiskServiceClient`（独立进程 + IPC）。这里的
`EngineRiskClient` 是**进程内适配器**，仅供回放/模拟盘使用。
两者契约一致（`.check(intent, ctx)`），所以换过去不需要改运行时；
但别把进程内版本当成生产版本 —— 进程一崩，风控和策略一起消失（R-A02）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .capital_allocator import NettingInput, SymbolKey, net_targets
from .contracts import (
    D, ContractViolation, Fill, Side, StrategyRunState, TargetDecision,
    Verdict, q,
)
from .ledger import Ledger
from .oms import Oms
from .risk_engine import RiskContext, RiskEngine, RiskState
from .strategy_adapter import AdapterConfig, adapt_signals
from .strategy_api import (
    MarketFrame, SignalOut, StrategyContext, StrategyError, coerce_signal,
)
from .strategy_pack import LoadedPack

__all__ = [
    "PaperBroker", "PaperFillConfig", "EngineRiskClient", "StrategyRun",
    "RunReport", "StrategyRuntime",
]


# --------------------------------------------------------------------------- #
#  成交模拟
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class PaperFillConfig:
    """
    模拟成交的口径。默认值刻意**保守**（多算成本），因为把成本算少了
    只会让策略看起来更好，而这类错误没人会主动去查。
    """

    #: 单边滑点（基点）。按方向不利计：买入加价、卖出减价。
    slippage_bps: Decimal = Decimal("5")
    #: 手续费（基点），按成交名义收。
    fee_bps: Decimal = Decimal("2")
    #: 计价货币。多币种要按标的分别给，这里只支持单一计价货币。
    quote_ccy: str = "USD"

    def __post_init__(self) -> None:
        if D(self.slippage_bps) < 0 or D(self.fee_bps) < 0:
            raise ContractViolation("滑点/费率不能为负 —— 负成本等于凭空发钱")
        if not self.quote_ccy:
            raise ContractViolation("quote_ccy 不可为空")


class PaperBroker:
    """
    立刻成交的假场所。**只用于模拟盘与回放**。

    它的存在意义不是"模拟得像"，而是"让账本有真实的分录可记"：
    没有成交，`apply_fill` 就永远跑不到，滑点/手续费归因就永远是 0，
    而 0 看起来像是"没有成本"，不是"没跑过"。
    """

    def __init__(self, config: Optional[PaperFillConfig] = None) -> None:
        self.config = config or PaperFillConfig()
        self._seq = 0

    def fill(self, intent, *, quantity: Decimal, decision_price: Decimal,
             now: datetime) -> Fill:
        if D(quantity) <= 0:
            raise ContractViolation(f"成交量必须为正，收到 {quantity}")
        p0 = D(decision_price)
        if p0 <= 0:
            raise ContractViolation(f"决策价必须为正，收到 {decision_price}")

        slip = D(self.config.slippage_bps) / Decimal(10000)
        price = q(p0 * (Decimal(1) + slip), 9) if intent.side is Side.BUY \
            else q(p0 * (Decimal(1) - slip), 9)
        if price <= 0:
            raise ContractViolation(f"滑点后价格非正：{price}（滑点设置过大？）")

        qty = D(quantity)
        gross = q(qty * price, 8)
        fee = q(gross * D(self.config.fee_bps) / Decimal(10000), 8)

        self._seq += 1
        return Fill(
            fill_id=f"pf-{intent.client_order_id}-{self._seq}",
            order_id=f"paper-{intent.client_order_id}",
            client_order_id=intent.client_order_id,
            venue_trade_id=f"pt-{intent.client_order_id}-{self._seq}",
            quantity=qty, price=price, fee=fee,
            fee_currency=self.config.quote_ccy, ts=now,
            liquidity="TAKER",
        )


class EngineRiskClient:
    """
    `RiskEngine` 的进程内适配器，满足 `Oms` 需要的 `.check(intent, ctx)`。

    **不是生产用**。生产必须走 `RiskServiceClient`，否则风控与策略同生共死（R-A02）。
    这里提供它，是为了让回放与模拟盘能在单进程里跑完整链路。
    """

    def __init__(self, engine: RiskEngine, state: RiskState) -> None:
        self.engine = engine
        self.state = state
        self.decisions: List[Any] = []

    def check(self, intent, ctx: RiskContext):
        decision = self.engine.check_order(intent, ctx, self.state)
        self.decisions.append(decision)
        return decision

    def allowed(self, decision) -> bool:
        return bool(getattr(decision, "allowed", False))


# --------------------------------------------------------------------------- #
#  运行单元与报告
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class StrategyRun:
    """一次运行里参与的一个策略：包 + 参数 + 运行态。"""

    pack: LoadedPack
    params: Mapping[str, Any] = field(default_factory=dict)
    state: StrategyRunState = StrategyRunState.ACTIVE

    @property
    def strategy_id(self) -> str:
        return self.pack.manifest.strategy_id


@dataclass(frozen=True)
class RunReport:
    """
    一次运行的完整账。字段设计目标是：**光看报告就能判断题出在哪一段**。

    这就是为什么 `blocked` / `rejected` / `errors` 分开列：
    三个都是"没成交"，但原因分别在组合层、风控层、策略层，
    混成一个 "failed" 字段就永远查不清。
    """

    as_of: datetime
    nav: Decimal
    signals: Tuple[Tuple[str, int], ...] = ()            # (策略, 信号数)
    intents: int = 0
    targets: Tuple[Any, ...] = ()                        # TargetPosition
    submitted: Tuple[str, ...] = ()                      # client_order_id
    rejected: Tuple[Tuple[str, str, str], ...] = ()      # (标的, policy_key, reason)
    #: 因风控 cap 而重算过的单：(标的, 原 delta, 重算后 delta)。
    #: 单独列出来，否则"下了但变小了"会被当成"正常下单"。
    capped: Tuple[Tuple[str, str, str], ...] = ()
    blocked: Tuple[Tuple[str, str], ...] = ()            # (symbol, 原因)
    fills: Tuple[Fill, ...] = ()
    errors: Tuple[Tuple[str, str], ...] = ()             # (策略, 异常)
    equity_after: Optional[Decimal] = None

    @property
    def traded(self) -> bool:
        return bool(self.fills)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "nav": str(self.nav),
            "signals": [{"strategy_id": s, "count": n} for s, n in self.signals],
            "intents": self.intents,
            "targets": [{
                "symbol": t.symbol, "venue": t.venue,
                "decision": getattr(t.decision, "value", str(t.decision)),
                "delta_notional": str(t.delta_notional),
                "delta_qty": str(t.delta_qty),
                "blocked_reason": t.blocked_reason,
            } for t in self.targets],
            "submitted": list(self.submitted),
            "rejected": [{"symbol": s, "policy_key": k, "reason": r}
                         for s, k, r in self.rejected],
            "capped": [{"symbol": s, "delta_before": b, "delta_after": a}
                       for s, b, a in self.capped],
            "blocked": [{"symbol": s, "reason": r} for s, r in self.blocked],
            "fills": [{"client_order_id": f.client_order_id,
                       "quantity": str(f.quantity), "price": str(f.price),
                       "fee": str(f.fee), "fee_currency": f.fee_currency}
                      for f in self.fills],
            "errors": [{"strategy_id": s, "error": e} for s, e in self.errors],
            "equity_after": None if self.equity_after is None else str(self.equity_after),
        }

    def summary(self) -> str:
        return (f"{self.as_of:%Y-%m-%d}: 信号 {sum(n for _s, n in self.signals)} 条 / "
                f"意图 {self.intents} 条 / 目标 {len(self.targets)} 个 / "
                f"成交 {len(self.fills)} 笔 / 被拒 {len(self.rejected)} / "
                f"限额重算 {len(self.capped)} / 阻断 {len(self.blocked)} / "
                f"异常 {len(self.errors)}")


# --------------------------------------------------------------------------- #
#  运行时
# --------------------------------------------------------------------------- #

class StrategyRuntime:
    """
    一次 `run_once()` = 一个调度周期。

    无内部状态（turnover 除外，它按日累计），所有事实由参数传入 ——
    否则回放与实盘会跑出不同结果，而那种差异极难查。
    """

    def __init__(self, *, ledger: Ledger, oms: Oms, risk_client: EngineRiskClient,
                 account_id: str, broker: Optional[PaperBroker] = None) -> None:
        if not account_id:
            raise ContractViolation("account_id 不可为空")
        self.ledger = ledger
        self.oms = oms
        self.risk = risk_client
        self.account_id = account_id
        self.broker = broker or PaperBroker()
        #: 策略 → 当日已成交名义。跨 run_once 累计，按 `_turnover_day` 清零。
        self._turnover: Dict[str, Decimal] = {}
        self._turnover_day: Optional[Any] = None

    # -- 主流程 ---------------------------------------------------------- #

    def run_once(self, frame: MarketFrame, runs: Sequence[StrategyRun], *,
                 nav: Decimal, now: datetime,
                 allocations: Mapping[str, Any],
                 policies: Mapping[str, Any],
                 risk_state: Optional[RiskState] = None,
                 min_notional: Optional[Mapping[str, Decimal]] = None,
                 ) -> RunReport:
        nav = D(nav)
        if nav <= 0:
            raise ContractViolation(f"NAV 必须为正，收到 {nav}")

        self._roll_turnover(now)

        # 价格表必须按 (venue, symbol) 索引：组合层、账本、风控三处都用这个键型，
        # 用裸 symbol 会在 sorted() 里和 tuple 比较，报一句跟业务毫无关系的 TypeError。
        prices = {(frame.venue, sym): px
                  for sym, px in frame.latest_prices().items()}
        errors: List[Tuple[str, str]] = []
        sig_counts: List[Tuple[str, int]] = []
        all_intents: List[Any] = []

        # ---- 1+2. 策略算信号 → 适配成意图 ----------------------------- #
        for run in runs:
            manifest = run.pack.manifest
            if manifest.venue != frame.venue:
                errors.append((run.strategy_id,
                               f"策略场所 {manifest.venue} 与行情场所 {frame.venue} 不一致"))
                continue
            try:
                params = manifest.resolve_params(run.params)
                ctx = StrategyContext(as_of=frame.as_of, params=params,
                                      positions=self._strategy_positions(run.strategy_id))
                raw = run.pack.compute(frame, ctx) or ()
                # 返回值在这里再过一遍：干跑过了，但换了参数可能走上别的分支。
                signals = tuple(coerce_signal(item) for item in raw)
                sig_counts.append((run.strategy_id, len(signals)))
                batch = adapt_signals(
                    signals,
                    AdapterConfig(strategy_id=run.strategy_id,
                                  strategy_version=manifest.version,
                                  venue=manifest.venue),
                    as_of=frame.as_of, ts=now)
                all_intents.extend(batch.intents)
            except (StrategyError, ContractViolation) as exc:
                errors.append((run.strategy_id, f"{type(exc).__name__}: {exc}"))
            except Exception as exc:                     # noqa: BLE001
                errors.append((run.strategy_id, f"{type(exc).__name__}: {exc}"))

        # ---- 3. 净额 --------------------------------------------------- #
        current_notional = {k: q(D(v) * prices[k], 2)
                            for k, v in self._position_qty().items() if k in prices}
        inflight = {}
        for order in self.oms.open_orders():
            key = (order.venue, order.symbol)
            inflight[key] = inflight.get(key, Decimal(0)) + D(order.signed_inflight)
        states = {r.strategy_id: r.state for r in runs}

        targets = net_targets(NettingInput(
            intents=tuple(all_intents), allocations=allocations, policies=policies,
            prices=prices, current_notional=current_notional,
            inflight_notional=inflight, strategy_states=states, now=now,
            min_notional=dict(min_notional or {}),
            current_by_strategy=self._strategy_notional_by_symbol(prices),
        ))

        # ---- 4. 下单（风控在这一步拦） --------------------------------- #
        ctx = self._risk_context(frame, now, nav, prices, allocations, risk_state)
        submitted: List[str] = []
        rejected: List[Tuple[str, str, str]] = []
        fills: List[Fill] = []

        capped: List[Tuple[str, str, str]] = []

        for target in targets:
            if target.decision is not TargetDecision.TRADE or target.delta_qty == 0:
                continue
            try:
                intent, result = self._submit_with_cap(
                    target, ctx, now, rejected, capped)
            except ContractViolation as exc:
                rejected.append((target.symbol, "contract", str(exc)))
                continue
            if intent is None:
                continue

            submitted.append(intent.client_order_id)

            # ---- 5. 成交 → 账本 ---------------------------------------- #
            fill = self.broker.fill(intent, quantity=D(intent.quantity),
                                    decision_price=D(target.price), now=now)
            self.oms.on_ack(intent.client_order_id, at=now)
            if not self.oms.on_fill(fill, at=now):
                errors.append((target.symbol, "成交未入 OMS（重复回报？）"))
                continue
            attribution = self._attribution_qty(target, D(intent.quantity))
            self.ledger.apply_fill(
                fill, venue=target.venue, symbol=target.symbol,
                account_id=self.account_id, side=intent.side,
                quote_ccy=self.broker.config.quote_ccy,
                decision_price=D(target.price), attribution=attribution, ts=now)
            fills.append(fill)

        # 换手按策略归属累计：一个成交可能跨多个策略，按名义绝对值分账。
        for target in targets:
            if target.decision is not TargetDecision.TRADE or target.delta_notional == 0:
                continue
            total = sum((abs(D(v)) for v in target.per_strategy.values()), Decimal(0))
            if total <= 0:
                continue
            for sid, val in target.per_strategy.items():
                share = abs(D(val)) / total
                self._turnover[sid] = self._turnover.get(sid, Decimal(0)) \
                    + abs(D(target.delta_notional)) * share

        blocked = [(t.symbol, t.blocked_reason) for t in targets if t.blocked_reason]
        equity = self._equity(prices)
        return RunReport(
            as_of=now, nav=nav, signals=tuple(sig_counts),
            intents=len(all_intents), targets=tuple(targets),
            submitted=tuple(submitted), rejected=tuple(rejected),
            capped=tuple(capped), blocked=tuple(blocked),
            fills=tuple(fills), errors=tuple(errors),
            equity_after=equity,
        )

    # -- 内部 ------------------------------------------------------------ #

    def _submit_with_cap(self, target, ctx, now, rejected, capped):
        """
        发一笔目标单，并在风控要求「按 cap 重算」时**重算一次**。

        这条契约以前是断的：`RiskEngine` 对大单返回 `REDUCE_REQUIRED` 并给出
        `cap_notional`，注释写着"要求上游按 cap 用原意图重算，风控不代改单量"，
        但上游根本没人在做这件事 —— 结果是**所有超过单笔上限的仓位永远建不起来**，
        而报告上只显示"被拒"，看起来像是风控在正常工作。

        只重算一次，理由：
          · cap 是上界，重算后的单一定更小，第二次还不过说明限制来自别处
            （敞口、换手、权限），再算也是白算；
          · 允许循环重算等于让策略用"被拒 → 缩小 → 再拒"的方式试探风控边界。
        """
        intent, _res = self.oms.prepare(target, now)
        if intent is None:
            rejected.append((target.symbol, "prepare_empty", "组合层没产出下单意图"))
            return None, None

        result = self.oms.submit(intent, ctx)
        if result.accepted:
            return intent, result

        decision = getattr(result, "decision", None)
        verdict = getattr(decision, "verdict", None)
        cap = getattr(decision, "cap_notional", None)
        if verdict is not Verdict.REDUCE_REQUIRED or cap is None:
            rejected.append((target.symbol,
                             getattr(decision, "policy_key", result.reason),
                             getattr(decision, "reason", result.reason)))
            return None, result

        shrunk = target.capped(D(cap))
        if shrunk.delta_qty == 0 or shrunk.delta_notional == target.delta_notional:
            rejected.append((target.symbol, "cap_no_effect",
                             f"按 cap {cap} 重算后仍无有效差额，原 delta "
                             f"{target.delta_notional}"))
            return None, result
        capped.append((target.symbol, str(target.delta_notional),
                       str(shrunk.delta_notional)))

        intent2, _res2 = self.oms.prepare(shrunk, now)
        if intent2 is None:
            rejected.append((target.symbol, "prepare_empty_after_cap",
                             "按 cap 重算后组合层没产出意图"))
            return None, result
        result2 = self.oms.submit(intent2, ctx)
        if result2.accepted:
            return intent2, result2
        d2 = getattr(result2, "decision", None)
        rejected.append((target.symbol,
                         getattr(d2, "policy_key", result2.reason),
                         "按 cap 重算后仍被拒：" + getattr(d2, "reason", result2.reason)))
        return None, result2


    def _roll_turnover(self, now: datetime) -> None:
        day = now.date()
        if self._turnover_day != day:
            self._turnover.clear()
            self._turnover_day = day

    def _position_qty(self) -> Dict[SymbolKey, Decimal]:
        return {key: D(pos.qty) for key, pos in self.ledger.positions().items()}

    def _strategy_positions(self, strategy_id: str) -> Dict[str, Decimal]:
        out: Dict[str, Decimal] = {}
        for (_venue, symbol), pos in self.ledger.positions().items():
            qty = D(pos.attr_qty.get(strategy_id, Decimal(0)))
            if qty != 0:
                out[symbol] = qty
        return out

    def _strategy_notional_by_symbol(self, prices: Mapping[SymbolKey, Decimal]
                                     ) -> Dict[str, Dict[SymbolKey, Decimal]]:
        out: Dict[str, Dict[SymbolKey, Decimal]] = {}
        for key, pos in self.ledger.positions().items():
            price = prices.get(key)
            if price is None:
                continue
            for sid, qty in pos.attr_qty.items():
                held = D(qty)
                if held == 0:
                    continue
                out.setdefault(sid, {})[key] = q(held * D(price), 2)
        return out

    def _risk_context(self, frame: MarketFrame, now: datetime, nav: Decimal,
                      prices: Mapping[SymbolKey, Decimal],
                      allocations: Mapping[str, Any],
                      risk_state: Optional[RiskState]) -> RiskContext:
        position_notional: Dict[SymbolKey, Decimal] = {}
        total_gross = Decimal(0)
        for key, qty in self._position_qty().items():
            price = prices.get(key)
            if price is None:
                # 缺价格的持仓不许按 0 计：那会让总敞口凭空变小，风控放行。
                continue
            notional = q(D(qty) * D(price), 2)
            position_notional[key] = notional
            total_gross += abs(notional)

        age = D((now - frame.as_of).total_seconds())
        return RiskContext(
            nav=nav, now=now, position_notional=position_notional,
            strategy_notional=self._strategy_notional_totals(prices),
            strategy_turnover=dict(self._turnover),
            strategy_capacity={sid: D(getattr(a, "effective_capacity_usd", 0))
                               for sid, a in allocations.items()},
            market_data_age_sec={frame.venue: age},
            total_gross_notional=total_gross,
        )

    def _strategy_notional_totals(self, prices: Mapping[SymbolKey, Decimal]
                                  ) -> Dict[str, Decimal]:
        out: Dict[str, Decimal] = {}
        for per in self._strategy_notional_by_symbol(prices).values():
            for sid, notional in per.items():
                out[sid] = out.get(sid, Decimal(0)) + abs(D(notional))
        return out

    @staticmethod
    def _attribution_qty(target, fill_qty: Decimal) -> Dict[str, Decimal]:
        """
        把成交数量按 per_strategy 名义比例分给各策略（R-D05）。

        卖了就要按卖的方向取份额：per_strategy 里卖出的名义是负的，
        直接用负数量会被 `_apply_attribution` 当成 0 跳过，
        结果是"卖了但不减策略归属"，归属数量越攒越多。
        """
        per = {sid: D(v) for sid, v in (target.per_strategy or {}).items() if D(v) != 0}
        total = sum((abs(v) for v in per.values()), Decimal(0))
        if total <= 0:
            return {}
        side_sign = 1 if D(target.delta_notional) > 0 else -1
        out: Dict[str, Decimal] = {}
        for sid, notional in per.items():
            if (notional > 0 and side_sign < 0) or (notional < 0 and side_sign > 0):
                continue
            out[sid] = q(fill_qty * abs(notional) / total, 8)
        return out

    def _equity(self, prices: Mapping[SymbolKey, Decimal]) -> Optional[Decimal]:
        try:
            report = self.ledger.performance(prices)
        except Exception:                                # noqa: BLE001
            return None
        return getattr(report, "nav", None) or getattr(report, "nav_base", None)
