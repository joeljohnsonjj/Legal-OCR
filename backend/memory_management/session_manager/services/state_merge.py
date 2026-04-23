"""
Deep-merge working-state patch and validate against SessionState schema (Phase 3).
Returns merged dict for persistence or None if invalid.
"""
import copy
import logging
from typing import Any, Dict, Optional

from ..core.schema import SessionState

logger = logging.getLogger(__name__)


def _deep_merge(current: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deep merge: for each key in patch, update current[key].
    Nested dicts are merged recursively; leaves and lists are replaced by patch value.
    Does not mutate current; returns a new dict.
    """
    result = copy.deepcopy(current)
    for key, patch_value in patch.items():
        if key not in result:
            result[key] = copy.deepcopy(patch_value)
        elif isinstance(patch_value, dict) and isinstance(result[key], dict):
            result[key] = _deep_merge(result[key], patch_value)
        else:
            result[key] = copy.deepcopy(patch_value)
    return result


def merge_working_state_patch(
    current: Dict[str, Any],
    patch: Dict[str, Any],
    *,
    log_context: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Deep-merge patch into current, then validate result against SessionState.
    Returns merged state as a dict (for persistence) or None if invalid
    (parse error, unknown field, wrong type).

    log_context: optional label (e.g. user_id/run_id/source) included in logs and WORKING_STATE trace rows.
    """
    merged = _deep_merge(current or {}, patch or {})
    try:
        validated = SessionState.model_validate(merged)
        return validated.model_dump()
    except Exception as e:
        ctx = f" [{log_context}]" if log_context else ""
        logger.warning("SessionState validation failed after merge%s: %s", ctx, e)
        try:
            from ..observability.working_state_trace import append_working_state_trace

            row: Dict[str, Any] = {
                "event": "merge_validation_failed",
                "error": str(e),
                "patch_keys": list(patch.keys()) if isinstance(patch, dict) else None,
                "patch_content": patch if isinstance(patch, dict) else None,
            }
            if log_context is not None:
                row["log_context"] = log_context
            append_working_state_trace(row)
        except Exception as trace_e:
            logger.debug("Working state trace append failed: %s", trace_e)
        return None
