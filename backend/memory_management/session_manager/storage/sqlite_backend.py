"""
SQLite SessionBackend: local session DB operations with a file path.
Implements the same API as SessionBackend (Postgres) using sqlite3.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _record_db_op(
    operation: str,
    duration_ms: float,
    run_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> None:
    try:
        from ..observability.db_metrics import record_session_db_op

        record_session_db_op(operation, duration_ms, run_id=run_id, user_id=user_id)
    except Exception:
        pass


def _json_dump(value: Any) -> str:
    try:
        return json.dumps(value or {})
    except (TypeError, ValueError):
        return "{}"


def _json_load(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    try:
        return json.loads(value) if value else {}
    except (TypeError, ValueError):
        return {}


class SQLiteSessionBackend:
    """SQLite session backend using a local file path."""

    def __init__(self, db_path: str):
        if not db_path:
            raise ValueError("SQLiteSessionBackend requires a non-empty db_path")
        self._db_path = db_path
        if db_path != ":memory:":
            directory = os.path.dirname(os.path.abspath(db_path))
            if directory:
                os.makedirs(directory, exist_ok=True)

    @property
    def get_connection(self) -> Callable[[], sqlite3.Connection]:
        return self._connect

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    @contextmanager
    def _ctx(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def create_session(self, run_id: str, user_id: str, ttl_seconds: int = 1800) -> bool:
        start = time.perf_counter()
        try:
            with self._ctx() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO session_registry (run_id, user_id)
                    VALUES (?, ?)
                    """,
                    (run_id, user_id),
                )
                cursor.execute(
                    """
                    INSERT INTO session_working_state (
                        run_id, user_id, working_state, ttl_seconds, status
                    )
                    VALUES (?, ?, ?, ?, 'active')
                    """,
                    (run_id, user_id, _json_dump({}), ttl_seconds),
                )
                conn.commit()
                return True
        except Exception as e:
            logger.error("sqlite create_session failed: %s", e)
            return False
        finally:
            _record_db_op("create_session", (time.perf_counter() - start) * 1000, run_id=run_id, user_id=user_id)

    def get_session(self, user_id: str, run_id: str) -> Optional[Dict[str, Any]]:
        start = time.perf_counter()
        try:
            with self._ctx() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT w.run_id, w.user_id, w.working_state, w.compacted_summary,
                           w.last_activity_at, w.ttl_seconds, r.created_at, w.version
                    FROM session_working_state w
                    JOIN session_registry r ON r.user_id = w.user_id AND r.run_id = w.run_id
                    WHERE w.user_id = ? AND w.run_id = ?
                      AND w.status = 'active'
                      AND datetime(w.last_activity_at, '+' || w.ttl_seconds || ' seconds') > CURRENT_TIMESTAMP
                    """,
                    (user_id, run_id),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                out = dict(row)
                out["working_state"] = _json_load(out.get("working_state"))
                return out
        except Exception as e:
            logger.error("sqlite get_session failed: %s", e)
            return None
        finally:
            _record_db_op("get_session", (time.perf_counter() - start) * 1000, run_id=run_id, user_id=user_id)

    def get_session_ignore_ttl(self, user_id: str, run_id: str) -> Optional[Dict[str, Any]]:
        start = time.perf_counter()
        try:
            with self._ctx() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT w.run_id, w.user_id, w.working_state, w.compacted_summary,
                           w.last_activity_at, w.ttl_seconds, r.created_at, w.version
                    FROM session_working_state w
                    JOIN session_registry r ON r.user_id = w.user_id AND r.run_id = w.run_id
                    WHERE w.user_id = ? AND w.run_id = ? AND w.status = 'active'
                    """,
                    (user_id, run_id),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                out = dict(row)
                out["working_state"] = _json_load(out.get("working_state"))
                return out
        except Exception as e:
            logger.error("sqlite get_session_ignore_ttl failed: %s", e)
            return None
        finally:
            _record_db_op("get_session_ignore_ttl", (time.perf_counter() - start) * 1000, run_id=run_id, user_id=user_id)

    def list_sessions_for_user(self, user_id: str, limit: int = 50, include_expired: bool = False) -> List[Dict[str, Any]]:
        if not user_id:
            return []
        start = time.perf_counter()
        try:
            with self._ctx() as conn:
                cursor = conn.cursor()
                if include_expired:
                    cursor.execute(
                        """
                        SELECT w.run_id, w.last_activity_at, r.created_at
                        FROM session_working_state w
                        JOIN session_registry r ON r.user_id = w.user_id AND r.run_id = w.run_id
                        WHERE w.user_id = ? AND w.status = 'active'
                        ORDER BY w.last_activity_at DESC
                        LIMIT ?
                        """,
                        (user_id, limit),
                    )
                else:
                    cursor.execute(
                        """
                        SELECT w.run_id, w.last_activity_at, r.created_at
                        FROM session_working_state w
                        JOIN session_registry r ON r.user_id = w.user_id AND r.run_id = w.run_id
                        WHERE w.user_id = ? AND w.status = 'active'
                          AND datetime(w.last_activity_at, '+' || w.ttl_seconds || ' seconds') > CURRENT_TIMESTAMP
                        ORDER BY w.last_activity_at DESC
                        LIMIT ?
                        """,
                        (user_id, limit),
                    )
                return [dict(r) for r in cursor.fetchall()]
        except Exception as e:
            logger.error("sqlite list_sessions_for_user failed: %s", e)
            return []
        finally:
            _record_db_op("list_sessions_for_user", (time.perf_counter() - start) * 1000, user_id=user_id)

    def update_compacted_summary(self, user_id: str, run_id: str, summary: Optional[str]) -> bool:
        start = time.perf_counter()
        try:
            with self._ctx() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    UPDATE session_working_state
                    SET compacted_summary = ?
                    WHERE user_id = ? AND run_id = ? AND status = 'active'
                    """,
                    (summary, user_id, run_id),
                )
                conn.commit()
                return cursor.rowcount == 1
        except Exception as e:
            logger.error("sqlite update_compacted_summary failed: %s", e)
            return False
        finally:
            _record_db_op("update_compacted_summary", (time.perf_counter() - start) * 1000, run_id=run_id, user_id=user_id)

    def update_session_activity(self, user_id: str, run_id: str) -> bool:
        start = time.perf_counter()
        try:
            with self._ctx() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    UPDATE session_working_state
                    SET last_activity_at = CURRENT_TIMESTAMP
                    WHERE user_id = ? AND run_id = ? AND status = 'active'
                    """,
                    (user_id, run_id),
                )
                conn.commit()
                return cursor.rowcount == 1
        except Exception as e:
            logger.error("sqlite update_session_activity failed: %s", e)
            return False
        finally:
            _record_db_op("update_session_activity", (time.perf_counter() - start) * 1000, run_id=run_id, user_id=user_id)

    def update_session_working_state(self, user_id: str, run_id: str, working_state: Dict[str, Any], version: int) -> bool:
        start = time.perf_counter()
        try:
            with self._ctx() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    UPDATE session_working_state
                    SET working_state = ?, version = version + 1
                    WHERE user_id = ? AND run_id = ? AND version = ? AND status = 'active'
                    """,
                    (_json_dump(working_state), user_id, run_id, version),
                )
                conn.commit()
                return cursor.rowcount == 1
        except Exception as e:
            logger.error("sqlite update_session_working_state failed: %s", e)
            return False
        finally:
            _record_db_op("update_session_working_state", (time.perf_counter() - start) * 1000, run_id=run_id, user_id=user_id)

    def append_session_event(self, run_id: str, seq: int, role: str, content: Optional[str] = None, tool_ref: Optional[str] = None, user_id: Optional[str] = None) -> bool:
        if user_id is None:
            logger.warning("sqlite append_session_event: user_id required; event not stored")
            return False
        start = time.perf_counter()
        try:
            with self._ctx() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO session_conversation_turns (run_id, user_id, seq, role, content, tool_ref)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (run_id, user_id, seq, role, content, tool_ref),
                )
                conn.commit()
                return True
        except Exception as e:
            logger.error("sqlite append_session_event failed: %s", e)
            return False
        finally:
            _record_db_op("append_session_event", (time.perf_counter() - start) * 1000, run_id=run_id, user_id=user_id)

    def get_session_events(self, run_id: str, limit: Optional[int] = None, offset: int = 0) -> List[Dict[str, Any]]:
        start = time.perf_counter()
        try:
            with self._ctx() as conn:
                cursor = conn.cursor()
                if limit is not None:
                    cursor.execute(
                        """
                        SELECT seq, role, content, tool_ref, created_at
                        FROM session_conversation_turns
                        WHERE run_id = ?
                        ORDER BY seq DESC
                        LIMIT ? OFFSET ?
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
                        WHERE run_id = ?
                        ORDER BY seq ASC
                        LIMIT -1 OFFSET ?
                        """,
                        (run_id, offset),
                    )
                    rows = cursor.fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            logger.error("sqlite get_session_events failed: %s", e)
            return []
        finally:
            _record_db_op("get_session_events", (time.perf_counter() - start) * 1000, run_id=run_id)

    def get_next_seq(self, run_id: str) -> int:
        start = time.perf_counter()
        try:
            with self._ctx() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq
                    FROM session_conversation_turns
                    WHERE run_id = ?
                    """,
                    (run_id,),
                )
                row = cursor.fetchone()
                return int(row["next_seq"]) if row and row["next_seq"] is not None else 1
        except Exception as e:
            logger.error("sqlite get_next_seq failed: %s", e)
            return 1
        finally:
            _record_db_op("get_next_seq", (time.perf_counter() - start) * 1000, run_id=run_id)

    def copy_events(self, from_run_id: str, to_run_id: str, to_user_id: Optional[str] = None) -> int:
        if not from_run_id or not to_run_id or from_run_id == to_run_id or to_user_id is None:
            return 0
        start = time.perf_counter()
        try:
            events = self.get_session_events(run_id=from_run_id, limit=None, offset=0)
            if not events:
                return 0
            with self._ctx() as conn:
                cursor = conn.cursor()
                for seq, ev in enumerate(events, start=1):
                    cursor.execute(
                        """
                        INSERT INTO session_conversation_turns (run_id, user_id, seq, role, content, tool_ref)
                        VALUES (?, ?, ?, ?, ?, ?)
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
            logger.error("sqlite copy_events failed: %s", e)
            return 0
        finally:
            _record_db_op("copy_events", (time.perf_counter() - start) * 1000, run_id=to_run_id, user_id=to_user_id)

    def get_session_event_count(self, run_id: str) -> int:
        start = time.perf_counter()
        try:
            with self._ctx() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT COUNT(*) AS cnt FROM session_conversation_turns WHERE run_id = ?",
                    (run_id,),
                )
                row = cursor.fetchone()
                return int(row["cnt"]) if row else 0
        except Exception as e:
            logger.error("sqlite get_session_event_count failed: %s", e)
            return 0
        finally:
            _record_db_op("get_session_event_count", (time.perf_counter() - start) * 1000, run_id=run_id)

    def delete_session_events_older_than(self, run_id: str, keep_last_n: int) -> int:
        try:
            with self._ctx() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    WITH max_seq AS (
                        SELECT COALESCE(MAX(seq), 0) AS mx FROM session_conversation_turns WHERE run_id = ?
                    )
                    DELETE FROM session_conversation_turns
                    WHERE run_id = ? AND seq < (SELECT MAX(0, mx - ? + 1) FROM max_seq)
                    """,
                    (run_id, run_id, keep_last_n),
                )
                deleted = cursor.rowcount
                conn.commit()
                return deleted
        except Exception as e:
            logger.error("sqlite delete_session_events_older_than failed: %s", e)
            return 0

    def insert_session_archive(self, user_id: str, run_id: str, compacted_summary: Optional[str], working_state: Dict[str, Any], last_activity_at: Any) -> bool:
        try:
            with self._ctx() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO session_archive (
                        run_id, user_id, compacted_summary, working_state, last_activity_at, archived_at
                    )
                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (
                        run_id,
                        user_id,
                        compacted_summary,
                        _json_dump(working_state or {}),
                        last_activity_at,
                    ),
                )
                conn.commit()
                return True
        except Exception as e:
            logger.error("sqlite insert_session_archive failed: %s", e)
            return False

    def list_expired_sessions(self, limit: int = 100) -> List[Dict[str, Any]]:
        try:
            with self._ctx() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT run_id, user_id, working_state, compacted_summary, last_activity_at
                    FROM session_working_state
                    WHERE status = 'active'
                      AND datetime(last_activity_at, '+' || ttl_seconds || ' seconds') < CURRENT_TIMESTAMP
                    ORDER BY last_activity_at ASC
                    LIMIT ?
                    """,
                    (limit,),
                )
                rows = [dict(r) for r in cursor.fetchall()]
                for row in rows:
                    row["working_state"] = _json_load(row.get("working_state"))
                return rows
        except Exception as e:
            logger.error("sqlite list_expired_sessions failed: %s", e)
            return []

    def _format_events_for_summary(self, events: List[Dict[str, Any]]) -> str:
        parts = []
        for e in events:
            role = e.get("role", "?")
            content = e.get("content") or e.get("tool_ref") or ""
            parts.append(f"{role}: {content}")
        return "\n".join(parts)

    def summarize_events_for_archive(self, run_id: str, llm_summarize: Optional[Callable[[str], str]] = None) -> Optional[str]:
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
                logger.warning("sqlite summarize_events_for_archive LLM failed: %s", e)
                return formatted[:2000] + ("..." if len(formatted) > 2000 else "")
        return formatted[:2000] + ("..." if len(formatted) > 2000 else "")

    def archive_and_clear_session(self, run_id: str, user_id: str, summary: Optional[str], working_state: Dict[str, Any], last_activity_at: Any, llm_summarize: Optional[Callable[[str], str]] = None) -> bool:
        effective_summary = summary
        if (effective_summary is None or (isinstance(effective_summary, str) and not effective_summary.strip())) and llm_summarize:
            effective_summary = self.summarize_events_for_archive(run_id, llm_summarize)
        try:
            with self._ctx() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT 1 FROM session_working_state
                    WHERE user_id = ? AND run_id = ? AND status = 'active'
                    """,
                    (user_id, run_id),
                )
                if cursor.fetchone() is None:
                    return False
                cursor.execute(
                    """
                    UPDATE session_registry
                    SET final_summary = ?
                    WHERE user_id = ? AND run_id = ?
                    """,
                    (effective_summary or "", user_id, run_id),
                )
                cursor.execute(
                    """
                    UPDATE session_working_state
                    SET status = 'archived', archived_at = CURRENT_TIMESTAMP
                    WHERE user_id = ? AND run_id = ? AND status = 'active'
                    """,
                    (user_id, run_id),
                )
                conn.commit()
                return cursor.rowcount == 1
        except Exception as e:
            logger.error("sqlite archive_and_clear_session failed: %s", e)
            return False

    def archive_expired_sessions(self, limit: int = 100, llm_summarize: Optional[Callable[[str], str]] = None) -> int:
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

    def list_archive_for_user(self, user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        if not user_id:
            return []
        try:
            with self._ctx() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT run_id, last_activity_at, archived_at
                    FROM session_working_state
                    WHERE user_id = ? AND status = 'archived'
                    ORDER BY last_activity_at DESC
                    LIMIT ?
                    """,
                    (user_id, limit),
                )
                return [dict(r) for r in cursor.fetchall()]
        except Exception as e:
            logger.error("sqlite list_archive_for_user failed: %s", e)
            return []

    def list_conversations_for_user(self, user_id: str, limit: int = 50, include_expired: bool = False) -> List[Dict[str, Any]]:
        if not user_id:
            return []
        start = time.perf_counter()
        try:
            with self._ctx() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT r.run_id, r.created_at, w.last_activity_at, w.status, w.archived_at
                    FROM session_registry r
                    JOIN session_working_state w ON w.user_id = r.user_id AND w.run_id = r.run_id
                    WHERE r.user_id = ?
                      AND (w.status = 'archived'
                           OR (w.status = 'active' AND (? OR datetime(w.last_activity_at, '+' || w.ttl_seconds || ' seconds') > CURRENT_TIMESTAMP)))
                    ORDER BY w.last_activity_at DESC
                    LIMIT ?
                    """,
                    (user_id, int(include_expired), limit),
                )
                rows = cursor.fetchall()
            result = []
            for row in rows:
                r = dict(row)
                result.append(
                    {
                        "run_id": r["run_id"],
                        "last_activity_at": r["last_activity_at"],
                        "source": "session" if r["status"] == "active" else "archive",
                        "created_at": r["created_at"] if r["status"] == "active" else r["archived_at"],
                    }
                )
            return result
        except Exception as e:
            logger.error("sqlite list_conversations_for_user failed: %s", e)
            return []
        finally:
            _record_db_op("list_conversations_for_user", (time.perf_counter() - start) * 1000, user_id=user_id)

    def get_archive_by_run_id(self, user_id: str, run_id: str) -> Optional[Dict[str, Any]]:
        start = time.perf_counter()
        try:
            with self._ctx() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT w.run_id, w.user_id,
                           COALESCE(r.final_summary, w.compacted_summary) AS compacted_summary,
                           w.working_state, w.last_activity_at, w.archived_at
                    FROM session_working_state w
                    JOIN session_registry r ON r.user_id = w.user_id AND r.run_id = w.run_id
                    WHERE w.user_id = ? AND w.run_id = ? AND w.status = 'archived'
                    """,
                    (user_id, run_id),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                out = dict(row)
                out["working_state"] = _json_load(out.get("working_state"))
                return out
        except Exception as e:
            logger.error("sqlite get_archive_by_run_id failed: %s", e)
            return None
        finally:
            _record_db_op("get_archive_by_run_id", (time.perf_counter() - start) * 1000, run_id=run_id, user_id=user_id)

    def get_session_or_archive(self, user_id: str, run_id: str) -> Tuple[Optional[Dict[str, Any]], str]:
        try:
            session = self.get_session(user_id=user_id, run_id=run_id)
            if session is not None:
                return (dict(session), "session")
            session = self.get_session_ignore_ttl(user_id=user_id, run_id=run_id)
            if session is not None:
                return (dict(session), "session")
            archive = self.get_archive_by_run_id(user_id=user_id, run_id=run_id)
            if archive is not None:
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
            _record_db_op("get_session_or_archive", 0.0, run_id=run_id, user_id=user_id)

    def continue_from_archived_session(self, user_id: str, from_run_id: str, ttl_seconds: int = 1800) -> Optional[str]:
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

    def delete_expired_sessions(self) -> int:
        try:
            with self._ctx() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    UPDATE session_working_state
                    SET status = 'archived', archived_at = CURRENT_TIMESTAMP
                    WHERE status = 'active'
                      AND datetime(last_activity_at, '+' || ttl_seconds || ' seconds') < CURRENT_TIMESTAMP
                    """,
                )
                count = cursor.rowcount
                conn.commit()
                return count
        except Exception as e:
            logger.error("sqlite delete_expired_sessions failed: %s", e)
            return 0

    def delete_all_sessions(self) -> int:
        try:
            with self._ctx() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM session_conversation_turns")
                cursor.execute("DELETE FROM session_working_state")
                cursor.execute("DELETE FROM session_registry")
                deleted = cursor.rowcount
                conn.commit()
                return deleted
        except Exception as e:
            logger.error("sqlite delete_all_sessions failed: %s", e)
            return 0

    def index_sessions_last_activity_exists(self) -> bool:
        try:
            with self._ctx() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='index' AND name=? LIMIT 1",
                    ("idx_sessions_last_activity",),
                )
                return cursor.fetchone() is not None
        except Exception as e:
            logger.debug("sqlite index_sessions_last_activity_exists check failed: %s", e)
            return False

    def index_session_working_state_last_activity_exists(self) -> bool:
        try:
            with self._ctx() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='index' AND name=? LIMIT 1",
                    ("idx_session_working_state_last_activity",),
                )
                return cursor.fetchone() is not None
        except Exception as e:
            logger.debug("sqlite index_session_working_state_last_activity_exists check failed: %s", e)
            return False
