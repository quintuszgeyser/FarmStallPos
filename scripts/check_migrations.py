"""
Diagnostic script to check PostgreSQL migration status.
Run on Mini PC: .venv/Scripts/python.exe check_migrations.py
"""

from app import db, app
from sqlalchemy import text

def check_table_columns(table_name):
    """Check what columns exist in a table."""
    print(f"\n=== {table_name} table columns ===")
    try:
        result = db.session.execute(text(f"""
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_name = '{table_name}'
            ORDER BY ordinal_position
        """)).fetchall()

        if not result:
            print(f"  ❌ Table '{table_name}' does not exist")
            return False

        for col in result:
            print(f"  ✓ {col[0]:30} {col[1]:20} NULL={col[2]:3} DEFAULT={col[3]}")
        return True
    except Exception as e:
        print(f"  ❌ Error: {e}")
        return False

def check_required_columns():
    """Check if required new columns exist."""
    print("\n=== Required new columns check ===")

    required = {
        'customers': [
            'auto_enrolled',
            'customer_number',
            'first_seen',
            'is_employee'
        ],
        'sales': ['customer_id'],
        'customer_physical_attributes': ['id'],
        'visit_sessions': ['id'],
        'customer_signal_history': ['id'],
        'detection_events': ['id'],
        'person_tracks': ['id'],
        'till_detections': ['id'],
        'customer_conflicts': ['id'],
        'customer_exclusions': ['id']
    }

    for table, columns in required.items():
        print(f"\n{table}:")
        if columns == ['id']:
            # Check if table exists
            try:
                result = db.session.execute(text(f"""
                    SELECT EXISTS (
                        SELECT FROM information_schema.tables
                        WHERE table_name = '{table}'
                    )
                """)).scalar()
                if result:
                    print(f"  ✓ Table exists")
                else:
                    print(f"  ❌ Table missing")
            except Exception as e:
                print(f"  ❌ Error checking table: {e}")
        else:
            # Check specific columns
            for col in columns:
                try:
                    result = db.session.execute(text(f"""
                        SELECT EXISTS (
                            SELECT FROM information_schema.columns
                            WHERE table_name = '{table}' AND column_name = '{col}'
                        )
                    """)).scalar()
                    if result:
                        print(f"  ✓ {col}")
                    else:
                        print(f"  ❌ {col} MISSING")
                except Exception as e:
                    print(f"  ❌ {col} error: {e}")

def try_manual_migration():
    """Try to add missing columns manually with verbose error reporting."""
    print("\n=== Attempting manual migrations ===")

    migrations = [
        ("Make customers.name nullable",
         "ALTER TABLE customers ALTER COLUMN name DROP NOT NULL"),

        ("Add customers.auto_enrolled",
         "ALTER TABLE customers ADD COLUMN auto_enrolled BOOLEAN DEFAULT FALSE"),

        ("Add customers.customer_number",
         "ALTER TABLE customers ADD COLUMN customer_number VARCHAR(20)"),

        ("Add customers.first_seen",
         "ALTER TABLE customers ADD COLUMN first_seen TIMESTAMP DEFAULT NOW()"),

        ("Add customers.is_employee",
         "ALTER TABLE customers ADD COLUMN is_employee BOOLEAN DEFAULT FALSE"),

        ("Add unique index on customer_number",
         "CREATE UNIQUE INDEX idx_customers_number ON customers(customer_number) WHERE customer_number IS NOT NULL"),

        ("Add sales.customer_id",
         "ALTER TABLE sales ADD COLUMN customer_id INTEGER REFERENCES customers(id)"),

        ("Add index on sales.customer_id",
         "CREATE INDEX idx_sales_customer ON sales(customer_id)"),
    ]

    for desc, sql in migrations:
        print(f"\n{desc}")
        print(f"  SQL: {sql}")
        try:
            db.session.execute(text(sql))
            db.session.commit()
            print(f"  ✓ SUCCESS")
        except Exception as e:
            db.session.rollback()
            error_str = str(e)
            if "already exists" in error_str or "duplicate" in error_str.lower():
                print(f"  ⚠ Already exists (OK)")
            else:
                print(f"  ❌ FAILED: {e}")

if __name__ == '__main__':
    with app.app_context():
        print("=" * 80)
        print("PostgreSQL Migration Diagnostic")
        print("=" * 80)

        # Check all customers columns
        check_table_columns('customers')

        # Check sales columns
        check_table_columns('sales')

        # Check required columns
        check_required_columns()

        # Attempt manual migration
        print("\n" + "=" * 80)
        response = input("\nAttempt manual migrations? (y/n): ")
        if response.lower() == 'y':
            try_manual_migration()
            print("\n" + "=" * 80)
            print("Re-checking after manual migrations...")
            check_required_columns()

        print("\n" + "=" * 80)
        print("Done!")
