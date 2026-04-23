"""
Postgres session provider: implements SessionProvider using session_services (Phase 1).
Phase 3 DI: optional session_backend; when provided, use it instead of module-level session_services.
"""
import copy
import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

from . import session_services
from .backend import SessionBackend

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 1800


def _session_to_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize session row: run_id as str, working_state as dict."""
    out = dict(row)
    if "run_id" in out and out["run_id"] is not None:
        out["run_id"] = str(out["run_id"])
    if "working_state" not in out or out["working_state"] is None:
        out["working_state"] = {}
    return out


class PostgresSessionProvider:
    """SessionProvider that uses sessions and session_events tables via session_services or injected backend."""

    def __init__(
        self,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        session_backend: Optional[SessionBackend] = None,
    ):
        self._ttl_seconds = ttl_seconds
        self._backend = session_backend  # When None, use session_services module

    def _svc(self):
        """Return backend when injected, else session_services module (for delegation)."""
        return self._backend if self._backend is not None else session_services

    def get_or_create_session(
        self,
        run_id: Optional[str],
        user_id: str,
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """
        If run_id is None or empty: generate new UUID, create session, return (run_id, session).
        If run_id provided: load session. If not found, create session with THAT run_id (do not generate a new one).
        Refresh last_activity_at on every successful load.
        """
        svc = self._svc()
        if not user_id:
            return (run_id or str(uuid.uuid4()), None)

        effective_run_id = (run_id or "").strip()
        logger.info(
            "get_or_create_session: run_id param=%r user_id=%r effective_run_id=%r",
            run_id, user_id, effective_run_id[:8] + "..." if effective_run_id and len(effective_run_id) > 8 else effective_run_id,
        )
        if effective_run_id:
            session = svc.get_session(user_id=user_id, run_id=effective_run_id)
            if session is not None:
                svc.update_session_activity(user_id=user_id, run_id=effective_run_id)
                logger.info("get_or_create_session: using existing session run_id=%s", effective_run_id[:8])
                return (str(session.get("run_id", effective_run_id)), _session_to_dict(session))
            # Not found or expired (last_activity + TTL < now); check for past chat to seed from
            logger.info(
                "Session: run_id=%s not loaded (not found or expired); checking for past chat to seed from",
                effective_run_id[:8] if len(effective_run_id) >= 8 else effective_run_id,
            )
            old_session = svc.get_session_ignore_ttl(
                user_id=user_id, run_id=effective_run_id
            )
            if old_session is not None:
                new_run_id = str(uuid.uuid4())
                ok = svc.create_session(
                    run_id=new_run_id,
                    user_id=user_id,
                    ttl_seconds=self._ttl_seconds,
                )
                if not ok:
                    return (new_run_id, None)
                # Copy old session's summary so context builder uses it
                old_summary = old_session.get("compacted_summary")
                if old_summary and str(old_summary).strip():
                    svc.update_compacted_summary(
                        user_id=user_id, run_id=new_run_id, summary=str(old_summary).strip()
                    )
                # Copy old chat events so "Recent messages" includes past conversation (Phase 3: to_user_id)
                svc.copy_events(
                    from_run_id=effective_run_id, to_run_id=new_run_id, to_user_id=user_id
                )
                svc.update_session_activity(user_id=user_id, run_id=new_run_id)
                session = svc.get_session(user_id=user_id, run_id=new_run_id)
                logger.info(
                    "Session seeded from expired run_id=%s -> new run_id=%s",
                    effective_run_id[:8],
                    new_run_id[:8],
                )
                return (new_run_id, _session_to_dict(session) if session else None)
            # Phase 4: run_id only in archive (view-only) -> do not create session; API will reject send
            _, source = svc.get_session_or_archive(
                user_id=user_id, run_id=effective_run_id
            )
            if source == "archive":
                logger.info(
                    "Session: run_id=%s is archived only (view-only); returning None session",
                    effective_run_id[:8],
                )
                return (effective_run_id, None)
            # run_id provided but no session row yet: create session with the SAME run_id (from resolve_ids)
            logger.info("get_or_create_session: creating new session with passed run_id=%s", effective_run_id[:8])
        else:
            effective_run_id = str(uuid.uuid4())
            logger.info("get_or_create_session: no run_id provided, generated run_id=%s", effective_run_id[:8])

        ok = svc.create_session(
            run_id=effective_run_id,
            user_id=user_id,
            ttl_seconds=self._ttl_seconds,
        )
        if not ok:
            return (effective_run_id, None)
        svc.update_session_activity(user_id=user_id, run_id=effective_run_id)
        session = svc.get_session(user_id=user_id, run_id=effective_run_id)
        return (effective_run_id, _session_to_dict(session) if session else None)

    def append_user_event(
        self, run_id: str, content: str, *, user_id: Optional[str] = None
    ) -> None:
        """Append user message to event log. Phase 3: pass user_id for session_conversation_turns."""
        svc = self._svc()
        seq = svc.get_next_seq(run_id=run_id)
        svc.append_session_event(
            run_id=run_id,
            seq=seq,
            role="user",
            content=content,
            tool_ref=None,
            user_id=user_id,
        )

    def append_assistant_event(
        self,
        run_id: str,
        content: str,
        tool_ref: Optional[str] = None,
        *,
        user_id: Optional[str] = None,
    ) -> None:
        """Append assistant message to event log. Phase 3: pass user_id for session_conversation_turns."""
        svc = self._svc()
        seq = svc.get_next_seq(run_id=run_id)
        svc.append_session_event(
            run_id=run_id,
            seq=seq,
            role="assistant",
            content=content,
            tool_ref=tool_ref,
            user_id=user_id,
        )

    def load_events(
        self,
        run_id: str,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return events for run_id, optionally last N (chronological order)."""
        return self._svc().get_session_events(run_id=run_id, limit=limit)

    def load_working_state(self, user_id: str, run_id: str) -> Dict[str, Any]:
        """Return working_state dict for the session."""
        session = self._svc().get_session(user_id=user_id, run_id=run_id)
        if session is None:
            return {}
        ws = session.get("working_state")
        return dict(ws) if isinstance(ws, dict) else {}

    def update_working_state(
        self,
        user_id: str,
        run_id: str,
        patch: Dict[str, Any],
        version: int,
    ) -> bool:
        """Merge patch into current working_state and persist (optimistic lock). Phase 9: one retry on version conflict."""
        svc = self._svc()
        current = self.load_working_state(user_id=user_id, run_id=run_id)
        merged = copy.deepcopy(current)
        for key, value in patch.items():
            merged[key] = value
        ok = svc.update_session_working_state(
            user_id=user_id,
            run_id=run_id,
            working_state=merged,
            version=version,
        )
        if ok:
            return True
        # Retry once with fresh version (concurrent update)
        session = svc.get_session(user_id=user_id, run_id=run_id)
        if session is None:
            return False
        new_version = session.get("version", version)
        if new_version == version:
            return False
        return svc.update_session_working_state(
            user_id=user_id,
            run_id=run_id,
            working_state=merged,
            version=new_version,
        )

    def refresh_activity(self, user_id: str, run_id: str) -> None:
        """Update last_activity_at for the session."""
        self._svc().update_session_activity(user_id=user_id, run_id=run_id)

    def update_compacted_summary(
        self, user_id: str, run_id: str, summary: Optional[str]
    ) -> bool:
        """Persist compacted_summary for the session."""
        return self._svc().update_compacted_summary(
            user_id=user_id, run_id=run_id, summary=summary
        )

    def build_session_context(
        self,
        run_id: str,
        user_id: str,
        last_n: int = 10,
    ) -> str:
        """Build session context string from last N events and working state (no compaction yet)."""
        events = self.load_events(run_id=run_id, limit=last_n)
        state = self.load_working_state(user_id=user_id, run_id=run_id)

        parts = []
        if events:
            parts.append("--- Session (recent messages) ---")
            for ev in events:
                role = ev.get("role", "?")
                content = ev.get("content") or ev.get("tool_ref") or ""
                parts.append(f"{role}: {content[:500]}{'...' if len(str(content)) > 500 else ''}")
            parts.append("--- End session ---")
        if state:
            parts.append("--- Working state ---")
            parts.append(str(state))
            parts.append("--- End working state ---")
        return "\n".join(parts) if parts else ""
