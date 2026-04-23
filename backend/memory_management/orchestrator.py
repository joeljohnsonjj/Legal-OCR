"""
Unified integration interface: initialize() at startup, process_turn() per request.
Orchestrates session, Mem0, compaction, and cleanup without requiring the app to wire each step.
"""
import asyncio
import functools
import inspect
import logging
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Module-level state for cleanup worker (set by initialize)
_cleanup_stop_event: Optional[threading.Event] = None
_cleanup_thread: Optional[threading.Thread] = None

# Mem0 vector search top-k merged into "--- User Background ---" (default adapter limit was 5).
MEMORY_CONTEXT_SEARCH_LIMIT = 15


def join_session_and_user_background(session_context: str, user_background: str) -> str:
    """Same concatenation as legacy build_full_context (for ProcessTurnResult.full_context)."""
    s = session_context or ""
    ub = user_background or ""
    if not str(ub).strip():
        return s
    if str(s).strip():
        return s + "\n" + ub
    return "\n" + ub


def resolve_ids(
    user_id: Optional[str],
    run_id: Optional[str],
) -> Tuple[str, str]:
    """
    Resolve user_id and run_id so the system works when the frontend sends no IDs.

    - If run_id missing or empty -> generate new UUID (new conversation).
    - If user_id missing or empty -> fall back to run_id (conversation still scoped).
    - If both provided -> use as-is.
    """
    if not (run_id and str(run_id).strip()):
        run_id = str(uuid.uuid4())
    else:
        run_id = str(run_id).strip()

    if not (user_id and str(user_id).strip()):
        user_id = run_id
    else:
        user_id = str(user_id).strip()

    return (user_id, run_id)


def _compaction_config_from_settings(settings: Any) -> Dict[str, Any]:
    """Build compaction config dict from app settings."""
    return {
        "compaction_enabled": getattr(settings, "compaction_enabled", True),
        "turns_threshold": getattr(settings, "compaction_turns_threshold", 10),
        "keep_last_n": getattr(settings, "compaction_keep_last_n", 20),
        "max_turns": getattr(settings, "compaction_max_turns", 10),
        "token_budget": getattr(settings, "compaction_token_budget", None),
    }


def _bump_turns_since_compaction(provider: Any, user_id: str, run_id: str) -> None:
    """
    After each completed Q&A, increment working_state.compaction.turns_since_last_compaction.
    Without this, the value stays at default 0 and build_session_context never reaches the threshold.
    """
    if not provider or not user_id or not run_id:
        return
    try:
        from memory_management.session_manager.services.state_merge import merge_working_state_patch

        ws = provider.load_working_state(user_id, run_id)
        comp = ws.get("compaction") if isinstance(ws, dict) else None
        prev = int(comp.get("turns_since_last_compaction", 0)) if isinstance(comp, dict) else 0
        merged = merge_working_state_patch(
            ws,
            {"compaction": {"turns_since_last_compaction": prev + 1}},
        )
        if merged is None:
            logger.debug("bump_turns_since_compaction: merge rejected run_id=%s", run_id)
            return
        _, sess = provider.get_or_create_session(run_id=run_id, user_id=user_id)
        if not sess:
            return
        ver = int(sess.get("version", 0))
        provider.update_working_state(user_id, run_id, merged, ver)
    except Exception as e:
        logger.warning("bump_turns_since_compaction failed: %s", e)


