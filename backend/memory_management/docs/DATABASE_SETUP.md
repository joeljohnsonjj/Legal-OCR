# Session Manager - Database Setup Guide

## Overview

This guide covers the **PostgreSQL** setup. For **SQLite**, use
`session_manager/setup_database_sqlite.sql` and set `SESSION_PROVIDER=sqlite`
with `SESSION_DB_PATH` in your environment.

The `session_manager` library requires three PostgreSQL tables to function:
1. **sessions** - Active conversation sessions with state
2. **session_events** - Append-only conversation event log
3. **session_archive** - (Optional) Archived sessions for historical analysis

## Quick Setup

### Option 1: Run SQL Script in pgAdmin

1. **Open pgAdmin** and connect to your PostgreSQL server
2. **Select your database** (or create a new one)
3. **Open Query Tool** (Tools → Query Tool or F5)
4. **Load the script**:
   - Click the folder icon to open a file
   - Navigate to `session_manager/setup_database.sql`
   - Or copy-paste the contents
5. **Execute** (F5 or click the play button)
6. **Verify** - You should see success messages in the output

### Option 2: Run from Command Line (psql)

```bash
# Connect to your database and run the script
psql -U your_username -d your_database -f session_manager/setup_database.sql

# Or pipe it directly
cat session_manager/setup_database.sql | psql -U your_username -d your_database
```

### Option 3: Run from Python

```python
import psycopg2

# Read the SQL script
with open('session_manager/setup_database.sql', 'r') as f:
    sql_script = f.read()

# Connect to database
conn = psycopg2.connect(
    host="your_host",
    database="your_database",
    user="your_user",
    password="your_password"
)

# Execute the script
with conn.cursor() as cur:
    cur.execute(sql_script)
    conn.commit()

print("Database setup complete!")
conn.close()
```

## Database Schema Details

### 1. sessions Table

Stores active conversation sessions with working state.

```sql
CREATE TABLE sessions (
    run_id              TEXT NOT NULL,
    user_id             TEXT NOT NULL,
    working_state       JSONB NOT NULL DEFAULT '{}',
    ttl_seconds         INTEGER NOT NULL DEFAULT 1800,
    compacted_summary   TEXT,
    last_activity_at    TIMESTAMP WITH TIME ZONE NOT NULL,
    created_at          TIMESTAMP WITH TIME ZONE NOT NULL,
    version             INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, run_id)
);
```

**Columns**:
- `run_id` - Unique conversation identifier (e.g., "conversation-123")
- `user_id` - User identifier (e.g., "user-456")
- `working_state` - JSONB storing structured state (investigation, hypotheses, progress). LLM-driven patches are applied by the **host** after `process_turn` — see **[WORKING_STATE_LLM_PATCH.md](WORKING_STATE_LLM_PATCH.md)**.
- `ttl_seconds` - Session time-to-live (default: 1800 = 30 minutes)
- `compacted_summary` - LLM-generated summary of conversation history
- `last_activity_at` - Timestamp of last activity (used for expiration)
- `created_at` - When the session was created
- `version` - Version number for optimistic locking (prevents concurrent update conflicts)

**Primary Key**: Composite key on (user_id, run_id) allows same run_id for different users

### 2. session_events Table

Append-only log of all conversation events.

```sql
CREATE TABLE session_events (
    run_id      TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT,
    tool_ref    TEXT,
    created_at  TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (run_id, seq)
);
```

**Columns**:
- `run_id` - Links to sessions.run_id
- `seq` - Sequence number (1, 2, 3...) for ordering events
- `role` - Message role: 'user', 'assistant', 'system', or 'tool'
- `content` - Message content (user question, assistant answer)
- `tool_ref` - Tool call reference or result (JSON string)
- `created_at` - When the event was created

**Primary Key**: Composite key on (run_id, seq) ensures unique, ordered events

**Note**: No foreign key constraint from session_events → sessions
- Events are retained even after session is deleted/archived
- Allows historical analysis of expired sessions

### 3. session_archive Table (Optional)

Stores archived sessions for historical analysis.

