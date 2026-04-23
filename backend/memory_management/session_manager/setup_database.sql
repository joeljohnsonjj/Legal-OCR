-- Session Manager Database Setup Script
-- Run this script in pgAdmin or psql to create the required tables for session_manager library.
-- Tables: session_registry, session_working_state, session_conversation_turns.
-- This script is idempotent - safe to run multiple times.

-- ============================================================================
-- SESSION MANAGER TABLES (Phase 8)
-- ============================================================================

-- 1. SESSION_REGISTRY
-- Master table: canonical list of conversation runs (run_id, user_id). Holds final_summary when archived.
CREATE TABLE IF NOT EXISTS session_registry (
    run_id         TEXT NOT NULL,
    user_id        TEXT NOT NULL,
    created_at     TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    final_summary  TEXT,
    PRIMARY KEY (user_id, run_id)
);

COMMENT ON TABLE session_registry IS 'Canonical list of conversation runs; final_summary set when session is archived';
COMMENT ON COLUMN session_registry.run_id IS 'Unique conversation/session identifier';
COMMENT ON COLUMN session_registry.user_id IS 'User identifier';
COMMENT ON COLUMN session_registry.created_at IS 'When the session was created';
COMMENT ON COLUMN session_registry.final_summary IS 'Summary written when session is archived (for loading expired chat)';

-- 2. SESSION_WORKING_STATE
-- One row per run: mutable state (working_state, compacted_summary, TTL). Soft delete on expiry (status='archived').
CREATE TABLE IF NOT EXISTS session_working_state (
    run_id             TEXT NOT NULL,
    user_id            TEXT NOT NULL,
    working_state      JSONB NOT NULL DEFAULT '{}',
    compacted_summary  TEXT,
    last_activity_at   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ttl_seconds        INTEGER NOT NULL DEFAULT 1800,
    version            INTEGER NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
    archived_at        TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (user_id, run_id)
);

COMMENT ON TABLE session_working_state IS 'Mutable session state per run; status=archived when expired';
COMMENT ON COLUMN session_working_state.working_state IS 'Structured conversation state (investigation, hypotheses, progress)';
COMMENT ON COLUMN session_working_state.compacted_summary IS 'LLM-generated summary of compacted history';
COMMENT ON COLUMN session_working_state.last_activity_at IS 'Timestamp of last activity (for expiration)';
COMMENT ON COLUMN session_working_state.ttl_seconds IS 'Session time-to-live in seconds';
COMMENT ON COLUMN session_working_state.version IS 'Version number for optimistic locking';
COMMENT ON COLUMN session_working_state.status IS 'active or archived (soft delete on expiry)';
COMMENT ON COLUMN session_working_state.archived_at IS 'When the session was archived';

-- 3. SESSION_CONVERSATION_TURNS
-- Append-only log of conversation turns (messages, tool calls) per run. Used to display chat history.
CREATE TABLE IF NOT EXISTS session_conversation_turns (
    run_id     TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'tool')),
    content    TEXT,
    tool_ref   TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, seq)
);

COMMENT ON TABLE session_conversation_turns IS 'Append-only conversation event log; used to display old chats';
COMMENT ON COLUMN session_conversation_turns.run_id IS 'Session identifier (links to session_registry.run_id)';
COMMENT ON COLUMN session_conversation_turns.user_id IS 'User identifier (required for Phase 8)';
COMMENT ON COLUMN session_conversation_turns.seq IS 'Sequence number for ordering events';
COMMENT ON COLUMN session_conversation_turns.role IS 'Message role: user, assistant, system, or tool';
COMMENT ON COLUMN session_conversation_turns.content IS 'Message content';
COMMENT ON COLUMN session_conversation_turns.tool_ref IS 'Tool call reference or result';

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

-- ============================================================================
-- VERIFY TABLES WERE CREATED
-- ============================================================================

DO $$
BEGIN
    RAISE NOTICE '============================================================';
    RAISE NOTICE 'Session Manager Database Setup Complete!';
    RAISE NOTICE '============================================================';
    RAISE NOTICE 'Tables created:';
    RAISE NOTICE '  - session_registry (canonical list of runs, final_summary when archived)';
    RAISE NOTICE '  - session_working_state (mutable state, compacted_summary, TTL, status)';
    RAISE NOTICE '  - session_conversation_turns (conversation history for display)';
    RAISE NOTICE '';
    RAISE NOTICE 'Indexes created:';
    RAISE NOTICE '  - idx_session_registry_user_id, idx_session_registry_created_at';
    RAISE NOTICE '  - idx_session_working_state_user_status, idx_session_working_state_last_activity';
    RAISE NOTICE '  - idx_session_conversation_turns_user_run, idx_session_conversation_turns_run_id, idx_session_conversation_turns_created_at';
    RAISE NOTICE '';
    RAISE NOTICE 'Ready to use session_manager library!';
    RAISE NOTICE '============================================================';
END $$;

-- ============================================================================
-- OPTIONAL: Sample data for testing
-- ============================================================================

-- Uncomment below to insert test data:
/*
INSERT INTO session_registry (run_id, user_id) VALUES ('test-run-001', 'test-user-123') ON CONFLICT (user_id, run_id) DO NOTHING;
INSERT INTO session_working_state (run_id, user_id, ttl_seconds) VALUES ('test-run-001', 'test-user-123', 1800) ON CONFLICT (user_id, run_id) DO NOTHING;
INSERT INTO session_conversation_turns (run_id, user_id, seq, role, content) VALUES
    ('test-run-001', 'test-user-123', 1, 'user', 'Hello, what is the vacation policy?'),
    ('test-run-001', 'test-user-123', 2, 'assistant', 'Our vacation policy provides 15 days of PTO per year.')
ON CONFLICT (run_id, seq) DO NOTHING;
*/
