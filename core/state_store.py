"""
Persistent state store
======================
Small atomic JSON store used for anything that must survive a process restart:

  - risk tracking (entry price, trailing-stop peak, portfolio peak)
  - stop-loss cooldowns
  - daily trade counters and the intraday order book
  - scheduler run marks

Why this exists: all of the above used to live in plain Python dicts, so every
process restart silently reset the risk system (stop-loss baselines, drawdown
circuit breaker, daily trade limits). On a machine where launchd restarts the
service after a crash or a reboot, that is a correctness bug, not a nicety.

Writes are atomic (temp file + os.replace) so a crash mid-write cannot leave a
half-written state file that would be silently treated as "empty".
"""

import json
import logging
import os
import tempfile
import threading
from typing import Any, Dict

logger = logging.getLogger(__name__)


class StateStore:
    """Atomic, crash-safe JSON document store."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)

    # ------------------------------------------------------------------ #
    #  Basic IO
    # ------------------------------------------------------------------ #

    def load(self) -> Dict[str, Any]:
        """Return the whole document. Missing or corrupt file -> empty dict."""
        with self._lock:
            if not os.path.exists(self.path):
                return {}
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    raise ValueError("state root is not an object")
                return data
            except (json.JSONDecodeError, ValueError, OSError) as exc:
                logger.error("State file %s is unreadable (%s) — quarantining", self.path, exc)
                self._quarantine()
                return {}

    def save(self, data: Dict[str, Any]) -> None:
        """Atomically replace the document."""
        with self._lock:
            directory = os.path.dirname(self.path) or "."
            os.makedirs(directory, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".state_", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2, default=str)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, self.path)
            except Exception:
                if os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                raise

    def update(self, **changes) -> Dict[str, Any]:
        """Read-modify-write a top-level key set and return the new document."""
        with self._lock:
            data = self.load()
            data.update(changes)
            self.save(data)
            return data

    def get(self, key: str, default: Any = None) -> Any:
        return self.load().get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.update(**{key: value})

    def _quarantine(self) -> None:
        """Keep a corrupt file for forensics instead of overwriting it silently."""
        try:
            os.replace(self.path, f"{self.path}.corrupt")
        except OSError:
            pass