def build_full_context(
    user_id: str,
    run_id: str,
    question: str,
    settings: Any,
    request_logger: Optional[Any] = None,
    *,
    session_enabled: Optional[bool] = None,
    memory_enabled: Optional[bool] = None,
) -> Tuple[str, str, bool, Optional[str], Optional[str], Any, str, int]:
    """
    Build session context and user background (Mem0) for the LLM as separate strings.

    session_enabled / memory_enabled: when None, use values from ``settings``; otherwise override for this call.

    Returns:
        (session_context, user_background, compaction_queued, run_id_compact, user_id_compact,
         provider, run_id_resolved, memory_search_count)
    """
    print(f"[DEBUG] build_full_context START: run_id={run_id}")
    session_context = ""
    user_background = ""
    compaction_queued = False
    run_id_compact = None
    user_id_compact = None
    provider = None
    run_id_resolved = run_id
    memory_search_count = 0
    session_messages_preview: List[Dict[str, Any]] = []
    memory_results_preview: List[str] = []

    session_on = (
        getattr(settings, "session_enabled", True) if session_enabled is None else session_enabled
    )
    memory_on = (
        getattr(settings, "memory_enabled", False) if memory_enabled is None else memory_enabled
    )

    if session_on:
        try:
            from memory_management.session_manager import (
                build_session_context,
                get_session_provider,
                session_config_from_settings,
            )

            config = session_config_from_settings(settings)
            provider = get_session_provider(config=config)
            print(f"[DEBUG] Before get_or_create_session: run_id={run_id}")
            logger.info("build_full_context: calling get_or_create_session run_id=%s user_id=%s", run_id, user_id)
            run_id_resolved, session = provider.get_or_create_session(run_id=run_id, user_id=user_id)
            logger.info("build_full_context: get_or_create_session returned run_id_resolved=%s (input run_id=%s)", run_id_resolved, run_id)
            if session:
                events = provider.load_events(run_id=run_id_resolved, limit=None)
                session_messages_preview = [
                    {"role": e.get("role"), "content": (e.get("content") or "")[:200]}
                    for e in (events or [])[:5]
                ]
                if request_logger is not None and hasattr(request_logger, "log_step"):
                    request_logger.log_step(
                        "session_fetch",
                        {
                            "run_id": run_id_resolved,
                            "user_id": user_id,
                            "messages_count": len(events) if events else 0,
                            "messages_preview": session_messages_preview,
                        },
                    )
                built_session, compaction_queued, run_id_compact, user_id_compact = build_session_context(
                    run_id_resolved,
                    user_id,
                    provider,
                    _compaction_config_from_settings(settings),
                    session=session,
                )
                session_context = built_session or ""
        except Exception as e:
            logger.warning("Session context build failed (continuing without session): %s", e)

    if memory_on:
        try:
            from memory_management import get_memory_provider

            mem_provider = get_memory_provider()
            if mem_provider:
                logger.info(
                    "[SessionMemory] build_full_context phase=memory_search user_id=%r run_id_resolved=%s "
                    "search_limit=%d question_len=%d",
                    user_id,
                    run_id_resolved,
                    MEMORY_CONTEXT_SEARCH_LIMIT,
                    len(question or ""),
                )
                results = mem_provider.search(
                    question, user_id=user_id, limit=MEMORY_CONTEXT_SEARCH_LIMIT
                )
                logger.info(
                    "[SessionMemory] build_full_context phase=memory_search_done result_count=%d",
                    len(results) if results else 0,
                )
                if results:
                    memory_search_count = len(results)
                    memory_parts = [str(r) for r in results if r]
                    memory_results_preview = [(p[:150] + "..." if len(p) > 150 else p) for p in memory_parts[:3]]
                    if request_logger is not None and hasattr(request_logger, "log_step"):
                        request_logger.log_step(
                            "memory_fetch",
                            {
                                "user_id": user_id,
                                "query": (question or "")[:200],
                                "count": len(memory_parts),
                                "memories_preview": memory_results_preview,
                            },
                        )
                    if memory_parts:
                        user_background = (
                            "--- User Background ---\n"
                            + "\n".join(f"- {p}" for p in memory_parts)
                            + "\n--- End User Background ---"
                        )
                        logger.info(
                            "[SessionMemory] build_full_context phase=user_background_built "
                            "bullet_count=%d session_len=%d user_background_len=%d",
                            len(memory_parts),
                            len(session_context or ""),
                            len(user_background or ""),
                        )
                else:
                    logger.info(
                        "[SessionMemory] build_full_context phase=memory_empty no results from provider",
                    )
            else:
                logger.info(
                    "[SessionMemory] build_full_context phase=no_memory_provider get_memory_provider() is None",
                )
        except Exception as e:
            logger.warning(
                "[SessionMemory] build_full_context phase=memory_exception err=%s",
                e,
                exc_info=True,
            )

    if request_logger is not None and hasattr(request_logger, "log_step"):
        joined_preview = join_session_and_user_background(session_context, user_background)
        request_logger.log_step(
            "full_context_built",
            {
                "run_id": run_id_resolved,
                "user_id": user_id,
                "session_context_length": len(session_context or ""),
                "user_background_length": len(user_background or ""),
                "context_length": len(joined_preview or ""),
                "compaction_queued": compaction_queued,
                "context_preview": (joined_preview or "")[:500],
            },
        )

    return (
        session_context or "",
        user_background or "",
        compaction_queued,
        run_id_compact,
        user_id_compact,
        provider,
        run_id_resolved,
        memory_search_count,
    )


