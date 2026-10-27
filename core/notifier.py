"""
Notifier
========
One place to raise an alert that a human will actually see.

Channels (all optional, all best-effort — a failing channel never breaks trading):
  1. Log record (always)
  2. Append-only audit trail at <state_dir>/alerts.jsonl
  3. Webhook (Slack / 企业微信 / 飞书 / 任意 HTTP POST) via ALERT_WEBHOOK_URL
  4. macOS desktop notification via osascript (local dev convenience)

Rationale: previously a stop-loss or a rejected order only produced one log
line in a file nobody tails. For live trading that is equivalent to no alert.
"""

import json
import logging
import os
import platform
import subprocess
import time
import urllib.request
from datetime import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Don't spam the same alert over and over inside one cycle.
_DEDUPE_WINDOW_SECONDS = 300
# Module-level so a batch process that builds one Notifier per session still
# dedupes: replaying 120 sessions must not mean 120 identical pop-ups.
_RECENT: Dict[str, float] = {}


class Notifier:
    def __init__(self,
                 webhook_url: Optional[str] = None,
                 state_dir: str = "state",
                 desktop: bool = True,
                 timeout: float = 5.0):
        self.webhook_url = webhook_url or os.getenv("ALERT_WEBHOOK_URL", "").strip()
        self.state_dir = state_dir
        # ALERT_DESKTOP=0 forces quiet mode regardless of what the caller asked.
        env_flag = os.getenv("ALERT_DESKTOP", "").strip().lower()
        if env_flag in ("0", "false", "no", "off"):
            desktop = False
        self.desktop = desktop and platform.system() == "Darwin"
        self.timeout = timeout
        self._recent = _RECENT
        os.makedirs(state_dir, exist_ok=True)
        self.audit_path = os.path.join(state_dir, "alerts.jsonl")

    # ------------------------------------------------------------------ #
    #  Public API
    # ------------------------------------------------------------------ #

    def info(self, title: str, message: str, **context):
        self.send("info", title, message, **context)

    def warning(self, title: str, message: str, **context):
        self.send("warning", title, message, **context)

    def critical(self, title: str, message: str, **context):
        self.send("critical", title, message, **context)

    def send(self, level: str, title: str, message: str, **context) -> None:
        key = f"{level}:{title}"
        now = time.time()
        if now - self._recent.get(key, 0) < _DEDUPE_WINDOW_SECONDS:
            logger.debug("Alert suppressed (dedupe): %s", title)
            return
        self._recent[key] = now

        event = {
            "ts": datetime.now().astimezone().isoformat(),
            "level": level,
            "title": title,
            "message": message,
            "context": context,
        }

        log_fn = {
            "info": logger.info,
            "warning": logger.warning,
            "critical": logger.error,
        }.get(level, logger.info)
        log_fn("[ALERT/%s] %s — %s", level.upper(), title, message)

        self._write_audit(event)
        if level in ("warning", "critical"):
            self._post_webhook(event)
            self._desktop_notify(level, title, message)

    # ------------------------------------------------------------------ #
    #  Channels
    # ------------------------------------------------------------------ #

    def _write_audit(self, event: Dict[str, Any]) -> None:
        try:
            with open(self.audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:
            logger.debug("Could not append alert audit: %s", exc)

    def _post_webhook(self, event: Dict[str, Any]) -> None:
        if not self.webhook_url:
            return
        body = json.dumps({
            "msgtype": "text",
            "text": {"content": f"[{event['level'].upper()}] {event['title']}\n{event['message']}"},
            "title": event["title"],
            "message": event["message"],
            "level": event["level"],
            "ts": event["ts"],
            "context": event["context"],
        }, ensure_ascii=False, default=str).encode("utf-8")
        req = urllib.request.Request(
            self.webhook_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout):
                pass
        except Exception as exc:
            logger.debug("Webhook alert failed: %s", exc)

    def _desktop_notify(self, level: str, title: str, message: str) -> None:
        if not self.desktop:
            return
        script = (
            f'display notification {json.dumps(message[:180])} '
            f'with title {json.dumps("Stock Quant")} '
            f'subtitle {json.dumps(title[:80])}'
        )
        try:
            subprocess.run(
                ["osascript", "-e", script],
                check=False, capture_output=True, timeout=self.timeout,
            )
        except Exception as exc:
            logger.debug("Desktop notification failed: %s", exc)
