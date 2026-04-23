"""
Mem0-backed memory provider: delegates to adapter (get_memory, search, add, get_all).
Uses config from config_holder; all logic remains in adapter.
"""
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class Mem0Provider:
    """
    MemoryProvider implementation backed by Mem0 via adapter.
    All logic (init, patch, config) remains in adapter; this only exposes the protocol.
    """

    def search(self, query: str, user_id: str, limit: int = 5) -> List[str]:
        from .adapter import search as adapter_search

        logger.info(
            "[Memory][Mem0Provider] delegate search user_id=%r limit=%d",
            user_id,
            limit,
        )
        return adapter_search(query=query, user_id=user_id, limit=limit)

    def add(
        self,
        messages: List[Dict[str, str]],
        user_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        from .adapter import add as adapter_add

        logger.info(
            "[Memory][Mem0Provider] delegate add user_id=%r messages=%d",
            user_id,
            len(messages),
        )
        adapter_add(messages, user_id=user_id, metadata=metadata)

    def get_all(self, user_id: str, limit: int = 100) -> List[str]:
        from .adapter import get_all as adapter_get_all

        logger.info("[Memory][Mem0Provider] delegate get_all user_id=%r limit=%d", user_id, limit)
        return adapter_get_all(user_id=user_id, limit=limit)
