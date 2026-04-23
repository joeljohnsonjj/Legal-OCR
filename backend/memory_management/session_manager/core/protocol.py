"""
Session plugin protocol: contract for swappable session backends.
Implementations must provide get_or_create_session, append events, load_events, working_state, refresh, and optional build_session_context.
"""
from typing import Any, Dict, List, Optional, Protocol, Tuple


class SessionProvider(Protocol):
    """Interface for session backends (e.g. Postgres, no-op)."""

    def get_or_create_session(
        self,
        run_id: Optional[str],
        user_id: str,
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """Return (run_id, session_dict). Session dict is None if provider is no-op or session not found."""
        ...

    def append_user_event(
        self, run_id: str, content: str, *, user_id: Optional[str] = None
    ) -> None:
        """Append a user message event. user_id optional (Phase 3: for session_conversation_turns)."""
        ...

    def append_assistant_event(
        self,
        run_id: str,
        content: str,
        tool_ref: Optional[str] = None,
        *,
        user_id: Optional[str] = None,
    ) -> None:
        """Append an assistant message event. user_id optional (Phase 3: for session_conversation_turns)."""
        ...

    def load_events(
        self,
        run_id: str,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return events for run_id, optionally last N. No-op returns []."""
        ...

    def load_working_state(self, user_id: str, run_id: str) -> Dict[str, Any]:
        """Return working_state dict for the session. No-op returns {}."""
        ...

    def update_working_state(
        self,
        user_id: str,
        run_id: str,
        patch: Dict[str, Any],
        version: int,
    ) -> bool:
        """Merge patch into working state and persist. Returns True if updated. No-op returns False."""
        ...

    def refresh_activity(self, user_id: str, run_id: str) -> None:
        """Update last_activity_at. No-op if disabled."""
        ...

    def update_compacted_summary(
        self, user_id: str, run_id: str, summary: Optional[str]
    ) -> bool:
        """Persist compacted_summary for the session. No-op returns False."""
        ...

    def build_session_context(
        self,
        run_id: str,
        user_id: str,
        last_n: int = 10,
    ) -> str:
        """Build session context string for LLM (events + working state). No-op returns ''."""
        ...
