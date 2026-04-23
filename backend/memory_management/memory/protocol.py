"""
Memory plugin protocol: contract for swappable memory backends.
Implementations must provide search, add, and get_all with these signatures.
"""
from typing import Any, Dict, List, Optional, Protocol


class MemoryProvider(Protocol):
    """Interface for memory backends (e.g. Mem0, no-op)."""

    def search(self, query: str, user_id: str, limit: int = 5) -> List[str]:
        """Return memory text strings for this user matching the query."""
        ...

    def add(
        self,
        messages: List[Dict[str, str]],
        user_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Store a conversation turn for this user."""
        ...

    def get_all(self, user_id: str, limit: int = 100) -> List[str]:
        """List all stored memory texts for this user (debugging)."""
        ...
