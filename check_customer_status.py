from sqlalchemy import create_engine, text

engine = create_engine('postgresql://farmstall:FarmStall@localhost:5432/farm_pos')

with engine.connect() as conn:
    # Check customers
    result = conn.execute(text('''
        SELECT id, customer_number, name, auto_enrolled, first_seen, is_employee
        FROM customers
        ORDER BY id
        LIMIT 20
    '''))

    customers = list(result)
    print(f'\n=== CUSTOMERS ({len(customers)} total) ===')
    for row in customers:
        print(f'  ID={row.id}, number={row.customer_number}, name={row.name}, auto={row.auto_enrolled}, employee={row.is_employee}')

    # Check auto-enrolled specifically
    result = conn.execute(text('SELECT COUNT(*) as cnt FROM customers WHERE auto_enrolled = true'))
    auto_count = result.scalar()
    print(f'\nAuto-enrolled customers: {auto_count}')

    # Check if new tables exist
    tables = ['customer_physical_attributes', 'visit_sessions', 'customer_signal_history',
              'detection_events', 'person_tracks', 'till_detections']
    print(f'\n=== TABLE STATUS ===')
    for table in tables:
        try:
            result = conn.execute(text(f'SELECT COUNT(*) FROM {table}'))
            count = result.scalar()
            print(f'  {table}: ✓ ({count} rows)')
        except Exception as e:
            print(f'  {table}: ✗ ({str(e)[:50]})')

    # Check customer plates
    try:
        result = conn.execute(text('SELECT COUNT(*) FROM customer_plates'))
        print(f'  customer_plates: ✓ ({result.scalar()} rows)')
    except:
        print(f'  customer_plates: ✗')

    # Check customer faces
    try:
        result = conn.execute(text('SELECT COUNT(*) FROM customer_faces'))
        print(f'  customer_faces: ✓ ({result.scalar()} rows)')
    except:
        print(f'  customer_faces: ✗')

print('\nDone!')
