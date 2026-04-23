"""
Phase 9: Background cleanup of expired sessions.
Phase 4: Optional archive flow (archive then clear session row only; keep session_events).
Phase A (library): Config abstraction; run_cleanup accepts optional config (env fallback).
Phase B (library): llm_summarize is injected by caller; no import of archive_summarizer or LLM here.
Phase C (library): run_cleanup_once returns CleanupResult(processed, errors); run_cleanup is alias.
Phase D (library): run_cleanup_loop for long-running worker; optional stop_event (e.g. threading.Event).
Phase F (library): optional advisory_lock_key for single-leader cleanup across instances.
"""
import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Tuple

from ..storage import session_services
from ..storage.backend import SessionBackend
from ..storage.session_services import (
    archive_and_clear_session,
    delete_expired_sessions,
    list_expired_sessions,
    summarize_events_for_archive,
)

logger = logging.getLogger(__name__)

# Env var names used by get_cleanup_config_from_env (same as settings for compatibility)
ENV_ARCHIVE_ON_EXPIRY = "ARCHIVE_ON_EXPIRY"
ENV_CLEANUP_ARCHIVE_LIMIT = "CLEANUP_ARCHIVE_LIMIT"
ENV_SESSION_CLEANUP_INTERVAL_SECONDS = "SESSION_CLEANUP_INTERVAL_SECONDS"

# Phase 7: single place for fallback defaults when env/settings unavailable (no magic numbers in body)
_DEFAULT_CLEANUP_ARCHIVE_LIMIT = 100
_DEFAULT_CLEANUP_INTERVAL_SECONDS = 300

# Audit: named constants for batch/log limits (CQ-005)
_LOG_RUN_IDS_MAX = 20
_DELETE_BATCH_LIMIT = 500


@dataclass(frozen=True)
class CleanupConfig:
    """Cleanup job config: archive vs delete, per-run limit, loop interval. Library-friendly (env or injected)."""

    archive_on_expiry: bool
    cleanup_archive_limit: int
    session_cleanup_interval_seconds: int


@dataclass
class CleanupResult:
    """Result of one cleanup run: number of sessions processed and per-session errors (run_id, message)."""

    processed: int
    errors: List[Tuple[str, str]]
    skipped_lock: bool = False
    expired_run_ids: Optional[List[str]] = None  # run_ids that were expired (for logs/activity)


def get_cleanup_config_from_settings(settings: Any) -> CleanupConfig:
    """
    Build CleanupConfig from the app's Pydantic Settings.
    Used when this app's settings are available (env/.env already loaded into Settings).
    """
    return CleanupConfig(
        archive_on_expiry=getattr(settings, "archive_on_expiry", True),
        cleanup_archive_limit=max(1, getattr(settings, "cleanup_archive_limit", _DEFAULT_CLEANUP_ARCHIVE_LIMIT)),
        session_cleanup_interval_seconds=max(
            1, getattr(settings, "session_cleanup_interval_seconds", _DEFAULT_CLEANUP_INTERVAL_SECONDS)
        ),
    )


def get_cleanup_config_from_env() -> CleanupConfig:
    """
    Read cleanup config from environment variables only (library-safe; no host app imports).
    Host should call load_dotenv() before cleanup if using a .env file.
    """
    raw_archive = os.environ.get(ENV_ARCHIVE_ON_EXPIRY, "true").strip().lower()
    archive_on_expiry = raw_archive in ("true", "1", "yes")
    raw_limit = os.environ.get(ENV_CLEANUP_ARCHIVE_LIMIT, "100").strip()
    try:
        cleanup_archive_limit = int(raw_limit)
    except ValueError:
        cleanup_archive_limit = _DEFAULT_CLEANUP_ARCHIVE_LIMIT
    raw_interval = os.environ.get(ENV_SESSION_CLEANUP_INTERVAL_SECONDS, "").strip()
    try:
        session_cleanup_interval_seconds = int(raw_interval) if raw_interval else _DEFAULT_CLEANUP_INTERVAL_SECONDS
    except ValueError:
        session_cleanup_interval_seconds = _DEFAULT_CLEANUP_INTERVAL_SECONDS
    return CleanupConfig(
        archive_on_expiry=archive_on_expiry,
        cleanup_archive_limit=max(1, cleanup_archive_limit),
        session_cleanup_interval_seconds=max(1, session_cleanup_interval_seconds),
    )


