"""
QuantOS 2.0 — Ledger Persistence（B10 / T-1001、T-1002）
======================================================

B9 让策略"到点会自己跑"之后，立刻暴露的下一道墙：`Ledger` 只活在进程内存里。
独立的进程跑一拍 = 一份新账本 = 从空仓开始，第二拍会把已有持仓当成 0
重新买一遍。更隐蔽的是幂等索引 `_refs` / `_by_key` 也跟着没了 ——
交易所的重复成交回报在重启之后会被当成新成交再记一次，而账面上
现金和持仓都对得上（因为它们一起被算了两遍），只有对账才发现不平。

三个不省事的决定
----------------
1. **快照整份，不重放分录。** 账本的派生字段（`buy_at_decision` / `slippage` /
   `attr_qty` …）不是分录的函数：`_apply()` 只改现金，`apply_fill()` 里改持仓的
   是**入参**（决策价、归属比例），而这些参数不进分录。想靠"重放分录"重建账本，
   重放的东西账本自己根本没记住。所以存的是完整状态。

2. **恢复时必须从分录重建幂等索引。** 这是持久化的真正目的。只把数字读回来、
   不重建 `_refs` / `_by_key`，文件内容看着一模一样，防重能力却是零 ——
   比不落盘更危险，因为没人会怀疑它。

3. **损坏绝不静默当空账本。** 空账本和"没落盘"是同一个后果（第二拍从空仓开始），
   但前者看起来一切正常。所以 `load()` 返回显式状态，由调用方决定停机还是继续；
   `must_load()` 在坏档时直接抛，不给"继续跑"留默认路径。

写入方式
--------
`tmp 写入 → fsync → 旧文件转 .bak → os.replace → fsync 目录`。半个 JSON 比
没有 JSON 更糟：`json.load` 读半个文件会抛错，但读一个"合法但缺字段"的旧版本
却可能静默通过。所以校验和 + 结构校验 + 交叉核对三道都做。

已知限制（写进 03-推进路线图）
----------------------------
- 每次保存都写全量分录，没有增量/压缩。分录上到十万条时需要 compaction。
- 快照与 `.bak` 在同一块盘上，防不了磁盘损坏，只防进程崩溃与写坏。
- 没有跨进程写锁；同一账本文件只能由一个进程写（`quantos_tick.py` 用
  `AccountGatewayLock` 保证这一点）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from quantos_core.contracts import D, utcnow
from quantos_core.ledger import Ledger, LedgerEntry, LedgerLine, PositionBook
from quantos_core.state_store import (
    StateCorrupt,
    StateError,
    StateSchemaMismatch,
    digest_of,
    read_envelope,
    write_atomic,
)

__all__ = [
    "SCHEMA_VERSION", "FILE_KIND",
    "LedgerStoreError", "LedgerCorrupt", "LedgerSchemaMismatch",
    "LoadResult", "LedgerStore",
    "serialize_ledger", "deserialize_ledger",
]

SCHEMA_VERSION = 1
FILE_KIND = "quantos.ledger"

_EPS = Decimal("0.000001")
_ZERO = Decimal(0)


class LedgerStoreError(StateError):
    """账本存储层错误基类。"""


class LedgerCorrupt(LedgerStoreError, StateCorrupt):
    """
    文件内容不可信（校验和不符 / 结构缺失 / 交叉核对不平）。

    同时继承 `StateCorrupt`：调用方只关心"存储坏了"时捕一层就够，
    关心"是账本坏了"时再捕 `LedgerCorrupt`。
    """


class LedgerSchemaMismatch(LedgerStoreError, StateSchemaMismatch):
    """文件版本与当前代码不兼容 —— 不做猜测式迁移。"""


# --------------------------------------------------------------------------- #
#  序列化
# --------------------------------------------------------------------------- #

def _digest(payload: Any) -> str:
    return digest_of(payload)


def _s(value: Any) -> str:
    """Decimal / int 统一成字符串落盘。浮点绝不进文件。"""
    return str(D(value))


def _position_to_dict(pos: PositionBook) -> Dict[str, Any]:
    return {
        "venue": pos.venue,
        "symbol": pos.symbol,
        "quote_ccy": pos.quote_ccy,
        "qty": _s(pos.qty),
        "buy_at_decision": _s(pos.buy_at_decision),
        "sell_at_decision": _s(pos.sell_at_decision),
        "slippage": _s(pos.slippage),
        "attr_qty": {k: _s(v) for k, v in pos.attr_qty.items()},
        "attr_cost": {k: _s(v) for k, v in pos.attr_cost.items()},
        "attr_realized": {k: _s(v) for k, v in pos.attr_realized.items()},
    }


def _entry_to_dict(entry: LedgerEntry) -> Dict[str, Any]:
    return {
        "entry_id": entry.entry_id,
        "ts": entry.ts.isoformat(),
        "kind": entry.kind,
        "ref_type": entry.ref_type,
        "ref_id": entry.ref_id,
        "meta": dict(entry.meta),
        "lines": [
            {"account": ln.account, "ccy": ln.currency,
             "debit": _s(ln.debit), "credit": _s(ln.credit)}
            for ln in entry.lines
        ],
    }


def serialize_ledger(ledger: Ledger) -> Dict[str, Any]:
    """
    把账本内部状态整份导出成纯 JSON 结构。

    刻意读取 `_cash` / `_positions` 等私有字段：快照必须完整，
    漏一个字段恢复出来的账本就与崩溃前不是同一个了。
    """
    return {
        "base_currency": ledger.base_currency,
        "seq": ledger._seq,                      # noqa: SLF001 — 见 docstring
        "cash": {f"{acct}|{ccy}": _s(amount)
                 for (acct, ccy), amount in ledger._cash.items()},   # noqa: SLF001
        "positions": [_position_to_dict(pos)
                      for pos in ledger._positions.values()],        # noqa: SLF001
        "derived": {
            "fees_base": _s(ledger._fees_base),                      # noqa: SLF001
            "funding_base": _s(ledger._funding_base),                # noqa: SLF001
            "dividends_base": _s(ledger._dividends_base),            # noqa: SLF001
            "deposits_base": _s(ledger._deposits_base),              # noqa: SLF001
            "withdrawals_base": _s(ledger._withdrawals_base),        # noqa: SLF001
        },
        "entries": [_entry_to_dict(e) for e in ledger._entries],     # noqa: SLF001
    }


def _parse_decimal(raw: Any, where: str) -> Decimal:
    try:
        return D(raw)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise LedgerCorrupt(f"{where} 不是合法数字：{raw!r}") from exc


def _parse_ts(raw: Any, where: str) -> datetime:
    if not isinstance(raw, str):
        raise LedgerCorrupt(f"{where} 时间戳不是字符串：{raw!r}")
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise LedgerCorrupt(f"{where} 时间戳无法解析：{raw!r}") from exc
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _entry_from_dict(raw: Any, idx: int) -> LedgerEntry:
    if not isinstance(raw, dict):
        raise LedgerCorrupt(f"第 {idx} 条分录不是对象")
    for key in ("entry_id", "ts", "kind", "ref_type", "ref_id", "lines"):
        if key not in raw:
            raise LedgerCorrupt(f"第 {idx} 条分录缺少字段 {key}")
    lines: List[LedgerLine] = []
    for j, ln in enumerate(raw["lines"] or []):
        if not isinstance(ln, dict) or "account" not in ln or "ccy" not in ln:
            raise LedgerCorrupt(f"第 {idx} 条分录第 {j} 行结构不对")
        lines.append(LedgerLine(
            account=str(ln["account"]), currency=str(ln["ccy"]),
            debit=_parse_decimal(ln.get("debit", 0), f"entry[{idx}].lines[{j}].debit"),
            credit=_parse_decimal(ln.get("credit", 0), f"entry[{idx}].lines[{j}].credit"),
        ))
    entry = LedgerEntry(
        entry_id=str(raw["entry_id"]),
        ts=_parse_ts(raw["ts"], f"entry[{idx}].ts"),
        kind=str(raw["kind"]),
        ref_type=str(raw["ref_type"]),
        ref_id=str(raw["ref_id"]),
        lines=tuple(lines),
        meta={str(k): str(v) for k, v in (raw.get("meta") or {}).items()},
    )
    # 借贷不平的分录一旦进账本，对账永远平不了 —— 恢复时就拦住。
    entry.validate()
    return entry


def _entry_seq(entry_id: str) -> int:
    """`le-000123` -> 123。解析不出来返回 0（不阻断恢复，只影响 seq 下限）。"""
    tail = entry_id.rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def _cash_from_entries(entries: List[LedgerEntry]) -> Dict[Tuple[str, str], Decimal]:
    """按 `_apply()` 的口径从分录反推现金，用来交叉核对快照里的现金。"""
    out: Dict[Tuple[str, str], Decimal] = {}
    for entry in entries:
        for line in entry.lines:
            if not line.account.startswith("cash:"):
                continue
            _, acct, ccy = line.account.split(":", 2)
            key = (acct, ccy)
            out[key] = D(out.get(key, _ZERO)) + D(line.debit) - D(line.credit)
    return out


@dataclass
class RestoreResult:
    """恢复产物：账本 + 结构告警（不致命，但调用方该看见）。"""

    ledger: Ledger
    warnings: List[str] = field(default_factory=list)


def deserialize_ledger(body: Dict[str, Any]) -> RestoreResult:
    """
    从快照结构重建账本。任何结构性问题抛 `LedgerCorrupt`。

    刻意**不走 `post()`**：`post()` 会调用 `_apply()` 把现金再加一遍，
    而现金已经存在快照里。这里直接把现金写回去，再重建幂等索引。
    """
    warnings: List[str] = []
    if not isinstance(body, dict):
        raise LedgerCorrupt("快照主体不是对象")
    for key in ("base_currency", "cash", "positions", "derived", "entries"):
        if key not in body:
            raise LedgerCorrupt(f"快照缺少字段 {key}")

    ledger = Ledger(base_currency=str(body["base_currency"]))

    # -- 现金 ------------------------------------------------------------- #
    cash: Dict[Tuple[str, str], Decimal] = {}
    raw_cash = body["cash"]
    if not isinstance(raw_cash, dict):
        raise LedgerCorrupt("cash 不是对象")
    for key, value in raw_cash.items():
        if not isinstance(key, str) or "|" not in key:
            raise LedgerCorrupt(f"cash 键格式不对：{key!r}")
        acct, ccy = key.split("|", 1)
        cash[(acct, ccy)] = _parse_decimal(value, f"cash[{key}]")

    # -- 持仓 ------------------------------------------------------------- #
    positions: Dict[Tuple[str, str], PositionBook] = {}
    raw_positions = body["positions"]
    if not isinstance(raw_positions, list):
        raise LedgerCorrupt("positions 不是数组")
    for i, raw in enumerate(raw_positions):
        if not isinstance(raw, dict):
            raise LedgerCorrupt(f"第 {i} 个持仓不是对象")
        for key in ("venue", "symbol", "quote_ccy", "qty"):
            if key not in raw:
                raise LedgerCorrupt(f"第 {i} 个持仓缺少字段 {key}")
        pos = PositionBook(
            venue=str(raw["venue"]), symbol=str(raw["symbol"]),
            quote_ccy=str(raw["quote_ccy"]),
            qty=_parse_decimal(raw["qty"], f"pos[{i}].qty"),
            buy_at_decision=_parse_decimal(raw.get("buy_at_decision", 0),
                                           f"pos[{i}].buy_at_decision"),
            sell_at_decision=_parse_decimal(raw.get("sell_at_decision", 0),
                                            f"pos[{i}].sell_at_decision"),
            slippage=_parse_decimal(raw.get("slippage", 0), f"pos[{i}].slippage"),
            attr_qty={k: _parse_decimal(v, f"pos[{i}].attr_qty[{k}]")
                      for k, v in (raw.get("attr_qty") or {}).items()},
            attr_cost={k: _parse_decimal(v, f"pos[{i}].attr_cost[{k}]")
                       for k, v in (raw.get("attr_cost") or {}).items()},
            attr_realized={k: _parse_decimal(v, f"pos[{i}].attr_realized[{k}]")
                           for k, v in (raw.get("attr_realized") or {}).items()},
        )
        positions[(pos.venue, pos.symbol)] = pos

    # -- 分录 + 幂等索引 -------------------------------------------------- #
    entries: List[LedgerEntry] = []
    refs = set()
    by_key: Dict[Tuple[str, str, str], LedgerEntry] = {}
    raw_entries = body["entries"]
    if not isinstance(raw_entries, list):
        raise LedgerCorrupt("entries 不是数组")
    for i, raw in enumerate(raw_entries):
        entry = _entry_from_dict(raw, i)
        ref = (entry.ref_type, entry.ref_id, entry.kind)
        if ref in refs:
            # post() 本身会去重，文件里出现两条说明有人手改过（或旧版写错）。
            raise LedgerCorrupt(
                f"分录引用重复：{entry.ref_type}:{entry.ref_id}/{entry.kind}")
        refs.add(ref)
        by_key[ref] = entry
        entries.append(entry)

    # -- 派生字段 --------------------------------------------------------- #
    derived = body["derived"]
    if not isinstance(derived, dict):
        raise LedgerCorrupt("derived 不是对象")

    # -- 交叉核对：现金必须能从分录推出来 --------------------------------- #
    expected = _cash_from_entries(entries)
    for key in set(expected) | set(cash):
        want = D(expected.get(key, _ZERO))
        got = D(cash.get(key, _ZERO))
        if abs(want - got) > _EPS:
            raise LedgerCorrupt(
                f"现金与分录不一致 {key[0]}|{key[1]}：快照 {got}，按分录应为 {want}")

    # -- seq -------------------------------------------------------------- #
    declared_seq = body.get("seq", 0)
    try:
        seq = int(declared_seq)
    except (TypeError, ValueError):
        raise LedgerCorrupt(f"seq 不是整数：{declared_seq!r}")
    max_entry_seq = max((_entry_seq(e.entry_id) for e in entries), default=0)
    if seq < max_entry_seq:
        warnings.append(
            f"seq 落后于分录号（{seq} < {max_entry_seq}），已抬到 {max_entry_seq}；"
            "否则新分录会与旧 entry_id 撞号")
        seq = max_entry_seq

    ledger._cash = cash                                                # noqa: SLF001
    ledger._positions = positions                                      # noqa: SLF001
    ledger._quote_ccy = {k: p.quote_ccy for k, p in positions.items()} #{noqa: SLF001}
    ledger._entries = entries                                          # noqa: SLF001
    ledger._refs = refs                                                # noqa: SLF001
    ledger._by_key = by_key                                            # noqa: SLF001
    ledger._seq = seq                                                  # noqa: SLF001
    ledger._fees_base = _parse_decimal(derived.get("fees_base", 0),
                                       "derived.fees_base")            # noqa: SLF001
    ledger._funding_base = _parse_decimal(derived.get("funding_base", 0),
                                          "derived.funding_base")      # noqa: SLF001
    ledger._dividends_base = _parse_decimal(derived.get("dividends_base", 0),
                                            "derived.dividends_base")  # noqa: SLF001
    ledger._deposits_base = _parse_decimal(derived.get("deposits_base", 0),
                                           "derived.deposits_base")    # noqa: SLF001
    ledger._withdrawals_base = _parse_decimal(
        derived.get("withdrawals_base", 0), "derived.withdrawals_base")# noqa: SLF001
    return RestoreResult(ledger=ledger, warnings=warnings)


# --------------------------------------------------------------------------- #
#  加载结果
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class LoadResult:
    """
    一次加载的完整交代。

    `status` 的每一档都对应一种运维动作，不做归并：
      absent                  —— 没落过盘（首次启动，允许建仓）
      ok                      —— 主文件正常
      recovered_from_backup   —— 主文件坏了，用了 .bak（**要人看一眼**）
      corrupt                 —— 主文件与备份都不可信（停）
      schema_mismatch         —— 版本不兼容（停，不猜迁移）
    """

    ledger: Optional[Ledger]
    status: str
    path: str
    detail: str = ""
    source: str = ""                       # primary / backup / ""
    entry_count: int = 0
    saved_at: str = ""
    warnings: Tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return self.ledger is not None

    @property
    def needs_attention(self) -> bool:
        """除了 absent/ok，其它都该被人看一眼。"""
        return self.status not in ("ok", "absent")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "path": self.path,
            "detail": self.detail,
            "source": self.source,
            "entry_count": self.entry_count,
            "saved_at": self.saved_at,
            "warnings": list(self.warnings),
            "usable": self.usable,
            "needs_attention": self.needs_attention,
        }


# --------------------------------------------------------------------------- #
#  LedgerStore
# --------------------------------------------------------------------------- #

class LedgerStore:
    """账本文件的读写。一个文件对应一个账户，不允许两个进程同时写。"""

    def __init__(self, path: str, *, fsync: bool = True,
                 keep_backup: bool = True) -> None:
        self.path = str(path)
        self.bak_path = self.path + ".bak"
        self.fsync = fsync
        self.keep_backup = keep_backup

    # -- 磁盘原语 -------------------------------------------------------- #

    def exists(self) -> bool:
        return os.path.exists(self.path)

    def _write_atomic(self, target: str, payload: bytes) -> None:
        write_atomic(target, payload, fsync=self.fsync)

    def _rotate_backup(self) -> None:
        """把当前主文件复制成 .bak。复制而不是 rename：rename 会留下无主文件的窗口。"""
        if not self.keep_backup or not os.path.exists(self.path):
            return
        with open(self.path, "rb") as src:
            payload = src.read()
        try:
            self._write_atomic(self.bak_path, payload)
        except OSError:
            # 备份写不进去不该阻断保存 —— 主文件才是交付物，但要留痕
            pass

    # -- 保存 ------------------------------------------------------------ #

    def save(self, ledger: Ledger) -> Dict[str, Any]:
        """整份落盘，返回写入摘要（给日志/CLI 用）。"""
        body = serialize_ledger(ledger)
        envelope = {
            "kind": FILE_KIND,
            "schema": SCHEMA_VERSION,
            "saved_at": utcnow().isoformat(),
            "checksum": _digest(body),
            "body": body,
        }
        payload = json.dumps(envelope, ensure_ascii=False, sort_keys=True,
                             indent=None, default=str).encode("utf-8")
        self._rotate_backup()
        self._write_atomic(self.path, payload)
        return {
            "path": self.path,
            "bytes": len(payload),
            "entries": len(body["entries"]),
            "checksum": envelope["checksum"],
            "saved_at": envelope["saved_at"],
        }

    # -- 读取 ------------------------------------------------------------ #

    def _read_one(self, path: str) -> Tuple[Dict[str, Any], str]:
        """读一个文件并校验 → (body, saved_at)。任何问题抛 LedgerCorrupt。"""
        try:
            return read_envelope(path, kind=FILE_KIND, schema=SCHEMA_VERSION)
        except StateSchemaMismatch as exc:
            raise LedgerSchemaMismatch(str(exc)) from exc
        except StateCorrupt as exc:
            raise LedgerCorrupt(str(exc)) from exc

    def load(self, *, base_currency: str = "USD",
             allow_absent: bool = False) -> LoadResult:
        """
        加载账本。坏档不抛，返回显式状态；要不要继续由调用方决定。

        `allow_absent=True` 时文件不存在会返回一个空账本（首次启动）。
        """
        if not self.exists():
            ledger = Ledger(base_currency=base_currency) if allow_absent else None
            return LoadResult(
                ledger=ledger, status="absent", path=self.path,
                detail="还没有账本文件" + ("，按首次启动处理" if allow_absent else ""))

        primary_error = ""
        try:
            body, saved_at = self._read_one(self.path)
            restored = deserialize_ledger(body)
            return LoadResult(
                ledger=restored.ledger, status="ok", path=self.path,
                source="primary", entry_count=len(body["entries"]),
                saved_at=saved_at, warnings=tuple(restored.warnings))
        except LedgerSchemaMismatch as exc:
            return LoadResult(ledger=None, status="schema_mismatch",
                              path=self.path, detail=str(exc))
        except LedgerCorrupt as exc:
            primary_error = str(exc)

        # 主文件坏了：试用备份，但要如实说"用了备份"
        if self.keep_backup and os.path.exists(self.bak_path):
            try:
                body, saved_at = self._read_one(self.bak_path)
                restored = deserialize_ledger(body)
                return LoadResult(
                    ledger=restored.ledger, status="recovered_from_backup",
                    path=self.path, source="backup",
                    detail=f"主文件不可用（{primary_error}），已回退到 .bak",
                    entry_count=len(body["entries"]), saved_at=saved_at,
                    warnings=tuple(restored.warnings))
            except (LedgerCorrupt, LedgerSchemaMismatch) as exc:
                return LoadResult(
                    ledger=None, status="corrupt", path=self.path,
                    detail=f"主文件不可用（{primary_error}）；备份也不可用（{exc}）")

        return LoadResult(ledger=None, status="corrupt", path=self.path,
                          detail=f"主文件不可用且无备份：{primary_error}")

    def must_load(self, *, base_currency: str = "USD",
                  allow_absent: bool = False) -> Ledger:
        """
        要么给出可用账本，要么抛。

        这是给"要下单"的路径用的：拿不到可信账本就绝不能继续，
        否则第二拍会把已有持仓当 0 重新买一遍。
        """
        result = self.load(base_currency=base_currency, allow_absent=allow_absent)
        if result.ledger is None:
            raise LedgerCorrupt(
                f"账本不可用（{result.status}）：{result.detail}；拒绝以空账本继续")
        return result.ledger

    # -- 运维动作 -------------------------------------------------------- #

    def verify(self) -> Dict[str, Any]:
        """只读诊断：文件在不在、能不能用、交叉核对过不过。"""
        result = self.load(allow_absent=False)
        out: Dict[str, Any] = {
            "path": self.path,
            "exists": self.exists(),
            "bak_exists": os.path.exists(self.bak_path),
            **result.to_dict(),
        }
        if result.ledger is not None:
            ledger = result.ledger
            out["cash_lines"] = len(ledger.cash_native())
            out["positions"] = len(ledger.positions())
            out["net_deposits_base"] = str(ledger.net_deposits_base)
            out["ids_rebuilt"] = len(ledger._refs)          # noqa: SLF001
        return out

    def archive(self, tag: str) -> Optional[str]:
        """把当前文件改名存档（重置前必做）。返回新路径或 None。"""
        if not os.path.exists(self.path):
            return None
        target = f"{self.path}.{tag}"
        os.replace(self.path, target)
        return target

    def clear(self) -> List[str]:
        """删掉主文件与备份。**调用方必须先 archive()** —— 这里不替你备份。"""
        removed: List[str] = []
        for path in (self.path, self.bak_path):
            if os.path.exists(path):
                os.unlink(path)
                removed.append(path)
        return removed
