"""
QuantOS 2.0 — 事件存储（T-101）
===============================

对应需求 R-S06（回测与实盘共用同一套事件）、R-A03（事件驱动）、R-O02（可恢复）。

设计要点
--------
1. **append-only**：事件只追加，不修改、不删除。任何"修正"都表现为新事件。
2. **单调 seq**：每个 store 内 seq 从 1 连续递增，重放顺序即事故发生顺序。
3. **强持久化**：写一行 → flush → fsync 才返回。断电后最后一行可能残缺，
   加载时**只截掉尾部残缺行**（torn write），中间任何损坏都视为真损坏并抛异常
   —— 静默跳过中间事件比崩溃更危险。
4. **幂等追加**：调用方传 event_id（例如 `fill:{venue_trade_id}`）时，重复追加
   不会产生第二条事件，返回 `created=False`。这是"重复回报不重复记账"的第一道闸。
5. **可切换后端**：JSONL（默认、免运维）与 SQLite（需要随机查询时）。接口一致，
   换后端不改上层代码。

关于 Decimal
------------
金额一律 Decimal，序列化时打标签 `{"__t__": "dec", "v": "1.23"}`，
反序列化还原成 Decimal。绝不允许 float 落盘后回读变成 1.2299999。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

UTC = timezone.utc
SCHEMA_VERSION = "1.0"

_DEC_TAG = "dec"
_DT_TAG = "dt"
_ENUM_TAG = "enum"


# --------------------------------------------------------------------------- #
#  序列化
# --------------------------------------------------------------------------- #

def to_jsonable(obj: Any) -> Any:
    """
    把领域对象转成可 JSON 化的结构（Decimal/dateTime/Enum 打标签）。

    key 必须是 str —— 元组 key 请在上层转成 "venue|symbol" 之类的字符串，
    否则回读时无法还原、且两套编码可能撞车。
    """
    if isinstance(obj, Decimal):
        return {"__t__": _DEC_TAG, "v": format(obj, "f")}
    if isinstance(obj, datetime):
        if obj.tzinfo is None:
            raise ValueError("拒绝序列化 naive datetime（R-A03 要求带时区）")
        return {"__t__": _DT_TAG, "v": obj.astimezone(UTC).isoformat()}
    if isinstance(obj, Enum):
        return {"__t__": _ENUM_TAG, "v": obj.value}
    if isinstance(obj, Mapping):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            if not isinstance(k, str):
                raise TypeError(f"事件 payload 的 key 必须是 str，收到 {type(k).__name__}:{k!r}")
            out[k] = to_jsonable(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    raise TypeError(f"不支持序列化的类型 {type(obj).__name__}")


def from_jsonable(obj: Any) -> Any:
    """还原 to_jsonable 打过的标签。Enum 还原为其 value（上层按需转枚举）。"""
    if isinstance(obj, Mapping):
        if obj.get("__t__") == _DEC_TAG:
            try:
                return Decimal(obj["v"])
            except (InvalidOperation, KeyError) as exc:
                raise ValueError(f"Decimal 反序列化失败: {obj!r}") from exc
        if obj.get("__t__") == _DT_TAG:
            return datetime.fromisoformat(obj["v"]).astimezone(UTC)
        if obj.get("__t__") == _ENUM_TAG:
            return obj["v"]
        return {k: from_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [from_jsonable(v) for v in obj]
    return obj


def canonical_json(obj: Any) -> str:
    """稳定序列化：key 排序、无多余空格。digest 与落盘都用它。"""
    return json.dumps(to_jsonable(obj), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


# --------------------------------------------------------------------------- #
#  事件封装
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class EventEnvelope:
    """事件信封。as_of 是事实发生时间，ts 是系统记录时间，两者不可混用。"""

    seq: int
    event_id: str
    event_type: str
    ts: datetime
    as_of: datetime
    payload: Mapping[str, Any]
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self):
        if self.seq <= 0:
            raise ValueError(f"seq 必须为正，收到 {self.seq}")
        for name in ("ts", "as_of"):
            dt = getattr(self, name)
            if not isinstance(dt, datetime) or dt.tzinfo is None:
                raise ValueError(f"EventEnvelope.{name} 必须是带时区的 datetime")

    def to_record(self) -> Dict[str, Any]:
        return {
            "seq": self.seq,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "ts": self.ts.astimezone(UTC).isoformat(),
            "as_of": self.as_of.astimezone(UTC).isoformat(),
            "schema_version": self.schema_version,
            "payload": to_jsonable(self.payload),
        }

    @staticmethod
    def from_record(rec: Mapping[str, Any]) -> "EventEnvelope":
        try:
            return EventEnvelope(
                seq=int(rec["seq"]),
                event_id=str(rec["event_id"]),
                event_type=str(rec["event_type"]),
                ts=datetime.fromisoformat(rec["ts"]).astimezone(UTC),
                as_of=datetime.fromisoformat(rec["as_of"]).astimezone(UTC),
                payload=from_jsonable(rec.get("payload") or {}),
                schema_version=str(rec.get("schema_version", SCHEMA_VERSION)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise EventStoreCorrupt(f"事件记录字段非法: {rec!r}") from exc


class EventStoreError(RuntimeError):
    """事件存储错误基类。"""


class EventStoreCorrupt(EventStoreError):
    """事件内容损坏 —— 必须人工介入，禁止静默跳过。"""


class EventStoreFull(EventStoreError):
    """写入失败（磁盘满/只读/权限）。调用方必须当作『系统不可用』处理。"""


# --------------------------------------------------------------------------- #
#  存储接口
# --------------------------------------------------------------------------- #

class EventStore:
    """事件存储抽象。上层只依赖这五个方法。"""

    stream: str = "default"

    def append(self, event_type: str, payload: Mapping[str, Any], *,
               as_of: Optional[datetime] = None, ts: Optional[datetime] = None,
               event_id: Optional[str] = None) -> Tuple[EventEnvelope, bool]:
        """追加事件，返回 (envelope, created)。event_id 重复时 created=False。"""
        raise NotImplementedError

    def read(self, from_seq: int = 1, to_seq: Optional[int] = None,
             event_types: Optional[Sequence[str]] = None) -> Iterator[EventEnvelope]:
        raise NotImplementedError

    @property
    def last_seq(self) -> int:
        raise NotImplementedError

    @property
    def count(self) -> int:
        raise NotImplementedError

    def digest(self, upto_seq: Optional[int] = None) -> str:
        """内容摘要。两次回放 digest 相同，说明读到的事件完全一致（T-101 DoD）。"""
        h = hashlib.sha256()
        for env in self.read(1, upto_seq):
            h.update(canonical_json(env.to_record()).encode("utf-8"))
            h.update(b"\n")
        return h.hexdigest()

    def close(self) -> None:
        pass

    # -- 内部工具 -------------------------------------------------------- #

    @staticmethod
    def _coerce_times(as_of: Optional[datetime], ts: Optional[datetime]
                      ) -> Tuple[datetime, datetime]:
        now = datetime.now(UTC)
        ts = ts or now
        as_of = as_of or ts
        for name, dt in (("ts", ts), ("as_of", as_of)):
            if not isinstance(dt, datetime):
                raise TypeError(f"{name} 必须是 datetime")
            if dt.tzinfo is None:
                raise ValueError(f"{name} 必须带时区")
        return ts.astimezone(UTC), as_of.astimezone(UTC)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False


# --------------------------------------------------------------------------- #
#  JSONL 后端
# --------------------------------------------------------------------------- #

class JsonlEventStore(EventStore):
    """
    一行一事件的追加日志。默认后端：无依赖、可 tail -f、可直接 diff。

    fsync 默认开启。高频场景可关掉换吞吐，但必须接受"断电丢最后几条"，
    对交易系统来说这个交换通常不划算，所以默认是 True。
    """

    def __init__(self, path: str, *, stream: str = "default",
                 fsync: bool = True, auto_repair_tail: bool = True):
        self.path = os.path.abspath(path)
        self.stream = stream
        self.fsync = fsync
        self.auto_repair_tail = auto_repair_tail
        self.repaired_bytes = 0
        self._lock = threading.RLock()
        self._ids: Dict[str, EventEnvelope] = {}
        self._last_seq = 0
        self._count = 0
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if not os.path.exists(self.path):
            open(self.path, "a", encoding="utf-8").close()
        self._load()

    # -- 加载与自愈 ------------------------------------------------------ #

    def _load(self) -> None:
        good_offset = 0
        seqs: List[int] = []
        with open(self.path, "rb") as fh:
            while True:
                start = fh.tell()
                raw = fh.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace")
                if not line.endswith("\n"):
                    # 尾部残缺行：断电/磁盘满导致写了一半。只允许截掉它。
                    if not self.auto_repair_tail:
                        raise EventStoreCorrupt(
                            f"{self.path} 尾部存在残缺事件（offset {start}），auto_repair_tail=False")
                    self.repaired_bytes = len(raw)
                    break
                text = line.strip()
                if not text:
                    good_offset = fh.tell()
                    continue
                try:
                    rec = json.loads(text)
                    env = EventEnvelope.from_record(rec)
                except (json.JSONDecodeError, EventStoreCorrupt) as exc:
                    raise EventStoreCorrupt(
                        f"{self.path} 第 {len(seqs) + 1} 行损坏（offset {start}）："
                        f"{text[:120]!r}") from exc
                if seqs and env.seq <= seqs[-1]:
                    raise EventStoreCorrupt(
                        f"{self.path} seq 非单调：{seqs[-1]} → {env.seq}")
                seqs.append(env.seq)
                self._ids[env.event_id] = env
                good_offset = fh.tell()

        if self.repaired_bytes:
            with open(self.path, "r+b") as fh:
                fh.truncate(good_offset)
                fh.flush()
                os.fsync(fh.fileno())

        self._last_seq = seqs[-1] if seqs else 0
        self._count = len(seqs)

    # -- 写入 ------------------------------------------------------------ #

    def append(self, event_type: str, payload: Mapping[str, Any], *,
               as_of: Optional[datetime] = None, ts: Optional[datetime] = None,
               event_id: Optional[str] = None) -> Tuple[EventEnvelope, bool]:
        if not event_type:
            raise ValueError("event_type 不可为空")
        ts_u, as_of_u = self._coerce_times(as_of, ts)
        with self._lock:
            if event_id is not None and event_id in self._ids:
                return self._ids[event_id], False

            seq = self._last_seq + 1
            env = EventEnvelope(
                seq=seq,
                event_id=event_id or f"{self.stream}:{seq:08d}",
                event_type=event_type, ts=ts_u, as_of=as_of_u,
                payload=dict(payload),
            )
            line = canonical_json(env.to_record()) + "\n"
            try:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line)
                    fh.flush()
                    if self.fsync:
                        os.fsync(fh.fileno())
            except OSError as exc:
                # 磁盘满/只读：抛专用异常，上层必须按"系统不可用"处理，不许当成功。
                raise EventStoreFull(f"事件写入失败 {self.path}: {exc}") from exc

            self._ids[env.event_id] = env
            self._last_seq = seq
            self._count += 1
            return env, True

    # -- 读取 ------------------------------------------------------------ #

    def read(self, from_seq: int = 1, to_seq: Optional[int] = None,
             event_types: Optional[Sequence[str]] = None) -> Iterator[EventEnvelope]:
        allowed = set(event_types) if event_types else None
        with self._lock:
            if from_seq > self._last_seq:
                return
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    text = line.strip()
                    if not text:
                        continue
                    env = EventEnvelope.from_record(json.loads(text))
                    if env.seq < from_seq:
                        continue
                    if to_seq is not None and env.seq > to_seq:
                        break
                    if allowed is not None and env.event_type not in allowed:
                        continue
                    yield env

    @property
    def last_seq(self) -> int:
        return self._last_seq

    @property
    def count(self) -> int:
        return self._count

    def replay_into(self, handler) -> int:
        """顺序喂给 handler(env)，返回处理条数。handler 抛异常即中断。"""
        n = 0
        for env in self.read():
            handler(env)
            n += 1
        return n


# --------------------------------------------------------------------------- #
#  SQLite 后端
# --------------------------------------------------------------------------- #

class SqliteEventStore(EventStore):
    """需要按 event_type / symbol 随机检索时用。语义与 JSONL 完全一致。"""

    def __init__(self, path: str, *, stream: str = "default"):
        self.path = os.path.abspath(path)
        self.stream = stream
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                seq           INTEGER PRIMARY KEY,
                event_id      TEXT NOT NULL UNIQUE,
                event_type    TEXT NOT NULL,
                ts            TEXT NOT NULL,
                as_of         TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                payload       TEXT NOT NULL
            )""")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type)")
        self._conn.commit()

    def append(self, event_type: str, payload: Mapping[str, Any], *,
               as_of: Optional[datetime] = None, ts: Optional[datetime] = None,
               event_id: Optional[str] = None) -> Tuple[EventEnvelope, bool]:
        if not event_type:
            raise ValueError("event_type 不可为空")
        ts_u, as_of_u = self._coerce_times(as_of, ts)
        with self._lock:
            if event_id is not None:
                row = self._conn.execute(
                    "SELECT seq,event_id,event_type,ts,as_of,schema_version,payload "
                    "FROM events WHERE event_id=?", (event_id,)).fetchone()
                if row:
                    return EventEnvelope.from_record(self._row_to_rec(row)), False

            seq = self.last_seq + 1
            env = EventEnvelope(
                seq=seq,
                event_id=event_id or f"{self.stream}:{seq:08d}",
                event_type=event_type, ts=ts_u, as_of=as_of_u,
                payload=dict(payload),
            )
            try:
                self._conn.execute(
                    "INSERT INTO events(seq,event_id,event_type,ts,as_of,schema_version,payload) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (env.seq, env.event_id, env.event_type,
                     env.ts.isoformat(), env.as_of.isoformat(),
                     env.schema_version, canonical_json(env.payload)))
                self._conn.commit()
            except sqlite3.OperationalError as exc:
                raise EventStoreFull(f"事件写入失败 {self.path}: {exc}") from exc
            return env, True

    def read(self, from_seq: int = 1, to_seq: Optional[int] = None,
             event_types: Optional[Sequence[str]] = None) -> Iterator[EventEnvelope]:
        sql = ("SELECT seq,event_id,event_type,ts,as_of,schema_version,payload "
               "FROM events WHERE seq >= ?")
        params: List[Any] = [int(from_seq)]
        if to_seq is not None:
            sql += " AND seq <= ?"
            params.append(int(to_seq))
        if event_types:
            sql += " AND event_type IN (" + ",".join("?" * len(event_types)) + ")"
            params.extend(event_types)
        sql += " ORDER BY seq"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        for row in rows:
            yield EventEnvelope.from_record(self._row_to_rec(row))

    @staticmethod
    def _row_to_rec(row) -> Dict[str, Any]:
        return {
            "seq": row[0], "event_id": row[1], "event_type": row[2],
            "ts": row[3], "as_of": row[4], "schema_version": row[5],
            "payload": json.loads(row[6]),
        }

    @property
    def last_seq(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT MAX(seq) FROM events").fetchone()
        return int(row[0] or 0)

    @property
    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM events").fetchone()
        return int(row[0])

    def close(self) -> None:
        self._conn.close()


# --------------------------------------------------------------------------- #
#  内存后端（测试/回放加速）
# --------------------------------------------------------------------------- #

class MemoryEventStore(EventStore):
    """进程内存储。测试与回放用，不落盘。"""

    def __init__(self, stream: str = "default"):
        self.stream = stream
        self._events: List[EventEnvelope] = []
        self._ids: Dict[str, EventEnvelope] = {}

    def append(self, event_type: str, payload: Mapping[str, Any], *,
               as_of: Optional[datetime] = None, ts: Optional[datetime] = None,
               event_id: Optional[str] = None) -> Tuple[EventEnvelope, bool]:
        if not event_type:
            raise ValueError("event_type 不可为空")
        ts_u, as_of_u = self._coerce_times(as_of, ts)
        if event_id is not None and event_id in self._ids:
            return self._ids[event_id], False
        env = EventEnvelope(
            seq=len(self._events) + 1,
            event_id=event_id or f"{self.stream}:{len(self._events) + 1:08d}",
            event_type=event_type, ts=ts_u, as_of=as_of_u, payload=dict(payload),
        )
        self._events.append(env)
        self._ids[env.event_id] = env
        return env, True

    def read(self, from_seq: int = 1, to_seq: Optional[int] = None,
             event_types: Optional[Sequence[str]] = None) -> Iterator[EventEnvelope]:
        allowed = set(event_types) if event_types else None
        for env in self._events:
            if env.seq < from_seq:
                continue
            if to_seq is not None and env.seq > to_seq:
                break
            if allowed is not None and env.event_type not in allowed:
                continue
            yield env

    @property
    def last_seq(self) -> int:
        return len(self._events)

    @property
    def count(self) -> int:
        return len(self._events)


# --------------------------------------------------------------------------- #
#  工厂
# --------------------------------------------------------------------------- #

def open_event_store(path: str, *, stream: str = "default",
                     backend: str = "jsonl", fsync: bool = True) -> EventStore:
    """按后缀/显式参数选择后端。`x.db`/`x.sqlite` 自动走 SQLite。"""
    lowered = path.lower()
    if backend == "sqlite" or lowered.endswith((".db", ".sqlite", ".sqlite3")):
        return SqliteEventStore(path, stream=stream)
    if backend == "memory":
        return MemoryEventStore(stream=stream)
    return JsonlEventStore(path, stream=stream, fsync=fsync)


__all__ = [
    "SCHEMA_VERSION", "to_jsonable", "from_jsonable", "canonical_json",
    "EventEnvelope", "EventStore", "EventStoreError", "EventStoreCorrupt",
    "EventStoreFull", "JsonlEventStore", "SqliteEventStore", "MemoryEventStore",
    "open_event_store",
]
