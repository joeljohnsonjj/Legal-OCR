"""
Session and compaction config object (library contract).
Phase 2 of SESSION_CONFIG_AND_LIBRARY_PLAN: SessionConfig + session_config_from_settings / from_env.
Compaction summarizer is not part of SessionConfig; the app injects it when building compaction_config.
"""
import os
from typing import Any, Optional

from pydantic import BaseModel, Field


class SessionConfig(BaseModel):
    """
    Session and compaction knobs. Build from Settings (this app) or from env/code (library use).
    Summarizer for compaction is injected by the app; not stored here.
    """

    # Session
    enabled: bool = Field(True, description="Master switch for session layer.")
    provider: str = Field("postgres", description="Session backend: 'postgres', 'sqlite', or 'noop'.")
    db_path: Optional[str] = Field(None, description="SQLite db file path when provider='sqlite'.")
    ttl_seconds: int = Field(1800, gt=0, description="Session idle TTL in seconds.")
    cleanup_interval_seconds: int = Field(300, gt=0, description="Cleanup loop interval in seconds.")

    # Compaction
    compaction_enabled: bool = Field(True, description="Run in-session compaction when threshold hit.")
    compaction_turns_threshold: int = Field(10, gt=0, description="Run compaction when turns_since >= this.")
    compaction_keep_last_n: int = Field(20, gt=0, description="Events to keep after compaction.")
    compaction_max_turns: int = Field(10, gt=0, description="Sliding-window size (recent messages).")
    compaction_token_budget: Optional[int] = Field(None, description="Optional; also trigger when tokens exceed this.")
    compaction_use_llm: bool = Field(True, description="Use LLM for summarization; false = truncate only.")
    compaction_llm_model: Optional[str] = Field(None, description="If set, use this model for summarization; else app LLM.")

    model_config = {"extra": "forbid", "frozen": False}


def session_config_from_settings(settings: Any) -> SessionConfig:
    """
    Build SessionConfig from the app's Settings (env -> Settings -> SessionConfig).
    Used by this app when wiring session/compaction from env.
    """
    return SessionConfig(
        enabled=getattr(settings, "session_enabled", True),
        provider=(getattr(settings, "session_provider", None) or "postgres").strip().lower(),
        db_path=getattr(settings, "session_db_path", None),
        ttl_seconds=getattr(settings, "session_ttl_seconds", 1800),
        cleanup_interval_seconds=getattr(settings, "session_cleanup_interval_seconds", 300),
        compaction_enabled=getattr(settings, "compaction_enabled", True),
        compaction_turns_threshold=getattr(settings, "compaction_turns_threshold", 10),
        compaction_keep_last_n=getattr(settings, "compaction_keep_last_n", 20),
        compaction_max_turns=getattr(settings, "compaction_max_turns", 10),
        compaction_token_budget=getattr(settings, "compaction_token_budget", None),
        compaction_use_llm=getattr(settings, "compaction_use_llm", True),
        compaction_llm_model=getattr(settings, "compaction_llm_model", None),
    )


def _parse_int(value: Optional[str], default: int) -> int:
    """Parse env string to int; return default if empty/None/invalid."""
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return default
    if isinstance(value, str) and value.strip().lower() == "none":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_bool(value: Optional[str], default: bool) -> bool:
    """Parse env string to bool; default for empty/None."""
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return default
    v = str(value).strip().lower()
    if v in ("true", "1", "yes"):
        return True
    if v in ("false", "0", "no", "none"):
        return False
    return default


def _parse_optional_str(value: Optional[str]) -> Optional[str]:
    """Return None for empty or 'none' string; else stripped value."""
    if value is None:
        return None
    s = value.strip()
    if s == "" or s.lower() == "none":
        return None
    return s


def session_config_from_env() -> SessionConfig:
    """
    Build SessionConfig from session/compaction-related env vars only.
    For library use when the app does not have this app's Settings class.
    """
    return SessionConfig(
        enabled=_parse_bool(os.environ.get("SESSION_ENABLED"), True),
        provider=(os.environ.get("SESSION_PROVIDER") or "postgres").strip().lower(),
        db_path=_parse_optional_str(os.environ.get("SESSION_DB_PATH")),
        ttl_seconds=_parse_int(os.environ.get("SESSION_TTL_SECONDS"), 1800),
        cleanup_interval_seconds=_parse_int(os.environ.get("SESSION_CLEANUP_INTERVAL_SECONDS"), 300),
        compaction_enabled=_parse_bool(os.environ.get("COMPACTION_ENABLED"), True),
        compaction_turns_threshold=_parse_int(os.environ.get("COMPACTION_TURNS_THRESHOLD"), 10),
        compaction_keep_last_n=_parse_int(os.environ.get("COMPACTION_KEEP_LAST_N"), 20),
        compaction_max_turns=_parse_int(os.environ.get("COMPACTION_MAX_TURNS"), 10),
        compaction_token_budget=_parse_int(os.environ.get("COMPACTION_TOKEN_BUDGET"), 0) or None,
        compaction_use_llm=_parse_bool(os.environ.get("COMPACTION_USE_LLM"), True),
        compaction_llm_model=_parse_optional_str(os.environ.get("COMPACTION_LLM_MODEL")),
    )
