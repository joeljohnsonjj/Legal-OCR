"""
SessionBackend: session DB operations with injected connection factory (Phase 1 DI plan).
Same contract as session_services; no dependency on src.database. Exposes get_connection for cleanup.
DB latency instrumentation: each method logs operation name and duration; app can set request
context to accumulate per-request totals.
"""
import logging
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

import psycopg2.extras

logger = logging.getLogger(__name__)


def _record_db_op(
    operation: str,
    duration_ms: float,
    run_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> None:
    """Log and record one DB operation for latency tracking."""
    try:
        from ..observability.db_metrics import record_session_db_op
        record_session_db_op(operation, duration_ms, run_id=run_id, user_id=user_id)
    except Exception:
        pass


class SessionBackend:
    """Holds connection factory; all session DB operations use self._get_connection()."""

    def __init__(self, get_connection: Callable):
        self._get_connection = get_connection

    @property
    def get_connection(self):
        """Return the injected connection factory (for cleanup advisory lock)."""
        return self._get_connection

    def create_session(
        self,
        run_id: str,
        user_id: str,
        ttl_seconds: int = 1800,
    ) -> bool:
        """Insert into session_registry and session_working_state (Phase 1: new tables only). Returns True on success."""
        start = time.perf_counter()
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO session_registry (run_id, user_id, created_at)
                        VALUES (%s, %s, CURRENT_TIMESTAMP)
                        """,
                        (run_id, user_id),
                    )
                    cursor.execute(
                        """
                        INSERT INTO session_working_state (
                            run_id, user_id, working_state, ttl_seconds, status
                        )
                        VALUES (%s, %s, %s, %s, 'active')
                        """,
                        (run_id, user_id, psycopg2.extras.Json({}), ttl_seconds),
                    )
                    conn.commit()
                    return True
        except Exception as e:
            logger.error("create_session failed: %s", e)
            return False
        finally:
            _record_db_op("create_session", (time.perf_counter() - start) * 1000, run_id=run_id, user_id=user_id)

    def get_session(self, user_id: str, run_id: str) -> Optional[Dict[str, Any]]:
        """
        Select session by (user_id, run_id). Phase 8: new tables only (session_working_state + session_registry).
        Return None if not found or expired (TTL).
        """
        start = time.perf_counter()
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT w.run_id, w.user_id, w.working_state, w.compacted_summary,
                               w.last_activity_at, w.ttl_seconds, r.created_at, w.version
                        FROM session_working_state w
                        JOIN session_registry r ON r.user_id = w.user_id AND r.run_id = w.run_id
                        WHERE w.user_id = %s AND w.run_id = %s
                          AND w.status = 'active'
                          AND w.last_activity_at + (w.ttl_seconds || ' seconds')::interval > NOW()
                        """,
                        (user_id, run_id),
                    )
                    row = cursor.fetchone()
                    return dict(row) if row else None
        except Exception as e:
            logger.error("get_session failed: %s", e)
            return None
        finally:
            _record_db_op("get_session", (time.perf_counter() - start) * 1000, run_id=run_id, user_id=user_id)


    def get_session_ignore_ttl(self, user_id: str, run_id: str) -> Optional[Dict[str, Any]]:
        """
        Select session by (user_id, run_id) without TTL check. Phase 8: new tables only (active only).
        Exclude status='archived' so get_session_or_archive returns archive via get_archive_by_run_id.
        """
        start = time.perf_counter()
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT w.run_id, w.user_id, w.working_state, w.compacted_summary,
                               w.last_activity_at, w.ttl_seconds, r.created_at, w.version
                        FROM session_working_state w
                        JOIN session_registry r ON r.user_id = w.user_id AND r.run_id = w.run_id
                        WHERE w.user_id = %s AND w.run_id = %s AND w.status = 'active'
                        """,
                        (user_id, run_id),
                    )
                    row = cursor.fetchone()
                    return dict(row) if row else None
        except Exception as e:
            logger.error("get_session_ignore_ttl failed: %s", e)
            return None
        finally:
            _record_db_op("get_session_ignore_ttl", (time.perf_counter() - start) * 1000, run_id=run_id, user_id=user_id)

    def list_sessions_for_user(self, 
        user_id: str,
        limit: int = 50,
        include_expired: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        List sessions for user_id, most recent first. Phase 8: session_working_state + session_registry only.
        """
        if not user_id:
            return []
        start = time.perf_counter()
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cursor:
                    if include_expired:
                        cursor.execute(
                            """
                            SELECT w.run_id, w.last_activity_at, r.created_at
                            FROM session_working_state w
                            JOIN session_registry r ON r.user_id = w.user_id AND r.run_id = w.run_id
                            WHERE w.user_id = %s AND w.status = 'active'
                            ORDER BY w.last_activity_at DESC
                            LIMIT %s
                            """,
                            (user_id, limit),
                        )
                    else:
                        cursor.execute(
                            """
                            SELECT w.run_id, w.last_activity_at, r.created_at
                            FROM session_working_state w
                            JOIN session_registry r ON r.user_id = w.user_id AND r.run_id = w.run_id
                            WHERE w.user_id = %s AND w.status = 'active'
                              AND w.last_activity_at + (w.ttl_seconds || ' seconds')::interval > NOW()
                            ORDER BY w.last_activity_at DESC
                            LIMIT %s
                            """,
                            (user_id, limit),
                        )
                    rows = [dict(r) for r in cursor.fetchall()]
            return rows
        except Exception as e:
            logger.error("list_sessions_for_user failed: %s", e)
            return []
        finally:
            _record_db_op("list_sessions_for_user", (time.perf_counter() - start) * 1000, user_id=user_id)

    def update_compacted_summary(self, user_id: str, run_id: str, summary: Optional[str]) -> bool:
        """Update compacted_summary. Phase 8: session_working_state only."""
        start = time.perf_counter()
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE session_working_state
                        SET compacted_summary = %s
                        WHERE user_id = %s AND run_id = %s AND status = 'active'
                        """,
                        (summary, user_id, run_id),
                    )
                    conn.commit()
                    return cursor.rowcount == 1
        except Exception as e:
            logger.error("update_compacted_summary failed: %s", e)
            return False
        finally:
            _record_db_op("update_compacted_summary", (time.perf_counter() - start) * 1000, run_id=run_id, user_id=user_id)


    def update_session_activity(self, user_id: str, run_id: str) -> bool:
        """Update last_activity_at. Phase 8: session_working_state only."""
        start = time.perf_counter()
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE session_working_state
                        SET last_activity_at = NOW()
                        WHERE user_id = %s AND run_id = %s AND status = 'active'
                        """,
                        (user_id, run_id),
                    )
                    conn.commit()
                    return cursor.rowcount == 1
        except Exception as e:
            logger.error("update_session_activity failed: %s", e)
            return False
        finally:
            _record_db_op("update_session_activity", (time.perf_counter() - start) * 1000, run_id=run_id, user_id=user_id)

    def update_session_working_state(self, 
        user_id: str,
        run_id: str,
        working_state: Dict[str, Any],
        version: int,
    ) -> bool:
        """
        Update working_state with optimistic lock. Phase 8: session_working_state only.
        Returns True if exactly one row was updated.
        """
        start = time.perf_counter()
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE session_working_state
                        SET working_state = %s, version = version + 1
                        WHERE user_id = %s AND run_id = %s AND version = %s AND status = 'active'
                        """,
                        (psycopg2.extras.Json(working_state), user_id, run_id, version),
                    )
                    conn.commit()
                    return cursor.rowcount == 1
        except Exception as e:
            logger.error("update_session_working_state failed: %s", e)
            return False
        finally:
                _record_db_op("update_session_working_state", (time.perf_counter() - start) * 1000, run_id=run_id, user_id=user_id)


    def append_session_event(self, 
        run_id: str,
        seq: int,
        role: str,
        content: Optional[str] = None,
        tool_ref: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> bool:
        """
        Insert one row into session_conversation_turns. Phase 8: user_id required (no fallback to session_events).
        Returns False if user_id is None (event not stored).
        """
        if user_id is None:
            logger.warning("append_session_event: user_id required (Phase 8); event not stored")
            return False
        start = time.perf_counter()
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO session_conversation_turns (run_id, user_id, seq, role, content, tool_ref)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (run_id, user_id, seq, role, content, tool_ref),
                    )
                    conn.commit()
                    return True
        except Exception as e:
            logger.error("append_session_event failed: %s", e)
            return False
        finally:
            _record_db_op("append_session_event", (time.perf_counter() - start) * 1000, run_id=run_id, user_id=user_id)

    def get_session_events(self, 
        run_id: str,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        Select events for run_id ordered by seq. Phase 8: session_conversation_turns only.
        Returns list of dicts with seq, role, content, tool_ref, created_at.
        """
        start = time.perf_counter()
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cursor:
                    if limit is not None:
                        cursor.execute(
                            """
                            SELECT seq, role, content, tool_ref, created_at
                            FROM session_conversation_turns
                            WHERE run_id = %s
                            ORDER BY seq DESC
                            LIMIT %s OFFSET %s
                            """,
                            (run_id, limit, offset),
                        )
                        rows = list(cursor.fetchall())
                        rows.reverse()
                    else:
                        cursor.execute(
                            """
                            SELECT seq, role, content, tool_ref, created_at
                            FROM session_conversation_turns
                            WHERE run_id = %s
                            ORDER BY seq ASC
                            OFFSET %s
                            """,
                            (run_id, offset),
                        )
                        rows = cursor.fetchall()
                    return [dict(r) for r in rows]
        except Exception as e:
            logger.error("get_session_events failed: %s", e)
            return []
        finally:
            _record_db_op("get_session_events", (time.perf_counter() - start) * 1000, run_id=run_id)


    def get_next_seq(self, run_id: str) -> int:
        """Return next seq for run_id. Phase 8: session_conversation_turns only."""
        start = time.perf_counter()
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq
                        FROM session_conversation_turns
                        WHERE run_id = %s
                        """,
                        (run_id,),
                    )
                    row = cursor.fetchone()
                    return int(row["next_seq"]) if row and row.get("next_seq") is not None else 1
        except Exception as e:
            logger.error("get_next_seq failed: %s", e)
            return 1
        finally:
            _record_db_op("get_next_seq", (time.perf_counter() - start) * 1000, run_id=run_id)


    def copy_events(self, 
        from_run_id: str,
        to_run_id: str,
        to_user_id: Optional[str] = None,
    ) -> int:
        """
        Copy all events from from_run_id to to_run_id (new seq 1, 2, 3, ...).
        Phase 8: session_conversation_turns only; to_user_id required (no fallback to session_events).
        Returns number of events copied; 0 if to_user_id is None.
        """
        if not from_run_id or not to_run_id or from_run_id == to_run_id or to_user_id is None:
            return 0
        start = time.perf_counter()
        try:
            events = self.get_session_events(run_id=from_run_id, limit=None, offset=0)
            if not events:
                return 0
            with self._get_connection() as conn:
                with conn.cursor() as cursor:
                    for seq, ev in enumerate(events, start=1):
                        cursor.execute(
                            """
                            INSERT INTO session_conversation_turns (run_id, user_id, seq, role, content, tool_ref)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            """,
                            (
                                to_run_id,
                                to_user_id,
                                seq,
                                ev.get("role", "user"),
                                ev.get("content") or "",
                                ev.get("tool_ref"),
                            ),
                        )
                    conn.commit()
                    return len(events)
        except Exception as e:
            logger.error("copy_events failed: %s", e)
            return 0
        finally:
            _record_db_op("copy_events", (time.perf_counter() - start) * 1000, run_id=to_run_id, user_id=to_user_id)


    def get_session_event_count(self, run_id: str) -> int:
        """Return count of events for run_id. Phase 8: session_conversation_turns only."""
        start = time.perf_counter()
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT COUNT(*) AS cnt FROM session_conversation_turns WHERE run_id = %s",
                        (run_id,),
                    )
                    row = cursor.fetchone()
                    return int(row["cnt"]) if row else 0
        except Exception as e:
            logger.error("get_session_event_count failed: %s", e)
            return 0
        finally:
            _record_db_op("get_session_event_count", (time.perf_counter() - start) * 1000, run_id=run_id)


    def delete_session_events_older_than(self, run_id: str, keep_last_n: int) -> int:
        """
        Delete events for run_id except the last keep_last_n (by seq). Phase 8: session_conversation_turns only.
        Returns number of rows deleted.
        """
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        WITH max_seq AS (
                            SELECT COALESCE(MAX(seq), 0) AS mx FROM session_conversation_turns WHERE run_id = %s
                        )
                        DELETE FROM session_conversation_turns
                        WHERE run_id = %s AND seq < (SELECT GREATEST(0, mx - %s + 1) FROM max_seq)
                        """,
                        (run_id, run_id, keep_last_n),
                    )
                    deleted = cursor.rowcount
                    conn.commit()
                    return deleted
        except Exception as e:
            logger.error("delete_session_events_older_than failed: %s", e)
            return 0


    # ---------------------------------------------------------------------------
    # Phase 2: Session archive (archive on expiry, clear session row only)
    # ---------------------------------------------------------------------------


    def insert_session_archive(self, 
        user_id: str,
        run_id: str,
        compacted_summary: Optional[str],
        working_state: Dict[str, Any],
        last_activity_at: Any,
    ) -> bool:
        """
        Insert one row into session_archive.
        Used when archiving an expired session. Returns True on success.
        On duplicate run_id (already archived), returns False (caller may skip).
        """
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO session_archive (
                            run_id, user_id, compacted_summary, working_state, last_activity_at
                        )
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            run_id,
                            user_id,
                            compacted_summary,
                            psycopg2.extras.Json(working_state or {}),
                            last_activity_at,
                        ),
                    )
                    conn.commit()
                    return True
        except Exception as e:
            logger.error("insert_session_archive failed: %s", e)
            return False


    def list_expired_sessions(self, limit: int = 100) -> List[Dict[str, Any]]:
        """
        List sessions where last_activity_at + ttl_seconds < NOW(). Phase 8: session_working_state only.
        Order by last_activity_at ASC (oldest first) so cleanup is deterministic.
        """
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT run_id, user_id, working_state, compacted_summary, last_activity_at
                        FROM session_working_state
                        WHERE status = 'active'
                          AND last_activity_at + (ttl_seconds || ' seconds')::interval < NOW()
                        ORDER BY last_activity_at ASC
                        LIMIT %s
                        """,
                        (limit,),
                    )
                    return [dict(r) for r in cursor.fetchall()]
        except Exception as e:
            logger.error("list_expired_sessions failed: %s", e)
            return []


    def _format_events_for_summary(self, events: List[Dict[str, Any]]) -> str:
        """Format events as 'role: content' lines for summarization. Returns a single string."""
        parts = []
        for e in events:
            role = e.get("role", "?")
            content = e.get("content") or e.get("tool_ref") or ""
            parts.append(f"{role}: {content}")
        return "\n".join(parts)


    def summarize_events_for_archive(self, 
        run_id: str,
        llm_summarize: Optional[Callable[[str], str]] = None,
    ) -> Optional[str]:
        """
        Load all events for run_id, format them, and return a summary.
        If llm_summarize is provided, call it with the formatted text; else truncate to 2000 chars.
        Returns None on error or if no events.
        """
        events = self.get_session_events(run_id=run_id, limit=None, offset=0)
        if not events:
            return None
        formatted = self._format_events_for_summary(events)
        if not formatted.strip():
            return ""
        if llm_summarize:
            try:
                prompt = (
                    "Summarize the following conversation according to your instructions.\n\n"
                    "Conversation:\n\n"
                    + formatted
                )
                return llm_summarize(prompt) or ""
            except Exception as e:
                logger.warning("summarize_events_for_archive LLM failed: %s", e)
                return formatted[:2000] + ("..." if len(formatted) > 2000 else "")
        return formatted[:2000] + ("..." if len(formatted) > 2000 else "")


    def archive_and_clear_session(self, 
        run_id: str,
        user_id: str,
        summary: Optional[str],
        working_state: Dict[str, Any],
        last_activity_at: Any,
        llm_summarize: Optional[Callable[[str], str]] = None,
    ) -> bool:
        """
        Phase 8: Soft delete only. If run is in session_working_state (status='active'), update
        session_registry.final_summary and session_working_state status='archived'. Else return False.
        Events are never deleted.
        """
        effective_summary = summary
        if (effective_summary is None or (isinstance(effective_summary, str) and not effective_summary.strip())) and llm_summarize:
            effective_summary = self.summarize_events_for_archive(run_id, llm_summarize)

        try:
            with self._get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT 1 FROM session_working_state
                        WHERE user_id = %s AND run_id = %s AND status = 'active'
                        """,
                        (user_id, run_id),
                    )
                    if cursor.fetchone() is None:
                        return False
                    cursor.execute(
                        """
                        UPDATE session_registry
                        SET final_summary = %s
                        WHERE user_id = %s AND run_id = %s
                        """,
                        (effective_summary or "", user_id, run_id),
                    )
                    cursor.execute(
                        """
                        UPDATE session_working_state
                        SET status = 'archived', archived_at = NOW()
                        WHERE user_id = %s AND run_id = %s AND status = 'active'
                        """,
                        (user_id, run_id),
                    )
                    conn.commit()
                    return cursor.rowcount == 1
        except Exception as e:
            logger.error("archive_and_clear_session failed: %s", e)
            return False


    def archive_expired_sessions(self, 
        limit: int = 100,
        llm_summarize: Optional[Callable[[str], str]] = None,
    ) -> int:
        """
        For each expired session: archive summary + working_state to session_archive,
        then delete the session row only (session_events kept). Returns number of sessions archived.
        Archive row uses a summary from ALL events (via summarize_events_for_archive) so the archive
        reflects the full conversation; the session's compacted_summary is only the "old" slice from compaction.
        """
        expired = self.list_expired_sessions(limit=limit)
        count = 0
        for row in expired:
            summary_for_archive = self.summarize_events_for_archive(row["run_id"], llm_summarize)
            if not (summary_for_archive and summary_for_archive.strip()):
                summary_for_archive = row.get("compacted_summary")
            ok = self.archive_and_clear_session(
                run_id=row["run_id"],
                user_id=row["user_id"],
                summary=summary_for_archive,
                working_state=row.get("working_state") or {},
                last_activity_at=row["last_activity_at"],
                llm_summarize=None,
            )
            if ok:
                count += 1
        return count


    # ---------------------------------------------------------------------------
    # Phase 3: List and get session – include archive (merged list, get_session_or_archive)
    # ---------------------------------------------------------------------------


    def list_archive_for_user(self, 
        user_id: str,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """
        List archived conversations. Phase 8: session_working_state (status='archived') only.
        Returns list of dicts with run_id, last_activity_at, archived_at.
        """
        if not user_id:
            return []
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT run_id, last_activity_at, archived_at
                        FROM session_working_state
                        WHERE user_id = %s AND status = 'archived'
                        ORDER BY last_activity_at DESC
                        LIMIT %s
                        """,
                        (user_id, limit),
                    )
                    return [dict(r) for r in cursor.fetchall()]
        except Exception as e:
            logger.error("list_archive_for_user failed: %s", e)
            return []


    def list_conversations_for_user(self, 
        user_id: str,
        limit: int = 50,
        include_expired: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Unified list of all conversations (active + archived) for user_id. Phase 8: new tables only.
        session_registry JOIN session_working_state; source='session' if active, 'archive' if archived.
        Same return shape: run_id, last_activity_at, source, created_at. Sorted by last_activity_at DESC.
        """
        if not user_id:
            return []
        start = time.perf_counter()
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT r.run_id, r.created_at, w.last_activity_at, w.status, w.archived_at
                        FROM session_registry r
                        JOIN session_working_state w ON w.user_id = r.user_id AND w.run_id = r.run_id
                        WHERE r.user_id = %s
                          AND (w.status = 'archived'
                               OR (w.status = 'active' AND (%s OR w.last_activity_at + (w.ttl_seconds || ' seconds')::interval > NOW())))
                        ORDER BY w.last_activity_at DESC
                        LIMIT %s
                        """,
                        (user_id, include_expired, limit),
                    )
                    rows = cursor.fetchall()
            result = []
            for row in rows:
                r = dict(row)
                result.append({
                    "run_id": r["run_id"],
                    "last_activity_at": r["last_activity_at"],
                    "source": "session" if r["status"] == "active" else "archive",
                    "created_at": r["created_at"] if r["status"] == "active" else r["archived_at"],
                })
            return result
        except Exception as e:
            logger.error("list_conversations_for_user failed: %s", e)
            return []
        finally:
            _record_db_op("list_conversations_for_user", (time.perf_counter() - start) * 1000, user_id=user_id)


    def get_archive_by_run_id(self, user_id: str, run_id: str) -> Optional[Dict[str, Any]]:
        """
        Return archived row for (user_id, run_id). Phase 8: session_working_state (status='archived') only,
        with registry.final_summary as compacted_summary. Same dict shape: run_id, user_id, compacted_summary,
        working_state, last_activity_at, archived_at.
        """
        start = time.perf_counter()
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT w.run_id, w.user_id,
                               COALESCE(r.final_summary, w.compacted_summary) AS compacted_summary,
                               w.working_state, w.last_activity_at, w.archived_at
                        FROM session_working_state w
                        JOIN session_registry r ON r.user_id = w.user_id AND r.run_id = w.run_id
                        WHERE w.user_id = %s AND w.run_id = %s AND w.status = 'archived'
                        """,
                        (user_id, run_id),
                    )
                    row = cursor.fetchone()
                    return dict(row) if row else None
        except Exception as e:
            logger.error("get_archive_by_run_id failed: %s", e)
            return None
        finally:
            _record_db_op("get_archive_by_run_id", (time.perf_counter() - start) * 1000, run_id=run_id, user_id=user_id)


    def get_session_or_archive(self, 
        user_id: str,
        run_id: str,
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        """
        Get session metadata from sessions table or session_archive.
        Returns (session_like_dict, "session" | "archive" | "none").
        For archive, returns dict with run_id, user_id, working_state, compacted_summary,
        last_activity_at, and archived_at (so callers know it's archived). Events always
        come from session_events (unchanged).
        """
        start = time.perf_counter()
        try:
            session = self.get_session(user_id=user_id, run_id=run_id)
            if session is not None:
                return (dict(session), "session")
            session = self.get_session_ignore_ttl(user_id=user_id, run_id=run_id)
            if session is not None:
                return (dict(session), "session")
            archive = self.get_archive_by_run_id(user_id=user_id, run_id=run_id)
            if archive is not None:
                # Session-like shape for context_builder (compacted_summary, working_state)
                session_like = {
                    "run_id": archive["run_id"],
                    "user_id": archive["user_id"],
                    "working_state": archive.get("working_state") or {},
                    "compacted_summary": archive.get("compacted_summary"),
                    "last_activity_at": archive["last_activity_at"],
                    "archived_at": archive.get("archived_at"),
                }
                return (session_like, "archive")
            return (None, "none")
        finally:
            _record_db_op("get_session_or_archive", (time.perf_counter() - start) * 1000, run_id=run_id, user_id=user_id)


    def continue_from_archived_session(self, 
        user_id: str,
        from_run_id: str,
        ttl_seconds: int = 1800,
    ) -> Optional[str]:
        """
        Create a new session seeded from an archived conversation (Phase 4 Continue).
        Copies compacted_summary, working_state, and all events from archive + session_events.
        Returns new run_id or None if from_run_id is not in archive.
        """
        archive = self.get_archive_by_run_id(user_id=user_id, run_id=from_run_id)
        if not archive:
            return None
        new_run_id = str(uuid.uuid4())
        if not self.create_session(run_id=new_run_id, user_id=user_id, ttl_seconds=ttl_seconds):
            return None
        summary = archive.get("compacted_summary")
        if summary and str(summary).strip():
            self.update_compacted_summary(user_id=user_id, run_id=new_run_id, summary=str(summary).strip())
        working_state = archive.get("working_state") or {}
        if working_state:
            self.update_session_working_state(
                user_id=user_id,
                run_id=new_run_id,
                working_state=working_state,
                version=0,
            )
        self.copy_events(from_run_id=from_run_id, to_run_id=new_run_id, to_user_id=user_id)
        self.update_session_activity(user_id=user_id, run_id=new_run_id)
        return new_run_id


    def delete_expired_sessions(self, ) -> int:
        """
        Phase 8: Soft-delete expired rows in session_working_state only. Returns count of rows updated.
        Does NOT delete events. Prefer archive_expired_sessions() to write summary/state.
        """
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE session_working_state
                        SET status = 'archived', archived_at = NOW()
                        WHERE status = 'active'
                          AND last_activity_at + (ttl_seconds || ' seconds')::interval < NOW()
                        """,
                    )
                    count = cursor.rowcount
                    conn.commit()
                    return count
        except Exception as e:
            logger.error("delete_expired_sessions failed: %s", e)
            return 0


    def delete_all_sessions(self, ) -> int:
        """
        Phase 8: Delete all session data (new tables only). Order: session_conversation_turns,
        session_working_state, session_registry. Returns number of registry rows deleted.
        """
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("DELETE FROM session_conversation_turns")
                    cursor.execute("DELETE FROM session_working_state")
                    cursor.execute("DELETE FROM session_registry")
                    deleted = cursor.rowcount
                    conn.commit()
                    return deleted
        except Exception as e:
            logger.error("delete_all_sessions failed: %s", e)
            return 0


    def index_sessions_last_activity_exists(self, ) -> bool:
        """
        Return True if idx_sessions_last_activity exists on sessions (Phase E).
        Used by tests and ops to verify the cleanup expiry index is present.
        """
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT 1 FROM pg_indexes
                        WHERE tablename = 'sessions' AND indexname = 'idx_sessions_last_activity'
                        LIMIT 1
                        """,
                    )
                    return cursor.fetchone() is not None
        except Exception as e:
            logger.debug("index_sessions_last_activity_exists check failed: %s", e)
            return False


    def index_session_working_state_last_activity_exists(self, ) -> bool:
        """
        Phase 7: Return True if idx_session_working_state_last_activity exists on
        session_working_state. Used by tests and ops to verify the expiry index is present.
        """
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT 1 FROM pg_indexes
                        WHERE tablename = 'session_working_state'
                          AND indexname = 'idx_session_working_state_last_activity'
                        LIMIT 1
                        """,
                    )
                    return cursor.fetchone() is not None
        except Exception as e:
            logger.debug("index_session_working_state_last_activity_exists check failed: %s", e)
            return False
