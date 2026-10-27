"""
策略包导入 —— 把外面收集来的策略，安全地放进 RESEARCH 阶段。

包长什么样
==========
一个普通的 `.py` 文件，暴露两样东西：

```python
MANIFEST = {
    "strategy_id": "ashare_ew20",
    "version": "v1",
    "venue": "TIGER",
    "kind": "rebalance",
    "rebalance_days": 20,
    "warmup_bars": 25,
    "params": [
        {"name": "top_n", "kind": "int", "low": 5, "high": 60, "default": 20},
    ],
    "description": "中证800 等权 20 日再平衡",
}

def compute(frame, ctx):
    series = frame.require("000300", ctx.params["top_n"] + 1)
    ...
    return [SignalOut("600000", Decimal("0.05"), "20日再平衡入选")]
```

没有基类、没有注册装饰器、不 import 任何生产模块 —— 这是刻意的：
策略包要能在任何机器上单独跑、单独回测。一旦它 import 了 `quantos_core`，
"研究=生产同一套逻辑"（R-S06）就变成"研究和生产共用一份实现"，
好处是省事，坏处是策略能顺着 import 摸到生产对象。

导入时强制做的五件事
====================
1. **AST 体检**：import 白名单 + 禁用内建 + 禁止碰 `__dunder__` 属性。
2. **指纹**：`sha256(源码)` 前 12 位。同版本号不同指纹 = 代码被改过。
3. **manifest 校验**：参数 schema、必填字段、未知键。
4. **干跑一次**：真调一遍 `compute()`，确认返回值能进适配器。
   只导入不跑，等于相信"看起来没问题"。
5. **落成 RESEARCH**：`to_instance()` 只会给 RESEARCH 阶段。
   想往 BACKTEST/PAPER/LIVE 走，必须走 `StrategyRegistry.promote()` 的准入检查。

关于安全的诚实说明
==================
AST 体检是**门槛，不是沙箱**。它能挡住 `import os`、`open()`、`exec()` 这类
明显的东西，挡不住精心构造的逃逸。真正的隔离需要：独立进程、无网络命名空间、
只读文件系统、资源限额。那些属于部署层（B10），本模块不做，也不假装做了。

所以规则是：**策略包只从你信任的来源导入**。AST 体检的作用是
"让无意的越界当场失败"，不是"让恶意的代码变安全"。
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .strategy_api import (
    FrameError, MarketFrame, ParamError, SignalOut, StrategyContext,
    StrategyError, StrategyManifest, check_strategy, coerce_signal,
)
from .capital_allocator import StrategyPolicy
from .strategy_registry import StrategyInstance, StrategyStage

__all__ = [
    "PackRejected", "PackAudit", "LoadedPack", "PackAdmission",
    "audit_source", "load_pack", "pack_from_object", "dry_run",
    "admission_blockers", "to_instance", "install",
    "ALLOWED_IMPORT_PREFIXES", "FORBIDDEN_CALLS",
]


class PackRejected(StrategyError):
    """包不能导入。消息里必须写清是哪一条不过，不允许只说"导入失败"。"""


# --------------------------------------------------------------------------- #
#  静态体检
# --------------------------------------------------------------------------- #

#: 策略包允许 import 的东西。全是纯计算/stdlib 里不带副作用的部分。
#: 少了任何一个都不会让人写不出策略，多给一个就多一个绕过手段。
ALLOWED_IMPORT_PREFIXES: Tuple[str, ...] = (
    "math", "statistics", "decimal", "fractions", "operator",
    "datetime", "typing", "dataclasses", "collections", "itertools",
    "functools", "enum", "string", "re",
    # 唯一放行的生产模块：纯数据契约，没有 IO、没有状态、没有单例。
    # 包用它的唯一场景是 `from quantos_core.strategy_api import SignalOut`，
    # 而更推荐返回 dict（零 import），见 strategy_api.coerce_signal 的说明。
    "quantos_core.strategy_api",
)

#: 直接禁掉的内建/函数名。`getattr` 没禁：适配器自己要用，
#: 而且禁了也挡不住 `.__getattribute__`（那条由 dunder 规则管）。
FORBIDDEN_CALLS: Tuple[str, ...] = (
    "open", "exec", "eval", "compile", "__import__", "input", "breakpoint",
    "globals", "locals", "vars", "setattr", "delattr", "exit", "quit",
    "memoryview",
)

#: 允许出现在**顶层**（模块级）的语句类型。策略包不该在 import 时干任何事 ——
#: 顶层写 `print` / 循环 / 条件，说明作者把"加载"当"运行"了。
_TOPLEVEL_OK = (
    ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign,
    ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
    # 顶层 try 一般用来给可选依赖兜底（`try: import x except: x = None`）。
    # 放行它不影响安全性：try 里面的 import 照样要被逐个审。
    ast.Try,
    ast.Expr,            # docstring 与常量表达式
)


@dataclass(frozen=True)
class PackAudit:
    """体检报告。三个列表都为空才算通过。"""

    bad_imports: Tuple[str, ...] = ()
    forbidden_calls: Tuple[str, ...] = ()
    dunder_access: Tuple[str, ...] = ()
    bad_toplevel: Tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not (self.bad_imports or self.forbidden_calls
                    or self.dunder_access or self.bad_toplevel)

    def reasons(self) -> List[str]:
        out: List[str] = []
        if self.bad_imports:
            out.append(f"不允许的 import：{list(self.bad_imports)}"
                       f"（白名单：{list(ALLOWED_IMPORT_PREFIXES)}）")
        if self.forbidden_calls:
            out.append(f"禁用的调用：{list(self.forbidden_calls)}")
        if self.dunder_access:
            out.append(f"禁止访问双下划线属性：{list(self.dunder_access)}")
        if self.bad_toplevel:
            out.append(f"顶层出现不该有的语句：{list(self.bad_toplevel)}")
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "bad_imports": list(self.bad_imports),
                "forbidden_calls": list(self.forbidden_calls),
                "dunder_access": list(self.dunder_access),
                "bad_toplevel": list(self.bad_toplevel),
                "reasons": self.reasons()}


def _import_allowed(name: str) -> bool:
    # 精确匹配或子模块。裸 startswith 会让 `datetimez` 冒充 `datetime`。
    return any(name == p or name.startswith(p + ".") for p in ALLOWED_IMPORT_PREFIXES)


def audit_source(source: str, *, filename: str = "<pack>") -> PackAudit:
    """
    对源码做 AST 体检。

    用 AST 而不是字符串匹配：注释和 docstring 里写 `import os` 是正常的
    （上面那段说明就在讨论它），子串匹配会把说明当成违规；
    反过来，`__import__("o"+"s")` 能骗过子串匹配，骗不过 AST。
    """
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        raise PackRejected(f"策略包语法错误：{exc}") from exc

    bad_imports: List[str] = []
    forbidden: List[str] = []
    dunders: List[str] = []
    bad_top: List[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _import_allowed(alias.name):
                    bad_imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level:
                bad_imports.append("." * node.level + mod)
            elif not _import_allowed(mod):
                bad_imports.append(mod)
        elif isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name in FORBIDDEN_CALLS:
                forbidden.append(str(name))
        elif isinstance(node, ast.Attribute):
            # `x.__class__.__bases__` 这类是经典逃逸路径。正规策略不需要它。
            if node.attr.startswith("__") and node.attr.endswith("__"):
                dunders.append(node.attr)

    for node in tree.body:
        if not isinstance(node, _TOPLEVEL_OK):
            bad_top.append(type(node).__name__)
        elif isinstance(node, ast.Expr) and not isinstance(node.value, ast.Constant):
            # 顶层写函数调用（比如 `print(...)` 或 `main()`）属于"import 即执行"。
            bad_top.append("顶层表达式调用")

    return PackAudit(
        bad_imports=tuple(sorted(set(bad_imports))),
        forbidden_calls=tuple(sorted(set(forbidden))),
        dunder_access=tuple(sorted(set(dunders))),
        bad_toplevel=tuple(sorted(set(bad_top))),
    )


# --------------------------------------------------------------------------- #
#  加载
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class LoadedPack:
    """一个已经通过体检、可以调用的策略包。"""

    manifest: StrategyManifest
    compute: Any
    source: str
    fingerprint: str
    origin: str                       #: 文件路径或 "<object>"
    audit: PackAudit
    dry_signals: Tuple[SignalOut, ...] = ()
    warnings: Tuple[str, ...] = ()

    @property
    def params_fingerprint_material(self) -> str:
        return f"{self.fingerprint}:{self.manifest.strategy_id}:{self.manifest.version}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "manifest": self.manifest.to_dict(),
            "fingerprint": self.fingerprint,
            "origin": self.origin,
            "audit": self.audit.to_dict(),
            "dry_run_signals": len(self.dry_signals),
            "warnings": list(self.warnings),
        }


def _fingerprint(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]


def load_pack(path: str, *, dry_frame: Optional[MarketFrame] = None,
              dry_params: Optional[Mapping[str, Any]] = None,
              positions: Optional[Mapping[str, Decimal]] = None) -> LoadedPack:
    """
    从文件加载策略包。

    `dry_frame` 给了就干跑一次（推荐：不给干跑的导入等于没验证）。
    干跑失败 → 整个导入失败。理由是：一个跑不起来的策略放进 RESEARCH
    只会让人以为"已经导进来了"。
    """
    p = Path(path)
    if not p.is_file():
        raise PackRejected(f"策略包不存在：{path}")
    source = p.read_text(encoding="utf-8")
    return _load_source(source, origin=str(p), filename=p.name,
                        dry_frame=dry_frame, dry_params=dry_params,
                        positions=positions)


def pack_from_object(obj: Any, *, source: str = "",
                     origin: str = "<object>") -> LoadedPack:
    """
    已经加载过的模块/对象 → LoadedPack（内置策略、测试、以及"从别处 import 来的策略"）。

    没有源码时 fingerprint 退化为"参数 schema 的摘要"，并在 warnings 里说明。
    这不是等价物：源码能证明"代码没改"，schema 摘要只能证明"声明没改"。
    """
    manifest, compute = check_strategy(obj)
    if source:
        audit = audit_source(source, filename=origin)
        if not audit.ok:
            raise PackRejected("；".join(audit.reasons()))
        fp = _fingerprint(source)
        warnings: Tuple[str, ...] = ()
    else:
        audit = PackAudit()
        fp = hashlib.sha256(
            (manifest.strategy_id + manifest.version
             + repr(sorted(s.name for s in manifest.params))).encode("utf-8")
        ).hexdigest()[:12]
        warnings = ("无源码，指纹退化为 schema 摘要 —— 只能证明声明没改，"
                    "证明不了代码没改",)
    return LoadedPack(manifest=manifest, compute=compute, source=source,
                      fingerprint=fp, origin=origin, audit=audit,
                      warnings=warnings)


def _load_source(source: str, *, origin: str, filename: str,
                 dry_frame: Optional[MarketFrame],
                 dry_params: Optional[Mapping[str, Any]],
                 positions: Optional[Mapping[str, Decimal]]) -> LoadedPack:
    audit = audit_source(source, filename=filename)
    if not audit.ok:
        raise PackRejected("策略包体检不过：" + "；".join(audit.reasons()))

    module = ModuleType(f"quantos_pack_{_fingerprint(source)}")
    module.__file__ = origin
    code = compile(source, filename, "exec")
    # exec 有意为之：包必须不依赖 sys.path 里的东西，也不该被缓存成可 import 的模块
    # （否则同名包会互相覆盖）。沙箱不在这一层做，见模块头的说明。
    exec(code, module.__dict__)      # noqa: S102

    loaded = pack_from_object(module, source=source, origin=origin)
    if not loaded.manifest.fingerprint:
        # 指纹回写进 manifest：注册时 `code_commit` 与它必须一致，
        # 否则"同版本号不同代码"就查不出来。
        loaded = replace(loaded, manifest=replace(loaded.manifest,
                                                  fingerprint=loaded.fingerprint))

    if dry_frame is None:
        return loaded

    signals = dry_run(loaded, dry_frame, dry_params, positions)
    return LoadedPack(manifest=loaded.manifest, compute=loaded.compute,
                      source=loaded.source, fingerprint=loaded.fingerprint,
                      origin=loaded.origin, audit=loaded.audit,
                      dry_signals=signals, warnings=loaded.warnings)


def dry_run(pack: LoadedPack, frame: MarketFrame,
            params: Optional[Mapping[str, Any]] = None,
            positions: Optional[Mapping[str, Decimal]] = None) -> Tuple[SignalOut, ...]:
    """
    真调一次 compute()，把结果按契约过一遍。

    这一步抓的是"manifest 写对了但函数返回垃圾"：权重超 1、没写 reason、
    返回字符串、返回 None、抛异常。这些错误如果不在这里炸，
    就会在凌晨的调度里炸，然后被当成"策略今天没信号"。
    """
    resolved = pack.manifest.resolve_params(params)
    ctx = StrategyContext(as_of=frame.as_of, params=resolved,
                          positions=dict(positions or {}))
    try:
        raw = pack.compute(frame, ctx)
    except (FrameError, ParamError):
        raise
    except Exception as exc:                      # noqa: BLE001
        raise PackRejected(
            f"策略 {pack.manifest.strategy_id} 干跑抛异常："
            f"{type(exc).__name__}: {exc}") from exc

    if raw is None:
        raise PackRejected(
            f"策略 {pack.manifest.strategy_id} 干跑返回 None —— "
            "没有信号请返回空列表，返回 None 分不清『没信号』和『忘了 return』")
    if isinstance(raw, (str, bytes, Mapping)):
        raise PackRejected(
            f"策略 {pack.manifest.strategy_id} 干跑返回 {type(raw).__name__}，"
            "必须返回信号序列")

    out: List[SignalOut] = []
    for i, item in enumerate(raw):
        try:
            out.append(coerce_signal(item))
        except StrategyError as exc:
            raise PackRejected(
                f"策略 {pack.manifest.strategy_id} 第 {i} 个返回值不合法：{exc}"
                "（策略只能给权重，不能自己造订单结构）") from exc
    return tuple(out)


# --------------------------------------------------------------------------- #
#  准入
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class PackAdmission:
    """
    准入结论。`blockers` 非空就不许进 NEXT 阶段。

    刻意**不**提供"忽略 blockers 强行提升"的参数：绕过准入的口子
    一旦存在，就一定会在某个赶时间的下午被用掉。
    """

    manifest: StrategyManifest
    blockers: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.blockers

    def to_dict(self) -> Dict[str, Any]:
        return {"strategy_id": self.manifest.strategy_id,
                "version": self.manifest.version,
                "ok": self.ok, "blockers": list(self.blockers),
                "notes": list(self.notes)}


#: R-S03 的准入条件，逐条对应。键名即证据名，缺一个就是缺证据。
ADMISSION_EVIDENCE = (
    ("out_of_sample_backtest", "通过样本外回测"),
    ("costs_modeled", "计入真实手续费与滑点"),
    ("paper_verified", "模拟盘交易状态正确"),
    ("error_path_tested", "异常订单处理测试通过"),
    ("human_approved", "人工授权上线"),
)


def admission_blockers(evidence: Mapping[str, Any],
                       *, min_paper_days: int = 14,
                       paper_days: int = 0) -> Tuple[str, ...]:
    """
    按 R-S03 逐条查证据。返回缺失项（空 == 可以上实盘）。

    `paper_days` 单独传而不是放进 evidence：模拟盘天数是可以自己数的，
    其他四项是别人给的结论 —— 混在一起会让人以为天数也是"声称"出来的。
    """
    blockers: List[str] = []
    for key, label in ADMISSION_EVIDENCE:
        if not evidence.get(key):
            blockers.append(f"缺少：{label}（{key}）")
    if paper_days < min_paper_days:
        blockers.append(f"模拟盘 {paper_days} 天 < 要求 {min_paper_days} 天")
    return tuple(blockers)


def to_instance(pack: LoadedPack, *, policy: StrategyPolicy,
                data_version: str, cost_model_version: str,
                actor: str, now: Optional[datetime] = None) -> StrategyInstance:
    """
    包 → 可注册的实例。**阶段固定为 RESEARCH**。

    为什么不给 stage 参数：导入动作的语义就是"进研究区"。
    想让它变成 PAPER/LIVE，走 `StrategyRegistry.promote()`，
    那里有准入检查与审计。这里开个口子等于把两道门合并成一道。
    """
    if policy.strategy_id != pack.manifest.strategy_id:
        raise PackRejected(
            f"配额声明 {policy.strategy_id!r} 与策略 {pack.manifest.strategy_id!r} 不一致")
    if not data_version:
        raise PackRejected("必须写明数据版本（data_version）—— 回测可复现的前提（R-S02）")
    if not cost_model_version:
        raise PackRejected("必须写明成本模型版本 —— 同一策略换个成本假设，结论可能反过来")

    inst = StrategyInstance(
        strategy_id=pack.manifest.strategy_id,
        version=pack.manifest.version,
        venue=pack.manifest.venue,
        policy=policy,
        stage=StrategyStage.RESEARCH,
        code_commit=pack.fingerprint,
        data_version=data_version,
        cost_model_version=cost_model_version,
    )
    if now is not None:
        inst.updated_at = now
    return inst


def install(registry, pack: LoadedPack, *, policy: StrategyPolicy,
            data_version: str, cost_model_version: str,
            actor: str, replace_existing: bool = False) -> StrategyInstance:
    """
    导入即注册。注册失败由 `StrategyRegistry` 抛，这里不吞异常。

    注意：**注册成功不代表能跑**。RESEARCH 阶段的策略不会被
    `StrategiesRegistry.policies()` 交给资金分配器（它只认 tradable 的），
    所以导进来一万个策略也不会产生任何一张订单。这是设计，不是疏漏。
    """
    if not actor:
        raise PackRejected("导入必须记录 actor —— 没有来源的策略等于没人负责")
    inst = to_instance(pack, policy=policy, data_version=data_version,
                       cost_model_version=cost_model_version, actor=actor)
    return registry.register(inst, actor=actor, replace_existing=replace_existing)
