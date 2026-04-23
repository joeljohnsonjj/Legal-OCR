"""
Session DB latency instrumentation: per-operation timing and per-request totals.
Set request context at the start of a request; each DB call records operation + duration;
at end of request log total DB calls and total DB time.
"""
import contextvars
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Request-scoped: list of {"operation": str, "duration_ms": float, "run_id": str|None, "user_id": str|None}
_session_db_metrics: contextvars.ContextVar[Optional[List[Dict[str, Any]]]] = contextvars.ContextVar(
    "session_db_metrics", default=None
)


def set_request_db_context(request_id: str) -> None:
    """Set context for the current request so DB ops are recorded. Call at start of request."""
    _session_db_metrics.set({"request_id": request_id, "metrics": []})


def get_and_clear_request_db_context() -> Optional[Dict[str, Any]]:
    """
    Return current request's DB metrics (request_id + list of ops) and clear context.
    Call at end of request to log total DB calls and total duration.
    """
    try:
        ctx = _session_db_metrics.get()
        if ctx is None:
            return None
        _session_db_metrics.set(None)
        return ctx
    except LookupError:
        return None


def record_session_db_op(
    operation: str,
    duration_ms: float,
    run_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> None:
    """Record one DB operation for latency tracking and append to request-scoped list."""
    logger.info(
        "session_db op=%s duration_ms=%s run_id=%s user_id=%s",
        operation, round(duration_ms, 2), run_id, user_id,
    )
    try:
        ctx = _session_db_metrics.get()
        if isinstance(ctx, dict) and "metrics" in ctx:
            ctx["metrics"].append({
                "operation": operation,
                "duration_ms": round(duration_ms, 2),
                "run_id": run_id,
                "user_id": user_id,
            })
    except LookupError:
        pass
