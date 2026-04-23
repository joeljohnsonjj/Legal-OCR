"""
Phase 4 compaction: sliding window, state-centric collapse, semantic summarization.
All algorithms are in this module; can be triggered manually for testing.
"""
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from ..core.schema import SessionState
from ..services.state_merge import merge_working_state_patch

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

# Approximate chars per token for budget check
CHARS_PER_TOKEN = 4
# Audit: named constant for truncation (CQ-005)
MAX_SUMMARY_PLAIN_LENGTH = 2000


def apply_sliding_window(
    events: List[Dict[str, Any]],
    max_turns: int = 10,
) -> List[Dict[str, Any]]:
    """
    Keep last N user-assistant turns in chronological order.
    Keeps last max_turns * 2 events (user+assistant per turn); tool refs kept with recent.
    """
    if not events or max_turns <= 0:
        return []
    n = max_turns * 2
    return events[-n:] if len(events) > n else events


def apply_state_collapse(
    events: List[Dict[str, Any]],
    working_state: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    For events in the "old" portion: if content is already represented in
    working_state (e.g. hypothesis, time_window), shorten or mark for removal.
    Returns new list with redundant content shortened.
    """
    try:
        state = SessionState.model_validate(working_state or {})
    except Exception as e:
        logger.debug("State collapse validation failed, using original events: %s", e)
        return events

    out = []
    for ev in events:
        new_ev = dict(ev)
        content = (ev.get("content") or ev.get("tool_ref") or "").strip()
        if not content:
            out.append(new_ev)
            continue
        role = (ev.get("role") or "").lower()
        if role not in ("user", "assistant"):
            out.append(new_ev)
            continue

        shortened = None
        if state.investigation:
            inv = state.investigation
            for part in [inv.time_window, inv.primary_service] + (inv.incident_ids or []):
                if part and str(part) in content:
                    shortened = "[In state: investigation]"
                    break
        if not shortened and state.hypotheses:
            for h in state.hypotheses:
                if h.description and h.description.strip() in content:
                    shortened = "[In state: hypotheses]"
                    break
        if not shortened and state.progress:
            for step in (state.progress.completed_steps or [])[:3]:
                if step and step in content:
                    shortened = "[In state: progress]"
                    break

        if shortened:
            new_ev["content"] = shortened
            if "tool_ref" in new_ev:
                new_ev["tool_ref"] = None
        out.append(new_ev)
    return out


def _estimate_tokens(text_or_char_count) -> int:
    """Rough token estimate from character count (str or int)."""
    n = len(text_or_char_count) if isinstance(text_or_char_count, str) else int(text_or_char_count)
    return max(0, n // CHARS_PER_TOKEN)


@trace(name="session.compaction_run", tags=["session_manager", "compaction"])
def run_compaction(
    run_id: str,
    user_id: str,
    provider: Any,
    llm_summarize: Optional[Callable[[str], str]] = None,
    turns_threshold: int = 10,
    token_budget: Optional[int] = None,
    keep_last_n: int = 20,
) -> bool:
    """
    Run full compaction: load events, split old vs recent, collapse old,
    optionally LLM-summarize, store summary and update compaction state.
    Returns True if compaction ran and persisted; False on skip or error.
    """
    events = provider.load_events(run_id=run_id, limit=None)
    if not events:
        return False

    run_id_resolved, session = provider.get_or_create_session(run_id=run_id, user_id=user_id)
    if not session:
        return False

    working_state = provider.load_working_state(user_id=user_id, run_id=run_id_resolved)
    compaction = (working_state.get("compaction") or {}) if isinstance(working_state, dict) else {}
    turns_since = int(compaction.get("turns_since_last_compaction", 0))

    # Trigger: turns since last compaction >= K
    should_compact = turns_since >= turns_threshold
    if not should_compact and token_budget is not None:
        total_chars = sum(
            len(str(e.get("content") or "")) + len(str(e.get("tool_ref") or ""))
            for e in events
        )
        if _estimate_tokens(total_chars) > token_budget:
            should_compact = True

    if not should_compact:
        logger.info(
            "Session compaction: skipped run_id=%s turns_since=%s (threshold=%s)",
            run_id, turns_since, turns_threshold,
        )
        return False

    # Split: old = events except last keep_last_n; recent kept in DB for context
    n = min(keep_last_n, len(events))
    old_events = events[:-n] if n < len(events) else []

    collapsed_old = apply_state_collapse(old_events, working_state) if old_events else []

    # First compaction run often has 0 old events (e.g. threshold=10, keep_last_n=20 -> 20 events total).
    # Use all events for the summary so we still produce a non-empty compacted_summary.
    events_to_summarize = collapsed_old if collapsed_old else (apply_state_collapse(events, working_state) if events else [])

    def _format_events(evs: List[Dict[str, Any]]) -> str:
        parts = []
        for e in evs:
            role = e.get("role", "?")
            content = e.get("content") or e.get("tool_ref") or ""
            parts.append(f"{role}: {content}")
        return "\n".join(parts)

    summary_text: Optional[str] = None
    if events_to_summarize:
        formatted = _format_events(events_to_summarize)
        if not formatted.strip():
            summary_text = ""
        elif llm_summarize:
            prompt = (
                "Summarize this conversation in a clear, topic-by-topic way. Stay strictly factual.\n\n"
                "Rules:\n"
                "- One bullet per user question (one per distinct topic the user asked). In the order the user asked. Do not collapse several questions into one bullet; do not split one user question into several bullets (e.g. one question about benefits = one bullet for benefits, not separate bullets for health insurance, retirement, PTO).\n"
                "- For each bullet, write only what was said or answered in the conversation (1–2 sentences). No generic knowledge.\n"
                "- Do not infer or add conclusions. Skip meta-questions like 'What did I just ask?' or 'Summarize our discussion' as separate topics; focus on the actual policy/content questions.\n"
                "- Output format: one bullet per topic, each starting with the topic then a colon, then 1–2 sentences. Do NOT use 'Investigation Summary' or incident focus / time window / hypotheses.\n\n"
                "Example format (your output should look like this, with as many bullets as there are distinct topics in the conversation):\n"
                "- **Topic 1:** What was asked and what answer was given (from the conversation only).\n"
                "- **Topic 2:** What was asked and what answer was given (from the conversation only).\n"
                "- **Topic 3:** ...\n\n"
                "Conversation:\n\n"
                + formatted
            )
            try:
                summary_text = llm_summarize(prompt) or ""
            except Exception as e:
                logger.warning("LLM summarization failed: %s", e)
                summary_text = ""
        else:
            summary_text = formatted[:MAX_SUMMARY_PLAIN_LENGTH] + ("..." if len(formatted) > MAX_SUMMARY_PLAIN_LENGTH else "")
    else:
        summary_text = ""

    now_iso = datetime.now(timezone.utc).isoformat()
    compaction_patch = {
        "compaction": {
            "last_compaction_at": now_iso,
            "needs_compaction": False,
            "summary_ref": summary_text,
            "turns_since_last_compaction": 0,
        },
    }
    merge_ctx = f"user_id={user_id} run_id={run_id_resolved} (compaction)"
    merged = merge_working_state_patch(
        working_state,
        compaction_patch,
        log_context=merge_ctx,
    )
    if merged is None:
        logger.warning("Compaction state merge validation failed (%s)", merge_ctx)
        return False

    version = session.get("version", 0)
    ok_state = provider.update_working_state(
        user_id=user_id,
        run_id=run_id_resolved,
        patch=compaction_patch,
        version=version,
    )
    if not ok_state:
        logger.warning(
            "Compaction update_working_state failed (optimistic lock or DB error) run_id=%s user_id=%s version=%s",
            run_id_resolved,
            user_id,
            version,
        )
        return False

    ok_summary = provider.update_compacted_summary(
        user_id=user_id,
        run_id=run_id_resolved,
        summary=summary_text,
    )
    if not ok_summary:
        logger.warning("update_compacted_summary failed")
    else:
        preview = (summary_text or "")[:500]
        if len(summary_text or "") > 500:
            preview += "..."
        logger.info(
            "Session compaction: RAN run_id=%s user_id=%s (summarized %d events, summary_len=%d)",
            run_id_resolved, user_id, len(events_to_summarize), len(summary_text or ""),
        )
        logger.info("Session compaction: summary (see session_activity.jsonl summary_preview):\n%s", preview)
        try:
            from ..observability.activity_log import write_activity
            summary_preview = (summary_text or "")[:MAX_SUMMARY_PLAIN_LENGTH]
            if len(summary_text or "") > MAX_SUMMARY_PLAIN_LENGTH:
                summary_preview += "..."
            write_activity(
                "compaction_ran",
                run_id=run_id_resolved,
                user_id=user_id,
                summary_preview=summary_preview,
            )
        except Exception as e:
            logger.debug("Activity log write failed: %s", e)
    
    # Add metadata to trace so compaction summary shows in Opik UI (if tracing is available)
    if _HAS_OPIK and opik_context:
        try:
            opik_context.update_current_span(
                metadata={
                    "run_id": run_id_resolved,
                    "user_id": user_id,
                    "events_summarized_count": len(events_to_summarize),
                    "summary_length": len(summary_text or ""),
                    "persisted": ok_summary,
                    "summary_preview": (summary_text or "").strip()[:500] + ("..." if len(summary_text or "") > 500 else "") if summary_text and str(summary_text).strip() else "",
                }
            )
        except Exception:
            pass
    
    return ok_summary
