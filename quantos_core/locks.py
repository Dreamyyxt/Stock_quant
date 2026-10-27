"""
QuantOS 2.0 — 账户×网关单活锁（T-204）
=====================================

为什么按「账户 × 网关」而不是「进程」加锁
----------------------------------------
同一个进程可能同时管两个账户（合规允许），也可能是同一账户跑了两个进程（绝不
允许）。锁的粒度必须是**被保护的资源**——同一账户在同一交易所的同一份钱，
而不是"某个程序实例"。

真实事故形态（现有 `core/trading_lock.py` 只按账户，漏了这一半）：
  进程 A 用 IBKR 网关交易 IBKR_US，进程 B 用 Tiger 网关交易同一个账户的 Tiger 侧。
  按账户加锁 → 两者互相阻塞，白白停掉一个市场的交易；
  按账户×网关 → 两个都能跑，但同网关永远只有一个写者。

锁文件内容
----------
JSON（pid / host / 起始时间 / 账户 / 网关）。被占用时读出来直接告诉人
"是谁在占"，比一个空文件有用得多。释放时清空，避免残留信息误导排查。
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from quantos_core.contracts import ContractViolation, slug

UTC = timezone.utc


class LockBusy(RuntimeError):
    """锁已被占用。消息里带持有者信息，便于直接定位是谁在跑。"""

    def __init__(self, path: str, owner: Optional["LockOwner"]):
        self.path = path
        self.owner = owner
        detail = ""
        if owner is not None:
            detail = (f"（账户 {owner.account_id} / 网关 {owner.gateway}，"
                      f"pid={owner.pid} host={owner.host} "
                      f"since={owner.started_at.isoformat()}）")
        super().__init__(f"单活锁已被占用：{path}{detail}")


@dataclass(frozen=True)
class LockOwner:
    pid: int
    host: str
    account_id: str
    gateway: str
    started_at: datetime

    def to_dict(self) -> dict:
        return {
            "pid": self.pid, "host": self.host,
            "account_id": self.account_id, "gateway": self.gateway,
            "started_at": self.started_at.isoformat(),
        }

    @staticmethod
    def from_dict(raw: dict) -> Optional["LockOwner"]:
        try:
            return LockOwner(
                pid=int(raw["pid"]), host=str(raw["host"]),
                account_id=str(raw["account_id"]), gateway=str(raw["gateway"]),
                started_at=datetime.fromisoformat(raw["started_at"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


class AccountGatewayLock:
    """
    独占式 advisory lock，基于 flock(2)。进程被 kill -9 时内核自动释放，
    所以不会出现"崩溃后锁文件残留、重启起不来"的经典困境。
    """

    def __init__(self, root: str, account_id: str, gateway: str):
        if not account_id or not gateway:
            raise ContractViolation("account_id / gateway 不可为空")
        self.root = os.path.abspath(root)
        self.account_id = account_id
        self.gateway = gateway.upper()
        self.path = os.path.join(
            self.root, f"{slug(account_id)}__{slug(gateway)}.lock")
        self._fh = None
        self._thread_lock = threading.Lock()

    # -- 生命周期 -------------------------------------------------------- #

    def acquire(self, *, wait: bool = False) -> LockOwner:
        """成功返回持有者信息；被占用抛 LockBusy。"""
        with self._thread_lock:
            if self._fh is not None:
                return self._read_owner() or self._new_owner()

            os.makedirs(self.root, exist_ok=True)
            fh = open(self.path, "a+", encoding="utf-8")
            flags = fcntl.LOCK_EX if wait else (fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                fcntl.flock(fh.fileno(), flags)
            except OSError:
                owner = self._read_owner_from(fh)
                fh.close()
                raise LockBusy(self.path, owner)

            owner = self._new_owner()
            try:
                fh.seek(0)
                fh.truncate()
                fh.write(json.dumps(owner.to_dict(), ensure_ascii=False))
                fh.flush()
                os.fsync(fh.fileno())
            except OSError:
                # 写元数据失败不影响锁的有效性，锁已由 flock 持有。
                pass
            self._fh = fh
            return owner

    def release(self) -> None:
        with self._thread_lock:
            if self._fh is None:
                return
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                self._fh.seek(0)
                self._fh.truncate()
                self._fh.flush()
            except OSError:
                pass
            finally:
                try:
                    self._fh.close()
                finally:
                    self._fh = None

    @property
    def is_held(self) -> bool:
        return self._fh is not None

    # -- 观测 ------------------------------------------------------------ #

    def owner(self) -> Optional[LockOwner]:
        """读取当前持有者（不尝试加锁，用于运维面板展示）。"""
        if self._fh is not None:
            return self._read_owner()
        if not os.path.exists(self.path):
            return None
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                return self._read_owner_from(fh)
        except OSError:
            return None

    def _read_owner(self) -> Optional[LockOwner]:
        if self._fh is None:
            return None
        return self._read_owner_from(self._fh)

    @staticmethod
    def _read_owner_from(fh) -> Optional[LockOwner]:
        try:
            fh.seek(0)
            text = fh.read().strip()
        except OSError:
            return None
        if not text:
            return None
        try:
            return LockOwner.from_dict(json.loads(text))
        except json.JSONDecodeError:
            return None

    def _new_owner(self) -> LockOwner:
        return LockOwner(
            pid=os.getpid(), host=socket.gethostname(),
            account_id=self.account_id, gateway=self.gateway,
            started_at=datetime.now(UTC),
        )

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc_info):
        self.release()
        return False


__all__ = ["LockBusy", "LockOwner", "AccountGatewayLock"]
