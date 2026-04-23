"""
Example usage of session_manager library.

This demonstrates how to use session_manager in a real application,
including session management, event tracking, and compaction.
"""
import os
from typing import Callable
from dotenv import load_dotenv

# Load environment variables BEFORE importing
load_dotenv()

from memory_management.session_manager import (
    # Config
    session_config_from_env,
    SessionConfig,
    
    # Provider
    get_session_provider,
    
    # Session operations
    create_session,
    get_session,
    append_session_event,
    get_session_events,
    update_session_working_state,
    
    # Context building
    build_session_context,
    
    # Cleanup
    run_cleanup_once,
    CleanupConfig,
)

# Mock database connection factory for this example
def get_db_connection():
    """
    Replace this with your actual database connection factory.
    
    Example with psycopg2:
        import psycopg2
        return psycopg2.connect(
            host=os.environ["DB_HOST"],
            database=os.environ["DB_NAME"],
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"]
        )
    """
    import psycopg2
    return psycopg2.connect(os.environ.get("DATABASE_URL", ""))


def example_basic_session_flow():
    """Example: Create session, add events, retrieve context."""
    print("=" * 60)
    print("Example 1: Basic Session Flow")
    print("=" * 60)
    
    # 1. Initialize configuration from environment
    config = session_config_from_env()
    print(f"Config loaded: provider={config.provider}, enabled={config.enabled}")
    
    # 2. Get session provider
    provider = get_session_provider(config=config, get_connection=get_db_connection)
    print("Provider initialized")
    
    # 3. Create a new session
    run_id = "demo-conversation-001"
    user_id = "user-123"
    
    session = create_session(
        provider=provider,
        run_id=run_id,
        user_id=user_id,
        ttl_seconds=1800,
    )
    print(f"Session created: {session['run_id']}")
    
    # 4. Add conversation events
    append_session_event(
        provider=provider,
        run_id=run_id,
        user_id=user_id,
        role="user",
        content="What is the company vacation policy?"
    )
    print("Added user message")
    
    append_session_event(
        provider=provider,
        run_id=run_id,
        user_id=user_id,
        role="assistant",
        content="Our company offers 15 days of paid vacation per year..."
    )
    print("Added assistant response")
    
    # 5. Retrieve events
    events = get_session_events(provider=provider, run_id=run_id, limit=10)
    print(f"Retrieved {len(events)} events")
    
    # 6. Build context for next LLM call
    context_str, compaction_queued, _, _ = build_session_context(
        run_id=run_id,
        user_id=user_id,
        provider=provider,
        compaction_config={
            "compaction_enabled": config.compaction_enabled,
            "turns_threshold": config.compaction_turns_threshold,
            "max_turns": config.compaction_max_turns,
            "keep_last_n": config.compaction_keep_last_n,
        }
    )
    print(f"Context built ({len(context_str)} chars), compaction queued: {compaction_queued}")
    print()


def example_working_state_tracking():
    """Example: Track structured conversation state."""
    print("=" * 60)
    print("Example 2: Working State Tracking")
    print("=" * 60)
    
    config = session_config_from_env()
    provider = get_session_provider(config=config, get_connection=get_db_connection)
    
    run_id = "demo-investigation-001"
    user_id = "user-456"
    
    # Create session
    session = create_session(provider=provider, run_id=run_id, user_id=user_id)
    
    # Update working state with structured investigation data
    state_patch = {
        "investigation": {
            "time_window": "2024-01-15 to 2024-01-20",
            "primary_service": "payment-service",
            "incident_ids": ["INC-001", "INC-002"]
        },
        "hypotheses": [
            {
                "description": "Database connection pool exhaustion",
                "confidence": "high",
                "status": "investigating"
            },
            {
                "description": "Memory leak in payment processor",
                "confidence": "medium",
                "status": "pending"
            }
        ],
        "progress": {
            "completed_steps": [
                "Gathered incident logs",
                "Identified time window"
            ],
            "current_focus": "Analyzing database metrics",
            "blockers": []
        }
    }
    
    success = update_session_working_state(
        provider=provider,
        user_id=user_id,
        run_id=run_id,
        patch=state_patch,
        version=session.get("version", 0)
    )
    print(f"Working state updated: {success}")
    
    # Retrieve updated session
    updated_session = get_session(provider=provider, run_id=run_id)
    working_state = updated_session.get("working_state", {})
    print(f"Investigation: {working_state.get('investigation', {}).get('primary_service')}")
    print(f"Hypotheses count: {len(working_state.get('hypotheses', []))}")
    print()


