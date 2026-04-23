"""
Provider factory: returns the configured MemoryProvider (NoOp or Mem0).
Uses config from config_holder (set_memory_config); no app imports.
"""
from .config_holder import _get
from .mem0_provider import Mem0Provider
from .noop_provider import NoOpProvider
from .protocol import MemoryProvider


def get_memory_provider() -> MemoryProvider:
    """
    Return the configured MemoryProvider.
    - NoOpProvider when memory_enabled is False or memory_provider is "noop".
    - Mem0Provider when memory is enabled and provider is "mem0" (or unset).
    """
    if not _get("memory_enabled", True):
        return NoOpProvider()
    provider_name = (_get("memory_provider") or "mem0").strip().lower()
    if provider_name == "noop":
        return NoOpProvider()
    return Mem0Provider()
