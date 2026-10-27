"""
QuantOS 2.0 — AI 研究通道（T-603）
=================================

要求原文：「AI 不能自己修改生产策略并直接上线实盘」。这个模块把这句话变成
**代码结构**，而不是一句写在文档里的纪律。

怎么做到"没有写生产的路径"：不是加一个权限判断，而是让这个模块**根本不认识**
生产侧的对象。它只 import `quantos_core.contracts`（数据契约），不 import
`strategy_registry` / `live_gate` / `oms` / `gateway`。没有 registry 引用，
就没有 `registry.promote()` 可调 —— 这比任何 `if actor.startswith("ai")` 都硬。
`assert_isolated()` 用 AST 扫描把这个约束变成可执行断言（见验收脚本）。

三道具体的闸：

1. **禁用键黑名单**。`payload` 里出现 `quota_pct` / `canary_weight` /
   `permission_level` / `armed` 这类字段直接拒，且**递归**检查嵌套结构。
   结论写进 `summary`（自由文本，永远进不了配置），参数写进 `payload`
   （结构化，受黑名单约束）。这个分工是刻意的：文本没法被误当成配置。

2. **候选参数必须指回证据**。`kind=CANDIDATE_PARAMS` 时 `evidence` 非空，
   且至少有一条长得像回测/回放标识（`bt:` / `replay:` 前缀）。没有可复现证据的
   参数建议，和"我觉得应该调到 0.3"没有区别。

3. **参数漂移上限**。给了 `baseline_params` 就逐键比对，相对变化超过
   `max_rel_drift` 直接拒。这条拦的是"AI 提议把仓位上限从 0.2 改到 2.0"
   这类量级错误 —— 它看起来是个正常数字，只是大了十倍。

出口只有一个：`ResearchDraft`。它是一份**数据**，必须由 `human:` 前缀的人
签名产出，然后由人在生产侧自行调用注册表上线。本模块不提供、也不会提供
`apply()` / `promote()` / `set_quota()` 之类的方法。
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from quantos_core.contracts import ContractViolation, D, q, utcnow

# --------------------------------------------------------------------------- #
#  常量：什么叫"生产控制面"
# --------------------------------------------------------------------------- #

#: 这些键一旦出现在研究输出里，就意味着 AI 想改执行控制而不是给建议。
#: 名单要短而准 —— 列太宽会把正常研究也拦掉，列太窄会漏掉真正危险的字段。
PRODUCTION_KEYS = frozenset({
    "stage", "run_state", "permission_level", "trading_mode",
    "quota_pct", "canary_weight", "max_gross_weight", "risk_budget_pct",
    "armed", "arm_weight", "live", "order_cap", "max_notional",
    "account_id", "broker", "api_key", "api_secret", "credentials",
})

#: 本模块允许 import 的模块前缀。`assert_isolated()` 按这个白名单检查。
ALLOWED_IMPORT_PREFIXES = (
    "__future__", "ast", "hashlib", "json", "os", "dataclasses", "datetime",
    "decimal", "enum", "pathlib", "typing", "quantos_core.contracts",
)

#: 模块里不允许出现的方法名。即使没人调用，名字本身也不该出现 ——
#: 出现了就说明有人正在把生产写入能力往研究通道里塞。
FORBIDDEN_METHODS = frozenset({
    "apply", "promote", "set_quota", "set_canary", "arm", "authorize",
    "register", "set_stage", "resume", "degrade", "write_config", "push_live",
})

#: 证据标识前缀：候选参数必须能指回一次可复现的回测或回放。
EVIDENCE_PREFIXES = ("bt:", "replay:", "report:")

MIN_SUMMARY_LEN = 20
MAX_REL_DRIFT = Decimal("0.5")


# --------------------------------------------------------------------------- #
#  错误
# --------------------------------------------------------------------------- #


class ResearchError(ContractViolation):
    """研究通道错误基类。"""


class ResearchRejected(ResearchError):
    """提交被拒。拒绝理由必须能让人照着改。"""


class ReviewRequired(ResearchError):
    """需要人工复核才能继续。"""


# --------------------------------------------------------------------------- #
#  类型
# --------------------------------------------------------------------------- #


class ResearchKind(str, Enum):
    REPORT = "report"                    # 研究报告（纯结论 + 证据）
    CANDIDATE_PARAMS = "candidate_params"  # 候选参数（必须有证据）
    HYPOTHESIS = "hypothesis"            # 待验证的假设
    CRITIQUE = "critique"                # 对现有策略的批评


class Verdict(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NEEDS_WORK = "needs_work"


def _is_human(actor: str) -> bool:
    return isinstance(actor, str) and actor.startswith("human:") and len(actor) > 6


def _scan_forbidden(obj: Any, path: str = "payload") -> List[str]:
    """
    递归找禁用键。**递归**是重点：把 `quota_pct` 塞进
    `payload["suggestions"][0]["changes"]` 同样是改生产参数。
    """
    hits: List[str] = []
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            key = str(k)
            if key.strip().lower() in PRODUCTION_KEYS:
                hits.append(f"{path}.{key}")
            hits.extend(_scan_forbidden(v, f"{path}.{key}"))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            hits.extend(_scan_forbidden(v, f"{path}[{i}]"))
    return hits


def _jsonable(obj: Any) -> bool:
    try:
        json.dumps(obj, ensure_ascii=False, default=str)
        return True
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------- #
#  提交与复核
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ResearchSubmission:
    """
    AI（或人）产出的一份研究material。

    `summary` 是给人看的结论；`payload` 是结构化内容，受禁用键白名单约束。
    把"能写进配置的东西"和"只能读的东西"分开，是这里唯一的防线形状。
    """

    submission_id: str
    author: str
    kind: ResearchKind
    strategy_id: str
    summary: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    evidence: Tuple[str, ...] = ()
    created_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if not self.submission_id:
            raise ResearchRejected("submission_id 不能为空")
        if not self.author:
            raise ResearchRejected("author 不能为空（谁的结论要能追溯到）")
        if not isinstance(self.kind, ResearchKind):
            raise ResearchRejected(f"kind 必须是 ResearchKind，收到 {self.kind!r}")
        if len((self.summary or "").strip()) < MIN_SUMMARY_LEN:
            raise ResearchRejected(
                f"summary 至少 {MIN_SUMMARY_LEN} 字：一句话的结论无法复核，"
                f"至少要说清数据区间和方向")
        if not _jsonable(self.payload):
            raise ResearchRejected("payload 必须可 JSON 序列化（否则落盘会静默丢字段）")

        hits = _scan_forbidden(self.payload)
        if hits:
            raise ResearchRejected(
                "payload 里出现生产控制字段：" + ", ".join(hits) +
                "。研究通道只产出建议，改配额/灰度/实盘开关必须由人在生产侧操作")

        if self.kind is ResearchKind.CANDIDATE_PARAMS:
            if not self.evidence:
                raise ResearchRejected(
                    "候选参数必须带证据：没有可复现回测的参数建议，"
                    "和「我觉得调到 0.3 更好」没有区别")
            if not any(str(e).startswith(EVIDENCE_PREFIXES) for e in self.evidence):
                raise ResearchRejected(
                    f"证据里至少要有一条可追溯标识（{'/'.join(EVIDENCE_PREFIXES)}），"
                    f"收到 {list(self.evidence)}")
            if "params" not in self.payload:
                raise ResearchRejected("候选参数必须在 payload['params'] 里给出具体值")

    # -- 序列化 ---------------------------------------------------------- #

    def to_dict(self) -> Dict:
        return {
            "submission_id": self.submission_id,
            "author": self.author,
            "kind": self.kind.value,
            "strategy_id": self.strategy_id,
            "summary": self.summary,
            "payload": _plain(self.payload),
            "evidence": list(self.evidence),
            "created_at": self.created_at.isoformat(),
        }


def _plain(obj: Any) -> Any:
    """把 Decimal 等转成 JSON 友好结构（决策记录要能直接落盘）。"""
    if isinstance(obj, Mapping):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, Enum):
        return obj.value
    return obj


@dataclass(frozen=True)
class ReviewRecord:
    submission_id: str
    reviewer: str
    verdict: Verdict
    note: str
    ts: datetime = field(default_factory=utcnow)

    def to_dict(self) -> Dict:
        return {
            "submission_id": self.submission_id, "reviewer": self.reviewer,
            "verdict": self.verdict.value, "note": self.note,
            "ts": self.ts.isoformat(),
        }


@dataclass(frozen=True)
class ResearchDraft:
    """
    从已复核的提交里提取出的**参数草案**。

    它是这个模块唯一的出口产物，而且:
      - 必须来自 `verdict=ACCEPTED` 的提交；
      - 必须由 `human:` 的人 `prepare`（`prepared_by`）；
      - 只是一份数据，没有任何方法能把它写进生产。

    把它交给生产侧是人的动作：人看过 `describe()` 之后自己调注册表。
    """

    strategy_id: str
    version: str
    params: Mapping[str, Any]
    source_submission_id: str
    prepared_by: str
    created_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if not _is_human(self.prepared_by):
            raise ReviewRequired(
                f"草案必须由人签名产出（prepared_by 需以 'human:' 开头），"
                f"收到 {self.prepared_by!r}")
        if not self.version:
            raise ResearchRejected("草案必须带版本号，否则无法灰度也无法回滚")
        if not self.params:
            raise ResearchRejected("草案参数为空")

    def describe(self) -> str:
        """给人读的一行摘要。审批时先看这个，再决定要不要看全文。"""
        body = ", ".join(f"{k}={_fmt(v)}" for k, v in sorted(self.params.items()))
        return (f"{self.strategy_id}@{self.version} ← {self.source_submission_id}"
                f"（{self.prepared_by}）: {body}")

    def to_dict(self) -> Dict:
        return {
            "strategy_id": self.strategy_id, "version": self.version,
            "params": _plain(self.params),
            "source_submission_id": self.source_submission_id,
            "prepared_by": self.prepared_by,
            "created_at": self.created_at.isoformat(),
        }


def _fmt(v: Any) -> str:
    return str(_plain(v))


# --------------------------------------------------------------------------- #
#  参数漂移闸
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Drift:
    key: str
    baseline: Decimal
    candidate: Decimal
    rel: Optional[Decimal]

    @property
    def blocking(self) -> bool:
        return self.rel is None or abs(self.rel) > MAX_REL_DRIFT

    def to_dict(self) -> Dict:
        return {"key": self.key, "baseline": str(self.baseline),
                "candidate": str(self.candidate),
                "rel": None if self.rel is None else str(q(self.rel, 6)),
                "blocking": self.blocking}


def parameter_drift(candidate: Mapping[str, Any], baseline: Mapping[str, Any],
                    *, max_rel: Decimal = MAX_REL_DRIFT) -> List[Drift]:
    """
    逐键比对数值型参数。`baseline` 缺失的键**不检查**（新参数没有参照），
    非数值型的键也不检查 —— 这条闸只管量级，不管语义。

    相对变化超过 `max_rel` 或基准为 0（无法算相对变化）都算阻断项：
    0 → 0.5 是"从没有到有"，比 20% → 30% 危险得多。
    """
    out: List[Drift] = []
    for key, raw in candidate.items():
        if key not in baseline:
            continue
        try:
            cand = D(raw)
            base = D(baseline[key])
        except (ArithmeticError, ValueError, TypeError):
            continue
        if base == 0:
            rel = None if cand != 0 else Decimal(0)
        else:
            rel = (cand - base) / abs(base)
        out.append(Drift(key=key, baseline=base, candidate=cand, rel=rel))
    return out


def _drift_blockers(drifts: Sequence[Drift], max_rel: Decimal) -> List[str]:
    bad = []
    for d in drifts:
        if d.rel is None:
            bad.append(f"{d.key}: 基准 0 → {d.candidate}（无法算相对变化）")
        elif abs(d.rel) > D(max_rel):
            bad.append(f"{d.key}: {d.baseline} → {d.candidate}（相对变化 "
                       f"{q(d.rel * 100, 2)}% 超过 {q(D(max_rel) * 100, 2)}%）")
    return bad


# --------------------------------------------------------------------------- #
#  收件箱
# --------------------------------------------------------------------------- #


class ResearchInbox:
    """
    只追加的研究收件箱。有 `path` 时写 JSONL（每行一条，append + fsync）。

    刻意没有的方法：`apply` / `promote` / `set_*`。不是"暂未实现"，
    是这个类**不应该**具备写生产的能力（`assert_isolated()` 会检查）。
    """

    def __init__(self, path: Optional[Path] = None, clock=utcnow) -> None:
        self._path = Path(path) if path else None
        self._clock = clock
        self._subs: Dict[str, ResearchSubmission] = {}
        self._reviews: Dict[str, List[ReviewRecord]] = {}
        self._order: List[str] = []

    # -- 读 -------------------------------------------------------------- #

    def __len__(self) -> int:
        return len(self._subs)

    def get(self, submission_id: str) -> ResearchSubmission:
        if submission_id not in self._subs:
            raise ResearchError(f"没有提交 {submission_id}")
        return self._subs[submission_id]

    def reviews(self, submission_id: str) -> List[ReviewRecord]:
        return list(self._reviews.get(submission_id, ()))

    def verdict(self, submission_id: str) -> Optional[Verdict]:
        rs = self._reviews.get(submission_id) or []
        return rs[-1].verdict if rs else None

    def pending(self) -> List[ResearchSubmission]:
        return [self._subs[i] for i in self._order if not self._reviews.get(i)]

    def accepted(self) -> List[ResearchSubmission]:
        return [self._subs[i] for i in self._order
                if self.verdict(i) is Verdict.ACCEPTED]

    def digest(self) -> str:
        blob = json.dumps(
            {"subs": [self._subs[i].to_dict() for i in self._order],
             "reviews": [r.to_dict() for rs in self._reviews.values() for r in rs]},
            sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    # -- 写 -------------------------------------------------------------- #

    def submit(self, *, author: str, kind: ResearchKind, strategy_id: str,
               summary: str, payload: Optional[Mapping[str, Any]] = None,
               evidence: Sequence[str] = (),
               baseline_params: Optional[Mapping[str, Any]] = None,
               submission_id: Optional[str] = None) -> ResearchSubmission:
        """
        收一份提交。给 `baseline_params` 时顺带做参数漂移检查。

        拒绝是**抛错**而不是返回 False：静默失败的提交通道会让 AI 反复重试
        同一个越界建议，而人永远不知道有人试过。
        """
        raw_params = (payload or {}).get("params") or {}
        if not isinstance(raw_params, Mapping):
            raise ResearchRejected("payload['params'] 必须是字典型（键 → 具体值）")
        params = dict(raw_params)
        if baseline_params and params:
            drifts = parameter_drift(params, baseline_params)
            bad = _drift_blockers(drifts, MAX_REL_DRIFT)
            if bad:
                raise ResearchRejected(
                    "候选参数偏离基准过大，拒绝入箱：" + "；".join(bad) +
                    "。量级跳变通常意味着单位搞错或杠杆失控，需要人先看数据")

        sid = submission_id or self._next_id(strategy_id)
        sub = ResearchSubmission(
            submission_id=sid, author=author, kind=ResearchKind(kind),
            strategy_id=strategy_id, summary=summary,
            payload=dict(payload or {}), evidence=tuple(evidence),
            created_at=self._clock())
        if sid in self._subs:
            raise ResearchRejected(f"submission_id {sid} 已存在（幂等键重复）")
        self._subs[sid] = sub
        self._order.append(sid)
        self._append({"event": "submitted", **sub.to_dict()})
        return sub

    def review(self, submission_id: str, *, reviewer: str, verdict: Verdict,
               note: str = "") -> ReviewRecord:
        """人工复核。`reviewer` 必须是人 —— AI 复核 AI 等于没有复核。"""
        if not _is_human(reviewer):
            raise ReviewRequired(
                f"复核必须由人执行（reviewer 需以 'human:' 开头），收到 {reviewer!r}")
        sub = self.get(submission_id)
        v = Verdict(verdict)
        if v is Verdict.REJECTED and len(note.strip()) < 6:
            raise ResearchRejected(
                f"拒绝 {sub.submission_id} 必须写理由（≥6 字）："
                f"AI 需要知道错在哪，否则下一轮会提交同样的东西")
        rec = ReviewRecord(submission_id=submission_id, reviewer=reviewer,
                           verdict=v, note=note, ts=self._clock())
        self._reviews.setdefault(submission_id, []).append(rec)
        self._append({"event": "reviewed", **rec.to_dict()})
        return rec

    def prepare_draft(self, submission_id: str, *, version: str,
                      prepared_by: str) -> ResearchDraft:
        """
        从已通过的提交里产出参数草案。

        这是本模块**唯一**的出口。它产出的是一份数据，`prepared_by` 必须是
        人 —— 一个人签字认下"这份参数可以拿去上生产"，而这件事 AI 做不了。
        """
        sub = self.get(submission_id)
        v = self.verdict(submission_id)
        if v is not Verdict.ACCEPTED:
            raise ReviewRequired(
                f"{submission_id} 当前判定 {None if v is None else v.value}，"
                f"只有 ACCEPTED 的提交能产出草案")
        params = dict((sub.payload or {}).get("params") or {})
        if not params:
            raise ResearchRejected(f"{submission_id} 的 payload 里没有 params")
        return ResearchDraft(strategy_id=sub.strategy_id, version=version,
                             params=params, source_submission_id=submission_id,
                             prepared_by=prepared_by, created_at=self._clock())

    # -- 内部 ------------------------------------------------------------ #

    def _next_id(self, strategy_id: str) -> str:
        n = len(self._order) + 1
        stamp = self._clock().strftime("%Y%m%d")
        return f"rs-{stamp}-{n:04d}"

    def _append(self, record: Dict) -> None:
        if self._path is None:
            return
        # 沙箱 shim 下 Path.mkdir(exist_ok=True) 会误报 FileExistsError，用 os.makedirs
        os.makedirs(str(self._path.parent), exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())


# --------------------------------------------------------------------------- #
#  静态隔离校验
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class IsolationReport:
    """本模块的隔离体检结果。`ok` 为 False 时说明有人往通道里接了生产能力。"""

    bad_imports: Tuple[str, ...]
    forbidden_methods: Tuple[str, ...]
    ok: bool

    def to_dict(self) -> Dict:
        return {"bad_imports": list(self.bad_imports),
                "forbidden_methods": list(self.forbidden_methods),
                "ok": self.ok}


def assert_isolated(module_path: Optional[Path] = None) -> IsolationReport:
    """
    用 AST 检查本模块的 import 与方法名。可被验收脚本直接调用。

    为什么用 AST 而不是 `inspect.getsource` 做子串匹配：注释和字符串里提到
    `strategy_registry` 是正常的（比如本文档顶部就在解释为什么不 import 它），
    子串匹配会把说明文字当成违规。AST 只看真正的 import 语句。
    """
    path = Path(module_path) if module_path else Path(__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))

    bad_imports: List[str] = []
    methods: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _import_allowed(alias.name):
                    bad_imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level:      # 相对 import 一律不允许（会绕过白名单）
                bad_imports.append("." * node.level + mod)
            elif not _import_allowed(mod):
                bad_imports.append(mod)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in FORBIDDEN_METHODS:
                methods.append(node.name)
    return IsolationReport(bad_imports=tuple(sorted(set(bad_imports))),
                           forbidden_methods=tuple(sorted(set(methods))),
                           ok=not bad_imports and not methods)


def _import_allowed(name: str) -> bool:
    # 只认精确匹配或带点的子模块。用裸 startswith 会把 `astropy` 当成 `ast`，
    # 白名单就形同虚设。`__future__` 是编译指令不是模块依赖，算通过。
    return any(name == p or name.startswith(p + ".")
               for p in ALLOWED_IMPORT_PREFIXES)


__all__ = [
    "ResearchInbox", "ResearchSubmission", "ResearchDraft", "ReviewRecord",
    "ResearchKind", "Verdict", "ResearchError", "ResearchRejected",
    "ReviewRequired", "PRODUCTION_KEYS", "FORBIDDEN_METHODS",
    "parameter_drift", "Drift", "assert_isolated", "IsolationReport",
]
