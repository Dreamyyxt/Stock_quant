"""
QuantOS 2.0 — 风控独立进程（T-201）
==================================

需求 R-A02：风控不能是策略进程里的一个函数调用。策略进程崩了、卡了、被人 Ctrl-C
了，风控必须还活着；反过来风控挂了，策略必须**下不了单**。

通信方式
--------
Unix domain socket，一行一个 JSON 请求/响应。选它而不是 HTTP 的原因：
  - 不走网卡，天然不对外暴露（R-O04：交易网关不直接公网可达）
  - 权限靠文件系统，比端口加 token 更难配错
  - 单机延迟低于毫秒，中低频交易完全够用

fail-closed 的实现位置
----------------------
**在客户端里**。`RiskServiceClient.check()` 永不抛异常：连不上、超时、返回乱码、
服务端报错，一律返回 `REJECT` + `policy_key="risk_service_unavailable"`。
把这件事放在客户端，是因为调用方（OMS）不该有"我要不要处理风控异常"的余地。

一个容易忽略的细节
------------------
`check()` 返回 REJECT 时仍然给出 `halt_actions`（冻结新增订单）。这样即使调用方
只判断 `allowed`，也会把"风控不可用"和"风控明确拒绝"区分记录。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Tuple

from quantos_core.contracts import (
    D, OrderIntent, OrderType, PermissionLevel, RiskLayer, Side, StrategyRunState,
    Verdict, utcnow,
)
from quantos_core.events import canonical_json, from_jsonable, to_jsonable
from quantos_core.risk_engine import (
    HaltAction, RiskContext, RiskDecision, RiskEngine, RiskPolicy, RiskState,
)

UTC = timezone.utc
PROTOCOL_VERSION = 1


class RiskServiceError(RuntimeError):
    """风控服务通信/协议错误。调用方必须按 fail-closed 处理。"""


class RiskServiceUnavailable(RiskServiceError):
    """连不上/超时/进程已死。"""


# --------------------------------------------------------------------------- #
#  编解码
# --------------------------------------------------------------------------- #
#  实现集中在 quantos_core/codec.py —— 风控服务、OMS 落盘快照、事件日志三处共用
#  同一份编码。这里只做再导出，禁止在服务端另写一套（字段漂移 = 风控静默失效）。
# --------------------------------------------------------------------------- #

from quantos_core.codec import (                      # noqa: E402
    ctx_from_payload, ctx_to_payload, decision_from_payload, decision_to_payload,
    intent_from_payload, intent_to_payload,
)


# --------------------------------------------------------------------------- #
#  服务端
# --------------------------------------------------------------------------- #

class RiskServiceServer:
    """
    持有 RiskEngine + RiskState 的常驻服务。

    状态落盘（`state_path`）不是可选项：R-K05 要求风控状态可恢复，否则一次重启
    就把 HALTED 忘掉，等于自动解除了熔断。
    """

    def __init__(self, sock_path: str, *, policy: Optional[RiskPolicy] = None,
                 state: Optional[RiskState] = None,
                 state_path: Optional[str] = None):
        self.sock_path = os.path.abspath(sock_path)
        self.policy = policy or RiskPolicy()
        self.engine = RiskEngine(self.policy)
        self.state_path = os.path.abspath(state_path) if state_path else None
        self.state = state or self._load_state() or RiskState()
        self._lock = threading.RLock()
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.requests_served = 0
        self.last_error: Optional[str] = None

    # -- 状态持久化 ------------------------------------------------------ #

    def _load_state(self) -> Optional[RiskState]:
        if not self.state_path or not os.path.exists(self.state_path):
            return None
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return None
        st = RiskState()
        st.permission_level = PermissionLevel(str(raw.get("permission_level", "NORMAL")))
        st.halted_reasons = list(raw.get("halted_reasons") or [])
        st.strategy_status = {k: StrategyRunState(v) for k, v in
                              (raw.get("strategy_status") or {}).items()}
        st.drawdown_pct = D(raw.get("drawdown_pct", 0))
        st.daily_pnl_pct = D(raw.get("daily_pnl_pct", 0))
        st.reconciliation_broken_accounts = set(raw.get("reconciliation_broken_accounts") or [])
        st.symbols_with_unknown_orders = set(raw.get("symbols_with_unknown_orders") or [])
        return st

    def _save_state(self) -> None:
        if not self.state_path:
            return
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(to_jsonable(self.state.to_dict()), fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.state_path)

    # -- 生命周期 -------------------------------------------------------- #

    def start(self) -> "RiskServiceServer":
        """后台线程启动。已有旧 socket 文件先清掉（上次异常退出的残留）。"""
        if os.path.exists(self.sock_path):
            os.unlink(self.sock_path)
        os.makedirs(os.path.dirname(self.sock_path) or ".", exist_ok=True)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.sock_path)
        self._sock.listen(64)
        self._sock.settimeout(0.5)
        os.chmod(self.sock_path, 0o600)
        self._stop.clear()
        self._thread = threading.Thread(target=self._accept_loop,
                                        name="risk-service", daemon=True)
        self._thread.start()
        return self

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._handle_conn(conn)
            except Exception as exc:                       # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _handle_conn(self, conn: socket.socket) -> None:
        conn.settimeout(5.0)
        chunks: List[bytes] = []
        while True:
            data = conn.recv(65536)
            if not data:
                break
            chunks.append(data)
            if b"\n" in data:
                break
        raw = b"".join(chunks).split(b"\n", 1)[0]
        if not raw:
            return
        try:
            request = json.loads(raw.decode("utf-8"))
            response = self.handle_request(from_jsonable(request))
        except Exception as exc:                           # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            response = {"ok": False, "error": self.last_error,
                        "error_type": type(exc).__name__}
        conn.sendall((canonical_json(response) + "\n").encode("utf-8"))
        self.requests_served += 1

    def handle_request(self, request: Mapping[str, Any]) -> Dict[str, Any]:
        """这里是唯一的业务入口。所有状态修改都在锁内完成。"""
        op = str(request.get("op", "")).strip()
        with self._lock:
            if op == "ping":
                return {"ok": True, "protocol": PROTOCOL_VERSION,
                        "ts": utcnow(), "permission_level": self.state.permission_level.value}

            if op == "check":
                intent = intent_from_payload(request["intent"])
                ctx = ctx_from_payload(request["ctx"])
                decision = self.engine.check_order(intent, ctx, self.state)
                return {"ok": True, "decision": decision_to_payload(decision)}

            if op == "state":
                return {"ok": True, "state": self.state.to_dict()}

            if op == "update_fund_state":
                self.engine.update_fund_state(
                    self.state, D(request["nav"]),
                    peak_nav=D(request["peak_nav"]) if request.get("peak_nav") else None,
                    day_start_nav=D(request["day_start_nav"])
                    if request.get("day_start_nav") else None)
                self._save_state()
                return {"ok": True, "state": self.state.to_dict()}

            if op == "set_permission":
                level = str(request["level"]).upper()
                reason = str(request.get("reason", "manual"))
                if level == PermissionLevel.NORMAL.value:
                    self.state.resume(reason)
                elif level == PermissionLevel.REDUCE_ONLY.value:
                    self.state.reduce_only(reason)
                elif level == PermissionLevel.HALTED.value:
                    self.state.halt(reason)
                else:
                    return {"ok": False, "error": f"未知权限级 {level!r}"}
                self._save_state()
                return {"ok": True, "state": self.state.to_dict()}

            if op == "set_strategy":
                try:
                    run_state = StrategyRunState(str(request["state"]).upper())
                except ValueError:
                    return {"ok": False, "error": "未知策略状态"}
                self.state.set_strategy(str(request["strategy_id"]), run_state)
                self._save_state()
                return {"ok": True, "state": self.state.to_dict()}

            if op == "set_broken_accounts":
                self.state.reconciliation_broken_accounts = set(
                    str(a) for a in request.get("accounts") or [])
                self._save_state()
                return {"ok": True, "state": self.state.to_dict()}

            if op == "freeze_symbol":
                symbol = str(request["symbol"])
                if request.get("frozen", True):
                    self.state.symbols_with_unknown_orders.add(symbol)
                else:
                    self.state.symbols_with_unknown_orders.discard(symbol)
                self._save_state()
                return {"ok": True, "state": self.state.to_dict()}

            if op == "shutdown":
                self._stop.set()
                return {"ok": True, "bye": True}

        return {"ok": False, "error": f"未知 op {op!r}"}

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        if os.path.exists(self.sock_path):
            try:
                os.unlink(self.sock_path)
            except OSError:
                pass

    def serve_forever(self) -> None:
        """前台阻塞运行（生产用）。"""
        self.start()
        try:
            while not self._stop.is_set():
                self._thread.join(timeout=0.5) if self._thread else None
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()


# --------------------------------------------------------------------------- #
#  客户端
# --------------------------------------------------------------------------- #

class RiskServiceClient:
    """
    fail-closed 客户端。`check()` 永不抛异常 —— 这是契约的一部分。

    如果哪天有人把这里改成"异常向上抛，让调用方处理"，那就等着在某次 socket
    超时后看到一笔没人审过的新仓吧。
    """

    def __init__(self, sock_path: str, *, timeout: float = 2.0):
        self.sock_path = os.path.abspath(sock_path)
        self.timeout = timeout
        self.unavailable_count = 0
        self.last_error: Optional[str] = None

    # -- 底层调用 -------------------------------------------------------- #

    def call(self, op: str, **payload) -> Dict[str, Any]:
        """成功返回响应 dict；任何通信失败抛 RiskServiceUnavailable。"""
        request = {"op": op, "protocol": PROTOCOL_VERSION, **payload}
        line = (canonical_json(request) + "\n").encode("utf-8")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.sock_path)
            sock.sendall(line)
            chunks: List[bytes] = []
            while True:
                data = sock.recv(65536)
                if not data:
                    break
                chunks.append(data)
                if b"\n" in data:
                    break
            raw = b"".join(chunks).strip()
            if not raw:
                raise RiskServiceUnavailable("风控服务返回空响应")
            try:
                decoded = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise RiskServiceUnavailable(f"风控服务响应不可解析: {exc}") from exc
            # 必须还原 Decimal/datetime 标签，否则 cap_notional 会变成 dict、
            # ts 会变成字符串，下游拿着这样的 decision 做金额比较必然出错。
            response = from_jsonable(decoded)
        except (OSError, socket.timeout) as exc:
            raise RiskServiceUnavailable(f"风控服务不可达: {exc}") from exc
        finally:
            try:
                sock.close()
            except OSError:
                pass

        if not isinstance(response, dict) or not response.get("ok"):
            err = response.get("error") if isinstance(response, dict) else response
            raise RiskServiceError(f"风控服务返回失败: {err}")
        return response

    def healthcheck(self) -> bool:
        try:
            self.call("ping")
            return True
        except RiskServiceError:
            return False

    # -- 业务方法 -------------------------------------------------------- #

    def check(self, intent: OrderIntent, ctx: RiskContext) -> RiskDecision:
        """
        **永不抛异常**。不可用时返回 REJECT + freeze 建议（fail-closed）。

        注意：这里返回的 decision 一定带 decision_id，便于在审计里区分
        "风控不可用"与"风控明确拒绝"——两者都不许下单，但排查方向完全不同。
        """
        try:
            response = self.call("check", intent=intent_to_payload(intent),
                                 ctx=ctx_to_payload(ctx))
            return decision_from_payload(response["decision"])
        except (RiskServiceError, KeyError, TypeError, ValueError) as exc:
            self.unavailable_count += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            return RiskDecision(
                decision_id=f"risk-unavail-{intent.order_intent_id}",
                layer=RiskLayer.FUND,
                policy_key="risk_service_unavailable",
                verdict=Verdict.REJECT,
                subject_id=intent.order_intent_id,
                inputs={"error": self.last_error},
                reason=("风控服务不可用，按 fail-closed 拒绝下单。"
                        "这不是策略问题，是基础设施问题：修复前不得放开新增风险。"),
                ts=ctx.now,
                halt_actions=(HaltAction.FREEZE_NEW_ORDERS.value,),
            )

    def state(self) -> Dict[str, Any]:
        return self.call("state")["state"]

    def set_permission(self, level: str, reason: str = "manual") -> Dict[str, Any]:
        return self.call("set_permission", level=level, reason=reason)["state"]

    def update_fund_state(self, nav, peak_nav=None, day_start_nav=None) -> Dict[str, Any]:
        return self.call("update_fund_state", nav=D(nav),
                         peak_nav=D(peak_nav) if peak_nav else None,
                         day_start_nav=D(day_start_nav) if day_start_nav else None)["state"]

    def freeze_symbol(self, symbol: str, frozen: bool = True) -> Dict[str, Any]:
        return self.call("freeze_symbol", symbol=symbol, frozen=frozen)["state"]

    def close(self) -> None:
        try:
            self.call("shutdown")
        except RiskServiceError:
            pass


# --------------------------------------------------------------------------- #
#  命令行入口（供故障注入与生产部署使用）
# --------------------------------------------------------------------------- #

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="QuantOS 风控独立进程")
    parser.add_argument("--socket", required=True, help="Unix socket 路径")
    parser.add_argument("--state", default=None, help="风控状态落盘路径")
    parser.add_argument("--drawdown-pause", default=None)
    parser.add_argument("--daily-loss-pause", default=None)
    args = parser.parse_args(argv)

    policy_kwargs = {}
    if args.drawdown_pause:
        policy_kwargs["drawdown_pause_pct"] = D(args.drawdown_pause)
    if args.daily_loss_pause:
        policy_kwargs["daily_loss_pause_pct"] = D(args.daily_loss_pause)
    policy = RiskPolicy(**policy_kwargs)

    server = RiskServiceServer(args.socket, policy=policy, state_path=args.state)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