def _do_cleanup_once(
    use_archive: bool,
    archive_limit: int,
    config: CleanupConfig,
    llm_summarize: Optional[Callable[[str], str]],
    session_backend: Optional[SessionBackend] = None,
) -> CleanupResult:
    """Inner cleanup pass (no lock). When session_backend is set, use it for all DB work."""
    list_exp = session_backend.list_expired_sessions if session_backend else list_expired_sessions
    sum_arch = session_backend.summarize_events_for_archive if session_backend else summarize_events_for_archive
    arch_clear = session_backend.archive_and_clear_session if session_backend else archive_and_clear_session
    del_exp = session_backend.delete_expired_sessions if session_backend else delete_expired_sessions
    try:
        if use_archive:
            expired = list_exp(limit=archive_limit)
            expired_run_ids = [str(r["run_id"]) for r in expired]
            if expired_run_ids:
                logger.info("Cleanup: expired sessions (archiving): %s", ", ".join(expired_run_ids[:_LOG_RUN_IDS_MAX]) + (" ..." if len(expired_run_ids) > _LOG_RUN_IDS_MAX else ""))
            errors: List[Tuple[str, str]] = []
            count = 0
            for row in expired:
                try:
                    summary = sum_arch(row["run_id"], llm_summarize)
                    if not (summary and summary.strip()):
                        summary = row.get("compacted_summary")
                    if arch_clear(
                        run_id=row["run_id"],
                        user_id=row["user_id"],
                        summary=summary,
                        working_state=row.get("working_state") or {},
                        last_activity_at=row["last_activity_at"],
                        llm_summarize=None,
                    ):
                        count += 1
                except Exception as e:
                    run_id_str = str(row.get("run_id", ""))
                    errors.append((run_id_str, str(e)))
                    logger.warning("Cleanup archive failed for run_id=%s: %s", run_id_str, e)
            if count:
                logger.info("Cleanup archived %d expired session(s)", count)
            return CleanupResult(processed=count, errors=errors, expired_run_ids=expired_run_ids or None)

        # Delete path: list first so we can log which sessions were expired
        expired = list_exp(limit=_DELETE_BATCH_LIMIT)
        expired_run_ids = [str(r["run_id"]) for r in expired]
        if expired_run_ids:
            logger.info("Cleanup: expired sessions (deleting): %s", ", ".join(expired_run_ids[:_LOG_RUN_IDS_MAX]) + (" ..." if len(expired_run_ids) > _LOG_RUN_IDS_MAX else ""))
        deleted = del_exp()
        if deleted:
            logger.info("Cleanup deleted %d expired session(s)", deleted)
        return CleanupResult(processed=deleted, errors=[], expired_run_ids=expired_run_ids or None)
    except Exception as e:
        logger.error("Cleanup failed: %s", e)
        return CleanupResult(processed=0, errors=[("", str(e))], expired_run_ids=None)