```sql
CREATE TABLE session_archive (
    run_id             TEXT PRIMARY KEY,
    user_id            TEXT NOT NULL,
    compacted_summary  TEXT,
    working_state      JSONB NOT NULL DEFAULT '{}',
    last_activity_at   TIMESTAMP WITH TIME ZONE NOT NULL,
    archived_at        TIMESTAMP WITH TIME ZONE NOT NULL
);
```

**When to use**:
- If you want to keep session metadata after expiration
- For analytics and historical analysis
- To track conversation patterns over time

**Note**: The cleanup worker can optionally archive sessions before deletion

## Indexes

The script creates these indexes for optimal performance:

```sql
-- Session lookups
idx_sessions_run_id              -- Fast lookup by run_id
idx_sessions_user_id             -- Fast lookup by user_id
idx_sessions_last_activity       -- Cleanup worker (find expired sessions)

-- Event queries
idx_session_events_run_id        -- Fast event loading
idx_session_events_created_at    -- Time-based queries

-- Archive queries
idx_session_archive_user_id      -- User history
idx_session_archive_last_activity -- Time-based queries
```

## Verify Setup

### Check Tables Exist

```sql
-- Check if tables were created
SELECT table_name 
FROM information_schema.tables 
WHERE table_schema = 'public' 
  AND table_name IN ('sessions', 'session_events', 'session_archive');
```

Expected output:
```
     table_name     
--------------------
 sessions
 session_events
 session_archive
```

### Check Indexes

```sql
-- Check if indexes were created
SELECT indexname 
FROM pg_indexes 
WHERE schemaname = 'public' 
  AND tablename IN ('sessions', 'session_events', 'session_archive')
ORDER BY tablename, indexname;
```

### Insert Test Data

```sql
-- Insert a test session
INSERT INTO sessions (run_id, user_id, ttl_seconds) 
VALUES ('test-001', 'test-user', 1800);

-- Insert test events
INSERT INTO session_events (run_id, seq, role, content) 
VALUES 
    ('test-001', 1, 'user', 'Hello!'),
    ('test-001', 2, 'assistant', 'Hi! How can I help?');

-- Verify
SELECT * FROM sessions WHERE run_id = 'test-001';
SELECT * FROM session_events WHERE run_id = 'test-001' ORDER BY seq;

-- Clean up
DELETE FROM session_events WHERE run_id = 'test-001';
DELETE FROM sessions WHERE run_id = 'test-001';
```

## Using the Library After Setup

Once tables are created, the library will automatically work:

```python
from dotenv import load_dotenv
load_dotenv()

from session_manager import (
    session_config_from_env,
    get_session_provider,
    create_session,
    append_session_event,
)

# Initialize
config = session_config_from_env()
provider = get_session_provider(config, your_db_connection_factory)

# Create session (automatically creates row in sessions table)
session = create_session(
    provider=provider,
    run_id="conv-123",
    user_id="user-456",
    ttl_seconds=1800
)

# Append events (automatically creates rows in session_events table)
append_session_event(
    provider=provider,
    run_id="conv-123",
    user_id="user-456",
    role="user",
    content="What is the vacation policy?"
)

append_session_event(
    provider=provider,
    run_id="conv-123",
    user_id="user-456",
    role="assistant",
    content="Our vacation policy provides 15 days of PTO..."
)
```

## Common Questions

### Q: What if I already have tables with these names?

**A**: The script uses `CREATE TABLE IF NOT EXISTS`, so it won't overwrite existing tables. However:
- Check that your existing schema matches the expected structure
- If schema differs, you may need to migrate or rename tables
- Run a verification query to check column names and types

### Q: Can I use a different schema (not public)?

**A**: Yes, modify the script:

```sql
-- At the top of setup_database.sql, add:
SET search_path TO your_schema_name;

-- Then run the rest of the script
```

Or specify schema in table names:
```sql
CREATE TABLE IF NOT EXISTS your_schema.sessions ( ... );
```

### Q: Do I need all three tables?

**A**: 
- **sessions** - ✅ Required
- **session_events** - ✅ Required
- **session_archive** - ❌ Optional (only for historical analysis)

### Q: What permissions does the app user need?

**A**: The database user needs:

