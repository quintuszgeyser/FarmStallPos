"""
Test script to verify anonymous customer creation works in QA.
Run: .venv/Scripts/python.exe test_customer_creation.py
"""

from app import db, app, Customer
from datetime import datetime
import os

# Force QA database
os.environ['FLASK_ENV'] = 'development'

if __name__ == '__main__':
    with app.app_context():
        print("Testing anonymous customer creation in QA database...")

        try:
            # Create test customer
            c = Customer(
                name=None,  # Anonymous
                auto_enrolled=True,
                customer_number='CUST-TEST-001',
                first_seen=datetime.utcnow(),
                enrolled_at=datetime.utcnow(),
                visit_count=0,
                active=True
            )

            db.session.add(c)
            db.session.commit()

            print(f"✓ SUCCESS!")
            print(f"  Customer ID: {c.id}")
            print(f"  Customer Number: {c.customer_number}")
            print(f"  Auto-enrolled: {c.auto_enrolled}")
            print(f"  Name: {c.name} (None = anonymous)")
            print(f"  First Seen: {c.first_seen}")

            # Clean up test customer
            db.session.delete(c)
            db.session.commit()
            print(f"\n✓ Test customer deleted (cleanup)")

        except Exception as e:
            print(f"❌ FAILED: {e}")
            db.session.rollback()
