#!/usr/bin/env python
"""
Database setup script for session_manager library.
Automatically creates required PostgreSQL tables and indexes.

Usage:
    python setup_db.py --help
    python setup_db.py --host localhost --database mydb --user postgres
    python setup_db.py --url postgresql://user:pass@localhost:5432/mydb
"""
import argparse
import sys
from pathlib import Path

try:
    import psycopg2
    from psycopg2 import sql
except ImportError:
    print("Error: psycopg2 not installed")
    print("Install with: pip install psycopg2-binary")
    sys.exit(1)


def read_sql_script():
    """Read the setup_database.sql file."""
    script_path = Path(__file__).parent / "setup_database.sql"
    
    if not script_path.exists():
        print(f"Error: SQL script not found at {script_path}")
        sys.exit(1)
    
    with open(script_path, 'r', encoding='utf-8') as f:
        return f.read()


def create_connection(host=None, port=None, database=None, user=None, password=None, url=None):
    """Create database connection from parameters or URL."""
    try:
        if url:
            conn = psycopg2.connect(url)
        else:
            conn_params = {}
            if host:
                conn_params['host'] = host
            if port:
                conn_params['port'] = port
            if database:
                conn_params['database'] = database
            if user:
                conn_params['user'] = user
            if password:
                conn_params['password'] = password
            
            conn = psycopg2.connect(**conn_params)
        
        return conn
    except psycopg2.Error as e:
        print(f"Error connecting to database: {e}")
        sys.exit(1)


def verify_tables(conn):
    """Verify that required tables were created (session_registry, session_working_state, session_conversation_turns)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_schema = 'public' 
              AND table_name IN ('session_registry', 'session_working_state', 'session_conversation_turns')
            ORDER BY table_name;
        """)
        tables = [row[0] for row in cur.fetchall()]
        return tables


def verify_indexes(conn):
    """Verify that required indexes were created."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT indexname 
            FROM pg_indexes 
            WHERE schemaname = 'public' 
              AND tablename IN ('session_registry', 'session_working_state', 'session_conversation_turns')
            ORDER BY indexname;
        """)
        indexes = [row[0] for row in cur.fetchall()]
        return indexes


def setup_database(conn, verbose=False):
    """Run the database setup script."""
    sql_script = read_sql_script()
    
    if verbose:
        print("Executing database setup script...")
    
    try:
        with conn.cursor() as cur:
            # Execute the script
            cur.execute(sql_script)
            conn.commit()
            
        if verbose:
            print("✓ SQL script executed successfully")
        
        # Verify tables
        tables = verify_tables(conn)
        if verbose:
            print(f"\n✓ Tables created: {', '.join(tables)}")
        
        if len(tables) < 3:
            print(f"Warning: Only {len(tables)} of 3 expected tables were created")
            return False
        
        # Verify indexes
        indexes = verify_indexes(conn)
        if verbose:
            print(f"✓ Indexes created: {len(indexes)} indexes")
        
        return True
        
    except psycopg2.Error as e:
        print(f"Error executing setup script: {e}")
        conn.rollback()
        return False