def example_compaction():
    """Example: Manual compaction with LLM summarization."""
    print("=" * 60)
    print("Example 3: Manual Compaction")
    print("=" * 60)
    
    from memory_management.session_manager.workers.compaction import run_compaction
    
    config = session_config_from_env()
    provider = get_session_provider(config=config, get_connection=get_db_connection)
    
    run_id = "demo-long-conversation-001"
    user_id = "user-789"
    
    # Create session with multiple events
    create_session(provider=provider, run_id=run_id, user_id=user_id)
    
    # Add multiple conversation turns (simulating a long conversation)
    for i in range(15):
        append_session_event(
            provider=provider,
            run_id=run_id,
            user_id=user_id,
            role="user",
            content=f"Question {i+1}: Tell me about topic {i+1}"
        )
        append_session_event(
            provider=provider,
            run_id=run_id,
            user_id=user_id,
            role="assistant",
            content=f"Answer {i+1}: Here's information about topic {i+1}..."
        )
    
    print("Added 15 conversation turns (30 events)")
    
    # Mock LLM summarizer
    def mock_llm_summarize(prompt: str) -> str:
        """Replace this with your actual LLM call."""
        return "Summary: User asked about multiple topics. Assistant provided detailed answers."
    
    # Run compaction
    success = run_compaction(
        run_id=run_id,
        user_id=user_id,
        provider=provider,
        llm_summarize=mock_llm_summarize,
        turns_threshold=10,
        token_budget=4000,
        keep_last_n=20,
    )
    print(f"Compaction completed: {success}")
    
    # Check compacted summary
    session = get_session(provider=provider, run_id=run_id)
    summary = session.get("compacted_summary", "")
    print(f"Compacted summary: {summary[:100]}...")
    print()


def example_cleanup():
    """Example: Run session cleanup."""
    print("=" * 60)
    print("Example 4: Session Cleanup")
    print("=" * 60)
    
    config = session_config_from_env()
    provider = get_session_provider(config=config, get_connection=get_db_connection)
    
    cleanup_config = CleanupConfig(
        enabled=True,
        session_ttl_seconds=1800,
        cleanup_interval_seconds=300,
        event_retention_days=30
    )
    
    # Run cleanup once (in production, use run_cleanup_loop for continuous cleanup)
    result = run_cleanup_once(provider=provider, config=cleanup_config)
    
    print(f"Sessions cleaned: {result.sessions_deleted}")
    print(f"Events cleaned: {result.events_deleted}")
    print(f"Duration: {result.duration_ms}ms")
    print()


def example_configuration_methods():
    """Example: Different ways to configure session_manager."""
    print("=" * 60)
    print("Example 5: Configuration Methods")
    print("=" * 60)
    
    # Method 1: From environment variables
    config_env = session_config_from_env()
    print(f"Method 1 (env): provider={config_env.provider}")
    
    # Method 2: Direct instantiation
    config_direct = SessionConfig(
        enabled=True,
        provider="postgres",
        ttl_seconds=3600,
        cleanup_interval_seconds=600,
        compaction_enabled=True,
        compaction_turns_threshold=15,
        compaction_keep_last_n=25,
        compaction_max_turns=12,
    )
    print(f"Method 2 (direct): ttl={config_direct.ttl_seconds}s")
    
    # Method 3: From your app's settings object
    class AppSettings:
        session_enabled = True
        session_provider = "postgres"
        session_ttl_seconds = 1800
        session_cleanup_interval_seconds = 300
        compaction_enabled = True
        compaction_turns_threshold = 10
        compaction_keep_last_n = 20
        compaction_max_turns = 10
        compaction_token_budget = 4000
        compaction_use_llm = True
        compaction_llm_model = None
    
    from memory_management.session_manager import session_config_from_settings
    config_settings = session_config_from_settings(AppSettings())
    print(f"Method 3 (settings): compaction_threshold={config_settings.compaction_turns_threshold}")
    print()


def main():
    """Run all examples."""
    print("\n" + "=" * 60)
    print("Session Manager Library - Usage Examples")
    print("=" * 60 + "\n")
    
    try:
        # Check if we can connect to database
        conn = get_db_connection()
        conn.close()
        print("Database connection successful\n")
    except Exception as e:
        print(f"Warning: Could not connect to database: {e}")
        print("Most examples will fail without a database connection.\n")
        return
    
    try:
        example_basic_session_flow()
        example_working_state_tracking()
        example_compaction()
        example_cleanup()
        example_configuration_methods()
        
        print("=" * 60)
        print("All examples completed successfully!")
        print("=" * 60)
        
    except Exception as e:
        print(f"\nError running examples: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
