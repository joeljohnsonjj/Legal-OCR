"""
Memory subpackage: pluggable providers (Mem0, no-op) via get_memory_provider().
Config via set_memory_config(settings_or_dict) once at startup (Option A).
"""
from .adapter import add, clear_memory_index, get_all, get_memory, search
from .config_holder import get_memory_config, set_memory_config
from .provider_factory import get_memory_provider
from .protocol import MemoryProvider

__all__ = [
    "add",
    "clear_memory_index",
    "get_all",
    "get_memory",
    "get_memory_config",
    "get_memory_provider",
    "MemoryProvider",
    "search",
    "set_memory_config",
]