def insert_test_data(conn):
    """Insert test data for verification (session_registry, session_working_state, session_conversation_turns)."""
    try:
        run_id, user_id = 'setup-test-001', 'setup-test-user'
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO session_registry (run_id, user_id) VALUES (%s, %s)
                ON CONFLICT (user_id, run_id) DO NOTHING;
            """, (run_id, user_id))
            cur.execute("""
                INSERT INTO session_working_state (run_id, user_id, ttl_seconds) VALUES (%s, %s, 1800)
                ON CONFLICT (user_id, run_id) DO NOTHING;
            """, (run_id, user_id))
            cur.execute("""
                INSERT INTO session_conversation_turns (run_id, user_id, seq, role, content) VALUES
                    (%s, %s, 1, 'user', 'Setup test question'),
                    (%s, %s, 2, 'assistant', 'Setup test answer')
                ON CONFLICT (run_id, seq) DO NOTHING;
            """, (run_id, user_id, run_id, user_id))
            conn.commit()

            cur.execute("SELECT COUNT(*) FROM session_registry WHERE run_id = %s;", (run_id,))
            registry_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM session_working_state WHERE run_id = %s;", (run_id,))
            state_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM session_conversation_turns WHERE run_id = %s;", (run_id,))
            turns_count = cur.fetchone()[0]

            cur.execute("DELETE FROM session_conversation_turns WHERE run_id = %s;", (run_id,))
            cur.execute("DELETE FROM session_working_state WHERE user_id = %s AND run_id = %s;", (user_id, run_id))
            cur.execute("DELETE FROM session_registry WHERE user_id = %s AND run_id = %s;", (user_id, run_id))
            conn.commit()

            return registry_count == 1 and state_count == 1 and turns_count == 2

    except psycopg2.Error as e:
        print(f"Error during test: {e}")
        conn.rollback()
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Set up database tables for session_manager library",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Using connection URL
  python setup_db.py --url postgresql://user:pass@localhost:5432/mydb
  
  # Using individual parameters
  python setup_db.py --host localhost --database mydb --user postgres --password secret
  
  # From environment variable
  export DATABASE_URL=postgresql://user:pass@localhost:5432/mydb
  python setup_db.py --url $DATABASE_URL
  
  # With test data
  python setup_db.py --url $DATABASE_URL --test
  
  # Quiet mode
  python setup_db.py --url $DATABASE_URL --quiet
        """
    )
    
    # Connection options
    parser.add_argument('--url', help='Database URL (postgresql://user:pass@host:port/database)')
    parser.add_argument('--host', help='Database host')
    parser.add_argument('--port', type=int, default=5432, help='Database port (default: 5432)')
    parser.add_argument('--database', help='Database name')
    parser.add_argument('--user', help='Database user')
    parser.add_argument('--password', help='Database password')
    
    # Options
    parser.add_argument('--test', action='store_true', help='Insert and verify test data')
    parser.add_argument('--quiet', action='store_true', help='Minimal output')
    parser.add_argument('--verbose', action='store_true', help='Verbose output')
    
    args = parser.parse_args()
    
    # Validate arguments
    if not args.url and not (args.host and args.database and args.user):
        parser.print_help()
        print("\nError: Must provide either --url or (--host, --database, --user)")
        sys.exit(1)
    
    verbose = args.verbose and not args.quiet
    
    if not args.quiet:
        print("=" * 60)
        print("Session Manager - Database Setup")
        print("=" * 60)
    
    # Create connection
    if verbose:
        if args.url:
            # Mask password in URL for display
            display_url = args.url
            if '@' in display_url:
                parts = display_url.split('@')
                if ':' in parts[0]:
                    user_pass = parts[0].split('://')[-1]
                    if ':' in user_pass:
                        user = user_pass.split(':')[0]
                        display_url = display_url.replace(user_pass, f"{user}:****")
            print(f"\nConnecting to: {display_url}")
        else:
            print(f"\nConnecting to: {args.host}:{args.port}/{args.database}")
    
    conn = create_connection(
        host=args.host,
        port=args.port,
        database=args.database,
        user=args.user,
        password=args.password,
        url=args.url
    )
    
    if verbose:
        print("✓ Connected to database")
    
    try:
        # Run setup
        success = setup_database(conn, verbose=verbose)
        
        if not success:
            print("\n✗ Database setup failed")
            sys.exit(1)
        
        # Run test if requested
        if args.test:
            if verbose:
                print("\nRunning test data insertion...")
            
            test_success = insert_test_data(conn)
            
            if test_success:
                if verbose:
                    print("✓ Test data inserted and verified successfully")
            else:
                print("✗ Test data verification failed")
                sys.exit(1)
        
        if not args.quiet:
            print("\n" + "=" * 60)
            print("✓ Database setup complete!")
            print("=" * 60)
            print("\nYou can now use the session_manager library.")
            print("See README.md for usage instructions.")
        
        return 0
        
    finally:
        conn.close()
        if verbose:
            print("\n✓ Database connection closed")


if __name__ == "__main__":
    sys.exit(main())
