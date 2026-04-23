"""
No-op memory provider: search and get_all return empty list; add does nothing.
Used when memory is disabled or MEMORY_PROVIDER=noop.
"""
from typing import Any, Dict, List, Optional


class NoOpProvider:
    """MemoryProvider that does not store or return any memories."""

    def search(self, query: str, user_id: str, limit: int = 5) -> List[str]:
        """Return empty list."""
        return []

    def add(
        self,
        messages: List[Dict[str, str]],
        user_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """No-op."""
        pass

    def get_all(self, user_id: str, limit: int = 100) -> List[str]:
        """Return empty list."""
        return []
