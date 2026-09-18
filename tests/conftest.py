"""
Rev 5 P0-1 — pytest harness core fixtures.

Test-DB lifecycle: `_test_postgres` brings up docker-compose.test.yml (or reuses
an already-running instance on port 5433 for fast local iteration) for the whole
session. `app` calls create_app() ONCE per session against that database —
strong_migrate() is idempotent DDL, not data, so re-running it per test would
only add latency, not safety.

Per-test isolation: `db_session` opens one Connection, begins an outer
transaction plus a SAVEPOINT, and temporarily replaces the app's default engine
(`db._app_engines[app][None]`) with that Connection. Flask-SQLAlchemy's
`Session.get_bind()` always returns `db.engines[None]` for any model with no
`__bind_key__` (see flask_sqlalchemy/session.py — every model in this app
qualifies), so this routes ALL ORM activity for the test, including the app's
own internal `db.session.commit()` calls, onto the one connection. An
`after_transaction_end` listener restarts the SAVEPOINT every time application
code commits, so a commit inside a route handler ends the SAVEPOINT but not the
outer transaction. Rolling back the outer transaction at teardown discards
everything the test did, still leaving the connection reusable next test.
"""
import os
import socket
import subprocess
import time

import pytest
import sqlalchemy as sa

TEST_DB_HOST = '127.0.0.1'
TEST_DB_PORT = 5433
TEST_DB_USER = 'farmpos_test'
TEST_DB_PASSWORD = 'farmpos_test_pw'
TEST_DB_NAME = 'farmpos_test'
TEST_DATABASE_URL = (
    f'postgresql://{TEST_DB_USER}:{TEST_DB_PASSWORD}@'
    f'{TEST_DB_HOST}:{TEST_DB_PORT}/{TEST_DB_NAME}'
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _pg_ready():
    result = subprocess.run(
        ['docker', 'exec', 'farmpos-postgres-test', 'pg_isready', '-U', TEST_DB_USER],
        capture_output=True,
    )
    return result.returncode == 0


def _port_open(host, port, timeout=0.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope='session')
def _test_postgres():
    already_running = _port_open(TEST_DB_HOST, TEST_DB_PORT)
    started_here = False
    if not already_running:
        subprocess.run(
            ['docker', 'compose', '-f', 'docker-compose.test.yml', 'up', '-d'],
            cwd=REPO_ROOT, check=True,
        )
        started_here = True

    for _ in range(60):
        if _pg_ready():
            break
        time.sleep(1)
    else:
        raise RuntimeError('Test Postgres (docker-compose.test.yml) did not become healthy in time')

    yield TEST_DATABASE_URL

    if started_here:
        subprocess.run(
            ['docker', 'compose', '-f', 'docker-compose.test.yml', 'down', '-v'],
            cwd=REPO_ROOT, check=False,
        )


@pytest.fixture(scope='session')
def app(_test_postgres):
    # Must be set before the first `import app` anywhere in the session — app.py
    # reads STORE_ID/APP_ENV as module-level constants at import time. Test posture
    # matches today's un-provisioned ("Lady Coleen box") path: STORE_ID unset, so the
    # SECRET_KEY boot guard (P1-2's target) does not fire and admin/admin123 seeds.
    os.environ['DATABASE_URL'] = _test_postgres
    os.environ.setdefault('APP_ENV', 'prod')
    os.environ.setdefault('SECRET_KEY', 'test-harness-secret-key-not-for-prod')
    os.environ.pop('STORE_ID', None)

    from app import create_app
    flask_app = create_app()
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    yield flask_app


@pytest.fixture()
def db_session(app):
    from models import db

    ctx = app.app_context()
    ctx.push()

    connection = db.engine.connect()
    outer_transaction = connection.begin()
    connection.begin_nested()

    real_engine = db._app_engines[app][None]
    db._app_engines[app][None] = connection

    session = db.session

    @sa.event.listens_for(session, 'after_transaction_end')
    def _restart_savepoint(sess, transaction):
        if transaction.nested and not transaction.parent.nested:
            if connection.closed:
                return
            connection.begin_nested()

    yield session

    sa.event.remove(session, 'after_transaction_end', _restart_savepoint)
    session.remove()
    db._app_engines[app][None] = real_engine
    outer_transaction.rollback()
    connection.close()
    ctx.pop()


@pytest.fixture()
def client(app, db_session):
    return app.test_client()
