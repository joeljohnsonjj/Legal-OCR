"""
Session manager library: public API (Phase 4).
Re-exports from internal modules; host applications may re-export the same symbols.
"""
from .core.config import (
    SessionConfig,
    session_config_from_env,
    session_config_from_settings,
)
from .core.schema import (
    CompactionState,
    Constraints,
    Hypothesis,
    InvestigationState,
    ProgressMarkers,
    SessionState,
)
from .services.context_builder import build_session_context
from .services.provider_factory import get_session_provider
from .services.state_merge import merge_working_state_patch
from .services.working_state_extract import (
    PATCH_MARKER,
    parse_llm_response_for_working_state,
)
from .storage.session_services import (
    append_session_event,
    create_session,
    delete_expired_sessions,
    delete_session_events_older_than,
    get_next_seq,
    get_session,
    get_session_event_count,
    get_session_events,
    update_session_activity,
    update_session_working_state,
)
from .workers.cleanup import (
    CleanupConfig,
    CleanupResult,
    get_cleanup_config_from_env,
    get_cleanup_config_from_settings,
    run_cleanup,
    run_cleanup_loop,
    run_cleanup_once,
)

__all__ = [
    "CleanupConfig",
    "CleanupResult",
    "CompactionState",
    "Constraints",
    "Hypothesis",
    "InvestigationState",
    "PATCH_MARKER",
    "ProgressMarkers",
    "SessionConfig",
    "SessionState",
    "append_session_event",
    "build_session_context",
    "create_session",
    "delete_expired_sessions",
    "delete_session_events_older_than",
    "get_cleanup_config_from_env",
    "get_cleanup_config_from_settings",
    "get_next_seq",
    "get_session",
    "get_session_event_count",
    "get_session_events",
    "get_session_provider",
    "merge_working_state_patch",
    "parse_llm_response_for_working_state",
    "run_cleanup",
    "run_cleanup_loop",
    "run_cleanup_once",
    "session_config_from_env",
    "session_config_from_settings",
    "update_session_activity",
    "update_session_working_state",
]
