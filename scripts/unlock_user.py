#!/usr/bin/env python3
"""
Rev 5 P1-3 — documented CLI unlock for a durably-locked account.

    DATABASE_URL=postgresql://... python scripts/unlock_user.py <username>

blueprints/auth.py locks a username after LOCKOUT_MAX_FAILURES (5) failed
logins within LOCKOUT_WINDOW (15 minutes), evaluated by counting recent
`login_attempts` rows with success=False for that username — there is no
separate "locked" flag to flip. This script clears a lock by inserting one
synthetic success=True row timestamped now: the lockout query only counts
failures, so a fresh success row resets the failure window without deleting
the forensic trail of what actually happened (the original failure rows are
left intact — this is an unlock, not a cover-up).

Writes an AuditLog event_type='account_unlocked' row so the unlock itself is
traceable. There is no web session / user_id for a CLI-initiated action, so
the note records the OS user that ran the script (the closest available
identifier) rather than fabricating an actor_user_id.

Like scripts/reconcile.py and scripts/backfill_vat_headers.py, this connects
via a plain SQLAlchemy engine bound to DATABASE_URL and never calls
create_app()/strong_migrate() — an operator unlocking one account should
never have a side effect of re-running startup migrations on a live box.

Exit codes: 0 = unlocked, 1 = user not found or bad args.
"""
import argparse
import getpass
import os
import sys
from datetime import datetime

import sqlalchemy as sa


def _db_url():
    url = os.environ.get('DATABASE_URL', 'sqlite:///pos.db')
    if url.startswith('postgres://'):
        url = url.replace('postgres://', 'postgresql+psycopg://', 1)
    elif url.startswith('postgresql://') and '+psycopg://' not in url:
        url = 'postgresql+psycopg://' + url.split('://', 1)[1]
    return url


def unlock(engine, username):
    with engine.connect() as conn:
        with conn.begin():
            exists = conn.execute(sa.text(
                "SELECT 1 FROM users WHERE username = :u"
            ), {'u': username}).scalar()
            if not exists:
                return False

            conn.execute(sa.text("""
                INSERT INTO login_attempts (username, ip, success, attempted_at)
                VALUES (:u, 'cli', TRUE, :now)
            """), {'u': username, 'now': datetime.utcnow()})

            conn.execute(sa.text("""
                INSERT INTO audit_log (created_at, event_type, actor_user_id,
                                        target_table, target_id, note)
                VALUES (:now, 'account_unlocked', NULL, 'users', :u, :note)
            """), {
                'now': datetime.utcnow(), 'u': username,
                'note': f'CLI unlock by OS user {getpass.getuser()!r}; no web session actor available',
            })
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('username')
    args = parser.parse_args()

    engine = sa.create_engine(_db_url())
    if unlock(engine, args.username):
        print(f"Unlocked: {args.username}")
        sys.exit(0)
    else:
        print(f"No such user: {args.username}")
        sys.exit(1)


if __name__ == '__main__':
    main()
