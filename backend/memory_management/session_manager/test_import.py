#!/usr/bin/env python
"""
Test script to verify session_manager can be imported without opik_template.
This validates that the library gracefully degrades when tracing is not available.
"""
import sys
import os

def test_basic_imports():
    """Test that core imports work without opik_template."""
    print("Testing basic imports without opik_template...")
    
    try:
        from memory_management.session_manager import (
            SessionConfig,
            session_config_from_env,
            SessionState,
            get_session_provider,
        )
        print("✓ Core imports successful")
        return True
    except ImportError as e:
        print(f"✗ Import failed: {e}")
        return False

def test_config_creation():
    """Test that SessionConfig can be created."""
    print("\nTesting SessionConfig creation...")
    
    try:
        from memory_management.session_manager import SessionConfig
        
        config = SessionConfig(
            enabled=True,
            provider="postgres",
            ttl_seconds=1800,
            cleanup_interval_seconds=300,
            compaction_enabled=True,
            compaction_turns_threshold=10,
            compaction_keep_last_n=20,
            compaction_max_turns=10,
        )
        print(f"✓ SessionConfig created: {config.provider}")
        return True
    except Exception as e:
        print(f"✗ Config creation failed: {e}")
        return False

def test_optional_tracing():
    """Test that tracing imports are optional."""
    print("\nTesting optional tracing...")
    
    try:
        # Try importing the modules that use tracing
        from memory_management.session_manager.workers import compaction
        from memory_management.session_manager.services import context_builder
        
        # Check if tracing is available
        has_tracing = hasattr(compaction, '_HAS_OPIK') and compaction._HAS_OPIK
        
        if has_tracing:
            print("✓ Tracing is available and enabled")
        else:
            print("✓ Tracing is not available, gracefully degraded")
        
        return True
    except Exception as e:
        print(f"✗ Optional tracing test failed: {e}")
        return False

def test_no_dotenv_side_effects():
    """Test that importing doesn't auto-load .env files."""
    print("\nTesting no dotenv side effects...")
    
    try:
        # Set a test env var before import
        test_key = "SESSION_MANAGER_TEST_VAR_SHOULD_NOT_CHANGE"
        test_value = "original_value"
        os.environ[test_key] = test_value
        
        # Import should not change environment
        import memory_management.session_manager
        
        if os.environ.get(test_key) == test_value:
            print("✓ No dotenv side effects detected")
            return True
        else:
            print("✗ Environment was modified by import")
            return False
    except Exception as e:
        print(f"✗ Dotenv test failed: {e}")
        return False

def main():
    """Run all tests."""
    print("=" * 60)
    print("Session Manager Library Tests")
    print("=" * 60)
    
    results = []
    
    results.append(("Basic Imports", test_basic_imports()))
    results.append(("Config Creation", test_config_creation()))
    results.append(("Optional Tracing", test_optional_tracing()))
    results.append(("No Dotenv Side Effects", test_no_dotenv_side_effects()))
    
    print("\n" + "=" * 60)
    print("Test Results:")
    print("=" * 60)
    
    for test_name, passed in results:
        status = "PASS" if passed else "FAIL"
        symbol = "✓" if passed else "✗"
        print(f"{symbol} {test_name}: {status}")
    
    all_passed = all(result[1] for result in results)
    
    print("=" * 60)
    if all_passed:
        print("All tests passed! ✓")
        return 0
    else:
        print("Some tests failed! ✗")
        return 1

if __name__ == "__main__":
    sys.exit(main())