@dataclass
class ProcessTurnResult:
    """Result of process_turn()."""

    success: bool
    answer: Optional[str]
    run_id: str
    user_id: str
    session_context: str
    user_background: str
    full_context: str
    session_context_len: int
    memory_search_count: int
    compaction_queued: bool
    compaction_ran: bool
    provider: Optional[Any] = None
    raw_answer: Any = None


def _effective_memory_enabled(settings: Any, memory_enabled: Optional[bool]) -> bool:
    if memory_enabled is None:
        return bool(getattr(settings, "memory_enabled", False))
    return bool(memory_enabled)


def _normalize_answer_content(raw_answer: Any) -> Optional[str]:
    if raw_answer is None:
        return None
    if hasattr(raw_answer, "content"):
        return getattr(raw_answer, "content", None)
    return str(raw_answer) if raw_answer else None


async def _invoke_generate_fn(
    generate_fn: Callable[..., Any],
    question: str,
    session_context: str,
    user_background: str,
    kwargs: Dict[str, Any],
) -> Any:
    if inspect.iscoroutinefunction(generate_fn):
        return await generate_fn(question, session_context, user_background, **kwargs)
    result = generate_fn(question, session_context, user_background, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _run_compaction_if_queued(
    *,
    compaction_queued: bool,
    run_id_compact: Optional[str],
    user_id_compact: Optional[str],
    provider: Any,
    settings: Any,
    compaction_config: Optional[Dict[str, Any]],
    llm_summarize: Optional[Callable[[str], str]],
) -> bool:
    if not (compaction_queued and run_id_compact and user_id_compact and provider):
        return False
    try:
        from memory_management.session_manager.workers.compaction import run_compaction

        summarizer = llm_summarize or getattr(settings, "_llm_summarize", None) or (lambda p: "")
        comp_config = compaction_config or _compaction_config_from_settings(settings)
        run_compaction(
            run_id=run_id_compact,
            user_id=user_id_compact,
            provider=provider,
            llm_summarize=summarizer,
            turns_threshold=comp_config.get("turns_threshold", 10),
            token_budget=comp_config.get("token_budget"),
            keep_last_n=comp_config.get("keep_last_n", 20),
        )
        return True
    except Exception as e:
        logger.warning("Compaction failed (continuing): %s", e)
        return False


def _apply_session_memory_after_assistant(
    question: str,
    answer_content: str,
    user_id: str,
    run_id_resolved: str,
    provider: Optional[Any],
    *,
    memory_enabled: bool,
) -> None:
    if provider and user_id:
        try:
            provider.append_assistant_event(run_id_resolved, answer_content, user_id=user_id)
        except Exception as e:
            logger.warning("Session append assistant event failed (continuing): %s", e)
        else:
            _bump_turns_since_compaction(provider, user_id, run_id_resolved)

    if memory_enabled and user_id:
        logger.info(
            "memory pipeline: about to call memory_add user_id=%s",
            user_id,
        )
        try:
            from memory_management import add as memory_add

            memory_add(
                messages=[
                    {"role": "user", "content": question},
                    {"role": "assistant", "content": answer_content},
                ],
                user_id=user_id,
            )
            logger.info("memory pipeline: memory_add completed user_id=%s", user_id)
        except Exception as e:
            logger.warning("Mem0 add failed (continuing): %s", e, exc_info=True)
    else:
        logger.info(
            "memory pipeline: memory_add skipped memory_enabled=%s user_id=%s",
            memory_enabled,
            user_id,
        )


def finalize_session_turn_after_assistant(
    question: str,
    answer_content: str,
    user_id: str,
    run_id_resolved: str,
    provider: Optional[Any],
    settings: Any,
    *,
    compaction_queued: bool,
    run_id_compact: Optional[str],
    user_id_compact: Optional[str],
    memory_enabled: bool,
    compaction_config: Optional[Dict[str, Any]] = None,
    llm_summarize: Optional[Callable[[str], str]] = None,
) -> bool:
    """
    After the model answered: append assistant event, bump compaction counter, Mem0 add, optional compaction.

    Use when the host streams or calls the LLM outside process_turn but wants the same session + memory + compaction
    behavior. Returns whether compaction ran.
    """
    _apply_session_memory_after_assistant(
        question,
        answer_content,
        user_id,
        run_id_resolved,
        provider,
        memory_enabled=memory_enabled,
    )
    return _run_compaction_if_queued(
        compaction_queued=compaction_queued,
        run_id_compact=run_id_compact,
        user_id_compact=user_id_compact,
        provider=provider,
        settings=settings,
        compaction_config=compaction_config,
        llm_summarize=llm_summarize,
    )


def process_turn(
    question: str,
    user_id: Optional[str],
    run_id: Optional[str],
    generate_fn: Callable[..., Any],
    settings: Any,
    *,
    session_enabled: Optional[bool] = None,
    memory_enabled: Optional[bool] = None,
    compaction_config: Optional[Dict[str, Any]] = None,
    llm_summarize: Optional[Callable[[str], str]] = None,
    request_logger: Optional[Any] = None,
    **kwargs: Any,
) -> ProcessTurnResult:
    """
    Execute one conversation turn with full session + memory integration.

    Steps: resolve IDs -> build context -> append user -> generate -> append assistant -> Mem0 add -> compaction.

    generate_fn(question, session_context, user_background, **kwargs) -> answer (str or object with .content, or None).
    Hosts should pass session_context and user_background into the LLM as separate labeled sections; see memory_management/docs/GETTING_STARTED.md §6b.
    """
    user_id_original, run_id_original = user_id, run_id
    user_id, run_id = resolve_ids(user_id, run_id)
    print(f"[DEBUG] After resolve_ids: run_id={run_id}")
    logger.info("process_turn: after resolve_ids run_id=%s user_id=%s", run_id, user_id)
    if request_logger is not None and hasattr(request_logger, "log_step"):
        request_logger.log_step(
            "ids_resolved",
            {
                "user_id_original": user_id_original,
                "run_id_original": run_id_original,
                "user_id_resolved": user_id,
                "run_id_resolved": run_id,
            },
        )
        if hasattr(request_logger, "log_db_state"):
            request_logger.log_db_state(run_id, user_id)

    print(f"[DEBUG] Before build_full_context: run_id={run_id}")
    (
        session_context,
        user_background,
        compaction_queued,
        run_id_compact,
        user_id_compact,
        provider,
        run_id_resolved,
        memory_search_count,
    ) = build_full_context(
        user_id,
        run_id,
        question,
        settings,
        request_logger=request_logger,
        session_enabled=session_enabled,
        memory_enabled=memory_enabled,
    )
    full_context = join_session_and_user_background(session_context, user_background)
    effective_memory = _effective_memory_enabled(settings, memory_enabled)

    if provider and user_id:
        try:
            provider.append_user_event(run_id_resolved, question, user_id=user_id)
        except Exception as e:
            logger.warning("Session append user event failed (continuing): %s", e)

    raw_answer = generate_fn(question, session_context, user_background, **kwargs)
    answer_content = _normalize_answer_content(raw_answer)

    if not answer_content:
        return ProcessTurnResult(
            success=False,
            answer=None,
            run_id=run_id_resolved,
            user_id=user_id,
            session_context=session_context,
            user_background=user_background,
            full_context=full_context,
            session_context_len=len(session_context or ""),
            memory_search_count=memory_search_count,
            compaction_queued=compaction_queued,
            compaction_ran=False,
            provider=provider,
            raw_answer=raw_answer,
        )

    compaction_ran = finalize_session_turn_after_assistant(
        question,
        answer_content,
        user_id,
        run_id_resolved,
        provider,
        settings,
        compaction_queued=compaction_queued,
        run_id_compact=run_id_compact,
        user_id_compact=user_id_compact,
        memory_enabled=effective_memory,
        compaction_config=compaction_config,
        llm_summarize=llm_summarize,
    )

    return ProcessTurnResult(
        success=True,
        answer=answer_content,
        run_id=run_id_resolved,
        user_id=user_id,
        session_context=session_context,
        user_background=user_background,
        full_context=full_context,
        session_context_len=len(session_context or ""),
        memory_search_count=memory_search_count,
        compaction_queued=compaction_queued,
        compaction_ran=compaction_ran,
        provider=provider,
        raw_answer=raw_answer,
    )


async def process_turn_async(
    question: str,
    user_id: Optional[str],
    run_id: Optional[str],
    generate_fn: Callable[..., Any],
    settings: Any,
    *,
    session_enabled: Optional[bool] = None,
    memory_enabled: Optional[bool] = None,
    compaction_config: Optional[Dict[str, Any]] = None,
    llm_summarize: Optional[Callable[[str], str]] = None,
    request_logger: Optional[Any] = None,
    **kwargs: Any,
) -> ProcessTurnResult:
    """
    Same pipeline as process_turn, but awaits an async generate_fn or an awaitable return value.

    generate_fn(question, session_context, user_background, **kwargs) — same contract as process_turn; see GETTING_STARTED.md §6b.

    Session append, Mem0 add, and compaction bookkeeping run synchronously; compaction (when queued) runs in
    the default executor so the event loop is not blocked.
    """
    user_id_original, run_id_original = user_id, run_id
    user_id, run_id = resolve_ids(user_id, run_id)
    logger.info("process_turn_async: after resolve_ids run_id=%s user_id=%s", run_id, user_id)
    if request_logger is not None and hasattr(request_logger, "log_step"):
        request_logger.log_step(
            "ids_resolved",
            {
                "user_id_original": user_id_original,
                "run_id_original": run_id_original,
                "user_id_resolved": user_id,
                "run_id_resolved": run_id,
            },
        )
        if hasattr(request_logger, "log_db_state"):
            request_logger.log_db_state(run_id, user_id)

    (
        session_context,
        user_background,
        compaction_queued,
        run_id_compact,
        user_id_compact,
        provider,
        run_id_resolved,
        memory_search_count,
    ) = build_full_context(
        user_id,
        run_id,
        question,
        settings,
        request_logger=request_logger,
        session_enabled=session_enabled,
        memory_enabled=memory_enabled,
    )
    full_context = join_session_and_user_background(session_context, user_background)
    effective_memory = _effective_memory_enabled(settings, memory_enabled)

    if provider and user_id:
        try:
            provider.append_user_event(run_id_resolved, question, user_id=user_id)
        except Exception as e:
            logger.warning("Session append user event failed (continuing): %s", e)

    raw_answer = await _invoke_generate_fn(
        generate_fn, question, session_context, user_background, kwargs
    )
    answer_content = _normalize_answer_content(raw_answer)

    if not answer_content:
        return ProcessTurnResult(
            success=False,
            answer=None,
            run_id=run_id_resolved,
            user_id=user_id,
            session_context=session_context,
            user_background=user_background,
            full_context=full_context,
            session_context_len=len(session_context or ""),
            memory_search_count=memory_search_count,
            compaction_queued=compaction_queued,
            compaction_ran=False,
            provider=provider,
            raw_answer=raw_answer,
        )

    _apply_session_memory_after_assistant(
        question,
        answer_content,
        user_id,
        run_id_resolved,
        provider,
        memory_enabled=effective_memory,
    )

    loop = asyncio.get_running_loop()
    compaction_partial = functools.partial(
        _run_compaction_if_queued,
        compaction_queued=compaction_queued,
        run_id_compact=run_id_compact,
        user_id_compact=user_id_compact,
        provider=provider,
        settings=settings,
        compaction_config=compaction_config,
        llm_summarize=llm_summarize,
    )
    compaction_ran = bool(await loop.run_in_executor(None, compaction_partial))

    return ProcessTurnResult(
        success=True,
        answer=answer_content,
        run_id=run_id_resolved,
        user_id=user_id,
        session_context=session_context,
        user_background=user_background,
        full_context=full_context,
        session_context_len=len(session_context or ""),
        memory_search_count=memory_search_count,
        compaction_queued=compaction_queued,
        compaction_ran=compaction_ran,
        provider=provider,
        raw_answer=raw_answer,
    )


def initialize(
    settings: Any,
    get_connection: Callable,
    *,
    llm_summarize: Optional[Callable[[str], str]] = None,
    start_cleanup_worker: bool = True,
) -> None:
    """
    Initialize memory_management for the host app. Call once at startup.

    - set_memory_config(settings)
    - SessionBackend(get_connection), set_default_backend, provider_factory._app_session_backend
    - If start_cleanup_worker: spawn background thread running run_cleanup_once periodically
    """
    from memory_management.memory import set_memory_config
    from memory_management.session_manager.storage.backend import SessionBackend
    from memory_management.session_manager.storage.sqlite_backend import SQLiteSessionBackend
    from memory_management.session_manager.storage import session_services
    from memory_management.session_manager.services import provider_factory

    set_memory_config(settings)
    logger.info("memory_management: set_memory_config done")

    session_provider = (getattr(settings, "session_provider", None) or "postgres").strip().lower()
    if session_provider == "sqlite":
        db_path = getattr(settings, "session_db_path", None) or ""
        if not db_path:
            raise RuntimeError("session_db_path must be set when session_provider=sqlite")
        backend = SQLiteSessionBackend(db_path)
    else:
        backend = SessionBackend(get_connection)
    session_services.set_default_backend(backend)
    provider_factory._app_session_backend = backend
    logger.info("memory_management: session backend wired")

    if start_cleanup_worker and getattr(settings, "session_cleanup_interval_seconds", 0) > 0:
        global _cleanup_stop_event, _cleanup_thread
        _cleanup_stop_event = threading.Event()

        def _cleanup_worker() -> None:
            from memory_management.session_manager.workers.cleanup import (
                get_cleanup_config_from_settings,
                run_cleanup_once,
            )
            try:
                config = get_cleanup_config_from_settings(settings)
            except Exception:
                from memory_management.session_manager.workers.cleanup import get_cleanup_config_from_env
                config = get_cleanup_config_from_env()
            interval = config.session_cleanup_interval_seconds
            while not _cleanup_stop_event.wait(timeout=interval):
                try:
                    result = run_cleanup_once(
                        config=config,
                        llm_summarize=llm_summarize,
                        session_backend=backend,
                    )
                    if result.processed:
                        logger.info("Session cleanup: archived %s expired session(s)", result.processed)
                except Exception as e:
                    logger.warning("Session cleanup run failed: %s", e)

        _cleanup_thread = threading.Thread(target=_cleanup_worker, daemon=True)
        _cleanup_thread.start()
        logger.info("memory_management: cleanup worker started (interval=%ss)", getattr(settings, "session_cleanup_interval_seconds", 300))


def shutdown_cleanup_worker() -> None:
    """Signal the cleanup worker to stop. Call on app shutdown if needed."""
    global _cleanup_stop_event
    if _cleanup_stop_event is not None:
        _cleanup_stop_event.set()
