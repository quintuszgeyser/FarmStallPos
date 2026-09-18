"""
Rev 5 P1-2 — boot-guard characterization tests.

STORE_ID is captured as a module-level constant at import time (app.py:51), so
exercising both branches of the SECRET_KEY guard (app.py:2431-2437) requires a
fresh process per case, not env-var patching + importlib.reload() within the
pytest process (reload doesn't reliably re-run module-level assignments and
leaves stale references in anything that already did `from app import ...`).
A subprocess is also the more faithful characterization here: Rev 5's own
P1-2 proof requirement is "app refuses to START" — an actual process boot,
not an in-process function call.

These pin TODAY'S behaviour. P1-2's second commit (removing the STORE_ID gate
so the guard is unconditional) has NOT landed — see appliance/verify-secrets.sh
and the P1-2 completion report for why it's gated on fleet evidence this
environment can't produce. Update
test_boot_guard_allows_default_secret_when_store_id_unset in the same commit
that removes the gate; it pins a deliberate, documented exemption, not a bug
to fix quietly.

Unlike the rest of the suite, these tests do NOT go through the SAVEPOINT-based
per-test isolation in conftest.py — each spawns a real `python -c "import app;
app.create_app()"` subprocess against the shared test Postgres. That's the only
way to observe a real module-import-time RuntimeError. create_app()'s startup
(strong_migrate, seed_first_admin, etc.) is idempotent DDL/seed, so repeated
real boots against the shared test DB converge safely and don't corrupt
per-test isolation for the rest of the suite.
"""
import os
import subprocess
import sys

from .conftest import TEST_DATABASE_URL, REPO_ROOT


def _boot(store_id, secret_key):
    env = os.environ.copy()
    env['DATABASE_URL'] = TEST_DATABASE_URL
    env.pop('STORE_ID', None)
    if store_id is not None:
        env['STORE_ID'] = store_id
    env.pop('SECRET_KEY', None)
    if secret_key is not None:
        env['SECRET_KEY'] = secret_key
    return subprocess.run(
        [sys.executable, '-c', 'import app; app.create_app()'],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=90,
    )


def test_boot_guard_raises_on_default_secret_when_store_id_set():
    result = _boot(store_id='test-store', secret_key=None)
    assert result.returncode != 0
    assert 'SECRET_KEY is unset on provisioned store' in result.stderr


def test_boot_guard_allows_default_secret_when_store_id_unset():
    # Deliberate, documented exemption (app.py:2429-2430 comment) — the original
    # Lady Coleen box never set STORE_ID and keeps its historical behaviour.
    # This is the exact gap P1-2's second commit closes, gated on fleet-wide
    # secret verification (appliance/verify-secrets.sh) that requires real SSH
    # access this session doesn't have.
    result = _boot(store_id=None, secret_key=None)
    assert result.returncode == 0, result.stderr


def test_boot_guard_passes_when_store_id_set_and_real_secret_provided():
    result = _boot(store_id='test-store', secret_key='a-real-unique-secret-key-value')
    assert result.returncode == 0, result.stderr
