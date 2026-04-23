"""
Session provider factory: returns the configured SessionProvider (NoOp or Postgres).
Phase 4 DI: app may set _app_session_backend so Postgres provider and cleanup use injected backend.
"""
from typing import Optional

from ..core.config import SessionConfig, session_config_from_env
from ..core.protocol import SessionProvider
from ..storage.noop_provider import NoOpSessionProvider
from ..storage.postgres_provider import PostgresSessionProvider
from ..storage.sqlite_backend import SQLiteSessionBackend

# Set by app at startup (Phase 4) so get_session_provider() and cleanup use the same backend
_app_session_backend = None


def get_session_provider(
    config: Optional[SessionConfig] = None,
    session_backend: Optional[object] = None,
) -> SessionProvider:
    """
    Return the configured SessionProvider.
    - When session_backend is provided (or _app_session_backend set), pass it to PostgresSessionProvider.
    - NoOpSessionProvider when session disabled or provider is "noop".
    - PostgresSessionProvider when enabled and provider is "postgres", with ttl_seconds from config.
    - PostgresSessionProvider when enabled and provider is "sqlite", with SQLiteSessionBackend.
    """
    if config is None:
        # Standalone / third-party apps: load SESSION_* / COMPACTION_* from os.environ (load_dotenv in host first).
        config = session_config_from_env()
    if not config.enabled:
        return NoOpSessionProvider()
    provider_name = config.provider.strip().lower()
    if provider_name == "noop":
        return NoOpSessionProvider()
    backend = session_backend if session_backend is not None else _app_session_backend
    if provider_name == "sqlite":
        if backend is None:
            if not config.db_path:
                raise RuntimeError("SESSION_DB_PATH must be set when SESSION_PROVIDER=sqlite")
            backend = SQLiteSessionBackend(config.db_path)
        return PostgresSessionProvider(ttl_seconds=config.ttl_seconds, session_backend=backend)
    return PostgresSessionProvider(ttl_seconds=config.ttl_seconds, session_backend=backend)
