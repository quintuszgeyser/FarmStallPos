"""
Script to create the 8 new tables for auto-enrollment system.
Run on Mini PC: .venv/Scripts/python.exe create_new_tables.py
"""

from app import db, app
from sqlalchemy import text

def create_tables():
    """Create all new tables for Phase 1."""

    tables = [
        ("customer_physical_attributes", """
            CREATE TABLE customer_physical_attributes (
                id SERIAL PRIMARY KEY,
                customer_id INTEGER REFERENCES customers(id) NOT NULL,
                height_cm INTEGER,
                hair_color VARCHAR(20),
                skin_tone VARCHAR(20),
                build VARCHAR(20),
                eye_color VARCHAR(20),
                age_range VARCHAR(20),
                gender VARCHAR(10),
                wearing_glasses BOOLEAN,
                facial_hair VARCHAR(20),
                detected_at TIMESTAMP DEFAULT NOW(),
                camera_source VARCHAR(50),
                confidence NUMERIC(3,2)
            )
        """),

        ("customer_physical_attributes indexes", """
            CREATE INDEX idx_physical_attrs_customer ON customer_physical_attributes(customer_id);
            CREATE INDEX idx_physical_attrs_height ON customer_physical_attributes(height_cm);
            CREATE INDEX idx_physical_attrs_hair ON customer_physical_attributes(hair_color);
            CREATE INDEX idx_physical_attrs_build ON customer_physical_attributes(build)
        """),

        ("visit_sessions", """
            CREATE TABLE visit_sessions (
                id SERIAL PRIMARY KEY,
                customer_id INTEGER REFERENCES customers(id) NOT NULL,
                session_start TIMESTAMP NOT NULL,
                session_end TIMESTAMP,
                entry_camera VARCHAR(50),
                checkout_camera VARCHAR(50),
                dwell_seconds INTEGER,
                purchase_made BOOLEAN DEFAULT FALSE,
                sale_ids TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """),

        ("visit_sessions indexes", """
            CREATE INDEX idx_visit_sessions_customer ON visit_sessions(customer_id);
            CREATE INDEX idx_visit_sessions_start ON visit_sessions(session_start)
        """),

        ("customer_signal_history", """
            CREATE TABLE customer_signal_history (
                id SERIAL PRIMARY KEY,
                customer_id INTEGER REFERENCES customers(id) NOT NULL,
                signal_type VARCHAR(20) NOT NULL,
                confidence NUMERIC(5,3),
                camera_source VARCHAR(50),
                detected_at TIMESTAMP DEFAULT NOW()
            )
        """),

        ("customer_signal_history indexes", """
            CREATE INDEX idx_signal_history_customer ON customer_signal_history(customer_id, signal_type)
        """),

        ("detection_events", """
            CREATE TABLE detection_events (
                id SERIAL PRIMARY KEY,
                event_id VARCHAR(50) UNIQUE,
                camera_source VARCHAR(50) NOT NULL,
                camera_zone VARCHAR(20),
                object_type VARCHAR(20),
                detected_at TIMESTAMP NOT NULL,
                snapshot_path TEXT,
                processed BOOLEAN DEFAULT FALSE,
                plate_number VARCHAR(20),
                plate_confidence NUMERIC(3,2),
                person_bbox TEXT,
                face_embedding BYTEA,
                gait_features BYTEA,
                physical_attributes TEXT,
                tracked_person_id INTEGER,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """),

        ("detection_events indexes", """
            CREATE INDEX idx_detection_events_time ON detection_events(detected_at);
            CREATE INDEX idx_detection_events_camera_zone ON detection_events(camera_zone, detected_at);
            CREATE INDEX idx_detection_events_person ON detection_events(tracked_person_id);
            CREATE INDEX idx_detection_events_unprocessed ON detection_events(processed) WHERE processed = FALSE
        """),

        ("person_tracks", """
            CREATE TABLE person_tracks (
                id SERIAL PRIMARY KEY,
                track_uuid VARCHAR(36) UNIQUE,
                customer_id INTEGER REFERENCES customers(id),
                first_seen TIMESTAMP NOT NULL,
                entry_camera VARCHAR(50),
                entry_zone VARCHAR(20),
                associated_plate VARCHAR(20),
                last_seen TIMESTAMP,
                last_camera VARCHAR(50),
                last_zone VARCHAR(20),
                height_cm_avg INTEGER,
                hair_color_consensus VARCHAR(20),
                skin_tone_consensus VARCHAR(20),
                build_consensus VARCHAR(20),
                gender_consensus VARCHAR(10),
                best_face_embedding BYTEA,
                best_gait_features BYTEA,
                enrolled_as_customer BOOLEAN DEFAULT FALSE,
                session_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """),

        ("person_tracks indexes", """
            CREATE INDEX idx_person_tracks_active ON person_tracks(session_active, last_seen);
            CREATE INDEX idx_person_tracks_plate ON person_tracks(associated_plate);
            CREATE INDEX idx_person_tracks_customer ON person_tracks(customer_id)
        """),

        ("till_detections", """
            CREATE TABLE till_detections (
                id SERIAL PRIMARY KEY,
                customer_id INTEGER REFERENCES customers(id) NOT NULL,
                detected_at TIMESTAMP DEFAULT NOW(),
                camera_source VARCHAR(50)
            )
        """),

        ("till_detections indexes", """
            CREATE INDEX idx_till_detections_time ON till_detections(detected_at DESC)
        """),

        ("customer_conflicts", """
            CREATE TABLE customer_conflicts (
                id SERIAL PRIMARY KEY,
                customer_id_a INTEGER REFERENCES customers(id) NOT NULL,
                customer_id_b INTEGER REFERENCES customers(id) NOT NULL,
                conflicting_signal VARCHAR(20),
                confidence_a NUMERIC(5,3),
                confidence_b NUMERIC(5,3),
                detected_at TIMESTAMP DEFAULT NOW(),
                resolved BOOLEAN DEFAULT FALSE,
                merged_into INTEGER REFERENCES customers(id)
            )
        """),

        ("customer_conflicts indexes", """
            CREATE INDEX idx_conflicts_unresolved ON customer_conflicts(resolved) WHERE resolved = FALSE
        """),

        ("customer_exclusions", """
            CREATE TABLE customer_exclusions (
                id SERIAL PRIMARY KEY,
                customer_id_a INTEGER REFERENCES customers(id),
                customer_id_b INTEGER REFERENCES customers(id),
                reason VARCHAR(200),
                created_at TIMESTAMP DEFAULT NOW()
            )
        """),
    ]

    print("=" * 80)
    print("Creating Phase 1 Tables")
    print("=" * 80)

    for name, sql in tables:
        print(f"\n{name}")
        print(f"  SQL: {sql[:100]}...")
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

    print("\n" + "=" * 80)
    print("Verifying tables...")
    print("=" * 80)

    # Verify all tables exist
    table_names = [
        'customer_physical_attributes',
        'visit_sessions',
        'customer_signal_history',
        'detection_events',
        'person_tracks',
        'till_detections',
        'customer_conflicts',
        'customer_exclusions'
    ]

    for table in table_names:
        try:
            result = db.session.execute(text(f"""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_name = '{table}'
                )
            """)).scalar()
            if result:
                print(f"  ✓ {table}")
            else:
                print(f"  ❌ {table} MISSING")
        except Exception as e:
            print(f"  ❌ {table} error: {e}")

if __name__ == '__main__':
    with app.app_context():
        create_tables()
        print("\n" + "=" * 80)
        print("Done! All Phase 1 tables created.")
        print("Next step: Restart FarmPOS-prod service to test")
        print("=" * 80)
