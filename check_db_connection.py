"""
Test database connection from Mini PC.
Run: .venv\Scripts\python.exe check_db_connection.py
"""

import os
os.environ['DATABASE_URL'] = 'postgresql://farmstall:FarmStall@localhost:5432/farm_pos'

from sqlalchemy import create_engine, text

def test_connection():
    """Test PostgreSQL connection."""
    db_url = os.environ['DATABASE_URL']

    # Rewrite to psycopg driver
    if db_url.startswith('postgres://'):
        db_url = db_url.replace('postgres://', 'postgresql+psycopg://', 1)

    print(f"Testing connection to: {db_url.split('@')[1]}")  # Hide password

    try:
        engine = create_engine(db_url, pool_pre_ping=True)
        with engine.connect() as conn:
            result = conn.execute(text('SELECT 1'))
            print("  ✓ Connection successful")

            # Test customers table
            result = conn.execute(text('SELECT COUNT(*) FROM customers'))
            count = result.scalar()
            print(f"  ✓ Customers table: {count} rows")

            # Test if new columns exist
            result = conn.execute(text("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'customers'
                AND column_name IN ('auto_enrolled', 'customer_number', 'first_seen')
            """))
            cols = [row[0] for row in result]
            print(f"  ✓ New columns found: {cols}")

            return True
    except Exception as e:
        print(f"  ✗ Connection failed: {e}")
        return False

if __name__ == '__main__':
    print("=" * 80)
    print("Database Connection Test")
    print("=" * 80)
    test_connection()
