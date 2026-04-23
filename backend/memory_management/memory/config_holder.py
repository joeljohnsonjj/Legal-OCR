"""
Config holder for the memory library (Option A: set once, use everywhere).
Consumer calls set_memory_config(settings_or_dict) once at startup; all memory code reads from here.
No dependency on the application.
"""
from typing import Any, Optional

_config: Optional[Any] = None


def set_memory_config(settings_or_dict: Any) -> None:
    """Set the config object or dict for the memory library. Call once at app startup."""
    global _config
    _config = settings_or_dict


def get_memory_config() -> Optional[Any]:
    """Return the current config (object or dict) or None if not set."""
    return _config


def _get(key: str, default: Any = None) -> Any:
    """Get a config value by key. Works for both dict and object-with-attributes."""
    c = get_memory_config()
    if c is None:
        return default
    if isinstance(c, dict):
        return c.get(key, default)
    return getattr(c, key, default)
