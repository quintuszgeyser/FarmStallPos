"""Test-only helpers (Rev 5 P0-1) — distinct from the app's own helpers.py."""
from decimal import Decimal


def D(x):
    """Decimal from a literal without the float-repr trap (Decimal(0.1) != Decimal('0.1'))."""
    return Decimal(str(x))


def login_as(client, username, password):
    resp = client.post('/api/login', json={'username': username, 'password': password})
    assert resp.status_code == 200, f'login failed: {resp.status_code} {resp.get_json()}'
    return resp.get_json()


def checkout(client, cart, payment_method='cash', **extra):
    body = {'cart': cart, 'payment_method': payment_method, **extra}
    return client.post('/api/transactions', json=body)


def refresh(session, obj):
    """session.refresh(obj) alone can silently revert a pending-but-unflushed
    mutation to its stale DB value: SQLAlchemy 2.0's Session.refresh()
    expires the object's attributes BEFORE it autoflushes (see the
    "autoflush up front" comment in sqlalchemy.orm.session.Session.refresh),
    so a change made in-process (e.g. by a helper like consume_fifo that
    doesn't commit) can be dropped right before the autoflush that was
    supposed to persist it. Always flush explicitly first — this wrapper
    exists so that mistake doesn't need rediscovering in every test file.
    """
    session.flush()
    session.refresh(obj)
