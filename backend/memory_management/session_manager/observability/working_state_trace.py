"""
Working state trace: append JSON lines to a dedicated file for debugging patch flow.
Set WORKING_STATE_TRACE_FILE (path) to enable; default is working_state_trace.jsonl in cwd.
"""
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_TRACE_PATH: Optional[str] = None


def get_trace_path() -> Path | None:
    """Return path to the trace file, or None if tracing is disabled."""
    global _TRACE_PATH
    if _TRACE_PATH is not None:
        return Path(_TRACE_PATH) if _TRACE_PATH else None
    path = (os.environ.get("WORKING_STATE_TRACE_FILE") or "").strip()
    if not path:
        path = "working_state_trace.jsonl"
    if not path or path.lower() in ("0", "false", "off"):
        _TRACE_PATH = ""
        return None
    _TRACE_PATH = path
    return Path(path)


def append_working_state_trace(entry: Dict[str, Any]) -> None:
    """
    Append one JSON object (one line) to the working state trace file.
    Safe to call from any thread; does not raise.
    """
    path = get_trace_path()
    if path is None:
        return
    if "ts" not in entry:
        entry = {**entry, "ts": datetime.now(timezone.utc).isoformat()}
    try:
        line = json.dumps(entry, default=str) + "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        logger.debug("Working state trace write failed: %s", e)
