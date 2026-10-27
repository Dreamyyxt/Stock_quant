"""
T-401 · 实盘开关分级（账户级 → 策略级 → 单笔上限）

设计要点
========
为什么不用一个 `live: true` 布尔？

    一个开关的意思是「某一次有人按过它」，之后没人记得它为什么还开着。
    真正的危险不是"没人开"，而是"开了没人关"。

三级分开，等于把「能不能碰真钱」拆成三个必须分别回答的问题：

    1. 这个账户今天允许碰真钱吗？   —— 账户级，按日失效，带总敞口上限
    2. 这个策略今天允许碰真钱吗？   —— 策略级，按日失效，带额度上限
    3. 这一笔最多多大？             —— 单笔上限，必须给数，没有默认值

任何一个问题是「没想过」（未武装 / 已过期 / 对账断裂），整笔就不能下。
**没有默认放行的路径**，也没有"少一级就走纸面"的路径。

三道显式确认
------------
武装一个层级需要 `Confirmation`，三个字段缺一不可：

    actor   必须是 `human:xxx` —— AI 与系统进程不得武装实盘
    reason  至少 6 个字 —— 逼人写下"为什么现在要开"
    echo    必须逐字等于被武装对象的名字 —— 防手滑点错账户

撤销永远不需要确认（fail-closed 的方向必须是"关掉很容易，打开很费劲"）。

过期即失效
----------
武装默认 12 小时有效。过期不是"降级提醒"，而是**直接当作未武装**。
这样即使忘了关，第二天开盘前也不会带着前一天的授权上场。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from .contracts import D, OrderIntent, q, utcnow

__all__ = [
    "TradingMode", "ArmScope", "Confirmation", "ConfirmationError",
    "ArmRecord", "GateCheck", "GateDecision", "LiveGateContext", "LiveGate",
]


# --------------------------------------------------------------------------- #
#  基础类型
# --------------------------------------------------------------------------- #

class TradingMode(str, Enum):
    """调用方必须自己声明模式。Gate **不猜** —— 猜错就是真钱。"""

    PAPER = "PAPER"
    LIVE = "LIVE"


class ArmScope(str, Enum):
    ACCOUNT = "ACCOUNT"
    STRATEGY = "STRATEGY"
    ORDER_CAP = "ORDER_CAP"


class ConfirmationError(ValueError):
    """确认不完整。这不是参数错误，是安全边界。"""


#: 武装的默认有效期。跨日重新武装是刻意的摩擦。
DEFAULT_ARM_TTL = timedelta(hours=12)

#: 理由最短字数。太短的理由等于没写。
MIN_REASON_LEN = 6

#: 单笔上限的硬顶：占 NAV 的百分比。防止"武装时手抖填了 100"。
#: 这条同时也是「单笔上限」这一级自身的上限。
MAX_ORDER_CAP_PCT = Decimal("10.0")


@dataclass(frozen=True)
class Confirmation:
    """
    一次显式确认的凭证。

    `echo` 不是"再确认一下"的客套：它要求人把手要动的那个账户号/策略名
    再打一遍。填错一次就打不开，比弹窗点"确定"有效得多。
    """

    actor: str
    reason: str
    echo: str

    def check(self, expected_echo: str, what: str) -> None:
        actor = (self.actor or "").strip()
        reason = (self.reason or "").strip()
        echo = (self.echo or "").strip()

        if not actor.startswith("human:"):
            raise ConfirmationError(
                f"武装{what}必须由人工执行：actor 需以 'human:' 开头，收到 {actor!r}")
        if len(reason) < MIN_REASON_LEN:
            raise ConfirmationError(
                f"武装{what}必须写明理由（至少 {MIN_REASON_LEN} 字），收到 {reason!r}")
        if echo != expected_echo:
            raise ConfirmationError(
                f"武装{what}需逐字回显 {expected_echo!r}，收到 {echo!r}")


@dataclass(frozen=True)
class ArmRecord:
    scope: ArmScope
    account_id: str
    subject: str                      # 策略级为 strategy_id；其他为 ""
    armed: bool
    armed_by: str = ""
    armed_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    reason: str = ""
    detail: Mapping[str, str] = field(default_factory=dict)

    def active(self, now: datetime) -> bool:
        if not self.armed or self.expires_at is None:
            return False
        return now < self.expires_at

    def state(self, now: datetime) -> str:
        if not self.armed:
            return "disarmed"
        if self.expires_at is None:
            return "armed_no_expiry"        # 不该出现，防御性返回
        if now >= self.expires_at:
            return "expired"
        return "armed"

    def to_dict(self, now: Optional[datetime] = None) -> Dict:
        now = now or utcnow()
        return {
            "scope": self.scope.value,
            "account_id": self.account_id,
            "subject": self.subject,
            "armed": self.armed,
            "state": self.state(now),
            "armed_by": self.armed_by,
            "armed_at": self.armed_at.isoformat() if self.armed_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "reason": self.reason,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class GateCheck:
    """
    一级检查的结论。**通过的也留痕** —— 出事时要能回答"当时是哪几级开着的"。
    """

    level: str
    ok: bool
    detail: str = ""

    def to_dict(self) -> Dict:
        return {"level": self.level, "ok": self.ok, "detail": self.detail}


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    mode: TradingMode
    blocked_layer: str
    reason: str
    checks: Tuple[GateCheck, ...] = ()
    ts: Optional[datetime] = None

    def to_dict(self) -> Dict:
        return {
            "allowed": self.allowed,
            "mode": self.mode.value,
            "blocked_layer": self.blocked_layer,
            "reason": self.reason,
            "checks": [c.to_dict() for c in self.checks],
            "ts": (self.ts or utcnow()).isoformat(),
        }


@dataclass(frozen=True)
class LiveGateContext:
    """一次授权判断所需的账户事实。显式传入，便于回放与测试。"""

    nav: Decimal
    account_gross: Decimal = Decimal(0)
    strategy_notional: Mapping[str, Decimal] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
#  LiveGate
# --------------------------------------------------------------------------- #

@dataclass
class _AccountArm:
    record: ArmRecord
    gross_cap_pct: Decimal


class LiveGate:
    """
    三级实盘开关。`authorize()` 是唯一出口，`allowed=False` 时调用方**不得**下单。

    本类不碰行情、不碰账本、不碰风控 —— 它只回答一个问题：
    「此刻这笔实盘单，三个层级的授权是否都还成立？」
    """

    def __init__(self, *, arm_ttl: timedelta = DEFAULT_ARM_TTL,
                 clock: Callable[[], datetime] = utcnow,
                 audit_sink: Optional[Callable[[Dict], None]] = None) -> None:
        self.arm_ttl = arm_ttl
        self.clock = clock
        self.audit_sink = audit_sink

        self._accounts: Dict[str, _AccountArm] = {}
        self._strategies: Dict[Tuple[str, str], ArmRecord] = {}
        self._caps: Dict[str, ArmRecord] = {}
        #: account_id -> (是否干净, 说明)。默认 **不干净**：没对过账就不能碰真钱。
        self._reconciliation: Dict[str, Tuple[bool, str]] = {}
        self.events: List[Dict] = []

    # -- 内部 ------------------------------------------------------------ #

    def _emit(self, kind: str, **fields) -> Dict:
        rec = {"ts": self.clock().isoformat(), "kind": kind, **fields}
        self.events.append(rec)
        if self.audit_sink is not None:
            self.audit_sink(rec)
        return rec

    # -- 武装（三级） ----------------------------------------------------- #

    def arm_account(self, account_id: str, *, gross_cap_pct: Decimal,
                    confirm: Confirmation,
                    now: Optional[datetime] = None,
                    ttl: Optional[timedelta] = None) -> ArmRecord:
        """账户级：允许这个账户碰真钱，并给出总敞口上限（占 NAV 百分比）。"""
        now = now or self.clock()
        confirm.check(account_id, "账户级实盘")
        cap = D(gross_cap_pct)
        if cap <= 0 or cap > Decimal("100"):
            raise ConfirmationError(f"账户级总敞口上限必须在 (0, 100] 内，收到 {cap}")

        rec = ArmRecord(
            scope=ArmScope.ACCOUNT, account_id=account_id, subject="",
            armed=True, armed_by=confirm.actor.strip(), armed_at=now,
            expires_at=now + (ttl or self.arm_ttl),
            reason=confirm.reason.strip(),
            detail={"gross_cap_pct": str(cap)},
        )
        self._accounts[account_id] = _AccountArm(record=rec, gross_cap_pct=cap)
        self._emit("arm", scope="ACCOUNT", account_id=account_id,
                   armed_by=rec.armed_by, expires_at=rec.expires_at.isoformat(),
                   gross_cap_pct=str(cap), reason=rec.reason)
        return rec

    def arm_strategy(self, account_id: str, strategy_id: str, *,
                     notional_cap: Decimal, confirm: Confirmation,
                     now: Optional[datetime] = None,
                     ttl: Optional[timedelta] = None) -> ArmRecord:
        """策略级：允许这个策略在这个账户上碰真钱，并给出额度上限。"""
        now = now or self.clock()
        confirm.check(strategy_id, f"策略级实盘（{strategy_id}）")
        cap = D(notional_cap)
        if cap <= 0:
            raise ConfirmationError(f"策略额度上限必须为正，收到 {cap}")

        rec = ArmRecord(
            scope=ArmScope.STRATEGY, account_id=account_id, subject=strategy_id,
            armed=True, armed_by=confirm.actor.strip(), armed_at=now,
            expires_at=now + (ttl or self.arm_ttl),
            reason=confirm.reason.strip(),
            detail={"notional_cap": str(cap)},
        )
        self._strategies[(account_id, strategy_id)] = rec
        self._emit("arm", scope="STRATEGY", account_id=account_id,
                   strategy_id=strategy_id, armed_by=rec.armed_by,
                   expires_at=rec.expires_at.isoformat(),
                   notional_cap=str(cap), reason=rec.reason)
        return rec

    def set_order_cap(self, account_id: str, *, max_notional: Decimal,
                      max_order_cap_pct: Decimal = MAX_ORDER_CAP_PCT,
                      confirm: Confirmation,
                      now: Optional[datetime] = None,
                      ttl: Optional[timedelta] = None) -> ArmRecord:
        """
        单笔上限。**没有默认值**：不显式设就是未武装。

        `max_notional` 还会被 `max_order_cap_pct × NAV` 再压一次
        （在 `authorize()` 里做），防止填了个天文数字。
        """
        now = now or self.clock()
        cap = D(max_notional)
        # 回显的是金额本身：人必须把要授权的数字再打一遍。
        confirm.check(str(q(cap, 2)), "单笔上限")
        if cap <= 0:
            raise ConfirmationError(f"单笔上限必须为正，收到 {cap}")

        rec = ArmRecord(
            scope=ArmScope.ORDER_CAP, account_id=account_id, subject="",
            armed=True, armed_by=confirm.actor.strip(), armed_at=now,
            expires_at=now + (ttl or self.arm_ttl),
            reason=confirm.reason.strip(),
            detail={"max_notional": str(q(cap, 2)),
                    "max_order_cap_pct": str(D(max_order_cap_pct))},
        )
        self._caps[account_id] = rec
        self._emit("arm", scope="ORDER_CAP", account_id=account_id,
                   armed_by=rec.armed_by, expires_at=rec.expires_at.isoformat(),
                   max_notional=str(q(cap, 2)), reason=rec.reason)
        return rec

    # -- 撤销 ------------------------------------------------------------ #

    def revoke(self, account_id: str, strategy_id: Optional[str] = None,
               *, by: str = "human:unknown", why: str = "") -> None:
        """
        撤销。**不需要确认**，且一次调用能撤到底。

        方向很重要：打开要三道确认，关掉要一按就关。
        """
        if strategy_id:
            rec = self._strategies.get((account_id, strategy_id))
            if rec is not None:
                self._strategies[(account_id, strategy_id)] = replace(
                    rec, armed=False, reason=why or rec.reason)
            self._emit("revoke", scope="STRATEGY", account_id=account_id,
                       strategy_id=strategy_id, by=by, why=why)
            return

        self._accounts.pop(account_id, None)
        self._caps.pop(account_id, None)
        for key in [k for k in self._strategies if k[0] == account_id]:
            self._strategies.pop(key, None)
        self._emit("revoke", scope="ALL", account_id=account_id,
                   by=by, why=why)

    # -- 对账状态（T-402 的耦合点） --------------------------------------- #

    def set_reconciliation(self, account_id: str, clean: bool,
                           detail: str = "") -> None:
        """
        日终对账结果写回这里。

        **默认是不干净**：没有对过账的账户等于没资格碰真钱。
        这一点经常被写成默认 True，那等于把"忘了对账"变成"对过账了"。
        """
        self._reconciliation[account_id] = (bool(clean), detail or "")
        self._emit("reconciliation", account_id=account_id, clean=bool(clean),
                   detail=detail or "")

    def reconciliation_clean(self, account_id: str) -> bool:
        return self._reconciliation.get(account_id, (False, ""))[0]

    # -- 授权 ------------------------------------------------------------ #

    def authorize(self, intent: OrderIntent, ctx: LiveGateContext, *,
                  mode: TradingMode,
                  now: Optional[datetime] = None) -> GateDecision:
        """
        唯一出口。`mode=PAPER` 时不检查武装（纸面单不需要真钱授权），
        但**必须由调用方显式传入** —— 本类不会从标的、场所或配置里推断模式。
        """
        now = now or self.clock()
        checks: List[GateCheck] = []

        if mode is TradingMode.PAPER:
            checks.append(GateCheck("mode", True, "纸面单，不走实盘开关"))
            return self._allow(mode, checks, now)

        if mode is not TradingMode.LIVE:
            return self._deny(mode, "mode", f"未知交易模式 {mode!r}", checks, now)

        account = intent.account_id

        # 0a. NAV 不可用 / 非正 → 一律拒绝。所有百分比上限都依赖 NAV，
        #     NAV 坏掉时"算得出来"本身就是错的（0 × 任何比例都通过）。
        if D(ctx.nav) <= 0:
            checks.append(GateCheck("nav", False, f"NAV={ctx.nav}"))
            return self._deny(mode, "nav", f"NAV 不可用（{ctx.nav}），拒绝实盘下单",
                              checks, now)

        # 从这里开始**刻意不短路**：一次把所有问题列全。
        # 短路的话运维要「改一个 → 刷新 → 发现还缺一个」，来回好几轮才知道
        # 三级到底缺哪几级。这也是 DoD「少一级就无法下单」要能看见的地方。
        notional = D(intent.notional)
        strategies = _attributed(intent)

        # 1. 账户级
        acc = self._accounts.get(account)
        acc_armed = acc is not None and acc.record.active(now)
        checks.append(GateCheck(
            "account", acc_armed,
            "未武装" if acc is None else (
                f"总敞口上限 {acc.gross_cap_pct}% NAV" if acc_armed
                else f"{acc.record.state(now)}（{acc.record.expires_at} 失效）")))

        # 2. 策略级（attribution 里每个策略分别查）
        if not strategies:
            checks.append(GateCheck("strategy", False, "订单无策略归属"))
        armed_strategies: List[str] = []
        for sid in strategies:
            rec = self._strategies.get((account, sid))
            ok = rec is not None and rec.active(now)
            checks.append(GateCheck(
                f"strategy:{sid}", ok,
                "未武装或已失效" if not ok else
                f"armed，额度 {rec.detail.get('notional_cap')}"))
            if ok:
                armed_strategies.append(sid)

        # 3. 单笔上限（武装）
        cap_rec = self._caps.get(account)
        cap_armed = cap_rec is not None and cap_rec.active(now)
        checks.append(GateCheck(
            "order_cap", cap_armed,
            "未武装或已失效" if not cap_armed else
            f"armed，上限 {cap_rec.detail.get('max_notional')}"))

        # 4. 单笔金额（需上限已武装；两层取小）
        if cap_armed:
            cap_abs = D(cap_rec.detail["max_notional"])
            cap_pct = D(cap_rec.detail.get("max_order_cap_pct", MAX_ORDER_CAP_PCT))
            cap_nav = q(D(ctx.nav) * cap_pct / Decimal("100"), 2)
            cap_eff = min(cap_abs, cap_nav)
            checks.append(GateCheck(
                "order_cap:amount", notional <= cap_eff,
                f"本笔 {q(notional, 2)} / 上限 {q(cap_eff, 2)}"
                f"（绝对 {q(cap_abs, 2)}，{cap_pct}% NAV {q(cap_nav, 2)}）"))

        # 5. 账户总敞口（需账户级已武装）
        if acc_armed:
            gross_cap = q(D(ctx.nav) * acc.gross_cap_pct / Decimal("100"), 2)
            gross_after = q(D(ctx.account_gross) + notional, 2)
            checks.append(GateCheck(
                "account:gross", gross_after <= gross_cap,
                f"下单后 {gross_after} / 上限 {gross_cap}"))

        # 6. 策略额度（只对已武装的策略算）
        for sid in armed_strategies:
            rec = self._strategies[(account, sid)]
            share = _strategy_share(intent, sid)
            cap = D(rec.detail["notional_cap"])
            used = D(ctx.strategy_notional.get(sid, Decimal(0)))
            after = q(used + share, 2)
            checks.append(GateCheck(
                f"strategy_cap:{sid}", after <= cap, f"下单后 {after} / 额度 {cap}"))

        # 7. 对账。放在最后是因为它最容易修（跑一次日终对账），
        #    而"某级没武装"是更根本的问题，先说根本的。
        clean, recon_detail = self._reconciliation.get(account, (False, "尚未对账"))
        checks.append(GateCheck(
            "reconciliation", clean,
            "已清零" if clean else recon_detail or "未清零"))

        failed = [c for c in checks if not c.ok]
        if failed:
            first = failed[0]
            others = len(failed) - 1
            reason = first.detail if not others else (
                f"{first.detail}（另有 {others} 项未通过："
                + ", ".join(c.level for c in failed[1:]) + "）")
            return self._deny(mode, first.level, reason, checks, now, account)

        return self._allow(mode, checks, now, account)

    # -- 结果构造 -------------------------------------------------------- #

    def _allow(self, mode: TradingMode, checks, now, account="") -> GateDecision:
        dec = GateDecision(True, mode, "", "三级授权齐全", tuple(checks), now)
        self._emit("authorize", allowed=True, mode=mode.value,
                   blocked_layer="", account_id=account)
        return dec

    def _deny(self, mode: TradingMode, layer: str, reason: str,
              checks, now, account="") -> GateDecision:
        dec = GateDecision(False, mode, layer, reason, tuple(checks), now)
        self._emit("authorize", allowed=False, mode=mode.value,
                   blocked_layer=layer, reason=reason, account_id=account)
        return dec

    # -- 观测 ------------------------------------------------------------ #

    def status(self, now: Optional[datetime] = None) -> Dict:
        """
        给运维面用。**每一级都列出来，包括没武装的** —— 缺项要看得到。

        `missing` 是给"为什么不能实盘"准备的答案：一次列全，不用让人猜。
        """
        now = now or self.clock()
        return {
            "as_of": now.isoformat(),
            "accounts": [a.record.to_dict(now) for a in self._accounts.values()],
            "strategies": [r.to_dict(now) for r in self._strategies.values()],
            "order_caps": [r.to_dict(now) for r in self._caps.values()],
            "reconciliation": [
                {"account_id": k, "clean": v[0], "detail": v[1]}
                for k, v in sorted(self._reconciliation.items())
            ],
            "live_ready": {
                acct: self._is_live_ready(acct, now)
                for acct in sorted(self._accounts)
            },
            "missing": {
                acct: self._missing_levels(acct, now)
                for acct in sorted(self._accounts)
            },
        }

    def _missing_levels(self, account_id: str, now: datetime) -> List[str]:
        """还差哪几级。空列表 = 可以实盘。这是"少一级就不能下单"的可见版本。"""
        out: List[str] = []
        acc = self._accounts.get(account_id)
        if acc is None:
            out.append("账户级未武装")
        elif not acc.record.active(now):
            out.append(f"账户级 {acc.record.state(now)}")

        armed = [r for (a, _s), r in self._strategies.items()
                 if a == account_id and r.active(now)]
        if not armed:
            out.append("没有策略被武装")

        cap = self._caps.get(account_id)
        if cap is None:
            out.append("单笔上限未设")
        elif not cap.active(now):
            out.append(f"单笔上限 {cap.state(now)}")

        clean, detail = self._reconciliation.get(account_id, (False, "尚未对账"))
        if not clean:
            out.append(f"对账未清零（{detail or '未对账'}）")
        return out

    def _is_live_ready(self, account_id: str, now: datetime) -> bool:
        if account_id not in self._accounts:
            return False
        return not self._missing_levels(account_id, now)


# --------------------------------------------------------------------------- #
#  小工具
# --------------------------------------------------------------------------- #

def _attributed(intent: OrderIntent) -> List[str]:
    return sorted(s for s, v in (intent.attribution or {}).items()
                  if D(v) != 0)


def _strategy_share(intent: OrderIntent, strategy_id: str) -> Decimal:
    """该策略在这一笔里占的名义金额（按归属数量比例）。"""
    qty = D(intent.quantity)
    attr = D((intent.attribution or {}).get(strategy_id, Decimal(0)))
    if qty <= 0:
        return Decimal(0)
    return q(D(intent.notional) * attr / qty, 2)