def run_cleanup_once(
    use_archive: bool | None = None,
    archive_limit: int | None = None,
    config: CleanupConfig | None = None,
    llm_summarize: Optional[Callable[[str], str]] = None,
    advisory_lock_key: Optional[int] = None,
    session_backend: Optional[SessionBackend] = None,
) -> CleanupResult:
    """
    One pass: list expired sessions, then archive or delete. Returns CleanupResult(processed, errors).
    When session_backend is provided, use it for the advisory lock and for all session DB work.
    Phase F: If advisory_lock_key is set, acquires pg_try_advisory_lock(key) on a dedicated connection;
    if not acquired returns CleanupResult(0, [], skipped_lock=True). Lock is released in finally after cleanup.
    """
    _config = config if config is not None else get_cleanup_config_from_env()
    _archive = use_archive if use_archive is not None else _config.archive_on_expiry
    _limit = archive_limit if archive_limit is not None else _config.cleanup_archive_limit

    # Phase 5: no db_manager; use injected backend or default from session_services
    effective_backend = session_backend if session_backend is not None else session_services._get_default_backend()
    get_conn = effective_backend.get_connection
    if advisory_lock_key is not None:
        with get_conn() as lock_conn:
            with lock_conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s) AS got", (advisory_lock_key,))
                row = cur.fetchone()
                # Support both tuple and dict-like rows (e.g. RealDictCursor)
                got = row[0] if row and isinstance(row, (list, tuple)) else (row.get("got") if row else None)
                if not got:
                    result = CleanupResult(processed=0, errors=[], skipped_lock=True, expired_run_ids=None)
                    try:
                        from ..observability.activity_log import write_activity
                        write_activity("cleanup_run", processed=0, errors=[], skipped_lock=True, use_archive=_archive, expired_run_ids=[])
                    except Exception as e:
                        logger.debug("Activity log write failed: %s", e)
                    return result
            try:
                result = _do_cleanup_once(
                    use_archive=_archive,
                    archive_limit=_limit,
                    config=_config,
                    llm_summarize=llm_summarize,
                    session_backend=effective_backend,
                )
                try:
                    from ..observability.activity_log import write_activity
                    write_activity(
                        "cleanup_run",
                        processed=result.processed,
                        errors=result.errors,
                        skipped_lock=getattr(result, "skipped_lock", False),
                        use_archive=_archive,
                        expired_run_ids=getattr(result, "expired_run_ids", None) or [],
                    )
                except Exception as e:
                    logger.debug("Activity log write failed: %s", e)
                return result
            finally:
                with lock_conn.cursor() as cur2:
                    cur2.execute("SELECT pg_advisory_unlock(%s)", (advisory_lock_key,))

    result = _do_cleanup_once(
        use_archive=_archive,
        archive_limit=_limit,
        config=_config,
        llm_summarize=llm_summarize,
        session_backend=effective_backend,
    )
    try:
        from ..observability.activity_log import write_activity
        write_activity(
            "cleanup_run",
            processed=result.processed,
            errors=result.errors,
            skipped_lock=getattr(result, "skipped_lock", False),
            use_archive=_archive,
            expired_run_ids=getattr(result, "expired_run_ids", None) or [],
        )
    except Exception as e:
        logger.debug("Activity log write failed: %s", e)
    return result


def run_cleanup(
    use_archive: bool | None = None,
    archive_limit: int | None = None,
    config: CleanupConfig | None = None,
    llm_summarize: Optional[Callable[[str], str]] = None,
    advisory_lock_key: Optional[int] = None,
    session_backend: Optional[SessionBackend] = None,
) -> int:
    """
    Deprecated alias: use run_cleanup_once() for CleanupResult. Returns number of sessions processed only.
    """
    return run_cleanup_once(
        use_archive=use_archive,
        archive_limit=archive_limit,
        config=config,
        llm_summarize=llm_summarize,
        advisory_lock_key=advisory_lock_key,
        session_backend=session_backend,
    ).processed


