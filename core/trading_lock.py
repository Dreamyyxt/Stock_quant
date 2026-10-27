"""
Single-instance guard
=====================
Prevents two processes from trading the same account at the same time.

The old failure mode was very real: launchd keeps `main.py --mode live` alive,
and a manual `python main.py --mode rebalance` (or a second launchd Load) would
run a second rebalance against the same account. Both would read the same
positions, both would compute the same deltas, and both would place orders —
doubling every trade.

Use a dedicated lock file per account so different accounts can still run in
parallel if that is ever needed.
"""

import fcntl
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


class SingleInstanceLock:
    """Exclusive advisory lock backed by flock(2)."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._fh: Optional[object] = None

    def acquire(self) -> bool:
        """Try to take the lock without blocking. Returns False if already held."""
        if self._fh is not None:
            return True
        fh = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            owner = self._read_owner(fh)
            logger.error(
                "Another trading process already holds %s%s — refusing to start",
                self.path, f" (pid {owner})" if owner else "",
            )
            fh.close()
            return False

        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        os.fsync(fh.fileno())
        self._fh = fh
        logger.info("Acquired trading lock %s (pid=%d)", self.path, os.getpid())
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            try:
                self._fh.close()
            finally:
                self._fh = None

    @staticmethod
    def _read_owner(fh) -> str:
        try:
            fh.seek(0)
            return fh.read().strip()
        except OSError:
            return ""

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError(f"Trading lock is already held: {self.path}")
        return self

    def __exit__(self, *exc_info):
        self.release()
        return False