```sql
-- Grant permissions
GRANT SELECT, INSERT, UPDATE, DELETE ON sessions TO your_app_user;
GRANT SELECT, INSERT, DELETE ON session_events TO your_app_user;
GRANT SELECT, INSERT ON session_archive TO your_app_user;  -- If using archive

-- Also grant sequence permissions if you add auto-incrementing IDs later
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO your_app_user;
```

### Q: How do I handle database migrations?

**A**: For schema changes:

1. **Never drop tables with data** - use ALTER TABLE instead
2. **Add columns with defaults**:
   ```sql
   ALTER TABLE sessions ADD COLUMN IF NOT EXISTS new_field TEXT DEFAULT '';
   ```
3. **Use migration tools** like Alembic or Flyway for version control
4. **Backup before migrations**:
   ```bash
   pg_dump -U user -d database > backup_before_migration.sql
   ```

### Q: Can I use a different database (MySQL, SQLite)?

**A**: The library is designed for PostgreSQL:
- Uses JSONB for working_state (Postgres-specific)
- Uses TIMESTAMP WITH TIME ZONE
- Uses specific Postgres functions

To adapt for other databases:
- Convert JSONB → JSON or TEXT
- Adjust timestamp types
- Modify provider implementation

### Q: How much storage will this use?

**A**: Approximate storage per session:

| Component | Size |
|-----------|------|
| Session row | ~1-5 KB (depends on working_state) |
| Event (average) | ~0.5-2 KB |
| 100-message conversation | ~50-200 KB |
| Compacted summary | ~2-10 KB |

**Example**: 1000 active sessions with 50 messages each = ~50-100 MB

**Note**: Use cleanup worker to remove expired sessions and old events

## Troubleshooting

### Error: "relation 'sessions' does not exist"

**Solution**: Tables not created yet. Run `setup_database.sql`

### Error: "permission denied for table sessions"

**Solution**: Grant permissions to your app user:
```sql
GRANT ALL PRIVILEGES ON sessions, session_events, session_archive TO your_app_user;
```

### Error: "duplicate key value violates unique constraint"

**Cause**: Trying to create a session that already exists (same user_id + run_id)

**Solution**: Use `get_or_create_session()` instead of `create_session()`

### Slow queries

**Solution**: Check indexes are created:
```sql
SELECT indexname FROM pg_indexes WHERE tablename = 'sessions';
```

If missing, re-run the index creation part of `setup_database.sql`

## Production Considerations

### 1. Monitoring

Monitor these metrics:
- Sessions table size: `SELECT COUNT(*) FROM sessions;`
- Events table size: `SELECT COUNT(*) FROM session_events;`
- Oldest session: `SELECT MIN(created_at) FROM sessions;`
- Expired sessions: `SELECT COUNT(*) FROM sessions WHERE last_activity_at < NOW() - INTERVAL '30 minutes';`

### 2. Cleanup Strategy

Run cleanup worker to prevent unlimited growth:

```python
from session_manager import run_cleanup_loop, CleanupConfig

config = CleanupConfig(
    enabled=True,
    session_ttl_seconds=1800,
    cleanup_interval_seconds=300,  # Run every 5 minutes
    event_retention_days=30         # Delete events older than 30 days
)

# Run as background task
await run_cleanup_loop(provider, config)
```

### 3. Backup Strategy

```bash
# Daily backup
pg_dump -U user -d database -t sessions -t session_events -t session_archive > backup_$(date +%Y%m%d).sql

# Restore
psql -U user -d database < backup_20260313.sql
```

### 4. Performance Tuning

```sql
-- Analyze tables for query optimization
ANALYZE sessions;
ANALYZE session_events;
ANALYZE session_archive;

-- Vacuum to reclaim storage
VACUUM FULL sessions;
VACUUM FULL session_events;
```

## Next Steps

1. ✅ Run `setup_database.sql` in pgAdmin or psql
2. ✅ Verify tables exist
3. ✅ Grant permissions to app user
4. ✅ Test with sample data
5. ✅ Configure environment variables in your app
6. ✅ Initialize session_manager in your code
7. ✅ Setup cleanup worker for production

---

**Need Help?**
- See `README.md` for library usage
- See `example_usage.py` for code examples
- Check database logs for connection issues
