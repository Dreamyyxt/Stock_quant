"""
共享断言工具。零依赖、零网络，输出人可读、退出码可判。

为什么要自己写而不是 pytest：
  这些脚本要在生产主机上直接跑（`python scripts/quantos_selftest_b1.py`），
  不希望为了跑一次自检去装测试框架。断言数量不多，但每条都要能定位。
"""

from __future__ import annotations

import sys
import traceback
from decimal import Decimal
from typing import Any, Callable, Iterable, List, Optional, Type

_TOTAL = 0
_FAILED: List[str] = []
_SECTION = ""


def section(title: str) -> None:
    global _SECTION
    _SECTION = title
    print(f"\n── {title} " + "─" * max(0, 58 - len(title)))


def _record(ok: bool, label: str, detail: str = "") -> None:
    global _TOTAL
    _TOTAL += 1
    if ok:
        print(f"  ✅ {label}")
    else:
        _FAILED.append(f"[{_SECTION}] {label} {detail}")
        print(f"  ❌ {label}  {detail}")


def truth(cond: Any, label: str) -> None:
    _record(bool(cond), label, "" if cond else "(条件为假)")


def falsy(cond: Any, label: str) -> None:
    _record(not bool(cond), label, "" if not cond else "(条件为真)")


def eq(actual: Any, expected: Any, label: str = "") -> None:
    """数值优先按 Decimal 严格比较（避免 0.1+0.2 的浮点假象），否则按类型比较。"""
    detail = ""
    ok = False
    try:
        ok = Decimal(str(actual)) == Decimal(str(expected))
        detail = f"(期望 {expected}，实际 {actual})"
    except Exception:                                        # noqa: BLE001
        ok = actual == expected
        detail = f"(期望 {expected!r}，实际 {actual!r})"
    _record(ok, label or "值相等", "" if ok else detail)


def ne(actual: Any, unexpected: Any, label: str = "") -> None:
    _record(actual != unexpected, label or "值不等", f"(实际 {actual!r})")


def near(actual: Any, expected: Any, tol: Any, label: str = "") -> None:
    a, e, t = Decimal(str(actual)), Decimal(str(expected)), Decimal(str(tol))
    ok = abs(a - e) <= t
    _record(ok, label or "近似相等", "" if ok else f"(期望 {e}±{t}，实际 {a})")


def raises(exc_type: Type[BaseException], fn: Callable[[], Any],
           label: str = "") -> Optional[BaseException]:
    try:
        fn()
    except exc_type as exc:
        _record(True, label or f"抛出 {exc_type.__name__}")
        return exc
    except BaseException as exc:                              # noqa: BLE001
        _record(False, label or f"抛出 {exc_type.__name__}",
                f"(实际抛出 {type(exc).__name__}: {exc})")
        return None
    _record(False, label or f"抛出 {exc_type.__name__}", "(没有抛异常)")
    return None


def guard(label: str, fn: Callable[[], None]) -> None:
    """跑一段断言块，异常算一次失败但不中断后续测试。"""
    try:
        fn()
    except BaseException as exc:                              # noqa: BLE001
        _record(False, label, f"({type(exc).__name__}: {exc})")
        traceback.print_exc(limit=3)


def failure_count() -> int:
    """当前失败数。故障注入脚本用它给每个场景单独判定 PASS/FAIL。"""
    return len(_FAILED)


def total_count() -> int:
    return _TOTAL


def summary(name: str) -> int:
    print("\n" + "=" * 66)
    if _FAILED:
        print(f"{name}：{_TOTAL - len(_FAILED)}/{_TOTAL} 通过，{len(_FAILED)} 失败")
        for item in _FAILED:
            print(f"  · {item}")
        return 1
    print(f"{name}：全部 {_TOTAL} 条断言通过")
    return 0


def run(name: str, body: Callable[[], None]) -> int:
    """统一入口：跑 body，打印结果，返回退出码。"""
    try:
        body()
    except BaseException as exc:                              # noqa: BLE001
        print(f"\n💥 {name} 意外中止：{type(exc).__name__}: {exc}")
        traceback.print_exc()
        return 2
    return summary(name)


__all__ = ["section", "truth", "falsy", "eq", "ne", "near", "raises",
           "guard", "summary", "run", "failure_count", "total_count"]
