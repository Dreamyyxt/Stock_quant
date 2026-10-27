"""
QuantOS 2.0 — 幂等下单键（T-202）
================================

需求 R-A08 / R-K06：超时**不等于**失败。重试必须复用同一个幂等键，
否则一次网络抖动就是两笔真实成交，而且事后无法分辨哪笔是"多余"的。

本模块解决三个具体问题
----------------------
1. **序号持久化**：`(账户, 交易日)` 的序号落盘并 fsync。进程重启后从上次继续，
   不会因为内存归零而重新从 0001 开始 —— 那是重复 cid 的经典成因。
2. **同义意图去重**：同一个 `(账户, 策略域, 标的, 日期, 方向)` 若已有"未关闭"的
   预留，再次申请**返回同一个 cid**，不分配新的。这条挡住了
   "超时后上层重新构建意图 → 换个序号 → 又下一单"的最危险路径。
3. **尝试计数**：每次重试 `bump_attempt`，超过上限就该停下来查交易所，
   而不是继续重试（重试次数不是勇气，是风险敞口）。

写入策略
--------
整文件原子替换（写 tmp → fsync → os.replace）。半个 JSON 比没有 JSON 更糟，
所以宁可整体替换也不做原地修改。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Mapping, Optional, Tuple

from quantos_core.contracts import (
    ContractViolation, D, make_client_order_id, slug,
)

UTC = timezone.utc

#: 预留状态机。只有 OPEN 状态会阻止同义意图分配新 cid。
OPEN_STATES = frozenset({"reserved", "submitted", "accepted", "partial", "unknown"})
CLOSED_STATES = frozenset({"filled", "cancelled", "void", "rejected", "expired"})


def _aware(dt: datetime, name: str = "when") -> datetime:
    if not isinstance(dt, datetime):
        raise TypeError(f"{name} 必须是 datetime")
    if dt.tzinfo is None:
        raise ValueError(f"{name} 必须带时区")
    return dt.astimezone(UTC)


@dataclass
class Reservation:
    """一次幂等键预留。`attempts` 只增不减，是"这笔到底试了几次"的唯一证据。"""

    client_order_id: str
    account_id: str
    strategy_scope: str
    symbol: str
    side: str
    trade_date: str
    seq: int
    created_at: datetime
    state: str = "reserved"
    attempts: int = 0
    order_id: Optional[str] = None

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES

    @property
    def dedupe_key(self) -> Tuple[str, str, str, str, str]:
        return (self.account_id, self.strategy_scope,
                self.symbol.upper(), self.trade_date, self.side.upper())

    def to_dict(self) -> Dict:
        return {
            "client_order_id": self.client_order_id,
            "account_id": self.account_id,
            "strategy_scope": self.strategy_scope,
            "symbol": self.symbol,
            "side": self.side,
            "trade_date": self.trade_date,
            "seq": self.seq,
            "created_at": self.created_at.isoformat(),
            "state": self.state,
            "attempts": self.attempts,
            "order_id": self.order_id,
        }

    @staticmethod
    def from_dict(raw: Mapping) -> "Reservation":
        return Reservation(
            client_order_id=str(raw["client_order_id"]),
            account_id=str(raw["account_id"]),
            strategy_scope=str(raw["strategy_scope"]),
            symbol=str(raw["symbol"]), side=str(raw["side"]),
            trade_date=str(raw["trade_date"]), seq=int(raw["seq"]),
            created_at=datetime.fromisoformat(raw["created_at"]),
            state=str(raw.get("state", "reserved")),
            attempts=int(raw.get("attempts", 0)),
            order_id=raw.get("order_id"),
        )


class ClientOrderIdStore:
    """
    持久化的幂等键分配器。一个进程一个实例，多进程共享同一个文件时靠 flock 保护。

    注意：这里**不做**跨进程互斥（那是 AccountGatewayLock 的职责）。单活锁确保
    同一账户只有一个写者，本类只需保证重启后序号不回退。
    """

    def __init__(self, path: str, *, fsync: bool = True, max_attempts: int = 3):
        self.path = os.path.abspath(path)
        self.fsync = fsync
        self.max_attempts = max_attempts
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._seq: Dict[str, int] = {}                 # "ACCOUNT|YYYYMMDD" -> next_seq
        self._by_cid: Dict[str, Reservation] = {}
        self._load()

    # -- 持久化 ---------------------------------------------------------- #

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (json.JSONDecodeError, OSError):
            # 文件损坏不应静默重置序号（会导致 cid 重复），直接拒绝启动。
            raise ContractViolation(
                f"幂等键文件损坏，拒绝启动：{self.path}（请人工核对后修复）")
        self._seq = {str(k): int(v) for k, v in (raw.get("seq") or {}).items()}
        self._by_cid = {str(k): Reservation.from_dict(v)
                        for k, v in (raw.get("reservations") or {}).items()}

    def _save(self) -> None:
        payload = {
            "version": 1,
            "seq": self._seq,
            "reservations": {k: v.to_dict() for k, v in self._by_cid.items()},
        }
        directory = os.path.dirname(self.path) or "."
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".cid-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=None)
                fh.flush()
                if self.fsync:
                    os.fsync(fh.fileno())
            os.replace(tmp, self.path)
            if self.fsync:
                dfd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # -- 分配 ------------------------------------------------------------ #

    def reserve(self, account_id: str, strategy_scope: str, symbol: str,
                side, when: datetime) -> Tuple[Reservation, bool]:
        """
        申请幂等键。返回 (reservation, created)。

        created=False 表示命中同义意图的未关闭预留 —— **必须复用**这个 cid。
        这是本模块最重要的返回值语义，调用方不许无视。
        """
        when = _aware(when)
        side_u = str(getattr(side, "value", side)).upper()
        date = when.strftime("%Y%m%d")

        existing = self.open_for(account_id, strategy_scope, symbol, side_u, when)
        if existing is not None:
            return existing, False

        key = f"{slug(account_id)}|{date}"
        seq = self._seq.get(key, 0) + 1
        self._seq[key] = seq

        cid = make_client_order_id(account_id, strategy_scope, symbol, when, seq)
        if cid in self._by_cid:
            # 理论上不该发生（序号单调）。真发生了说明文件被外部改过。
            raise ContractViolation(f"cid 冲突，幂等键文件可能被篡改：{cid}")

        res = Reservation(
            client_order_id=cid, account_id=account_id,
            strategy_scope=strategy_scope, symbol=symbol.upper(), side=side_u,
            trade_date=date, seq=seq, created_at=when,
        )
        self._by_cid[cid] = res
        self._save()
        return res, True

    def get(self, client_order_id: str) -> Optional[Reservation]:
        return self._by_cid.get(client_order_id)

    def open_for(self, account_id: str, strategy_scope: str, symbol: str,
                 side, when: datetime) -> Optional[Reservation]:
        key = (account_id, strategy_scope, symbol.upper(),
               _aware(when).strftime("%Y%m%d"), str(getattr(side, "value", side)).upper())
        for res in sorted(self._by_cid.values(), key=lambda r: r.seq):
            if res.dedupe_key == key and res.is_open:
                return res
        return None

    def open_reservations(self) -> List[Reservation]:
        return [r for r in self._by_cid.values() if r.is_open]

    # -- 状态推进 -------------------------------------------------------- #

    def bump_attempt(self, client_order_id: str) -> int:
        """
        记录一次下单尝试。超过 max_attempts 后调用方必须停止重试、转去查交易所。
        """
        res = self._require(client_order_id)
        res.attempts += 1
        if res.state == "reserved":
            res.state = "submitted"
        self._save()
        return res.attempts

    def exceeds_attempts(self, client_order_id: str) -> bool:
        return self._require(client_order_id).attempts > self.max_attempts

    def mark(self, client_order_id: str, state: str,
             order_id: Optional[str] = None) -> Reservation:
        if state not in OPEN_STATES and state not in CLOSED_STATES:
            raise ContractViolation(f"未知预留状态 {state!r}")
        res = self._require(client_order_id)
        res.state = state
        if order_id is not None:
            res.order_id = order_id
        self._save()
        return res

    def close(self, client_order_id: str, state: str = "filled") -> Reservation:
        return self.mark(client_order_id, state)

    def _require(self, client_order_id: str) -> Reservation:
        res = self._by_cid.get(client_order_id)
        if res is None:
            raise ContractViolation(f"未登记的 client_order_id：{client_order_id}")
        return res

    # -- 观测 ------------------------------------------------------------ #

    def stats(self) -> Dict[str, int]:
        opened = [r for r in self._by_cid.values() if r.is_open]
        return {
            "total": len(self._by_cid),
            "open": len(opened),
            "max_attempts": max((r.attempts for r in self._by_cid.values()), default=0),
        }


__all__ = [
    "OPEN_STATES", "CLOSED_STATES", "Reservation", "ClientOrderIdStore",
]
