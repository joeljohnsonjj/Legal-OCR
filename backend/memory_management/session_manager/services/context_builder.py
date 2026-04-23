"""
Phase 4 context builder: session context string for the LLM (summary + recent messages + state).
Compaction is deferred: when threshold is exceeded we mark needs_compaction and return a flag
so the caller can enqueue a background compaction task; the request uses existing summary + events.
"""
import logging
from typing import Any, Dict, List, Optional, Tuple

from .state_merge import merge_working_state_patch
from ..workers.compaction import apply_sliding_window

logger = logging.getLogger(__name__)

# Optional Opik tracing support - gracefully degrade if not available
try:
    from opik import opik_context
    from opik_template import trace
    _HAS_OPIK = True
except ImportError:
    _HAS_OPIK = False
    opik_context = None
    # Identity decorator when tracing is not available
    def trace(*args, **kwargs):
        def decorator(func):
            return func
        return decorator

# Type alias: (context_str, compaction_queued, run_id_for_compaction, user_id_for_compaction)
BuildContextResult = Tuple[str, bool, Optional[str], Optional[str]]


@trace(name="session.build_context", tags=["session_manager", "context"])
def build_session_context(
    run_id: str,
    user_id: str,
    provider: Any,
    compaction_config: Optional[Dict[str, Any]] = None,
    pipeline_trace: Optional[List[Dict[str, Any]]] = None,
    session: Optional[Dict[str, Any]] = None,
) -> BuildContextResult:
    """
    Load session, load events, apply sliding window. When compaction would be triggered
    (turns_since >= threshold), mark session needs_compaction and return compaction_queued=True
    so the caller can enqueue a background task; do NOT run compaction in the request path.
    Build context from existing summary + recent events + working state.
    If session is provided, use it and run_id as-is (do not call get_or_create_session again).
    Returns (context_str, compaction_queued, run_id_for_compaction, user_id_for_compaction).
    """
    compaction_config = compaction_config or {}
    max_turns = int(compaction_config.get("max_turns", 10))
    turns_threshold = int(compaction_config.get("turns_threshold", 10))
    token_budget = compaction_config.get("token_budget")
    keep_last_n = int(compaction_config.get("keep_last_n", 20))

    if session is not None:
        run_id_resolved = run_id
        # Use pre-fetched session; do NOT call get_or_create_session (single source of truth in orchestrator)
    else:
        run_id_resolved, session = provider.get_or_create_session(run_id=run_id, user_id=user_id)
    if not session:
        return ("", False, None, None)

    events = provider.load_events(run_id=run_id_resolved, limit=None)
    events = apply_sliding_window(events, max_turns=max_turns)

    working_state = session.get("working_state") if session else {}
    if not isinstance(working_state, dict):
        working_state = {}
    compaction = (working_state.get("compaction") or {}) if isinstance(working_state, dict) else {}
    turns_since = int(compaction.get("turns_since_last_compaction", 0))
    needs_compaction_already = bool(compaction.get("needs_compaction", False))

    # Check if compaction would be triggered; if so, defer to background (mark + return flag)
    compaction_queued = False
    if compaction_config.get("compaction_enabled", True):
        should_compact = turns_since >= turns_threshold
        if not should_compact and token_budget is not None and events:
            from ..workers.compaction import _estimate_tokens
            total_chars = sum(
                len(str(e.get("content") or "")) + len(str(e.get("tool_ref") or ""))
                for e in events
            )
            if _estimate_tokens(total_chars) > token_budget:
                should_compact = True

        if should_compact:
            if needs_compaction_already:
                # Already marked; background task may be running. Do not enqueue again.
                logger.info(
                    "Session context: compaction already pending run_id=%s (using existing summary)",
                    run_id_resolved,
                )
            else:
                # Mark session so we don't enqueue twice; caller will enqueue background compaction
                patch = {"compaction": {"needs_compaction": True}}
                merged = merge_working_state_patch(working_state, patch)
                if merged is not None:
                    version = session.get("version", 0)
                    if provider.update_working_state(
                        user_id=user_id,
                        run_id=run_id_resolved,
                        patch=merged,
                        version=version,
                    ):
                        compaction_queued = True
                        logger.info(
                            "Session context: deferred compaction run_id=%s user_id=%s (enqueue background)",
                            run_id_resolved,
                            user_id,
                        )

        if pipeline_trace is not None:
            pipeline_trace.append({
                "step": "compaction_deferred",
                "input": {"run_id": run_id_resolved, "user_id": user_id},
                "output": {"compaction_queued": compaction_queued, "needs_compaction_already": needs_compaction_already},
            })

    # Build context from existing summary + events + working state (no compaction in request path)
    summary = session.get("compacted_summary") if session else None
    parts = []

    if summary and str(summary).strip():
        parts.append("--- Previous conversation summary ---")
        parts.append(str(summary).strip())
        parts.append("--- End summary ---")

    if events:
        parts.append("--- Recent messages ---")
        for ev in events:
            role = ev.get("role", "?")
            content = ev.get("content") or ev.get("tool_ref") or ""
            parts.append(f"{role}: {content[:500]}{'...' if len(str(content)) > 500 else ''}")
        parts.append("--- End recent messages ---")

    if working_state and isinstance(working_state, dict):
        parts.append("--- Current working state ---")
        parts.append(str(working_state))
        parts.append("--- End working state ---")

    out = "\n".join(parts) if parts else ""
    logger.info(
        "Session context built run_id=%s summary_len=%d events_count=%d working_state_keys=%s total_context_len=%d compaction_queued=%s",
        run_id_resolved,
        len(str(summary).strip()) if summary else 0,
        len(events) if events else 0,
        list(working_state.keys()) if isinstance(working_state, dict) and working_state else [],
        len(out),
        compaction_queued,
    )
    run_id_for_task = run_id_resolved if compaction_queued else None
    user_id_for_task = user_id if compaction_queued else None
    return (out, compaction_queued, run_id_for_task, user_id_for_task)
