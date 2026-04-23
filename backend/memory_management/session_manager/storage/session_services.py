"""
Session database service: CRUD for sessions and session_events tables.
Used by the session plugin (PostgresSessionProvider). Phase 2: delegates to SessionBackend (default uses db_manager).
"""
import logging
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

import psycopg2.extras

from .backend import SessionBackend

logger = logging.getLogger(__name__)

# Phase 5: No db_manager import. App must call set_default_backend() at startup.
_default_backend: Optional[SessionBackend] = None


def set_default_backend(backend: SessionBackend) -> None:
    """Set the default session backend (call at app startup with SessionBackend(db_manager.get_connection))."""
    global _default_backend
    _default_backend = backend


def _get_default_backend() -> SessionBackend:
    """Return the default backend; raise if not set (app must call set_default_backend at startup)."""
    if _default_backend is None:
        raise RuntimeError(
            "Session backend not configured. Call session_services.set_default_backend(SessionBackend(get_connection)) at app startup."
        )
    return _default_backend


def create_session(
    run_id: str,
    user_id: str,
    ttl_seconds: int = 1800,
) -> bool:
    """Insert into session_registry and session_working_state (Phase 1: new tables only). Returns True on success."""
    return _get_default_backend().create_session(run_id=run_id, user_id=user_id, ttl_seconds=ttl_seconds)


def get_session(user_id: str, run_id: str) -> Optional[Dict[str, Any]]:
    """
    Select session by (user_id, run_id). Phase 8: new tables only (session_working_state + session_registry).
    Return None if not found or expired (TTL).
    """
    return _get_default_backend().get_session(user_id=user_id, run_id=run_id)


def get_session_ignore_ttl(user_id: str, run_id: str) -> Optional[Dict[str, Any]]:
    """
    Select session by (user_id, run_id) without TTL check. Phase 8: new tables only (active only).
    Exclude status='archived' so get_session_or_archive returns archive via get_archive_by_run_id.
    """
    return _get_default_backend().get_session_ignore_ttl(user_id=user_id, run_id=run_id)


def list_sessions_for_user(
    user_id: str,
    limit: int = 50,
    include_expired: bool = False,
) -> List[Dict[str, Any]]:
    """
    List sessions for user_id, most recent first. Phase 8: session_working_state + session_registry only.
    """
    return _get_default_backend().list_sessions_for_user(
        user_id=user_id, limit=limit, include_expired=include_expired
    )


def update_compacted_summary(user_id: str, run_id: str, summary: Optional[str]) -> bool:
    """Update compacted_summary. Phase 8: session_working_state only."""
    return _get_default_backend().update_compacted_summary(
        user_id=user_id, run_id=run_id, summary=summary
    )


def update_session_activity(user_id: str, run_id: str) -> bool:
    """Update last_activity_at. Phase 8: session_working_state only."""
    return _get_default_backend().update_session_activity(user_id=user_id, run_id=run_id)


def update_session_working_state(
    user_id: str,
    run_id: str,
    working_state: Dict[str, Any],
    version: int,
) -> bool:
    """
    Update working_state with optimistic lock. Phase 8: session_working_state only.
    Returns True if exactly one row was updated.
    """
    return _get_default_backend().update_session_working_state(
        user_id=user_id, run_id=run_id, working_state=working_state, version=version
    )


def append_session_event(
    run_id: str,
    seq: int,
    role: str,
    content: Optional[str] = None,
    tool_ref: Optional[str] = None,
    user_id: Optional[str] = None,
) -> bool:
    """
    Insert one row into session_conversation_turns. Phase 8: user_id required (no fallback to session_events).
    Returns False if user_id is None (event not stored).
    """
    return _get_default_backend().append_session_event(
        run_id=run_id,
        seq=seq,
        role=role,
        content=content,
        tool_ref=tool_ref,
        user_id=user_id,
    )