def run_cleanup_loop(
    interval_seconds: Optional[int] = None,
    use_archive: bool | None = None,
    archive_limit: int | None = None,
    config: CleanupConfig | None = None,
    llm_summarize: Optional[Callable[[str], str]] = None,
    stop_event: Optional[Any] = None,
    advisory_lock_key: Optional[int] = None,
    session_backend: Optional[SessionBackend] = None,
) -> None:
    """
    Run cleanup periodically until stop_event is set (if provided). Does not start on import; caller starts it.
    interval_seconds: delay between runs; if None, use config.session_cleanup_interval_seconds.
    stop_event: optional object with is_set() -> bool (e.g. threading.Event); when set, loop exits.
    advisory_lock_key: optional; if set, each run uses pg_try_advisory_lock so only one instance processes.
    session_backend: optional; when set, use it for lock and all session DB work.
    """
    _config = config if config is not None else get_cleanup_config_from_env()
    interval = interval_seconds if interval_seconds is not None else _config.session_cleanup_interval_seconds
    _archive = use_archive if use_archive is not None else _config.archive_on_expiry
    while True:
        logger.info(
            "Session cleanup: periodic run (interval=%ds, archive=%s)",
            interval,
            _archive,
        )
        try:
            from ..observability.activity_log import write_activity
            write_activity("cleanup_loop_trigger", interval_seconds=interval, use_archive=_archive)
        except Exception as e:
            logger.debug("Activity log write failed: %s", e)
        run_cleanup_once(
            use_archive=use_archive,
            archive_limit=archive_limit,
            config=config,
            llm_summarize=llm_summarize,
            advisory_lock_key=advisory_lock_key,
            session_backend=session_backend,
        )
        if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
            break
        time.sleep(interval)


def main() -> None:
    """Run cleanup once or in a loop (e.g. every 5 minutes)."""
    parser = argparse.ArgumentParser(description="Expired session cleanup (Phase 9/4)")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run once and exit; default is run every 5 minutes",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Seconds between runs (default from SESSION_CLEANUP_INTERVAL_SECONDS)",
    )
    parser.add_argument(
        "--archive",
        action="store_true",
        help="Phase 4: archive to session_archive then clear session row only (keep events)",
    )
    parser.add_argument(
        "--no-archive",
        action="store_true",
        help="Override ARCHIVE_ON_EXPIRY: delete expired sessions instead of archiving",
    )
    parser.add_argument(
        "--archive-limit",
        type=int,
        default=None,
        help="Max sessions to archive per run when using archive (default from CLEANUP_ARCHIVE_LIMIT)",
    )
    parser.add_argument(
        "--advisory-lock-key",
        type=int,
        default=None,
        help="Phase F: pg advisory lock key; only one instance with same key runs cleanup (same DB)",
    )
    args = parser.parse_args()

    cfg = get_cleanup_config_from_env()
    interval = args.interval if args.interval is not None else cfg.session_cleanup_interval_seconds
    use_archive = args.archive or (cfg.archive_on_expiry and not args.no_archive)
    archive_limit = args.archive_limit if args.archive_limit is not None else cfg.cleanup_archive_limit

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    # Optional LLM for archive summaries: inject from host app or set via your own wrapper script.
    # This CLI does not import the parent application.
    llm_summarize: Optional[Callable[[str], str]] = None

    if args.once:
        result = run_cleanup_once(
            use_archive=use_archive,
            archive_limit=archive_limit,
            llm_summarize=llm_summarize,
            advisory_lock_key=args.advisory_lock_key,
        )
        if result.skipped_lock:
            logger.info("Cleanup skipped: another instance holds the advisory lock")
            return
        if result.errors:
            for run_id, msg in result.errors:
                logger.error("Cleanup error run_id=%s: %s", run_id or "(delete)", msg)
            logger.warning("Cleanup finished: %d processed, %d error(s)", result.processed, len(result.errors))
            sys.exit(1)
        if use_archive and result.processed:
            logger.info("Archived %d expired session(s)", result.processed)
        elif not use_archive and result.processed:
            logger.info("Deleted %d expired session(s)", result.processed)
        return

    logger.info(
        "Starting session cleanup loop (interval=%ds, archive=%s)",
        interval,
        use_archive,
    )
    run_cleanup_loop(
        interval_seconds=interval,
        use_archive=use_archive,
        archive_limit=archive_limit,
        llm_summarize=llm_summarize,
        advisory_lock_key=args.advisory_lock_key,
    )


if __name__ == "__main__":
    main()
