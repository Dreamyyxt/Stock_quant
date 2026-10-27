"""
Structured event logger for trading activity.
"""

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict

logger = logging.getLogger(__name__)


class EventLogger:
    """Append structured trading events to a JSONL file."""

    def __init__(self, log_dir: str = "logs", filename: str = "trade_events.jsonl"):
        self.log_dir = log_dir
        self.path = os.path.join(log_dir, filename)
        os.makedirs(log_dir, exist_ok=True)

    def log(self, event_type: str, **payload: Any) -> Dict[str, Any]:
        event = {
            "ts": datetime.now().astimezone().isoformat(),
            "type": event_type,
            **payload,
        }
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.warning("Failed to write trade event %s: %s", event_type, exc)
        return event
