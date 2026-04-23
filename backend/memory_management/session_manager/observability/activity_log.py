"""
Append session/compaction/cleanup events to logs/session_activity.jsonl (one JSON object per line).
Useful for inspecting what happened without scraping text logs.
"""
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict

ACTIVITY_LOG_DIR = "logs"
ACTIVITY_LOG_FILE = "session_activity.jsonl"

logger = logging.getLogger(__name__)


def write_activity(event_type: str, **payload: Any) -> None:
    """
    Append one event as a JSON line to logs/session_activity.jsonl.

    event_type: e.g. session_loaded, compaction_turn, compaction_ran, cleanup_run.
    payload: any JSON-serializable keys (run_id, user_id, processed, etc.).
    Do not pass secrets (e.g. password, token, api_key) in payload; they are written to disk.
    On write failure, the exception is suppressed so that request/cleanup does not fail.
    """
    record: Dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
        **payload,
    }
    try:
        os.makedirs(ACTIVITY_LOG_DIR, exist_ok=True)
        path = os.path.join(ACTIVITY_LOG_DIR, ACTIVITY_LOG_FILE)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug("Activity log write failed: %s", e)
