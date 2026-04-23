"""
No-op session provider: all methods are no-ops or return empty/None/False.
Used when SESSION_ENABLED=false or SESSION_PROVIDER=noop.
"""
import uuid
from typing import Any, Dict, List, Optional, Tuple


class NoOpSessionProvider:
    """SessionProvider that does not store or return any session data."""

    def get_or_create_session(
        self,
        run_id: Optional[str],
        user_id: str,
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """Return (run_id or new uuid, None)."""
        return (run_id or str(uuid.uuid4()), None)

    def append_user_event(
        self, run_id: str, content: str, *, user_id: Optional[str] = None
    ) -> None:
        """No-op."""
        pass

    def append_assistant_event(
        self,
        run_id: str,
        content: str,
        tool_ref: Optional[str] = None,
        *,
        user_id: Optional[str] = None,
    ) -> None:
        """No-op."""
        pass

    def load_events(
        self,
        run_id: str,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return empty list."""
        return []

    def load_working_state(self, user_id: str, run_id: str) -> Dict[str, Any]:
        """Return empty dict."""
        return {}

    def update_working_state(
        self,
        user_id: str,
        run_id: str,
        patch: Dict[str, Any],
        version: int,
    ) -> bool:
        """Return False."""
        return False

    def refresh_activity(self, user_id: str, run_id: str) -> None:
        """No-op."""
        pass

    def update_compacted_summary(
        self, user_id: str, run_id: str, summary: Optional[str]
    ) -> bool:
        """No-op."""
        return False

    def build_session_context(
        self,
        run_id: str,
        user_id: str,
        last_n: int = 10,
    ) -> str:
        """Return empty string."""
        return ""
