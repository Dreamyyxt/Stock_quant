"""
QuantOS 2.0 — State Persistence Primitives（B10 / T-1001、T-1003）
=================================================================

进程会死，状态不能跟着死。这个模块只管两件底层的事，业务语义不在这里：

1. **原子写**：`tmp → write → fsync(file) → replace → fsync(dir)`。
   直接往目标文件写，进程在中途被杀就会留下半截 JSON —— 而半截 JSON
   比没有文件更糟：它看起来"存在"，读起来抛一个与业务无关的异常。

2. **带校验的信封**：`{kind, schema, saved_at, checksum, body}`。
   校验和防"文件被改坏却依然合法"，`kind` 防"拿错文件类型"（比如把排期
   文件当账本读），`schema` 防"用新代码读旧结构"。

为什么不用 SQLite：全项目零第三方依赖、单机单进程量级、账本每拍才写一次。
SQLite 会引入"文件锁 + WAL + 迁移"三件需要运维的东西，而现在需要的是
"能读出来、读不出来要说清楚"。

**不许静默**：读取失败一律返回显式状态（`StateLoad`），不返回空值冒充成功。
调用方要决定停机还是继续，这个决定不该由存储层替它做。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

from quantos_core.contracts import utcnow

__all__ = [
    "StateError", "StateCorrupt", "StateSchemaMismatch", "StateLoad",
    "write_atomic", "canonical_json", "digest_of", "read_envelope",
    "JsonStateStore", "RiskStateStore",
]


class StateError(RuntimeError):
    """状态存储层错误基类。"""


class StateCorrupt(StateError):
    """内容不可信：不是合法 JSON / 校验和不符 / 结构缺失。"""


class StateSchemaMismatch(StateError):
    """版本与代码不兼容 —— 不做猜测式迁移。"""


# --------------------------------------------------------------------------- #
#  底层原语
# --------------------------------------------------------------------------- #

def canonical_json(payload: Any) -> bytes:
    """校验和用的规范编码：键排序 + 紧凑分隔符，跨机器可复现。"""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str).encode("utf-8")


def digest_of(payload: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(payload)).hexdigest()


def write_atomic(path: str, payload: bytes, *, fsync: bool = True) -> None:
    """
    原子替换写入。`fsync` 连目录一起刷 —— 只刷文件时，rename 本身
    仍可能在断电后丢失，表现是"文件回到上一版"，与"写成功了"难以区分。
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            if fsync:
                os.fsync(fh.fileno())
        os.replace(tmp, path)
        if fsync:
            dfd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_envelope(path: str, *, kind: Optional[str] = None,
                  schema: Optional[int] = None) -> Tuple[Dict[str, Any], str]:
    """
    读一个信封文件并校验 → `(body, saved_at)`。任何问题抛异常。

    `kind` / `schema` 传 None 表示不校验（给"还不知道是什么"的诊断用）。
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise StateCorrupt(f"读不到 {path}：{exc}") from exc
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateCorrupt(f"{path} 不是合法 JSON（可能写了一半）：{exc}") from exc
    if not isinstance(envelope, dict):
        raise StateCorrupt(f"{path} 顶层不是对象")
    if kind is not None and envelope.get("kind") != kind:
        raise StateCorrupt(f"{path} 类型不符（kind={envelope.get('kind')!r}，期望 {kind}）")
    declared = envelope.get("schema")
    if schema is not None and declared != schema:
        raise StateSchemaMismatch(
            f"{path} 版本 {declared!r} 与代码 {schema} 不兼容；拒绝猜测式迁移")
    body = envelope.get("body")
    if not isinstance(body, dict):
        raise StateCorrupt(f"{path} 缺少 body")
    want = envelope.get("checksum")
    got = digest_of(body)
    if want != got:
        raise StateCorrupt(f"{path} 校验和不符（期望 {want}，实际 {got}）")
    return body, str(envelope.get("saved_at", ""))


# --------------------------------------------------------------------------- #
#  加载结果
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class StateLoad:
    """
    读取交代。`status` 每一档对应一种运维动作：

      absent   —— 文件不存在（首次启动 / 从没写过）
      ok       —— 正常
      corrupt  —— 内容不可信（停，别拿空状态顶上）
      schema   —— 版本不兼容（停）
    """

    status: str
    path: str
    body: Optional[Dict[str, Any]] = None
    saved_at: str = ""
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.body is not None

    @property
    def needs_attention(self) -> bool:
        return self.status not in ("ok", "absent")

    def to_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "path": self.path, "saved_at": self.saved_at,
                "detail": self.detail, "usable": self.usable,
                "needs_attention": self.needs_attention}


class JsonStateStore:
    """一个文件一个状态对象。带 kind/schema/checksum 信封与原子写。"""

    KIND = "quantos.state"
    SCHEMA = 1

    def __init__(self, path: str, *, fsync: bool = True,
                 keep_backup: bool = False) -> None:
        self.path = str(path)
        self.bak_path = self.path + ".bak"
        self.fsync = fsync
        self.keep_backup = keep_backup

    # -- 写 -------------------------------------------------------------- #

    def save(self, body: Mapping[str, Any]) -> Dict[str, Any]:
        payload = {
            "kind": self.KIND, "schema": self.SCHEMA,
            "saved_at": utcnow().isoformat(),
            "checksum": digest_of(dict(body)),
            "body": dict(body),
        }
        raw = canonical_json(payload)
        if self.keep_backup and os.path.exists(self.path):
            try:
                with open(self.path, "rb") as fh:
                    previous = fh.read()
                write_atomic(self.bak_path, previous, fsync=self.fsync)
            except OSError:
                pass
        write_atomic(self.path, raw, fsync=self.fsync)
        return {"path": self.path, "bytes": len(raw), "saved_at": payload["saved_at"],
                "checksum": payload["checksum"]}

    # -- 读 -------------------------------------------------------------- #

    def load(self) -> StateLoad:
        if not os.path.exists(self.path):
            return StateLoad(status="absent", path=self.path, detail="还没有状态文件")
        try:
            body, saved_at = read_envelope(self.path, kind=self.KIND,
                                           schema=self.SCHEMA)
            return StateLoad(status="ok", path=self.path, body=body, saved_at=saved_at)
        except StateSchemaMismatch as exc:
            return StateLoad(status="schema", path=self.path, detail=str(exc))
        except StateCorrupt as exc:
            error = str(exc)

        if self.keep_backup and os.path.exists(self.bak_path):
            try:
                body, saved_at = read_envelope(self.bak_path, kind=self.KIND,
                                               schema=self.SCHEMA)
                return StateLoad(status="recovered_from_backup", path=self.path,
                                 body=body, saved_at=saved_at,
                                 detail=f"主文件不可用（{error}），已回退到 .bak")
            except (StateCorrupt, StateSchemaMismatch) as exc:
                return StateLoad(status="corrupt", path=self.path,
                                 detail=f"主文件不可用（{error}）；备份也不可用（{exc}）")
        return StateLoad(status="corrupt", path=self.path,
                         detail=f"主文件不可用：{error}")

    # -- 运维动作 -------------------------------------------------------- #

    def archive(self, tag: str) -> Optional[str]:
        """改名存档（重置前必做）。返回新路径或 None。"""
        if not os.path.exists(self.path):
            return None
        target = f"{self.path}.{tag}"
        os.replace(self.path, target)
        return target


class RiskStateStore(JsonStateStore):
    """
    风控状态落盘。

    为什么单独存：`drawdown_pct` / `halted_reasons` / 策略运行态是**熔断记忆**。
    进程重启就归零，等于"熔断器每次重启都自动复位"——亏 15% 触发 HALTED，
    重启后又是 NORMAL，可以继续下单。这不是缓存，是安全状态。
    """

    KIND = "quantos.risk_state"
    SCHEMA = 1

    def save_state(self, state) -> Dict[str, Any]:
        return self.save(state.to_dict())

    def load_state(self) -> Tuple[Optional[Any], StateLoad]:
        """返回 `(RiskState 或 None, 加载交代)`。损坏时**不**给默认状态。"""
        from quantos_core.risk_engine import RiskState

        result = self.load()
        if result.body is None:
            return None, result
        return state_from_dict(result.body, RiskState), result


def state_from_dict(raw: Mapping[str, Any], state_cls) -> Any:
    """把 `RiskState.to_dict()` 的结构读回对象。字段缺失按默认值，类型不对就抛。"""
    from quantos_core.contracts import PermissionLevel, StrategyRunState

    state = state_cls()
    level = str(raw.get("permission_level", "NORMAL"))
    state.permission_level = PermissionLevel(level)
    state.halted_reasons = [str(x) for x in (raw.get("halted_reasons") or [])]
    state.strategy_status = {
        str(k): StrategyRunState(str(v))
        for k, v in (raw.get("strategy_status") or {}).items()
    }
    state.drawdown_pct = _decimal(raw.get("drawdown_pct", 0), "drawdown_pct")
    state.daily_pnl_pct = _decimal(raw.get("daily_pnl_pct", 0), "daily_pnl_pct")
    state.reconciliation_broken_accounts = set(
        raw.get("reconciliation_broken_accounts") or [])
    state.symbols_with_unknown_orders = set(
        raw.get("symbols_with_unknown_orders") or [])
    return state


def _decimal(raw: Any, where: str):
    from decimal import Decimal, InvalidOperation

    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise StateCorrupt(f"字段 {where} 不是合法数字：{raw!r}") from exc
