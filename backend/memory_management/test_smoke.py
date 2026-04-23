"""
Smoke tests for the memory_management library (run after: pip install -e ./memory_management).
Tests session and memory entry points without changing the application.
"""
import sys


def test_memory_noop():
    """Memory: set config (noop), get provider, search returns []."""
    from memory_management.memory import set_memory_config, get_memory_provider

    set_memory_config({"memory_enabled": False, "memory_provider": "noop"})
    provider = get_memory_provider()
    assert provider.search("test", "user1") == []
    assert provider.get_all("user1") == []
    print("  OK memory (noop)")


def test_session_imports():
    """Session: import main entry points from memory_management.session_manager."""
    from memory_management.session_manager import (
        SessionConfig,
        get_session_provider,
        session_config_from_env,
    )

    try:
        config = session_config_from_env()
        assert config is not None
    except Exception as e:
        # Env or DB not set in test env is OK; we only care that imports work
        print("  OK session imports (config_from_env: %s)" % (e.__class__.__name__,))
        return
    print("  OK session imports and config_from_env")


def test_root_imports():
    """Root: from memory_management import get_session_provider, get_memory_provider, set_memory_config."""
    from memory_management import (
        get_memory_provider,
        get_session_provider,
        set_memory_config,
    )

    set_memory_config({"memory_enabled": False})
    p_mem = get_memory_provider()
    p_ses = get_session_provider()
    assert p_mem is not None
    assert p_ses is not None
    print("  OK root imports")


def main():
    print("Smoke tests (memory_management)...")
    test_memory_noop()
    test_session_imports()
    test_root_imports()
    print("All smoke tests passed.")


if __name__ == "__main__":
    main()
    sys.exit(0)
