-- Session Manager Database Setup Script (SQLite)
-- Run this script using sqlite3 to create required tables.
-- Tables: session_registry, session_working_state, session_conversation_turns, session_archive.

-- ============================================================================
-- SESSION MANAGER TABLES (SQLite)
-- ============================================================================

CREATE TABLE IF NOT EXISTS session_registry (
    run_id         TEXT NOT NULL,
    user_id        TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
    final_summary  TEXT,
    PRIMARY KEY (user_id, run_id)
);

CREATE TABLE IF NOT EXISTS session_working_state (
    run_id             TEXT NOT NULL,
    user_id            TEXT NOT NULL,
    working_state      TEXT NOT NULL DEFAULT '{}',
    compacted_summary  TEXT,
    last_activity_at   TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
    ttl_seconds        INTEGER NOT NULL DEFAULT 1800,
    version            INTEGER NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'active',
    archived_at        TEXT,
    PRIMARY KEY (user_id, run_id)
);

CREATE TABLE IF NOT EXISTS session_conversation_turns (
    run_id     TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT,
    tool_ref   TEXT,
    created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
    PRIMARY KEY (run_id, seq)
);

-- Optional archive table (compat with insert_session_archive)
CREATE TABLE IF NOT EXISTS session_archive (
    run_id            TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL,
    compacted_summary TEXT,
    working_state     TEXT NOT NULL DEFAULT '{}',
    last_activity_at  TEXT NOT NULL,
    archived_at       TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
);

-- ============================================================================
-- INDEXES FOR PERFORMANCE
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_session_registry_user_id ON session_registry(user_id);
CREATE INDEX IF NOT EXISTS idx_session_registry_created_at ON session_registry(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_session_working_state_user_status ON session_working_state(user_id, status);
CREATE INDEX IF NOT EXISTS idx_session_working_state_last_activity ON session_working_state(last_activity_at);

CREATE INDEX IF NOT EXISTS idx_session_conversation_turns_user_run ON session_conversation_turns(user_id, run_id);
CREATE INDEX IF NOT EXISTS idx_session_conversation_turns_run_id ON session_conversation_turns(run_id);
CREATE INDEX IF NOT EXISTS idx_session_conversation_turns_created_at ON session_conversation_turns(created_at);

CREATE INDEX IF NOT EXISTS idx_session_archive_user_id ON session_archive(user_id);
CREATE INDEX IF NOT EXISTS idx_session_archive_last_activity ON session_archive(last_activity_at);
