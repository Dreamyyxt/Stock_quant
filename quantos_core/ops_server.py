"""
QuantOS 2.0 — 运维中心服务（T-301 / T-303 的 HTTP 面）
====================================================

把 `OpsMetrics`（指标）与 `StrategyRegistry`（策略实例 + 审计）暴露成一个
**只绑定 127.0.0.1** 的小服务，供控制台页面读取真实数据。

端点：
  GET  /                       控制台页面（同源，避免 mixed content）
  GET  /api/ops/health         存活
  GET  /api/ops/snapshot       指标快照（含 overall / root_causes / facts）
  GET  /api/ops/strategies     策略实例列表
  GET  /api/ops/audit?limit=N  审计记录（默认 50 条）
  POST /api/ops/actions        暂停/恢复/降级/灰度/配额/晋级

安全口径（刻意保守）：
  - 只 bind 127.0.0.1，不监听 0.0.0.0；
  - 写操作必须带 `X-Ops-Token`，token 未配置时**一律拒绝写**（fail-closed）；
  - 不返回 `Access-Control-Allow-Origin`：页面必须同源，避免任意网页读取持仓；
  - 请求体上限 64KB，超限直接 413；
  - 目录穿越在路由层就被挡住（只认 `/` 与 `/index.html`）。

明确不做的事：
  - 不做用户体系（这是本机运维口，靠 token + 本机回环）；
  - 不代理交易网关（下单永远走 OMS，不走这里）。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Mapping, Optional
from urllib.parse import parse_qs, urlparse

from quantos_core.contracts import D, utcnow
from quantos_core.metrics import OpsMetrics, OpsSnapshot
from quantos_core.strategy_registry import (
    StrategyRegistry, StrategyStage, StrategyRegistryError,
)

#: 请求体上限。运维动作都是小 JSON，超过这个大小一定是出问题了。
MAX_BODY_BYTES = 64 * 1024


# --------------------------------------------------------------------------- #
#  业务服务层（与 socket 解耦，便于直接单测）
# --------------------------------------------------------------------------- #


class OpsService:
    """
    指标 + 策略实例的业务门面。

    所有写操作都返回 `{"ok": bool, ...}`，即使被拒也不抛给 HTTP 层 ——
    因为「被风控/准入拒绝」是**正常的业务结果**，不是服务器错误（500），
    页面需要把它当提示展示。
    """

    def __init__(self, *, metrics: OpsMetrics, registry: StrategyRegistry,
                 prices_provider: Optional[Callable[[], Optional[Mapping]]] = None,
                 token: str = "", clock: Callable[[], datetime] = utcnow,
                 series=None, live_gate=None,
                 risk_model_store=None, canary_router=None,
                 research_inbox=None, schedule_provider=None):
        self.metrics = metrics
        self.registry = registry
        self._prices_provider = prices_provider
        self.token = token or ""
        self._clock = clock
        #: T-403 净值时序。没有它就是"没有历史" —— 页面画不出真曲线，
        #: 会显式提示，而不是拿演示曲线顶上。
        self.series = series
        #: T-401 三级实盘开关（只读展示）。
        self.live_gate = live_gate
        #: T-701 风险模型来源。只读：**没有一个 HTTP 端点能改风险假设**。
        self.risk_model_store = risk_model_store
        #: T-602 灰度路由（只读镜像）。
        self.canary_router = canary_router
        #: T-603 AI 研究收件箱（只读）。
        self.research_inbox = research_inbox
        #: B9 策略排期（只读）。给的是一个**返回快照的函数**，不是调度器对象 ——
        #: 只读看板不该握着「能推进一拍」的东西（那等于给它一个下单按钮）。
        self.schedule_provider = schedule_provider

    # -- 读 -------------------------------------------------------------- #

    def snapshot(self) -> Dict[str, Any]:
        prices = None
        if self._prices_provider is not None:
            prices = self._prices_provider()
        snap: OpsSnapshot = self.metrics.sample(prices)
        return snap.to_dict()

    def strategies(self) -> Dict[str, Any]:
        return {
            "as_of": self._clock().isoformat(),
            "count": len(self.registry),
            "strategies": self.registry.snapshot(),
            "inconsistencies": self.registry.verify_against_audit(),
        }

    def audit(self, limit: int = 50) -> Dict[str, Any]:
        limit = max(1, min(int(limit or 50), 500))
        records = self.registry.audit.records(limit=limit)
        return {
            "as_of": self._clock().isoformat(),
            "count": self.registry.audit.count(),
            "digest": self.registry.audit.digest()[:16],
            "records": [r.to_dict() for r in reversed(records)],
        }

    def portfolio(self) -> Dict[str, Any]:
        """
        组合层只读视图：风险模型的来源与新鲜度、灰度状态、AI 收件箱。

        全部走 `metrics.sample()` 的 facts，不重新算一遍 —— 页面看到的数字
        必须和报警判定用的是同一份数据，否则会出现「报警说 warn、页面说 ok」。

        为什么这里没有任何写操作：风险假设和灰度权重都是**能改变风险敞口**的
        东西，和实盘开关一样，只能由人工通过 Python 接口操作。
        """
        snap = self.metrics.sample(
            self._prices_provider() if self._prices_provider else None)
        facts = snap.facts or {}
        pf = facts.get("portfolio") or {}
        return {
            "as_of": self._clock().isoformat(),
            "risk_model": pf.get("risk_model",
                                 {"available": False, "reason": "指标层未产出该区块"}),
            "canary": pf.get("canary",
                             {"available": False, "reason": "指标层未产出该区块"}),
            "research": pf.get("research",
                               {"available": False, "reason": "指标层未产出该区块"}),
            "metrics": [m.to_dict() for m in snap.metrics
                        if m.key.startswith(("risk.model", "canary", "research."))],
        }

    def health(self) -> Dict[str, Any]:
        return {"ok": True, "ts": self._clock().isoformat(),
                "strategies": len(self.registry),
                "write_enabled": bool(self.token)}

    def nav_history(self, limit: int = 0) -> Dict[str, Any]:
        """
        净值时序（T-403）。页面用它画**真实**曲线。

        没有时序时返回 `available: False` 与一句说明，而不是空数组 ——
        空数组会让前端画出一条平线，看起来像"净值一直没变"。
        """
        if self.series is None:
            return {"as_of": self._clock().isoformat(), "available": False,
                    "count": 0, "points": [],
                    "reason": "未接入净值时序（需先跑一次日终 T-402/T-403）"}
        points = self.series.points(limit=max(0, int(limit or 0)))
        return {
            "as_of": self._clock().isoformat(), "available": bool(points),
            "count": len(self.series),
            "points": [p.to_dict() for p in points],
            "reason": "" if points else "净值时序为空，尚未跑过日终",
        }

    def live(self) -> Dict[str, Any]:
        """三级实盘开关现状（T-401）。只读：武装仍然必须走人工确认。"""
        if self.live_gate is None:
            return {"as_of": self._clock().isoformat(), "available": False,
                    "reason": "未接入实盘开关"}
        return {"as_of": self._clock().isoformat(), "available": True,
                **self.live_gate.status(self._clock())}

    def schedule(self) -> Dict[str, Any]:
        """
        策略排期（B9）。只读。

        没有日历 / 没有排期 / 数据日未校准，三种情况都返回 `available` 与
        `reason`，**不填默认值、不挂绿灯** —— 一个"看起来今天该跑但其实是编的"
        排期表，比没有这张表危险得多。
        """
        if self.schedule_provider is None:
            return {"as_of": self._clock().isoformat(), "available": False,
                    "reason": "未接入策略排期", "entries": [],
                    "entry_count": 0, "needs_attention": [], "healthy": False}
        try:
            return dict(self.schedule_provider())
        except (RuntimeError, OSError, ValueError) as exc:
            return {"as_of": self._clock().isoformat(), "available": False,
                    "reason": f"排期快照读取失败：{type(exc).__name__}: {exc}",
                    "entries": [], "entry_count": 0,
                    "needs_attention": [], "healthy": False}

    # -- 写 -------------------------------------------------------------- #

    def perform(self, payload: Mapping[str, Any], *, actor: str) -> Dict[str, Any]:
        """
        执行一个运维动作。

        `actor` 由 HTTP 层从 token 推导，绝不从请求体取 —— 否则谁都能自称
        `human:alice` 把自己写的策略放上实盘。
        """
        action = str(payload.get("action") or "").strip()
        sid = str(payload.get("strategy_id") or "").strip()
        reason = str(payload.get("reason") or "").strip()

        if not action:
            return {"ok": False, "error": "缺少 action", "error_type": "bad_request"}
        if not sid:
            return {"ok": False, "error": "缺少 strategy_id",
                    "error_type": "bad_request"}

        try:
            if action == "pause":
                inst = self.registry.pause(sid, reason or "控制台人工暂停", actor=actor)
            elif action == "degrade":
                inst = self.registry.degrade(sid, reason or "控制台人工降级", actor=actor)
            elif action == "resume":
                inst = self.registry.resume(
                    sid, reason or "控制台人工恢复", actor=actor,
                    live=bool(payload.get("live")))
            elif action == "set_canary":
                inst = self.registry.set_canary(
                    sid, D(payload.get("canary_weight")), actor=actor, reason=reason)
            elif action == "set_quota":
                inst = self.registry.set_quota(
                    sid, D(payload.get("quota_pct")), actor=actor, reason=reason)
            elif action == "promote":
                stage_raw = str(payload.get("to_stage") or "").upper()
                try:
                    stage = StrategyStage(stage_raw)
                except ValueError:
                    return {"ok": False,
                            "error": f"未知阶段：{stage_raw or '(空)'}",
                            "error_type": "bad_request"}
                inst = self.registry.promote(
                    sid, stage, actor=actor,
                    backtest_report_id=str(payload.get("backtest_report_id") or ""),
                    code_commit=str(payload.get("code_commit") or ""),
                    data_version=str(payload.get("data_version") or ""),
                    cost_model_version=str(payload.get("cost_model_version") or ""))
            else:
                return {"ok": False, "error": f"未知动作：{action}",
                        "error_type": "bad_request"}
        except StrategyRegistryError as exc:
            # 被拒也是结果：审计里已经有 rejected 记录，这里如实回给页面。
            return {"ok": False, "error": str(exc),
                    "error_type": type(exc).__name__,
                    "audit_seq": self.registry.audit.count()}
        except (TypeError, ValueError, ArithmeticError) as exc:
            return {"ok": False, "error": f"参数不合法：{exc}",
                    "error_type": "bad_argument"}

        # 策略状态变了要立刻同步给风控，否则页面显示暂停、实际还在下单。
        if self.metrics.risk_state is not None:
            self.registry.apply_to(self.metrics.risk_state)

        return {"ok": True, "action": action, "strategy": inst.to_dict(),
                "audit_seq": self.registry.audit.count()}


# --------------------------------------------------------------------------- #
#  HTTP 层
# --------------------------------------------------------------------------- #


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    server_version = "QuantOS-Ops/1.0"
    #: 由 OpsHttpServer 在创建子类时注入
    service: OpsService = None          # type: ignore[assignment]
    html_path: Optional[str] = None
    quiet: bool = True

    # -- 工具 ------------------------------------------------------------ #

    def log_message(self, fmt: str, *args) -> None:      # noqa: A003
        if not self.quiet:
            super().log_message(fmt, *args)

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        # 刻意不发 CORS 头：调用方必须同源。
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj: Any) -> None:
        self._send(status, _json_bytes(obj), "application/json; charset=utf-8")

    def _actor(self) -> str:
        """写操作的身份来自 token 校验结果，绝不来自请求体。"""
        return "human:ops-console"

    def _check_token(self) -> bool:
        want = getattr(self.service, "token", "") or ""
        if not want:
            return False
        got = self.headers.get("X-Ops-Token", "")
        return got == want

    # -- 路由 ------------------------------------------------------------ #

    def do_GET(self) -> None:                            # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            return self._serve_page()

        if path == "/api/ops/health":
            return self._json(HTTPStatus.OK, self.service.health())

        if path == "/api/ops/snapshot":
            return self._json(HTTPStatus.OK, self.service.snapshot())

        if path == "/api/ops/strategies":
            return self._json(HTTPStatus.OK, self.service.strategies())

        if path == "/api/ops/audit":
            try:
                limit = int((query.get("limit") or ["50"])[0])
            except (TypeError, ValueError):
                limit = 50
            return self._json(HTTPStatus.OK, self.service.audit(limit))

        if path == "/api/ops/nav-history":
            try:
                limit = int((query.get("limit") or ["0"])[0])
            except (TypeError, ValueError):
                limit = 0
            return self._json(HTTPStatus.OK, self.service.nav_history(limit))

        if path == "/api/ops/live":
            return self._json(HTTPStatus.OK, self.service.live())

        if path == "/api/ops/portfolio":
            return self._json(HTTPStatus.OK, self.service.portfolio())

        if path == "/api/ops/schedule":
            return self._json(HTTPStatus.OK, self.service.schedule())

        return self._json(HTTPStatus.NOT_FOUND,
                          {"ok": False, "error": f"未知路径 {path}"})

    def do_POST(self) -> None:                           # noqa: N802
        path = urlparse(self.path).path
        if path != "/api/ops/actions":
            return self._json(HTTPStatus.NOT_FOUND,
                              {"ok": False, "error": f"未知路径 {path}"})

        if not self._check_token():
            # token 未配置也算拒绝：宁可用不了，也不能裸奔写状态。
            return self._json(HTTPStatus.FORBIDDEN, {
                "ok": False,
                "error": ("写操作已禁用：未配置 OPS token"
                          if not (getattr(self.service, "token", "") or "")
                          else "X-Ops-Token 不匹配"),
                "error_type": "forbidden",
            })

        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            return self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                              {"ok": False, "error": "请求体过大"})
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return self._json(HTTPStatus.BAD_REQUEST,
                              {"ok": False, "error": f"JSON 解析失败：{exc}"})
        if not isinstance(payload, dict):
            return self._json(HTTPStatus.BAD_REQUEST,
                              {"ok": False, "error": "请求体必须是对象"})

        result = self.service.perform(payload, actor=self._actor())
        status = HTTPStatus.OK if result.get("ok") else HTTPStatus.CONFLICT
        return self._json(status, result)

    # -- 静态页面 -------------------------------------------------------- #

    def _serve_page(self) -> None:
        path = self.html_path
        if not path or not os.path.isfile(path):
            return self._json(HTTPStatus.NOT_FOUND, {
                "ok": False,
                "error": "未配置控制台页面路径（OpsHttpServer(html_path=...))",
            })
        with open(path, "rb") as fh:
            body = fh.read()
        return self._send(HTTPStatus.OK, body, "text/html; charset=utf-8")


class OpsHttpServer:
    """
    轻量运维服务。用 `with OpsHttpServer(...) as srv:` 或 start()/stop()。
    """

    def __init__(self, service: OpsService, *, host: str = "127.0.0.1",
                 port: int = 8787, html_path: Optional[str] = None,
                 quiet: bool = True):
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError(
                "运维服务只允许绑定回环地址：暴露到 0.0.0.0 等于把持仓和"
                "策略开关交给整个局域网")
        self.service = service
        self.host = host
        self.port = int(port)
        self.html_path = html_path
        self.quiet = quiet
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # -- 生命周期 -------------------------------------------------------- #

    def start(self) -> "OpsHttpServer":
        handler = type("BoundOpsHandler", (_Handler,), {
            "service": self.service,
            "html_path": self.html_path,
            "quiet": self.quiet,
        })
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        kwargs={"poll_interval": 0.05},
                                        name="quantos-ops", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def __enter__(self) -> "OpsHttpServer":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()


__all__ = ["OpsService", "OpsHttpServer", "MAX_BODY_BYTES"]