def get_session_events(
    run_id: str,
    limit: Optional[int] = None,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """
    Select events for run_id ordered by seq. Phase 8: session_conversation_turns only.
    Returns list of dicts with seq, role, content, tool_ref, created_at.
    """
    return _get_default_backend().get_session_events(
        run_id=run_id, limit=limit, offset=offset
    )


def get_next_seq(run_id: str) -> int:
    """Return next seq for run_id. Phase 8: session_conversation_turns only."""
    return _get_default_backend().get_next_seq(run_id=run_id)


def copy_events(
    from_run_id: str,
    to_run_id: str,
    to_user_id: Optional[str] = None,
) -> int:
    """
    Copy all events from from_run_id to to_run_id (new seq 1, 2, 3, ...).
    Phase 8: session_conversation_turns only; to_user_id required (no fallback to session_events).
    Returns number of events copied; 0 if to_user_id is None.
    """
    return _get_default_backend().copy_events(
        from_run_id=from_run_id, to_run_id=to_run_id, to_user_id=to_user_id
    )


def get_session_event_count(run_id: str) -> int:
    """Return count of events for run_id. Phase 8: session_conversation_turns only."""
    return _get_default_backend().get_session_event_count(run_id=run_id)


def delete_session_events_older_than(run_id: str, keep_last_n: int) -> int:
    """
    Delete events for run_id except the last keep_last_n (by seq). Phase 8: session_conversation_turns only.
    Returns number of rows deleted.
    """
    return _get_default_backend().delete_session_events_older_than(
        run_id=run_id, keep_last_n=keep_last_n
    )


# ---------------------------------------------------------------------------
# Phase 2: Session archive (archive on expiry, clear session row only)
# ---------------------------------------------------------------------------


def insert_session_archive(
    user_id: str,
    run_id: str,
    compacted_summary: Optional[str],
    working_state: Dict[str, Any],
    last_activity_at: Any,
) -> bool:
    """
    Insert one row into session_archive.
    Used when archiving an expired session. Returns True on success.
    On duplicate run_id (already archived), returns False (caller may skip).
    """
    return _get_default_backend().insert_session_archive(
        user_id=user_id,
        run_id=run_id,
        compacted_summary=compacted_summary,
        working_state=working_state,
        last_activity_at=last_activity_at,
    )


def list_expired_sessions(limit: int = 100) -> List[Dict[str, Any]]:
    """
    List sessions where last_activity_at + ttl_seconds < NOW(). Phase 8: session_working_state only.
    Order by last_activity_at ASC (oldest first) so cleanup is deterministic.
    """
    return _get_default_backend().list_expired_sessions(limit=limit)


def _format_events_for_summary(events: List[Dict[str, Any]]) -> str:
    """Format events as 'role: content' lines for summarization. Returns a single string."""
    parts = []
    for e in events:
        role = e.get("role", "?")
        content = e.get("content") or e.get("tool_ref") or ""
        parts.append(f"{role}: {content}")
    return "\n".join(parts)


def summarize_events_for_archive(
    run_id: str,
    llm_summarize: Optional[Callable[[str], str]] = None,
) -> Optional[str]:
    """
    Load all events for run_id, format them, and return a summary.
    If llm_summarize is provided, call it with the formatted text; else truncate to 2000 chars.
    Returns None on error or if no events.
    """
    return _get_default_backend().summarize_events_for_archive(
        run_id=run_id, llm_summarize=llm_summarize
    )


def archive_and_clear_session(
    run_id: str,
    user_id: str,
    summary: Optional[str],
    working_state: Dict[str, Any],
    last_activity_at: Any,
    llm_summarize: Optional[Callable[[str], str]] = None,
) -> bool:
    """
    Phase 8: Soft delete only. If run is in session_working_state (status='active'), update
    session_registry.final_summary and session_working_state status='archived'. Else return False.
    Events are never deleted.
    """
    return _get_default_backend().archive_and_clear_session(
        run_id=run_id,
        user_id=user_id,
        summary=summary,
        working_state=working_state,
        last_activity_at=last_activity_at,
        llm_summarize=llm_summarize,
    )


def archive_expired_sessions(
    limit: int = 100,
    llm_summarize: Optional[Callable[[str], str]] = None,
) -> int:
    """
    For each expired session: archive summary + working_state to session_archive,
    then delete the session row only (session_events kept). Returns number of sessions archived.
    Archive row uses a summary from ALL events (via summarize_events_for_archive) so the archive
    reflects the full conversation; the session's compacted_summary is only the "old" slice from compaction.
    """
    return _get_default_backend().archive_expired_sessions(
        limit=limit, llm_summarize=llm_summarize
    )


# ---------------------------------------------------------------------------
# Phase 3: List and get session – include archive (merged list, get_session_or_archive)
# ---------------------------------------------------------------------------


def list_archive_for_user(
    user_id: str,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """
    List archived conversations. Phase 8: session_working_state (status='archived') only.
    Returns list of dicts with run_id, last_activity_at, archived_at.
    """
    return _get_default_backend().list_archive_for_user(user_id=user_id, limit=limit)


def list_conversations_for_user(
    user_id: str,
    limit: int = 50,
    include_expired: bool = False,
) -> List[Dict[str, Any]]:
    """
    Unified list of all conversations (active + archived) for user_id. Phase 8: new tables only.
    session_registry JOIN session_working_state; source='session' if active, 'archive' if archived.
    Same return shape: run_id, last_activity_at, source, created_at. Sorted by last_activity_at DESC.
    """
    return _get_default_backend().list_conversations_for_user(
        user_id=user_id, limit=limit, include_expired=include_expired
    )


def get_archive_by_run_id(user_id: str, run_id: str) -> Optional[Dict[str, Any]]:
    """
    Return archived row for (user_id, run_id). Phase 8: session_working_state (status='archived') only,
    with registry.final_summary as compacted_summary. Same dict shape: run_id, user_id, compacted_summary,
    working_state, last_activity_at, archived_at.
    """
    return _get_default_backend().get_archive_by_run_id(user_id=user_id, run_id=run_id)


def get_session_or_archive(
    user_id: str,
    run_id: str,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    Get session metadata from sessions table or session_archive.
    Returns (session_like_dict, "session" | "archive" | "none").
    For archive, returns dict with run_id, user_id, working_state, compacted_summary,
    last_activity_at, and archived_at (so callers know it's archived). Events always
    come from session_events (unchanged).
    """
    return _get_default_backend().get_session_or_archive(user_id=user_id, run_id=run_id)


def continue_from_archived_session(
    user_id: str,
    from_run_id: str,
    ttl_seconds: int = 1800,
) -> Optional[str]:
    """
    Create a new session seeded from an archived conversation (Phase 4 Continue).
    Copies compacted_summary, working_state, and all events from archive + session_events.
    Returns new run_id or None if from_run_id is not in archive.
    """
    return _get_default_backend().continue_from_archived_session(
        user_id=user_id, from_run_id=from_run_id, ttl_seconds=ttl_seconds
    )


def delete_expired_sessions() -> int:
    """
    Phase 8: Soft-delete expired rows in session_working_state only. Returns count of rows updated.
    Does NOT delete events. Prefer archive_expired_sessions() to write summary/state.
    """
    return _get_default_backend().delete_expired_sessions()


def delete_all_sessions() -> int:
    """
    Phase 8: Delete all session data (new tables only). Order: session_conversation_turns,
    session_working_state, session_registry. Returns number of registry rows deleted.
    """
    return _get_default_backend().delete_all_sessions()


def index_sessions_last_activity_exists() -> bool:
    """
    Return True if idx_sessions_last_activity exists on sessions (Phase E).
    Used by tests and ops to verify the cleanup expiry index is present.
    """
    return _get_default_backend().index_sessions_last_activity_exists()


def index_session_working_state_last_activity_exists() -> bool:
    """
    Phase 7: Return True if idx_session_working_state_last_activity exists on
    session_working_state. Used by tests and ops to verify the expiry index is present.
    """
    return _get_default_backend().index_session_working_state_last_activity_exists()
