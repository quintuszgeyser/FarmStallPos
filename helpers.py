"""
Shared utilities - imported by app.py and (eventually) blueprints.
Import order: helpers → models → db. Never import from app.py here.
"""

import json
import logging
import os
import re
import uuid
import random
from decimal import Decimal
from datetime import datetime, timedelta

logger = logging.getLogger('helpers')

from flask import session, abort, request, jsonify, make_response, g
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash

from decimal import ROUND_HALF_UP

from models import (
    db,
    User, UserSession, Setting,
    Product, ProductImage, RecipeLine, Category,
    StockBatch, StockConsumption, StockAdjustment, StockMovement,
    Sale, Purchase,
    ConsignmentLiability,
    ProductPurchaseOption,
    Supplier,
    AuditLog,
    SESSION_TIMEOUT_MINUTES, SESSION_LOGOUT_HOURS,
)

# STORE_ID mirrors app.py's own module-level read of the same env var (never
# imported from app.py — see the import-order rule above). Used only to stamp
# AuditLog.store_id.
STORE_ID = os.environ.get('STORE_ID', '').strip()


def qty_bucket(qty):
    """Map a sale quantity to a bucket for packaging suggestions.
    Bucket 1=1-2, 2=3-6, 3=7-12, 4=12+
    """
    try:
        qty = int(qty or 0)
    except (TypeError, ValueError):
        qty = 1
    if qty <= 2:  return 1
    if qty <= 6:  return 2
    if qty <= 12: return 3
    return 4


# ---------------------------------------------------------------------------
# Category helpers
# ---------------------------------------------------------------------------

def normalize_category_name(name):
    """Trim and collapse internal whitespace. Returns '' for None/blank."""
    return re.sub(r'\s+', ' ', (name or '').strip())


def get_or_create_category(name):
    """Resolve a category by case-insensitive normalized name, creating it if
    it does not yet exist. Returns the Category row, or None when name is blank.
    Does NOT commit - caller's transaction owns the flush/commit."""
    clean = normalize_category_name(name)
    if not clean:
        return None
    norm = clean.lower()
    cat = Category.query.filter_by(name_norm=norm).first()
    if cat:
        return cat
    cat = Category(name=clean, name_norm=norm)
    db.session.add(cat)
    db.session.flush()
    return cat


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

def get_setting(key, default=None):
    s = Setting.query.filter_by(key=key).first()
    return s.value if s else default


def set_setting(key, value):
    s = Setting.query.filter_by(key=key).first()
    if s:
        s.value = str(value)
    else:
        s = Setting(key=key, value=str(value))
        db.session.add(s)
    db.session.commit()


# ---------------------------------------------------------------------------
# Auth helpers - no dependency on the Flask app object
# ---------------------------------------------------------------------------

def current_user():
    if 'user_id' not in session:
        return None
    return db.session.get(User, session.get('user_id'))


# ---------------------------------------------------------------------------
# Audit service (Rev 5 P3-1)
# ---------------------------------------------------------------------------

def _audit_correlation_id():
    """One id per request, shared across every audit_event() call within it —
    so a single edit that touches several entities can be traced as one
    operation. Falls back to a fresh id per call outside a request context
    (CLI/repair scripts), where there is no Flask `g` to hold it."""
    try:
        if not hasattr(g, '_audit_cid'):
            g._audit_cid = str(uuid.uuid4())
        return g._audit_cid
    except RuntimeError:  # outside an application/request context
        return str(uuid.uuid4())


def audit_event(event_type, entity_table, entity_id, before=None, after=None,
                 reason=None, source='ui', actor_user_id=None, correlation_id=None):
    """Rev 5 P3-1 — the shared audit service. Writes ONE AuditLog row via
    db.session.add(), in the SAME transaction as the caller's business
    mutation (no separate commit here) — a failed audit write rolls the
    mutation back with it, and a failed mutation never leaves an orphaned
    audit row. This replaces route-decorator audit logging, which cannot see
    mutations made inside helpers/background jobs/CLI scripts, cannot capture
    an after-snapshot before the caller's own commit fires, and (because it
    wraps the response) can't roll anything back when the audit write itself
    fails.

    before/after: any JSON-serializable structure (dict, list of dicts, ...) —
    typically the caller's own row-serialization helper's output. Decimals/
    dates are stringified via default=str rather than requiring the caller to
    pre-serialize them.

    source: 'ui' (default — a human acting through the app), 'api' (a direct
    API call, e.g. the Lady Coleen web shop), 'cli' (an operator running a
    management script), 'migration' (a schema/data migration), or 'repair'
    (a P4-style reconciliation run). CLI/repair/migration callers should pass
    their own correlation_id (e.g. a run id) so every row from one script
    invocation is traceable as a single operation.
    """
    if actor_user_id is None:
        try:
            u = current_user()
            actor_user_id = u.id if u else None
        except RuntimeError:  # outside a request context (CLI/repair/migration)
            actor_user_id = None
    db.session.add(AuditLog(
        event_type=event_type,
        actor_user_id=actor_user_id,
        target_table=entity_table,
        target_id=str(entity_id) if entity_id is not None else None,
        before_json=json.dumps(before, default=str) if before is not None else None,
        after_json=json.dumps(after, default=str) if after is not None else None,
        note=reason,
        correlation_id=correlation_id or _audit_correlation_id(),
        store_id=STORE_ID or None,
        source=source,
    ))


# ---------------------------------------------------------------------------
# Mutation registry (Rev 5 P3-1b)
# ---------------------------------------------------------------------------
# "Every non-GET route writes an audit row" is the wrong CI rule — HTTP method
# is a signal, not a boundary (a state-changing GET or a side-effect-only POST
# both slip past it). Every registered operation instead declares an explicit
# policy via @audit_policy, checked by tests/test_mutation_registry.py:
#   AUDITED             — writes through audit_event() when it mutates.
#   SECURITY_EVENT_ONLY  — security-relevant (login/logout/password/account
#                          lifecycle); still writes through audit_event(),
#                          categorized separately from ordinary business
#                          mutations for reporting purposes.
#   NO_STATE_CHANGE      — reads only, or a side effect with no persisted
#                          state change (e.g. sending a print job).
#   EXPLICITLY_EXEMPT     — mutates but deliberately carries no audit trail;
#                          REQUIRES a written reason, checked at decoration
#                          time, not left to be filled in "later."
#
# Scope note: only blueprints in ADOPTED_AUDIT_BLUEPRINTS (see
# tests/test_mutation_registry.py) are coverage-checked today — this is a
# gradual-adoption registry, not full coverage of the app's ~370 routes in
# one pass. transactions.py and auth.py are the two adopted so far (the
# highest-stakes: money movement and account/session lifecycle).
def audit_policy(policy, reason=None):
    valid = {'AUDITED', 'NO_STATE_CHANGE', 'SECURITY_EVENT_ONLY', 'EXPLICITLY_EXEMPT'}
    if policy not in valid:
        raise ValueError(f'invalid audit policy {policy!r} — must be one of {sorted(valid)}')
    if policy == 'EXPLICITLY_EXEMPT' and not reason:
        raise ValueError('EXPLICITLY_EXEMPT requires a reason')

    def decorator(fn):
        fn._audit_policy = policy
        fn._audit_policy_reason = reason
        return fn
    return decorator


def _install_audit_completeness_check(app):
    """Rev 5 P3-1b runtime half of the proof: "CI fails when [a route]
    declaring AUDITED completes without writing an event." A route can only
    be proven to have written one by actually exercising it — this hooks
    every response and, for a 2xx response from a route policed AUDITED,
    checks whether an AuditLog row with this request's correlation_id exists.
    In TESTING config this raises (so the existing test suite's own coverage
    of audited routes doubles as the completeness check); in production it
    only logs, since a forensic-trail gap must never take down a live
    response the business mutation itself already succeeded on.
    """
    @app.after_request
    def _check_audit_completeness(response):
        try:
            fn = app.view_functions.get(request.endpoint) if request.endpoint else None
            policy = getattr(fn, '_audit_policy', None) if fn else None
            if policy == 'AUDITED' and 200 <= response.status_code < 300:
                cid = getattr(g, '_audit_cid', None)
                if cid and not AuditLog.query.filter_by(correlation_id=cid).first():
                    msg = f'AUDITED route {request.endpoint} completed 2xx with no AuditLog row for correlation_id={cid}'
                    if app.config.get('TESTING'):
                        raise AssertionError(msg)
                    logger.error(msg)
        except AssertionError:
            raise
        except Exception:
            pass  # the completeness check itself must never break a response
        return response


# Rev 5 P1-3 — routes a user with must_change_password=True may still reach.
# Everything else is blocked until they change it. Both require_login() and
# require_role() enforce this (they are independent implementations, not one
# calling the other, so the check has to live in both call sites to close the
# gap for admin-only routes that use require_role() directly).
_PASSWORD_CHANGE_EXEMPT_PATHS = {'/api/me', '/api/logout', '/api/users/change_password'}


def _enforce_password_change(user):
    """Aborts with a distinct 403 body (code=PASSWORD_CHANGE_REQUIRED) — not a
    generic 401 — so a caller can tell "must change password" apart from
    "not logged in" if it ever wants to react to it."""
    if not user or not user.must_change_password:
        return
    if request.path in _PASSWORD_CHANGE_EXEMPT_PATHS:
        return
    abort(make_response(jsonify({
        'error': 'Password change required before continuing.',
        'code': 'PASSWORD_CHANGE_REQUIRED',
    }), 403))


def require_login():
    if 'user_id' not in session:
        return False
    user = db.session.get(User, session['user_id'])
    if not user or not user.active:
        session.clear()
        return False
    sid = session.get('session_id')
    if sid:
        sess = db.session.get(UserSession, sid)
        if sess and sess.logged_out is None:
            last = sess.last_active or sess.logged_in
            now  = datetime.utcnow()
            # Hard logout after SESSION_LOGOUT_HOURS total
            if last < now - timedelta(hours=SESSION_LOGOUT_HOURS):
                sess.logged_out = last
                db.session.commit()
                session.clear()
                return False
            # Idle logout after SESSION_TIMEOUT_MINUTES of inactivity
            if last < now - timedelta(minutes=SESSION_TIMEOUT_MINUTES):
                sess.logged_out = last
                db.session.commit()
                session.clear()
                return False
    _enforce_password_change(user)
    return True


def require_role(*roles):
    u = current_user()
    if not u or not u.active:
        session.clear()
        abort(401)   # unauthenticated — JS api() handles 401 with re-login reload
    _enforce_password_change(u)
    return bool(u.has_role(*roles))


# ---------------------------------------------------------------------------
# Password policy (Rev 5 P1-3)
# ---------------------------------------------------------------------------

PASSWORD_MIN_LENGTH = 12

# Common/breached passwords, deliberately embedded (no network/file dependency —
# must work fully offline on an appliance box with no internet access). Not
# exhaustive; a floor, not a complete deny-list.
_COMMON_PASSWORDS = {
    '123456', '123456789', '12345678', '1234567890', 'qwerty', 'password',
    'password1', 'password123', 'passw0rd', '111111', '000000', '123123',
    'abc123', 'admin', 'admin123', 'letmein', 'welcome', 'welcome1',
    'monkey', 'dragon', 'master', 'iloveyou', 'sunshine', 'princess',
    'football', 'baseball', 'shadow', 'superman', 'trustno1', 'starwars',
    'qwertyuiop', 'qwerty123', '1q2w3e4r', '1qaz2wsx', 'zaq12wsx',
    'letmein123', 'changeme', 'changeit', 'temppass', 'temp1234',
    'password!', 'Password1', 'Password123', 'P@ssw0rd', 'P@ssword1',
    'admin1234', 'administrator', 'root', 'toor', 'guest', 'guest123',
    'default', 'default123', 'secret', 'secret123', 'test1234', 'testtest',
    'aaaaaaaa', 'aaaaaaaaaa', '11111111', '22222222', '99999999',
    'asdfasdf', 'asdfghjk', 'zxcvbnm1', 'football1', 'baseball1',
    'nicole1234', 'chelsea12', 'summer2024', 'summer2025', 'winter2024',
    'winter2025', 'january2025', 'freedom123', 'whatever1', 'trustno1234',
    'access123', 'login1234', 'passw0rd1', 'p@ssw0rd1', 'iloveyou1',
    'newpassword', 'changepassword', 'oldpassword', 'business1',
    'companypass', 'store12345', 'shop123456', 'cashier123', 'teller1234',
    'employee12', 'pointofsale', 'register12', 'farmstall1', 'farmpos123',
    'welcometothejungle', 'letmeinplease', 'opensesame', 'thisisapassword',
    '12341234', '1234512345', 'qazwsxedc', 'mynewpassword', 'reallylongpassword',
}


def validate_password(pw):
    """Rev 5 P1-3 shared password policy — returns None if `pw` is acceptable,
    or a short human-readable reason string naming exactly which rule failed
    (used by every password-setting call site so the error the caller sees is
    consistent: api_users_post, api_users_update, api_users_change_password)."""
    if pw is None or len(pw) < PASSWORD_MIN_LENGTH:
        return f'Password must be at least {PASSWORD_MIN_LENGTH} characters.'
    if pw.lower() in _COMMON_PASSWORDS:
        return 'That password is too common — choose something less guessable.'
    return None


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

def seed_first_admin():
    # NOTE: this runs in EVERY gunicorn worker at startup. On a fresh (empty) DB all
    # workers race - several see count()==0 and try to INSERT the same admin. The loser
    # hits a UniqueViolation, so each insert is guarded: attempt, and on IntegrityError
    # roll back and treat it as "another worker already seeded it" (same philosophy as
    # db.create_all() skip-on-conflict in strong_migrate()).
    if User.query.count() == 0:
        admin_user = os.getenv('ADMIN_USER', 'admin')
        admin_pass = os.getenv('ADMIN_PASS', 'admin123')
        # On a provisioned appliance box, refuse to seed the well-known default -
        # register-store.sh always supplies a unique ADMIN_PASS. Gated on STORE_ID so
        # the original Lady Coleen box (which seeds admin/admin123) is unchanged.
        if os.getenv('STORE_ID', '').strip() and admin_pass == 'admin123':
            raise RuntimeError(
                "ADMIN_PASS is unset (still 'admin123') on a provisioned store box. "
                "register-store.sh must generate a unique admin password per store."
            )
        hashed = generate_password_hash(admin_pass)
        # Rev 5 P1-3 — force a change on first login. Defense-in-depth for every
        # seeded account, not just the literal admin123 case: even a provisioned
        # box's unique generated ADMIN_PASS was never chosen by the person who
        # will actually use it.
        db.session.add(User(username=admin_user, password_hash=hashed, role='admin',
                             active=True, must_change_password=True))
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()  # another worker won the race - fine
        else:
            default_markup = os.getenv('DEFAULT_MARKUP_PERCENT')
            if default_markup:
                try:
                    set_setting('markup_percent', float(default_markup))
                except Exception:
                    pass
    if not User.query.filter_by(username='Online Shop').first():
        db.session.add(User(
            username='Online Shop',
            password_hash='!',
            role='teller',
            active=False,
        ))
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()  # another worker won the race - fine


def get_online_user_id():
    u = User.query.filter_by(username='Online Shop').first()
    return u.id if u else None


# ---------------------------------------------------------------------------
# FIFO inventory helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Rev 5 P2-0 — stock movement ledger (dual-write, additive)
# ---------------------------------------------------------------------------
# write_stock_movement() is called ALONGSIDE the existing StockBatch/
# StockConsumption mutations below, never instead of them — StockBatch.qty_
# remaining_base stays the live source of truth until the P2-0a rebuild gate
# passes and the ledger is promoted to authoritative. A failure here must
# never be allowed to break the real write path, so callers add the movement
# in the same flush/commit as the business mutation and let any constraint
# violation surface normally (same transaction, same all-or-nothing commit)
# rather than swallowing it — a silently-missing movement row would defeat
# the whole point of building this ledger to check against.
#
# Coverage: consume_fifo (sale/write-off/production input consumption) and
# reverse_fifo (void/edit/production-undo reversal, and — since P2-2b routed
# invoices.py's undo through this same function — invoice-undo reversal too)
# write movements. Receive, stocktake, return, manual-produce, and
# absorb_neg_placeholder are wired per call site below.
# auto_produce_on_negative (the sold-at-zero-stock auto-produce path) writes
# its own PRODUCTION_OUTPUT / PRODUCTION_ABSORB_SHORTFALL movements alongside
# its StockBatch mutations, mirroring the manual produce endpoint in
# blueprints/products.py.
def write_stock_movement(batch, movement_type, qty_delta, unit_cost, source_type,
                          source_id=None, source_line_id=None, note=None,
                          user_id=None, when=None):
    db.session.add(StockMovement(
        movement_type=movement_type,
        batch_id=batch.id if hasattr(batch, 'id') else batch,
        qty_delta=qty_delta,
        unit_cost=unit_cost,
        source_type=source_type,
        source_id=str(source_id) if source_id is not None else None,
        source_line_id=str(source_line_id) if source_line_id is not None else None,
        note=note,
        user_id=user_id,
        created_at=when or datetime.utcnow(),
    ))


def void_unconsumed_batch(batch, source_type, source_id, note=None, user_id=None, when=None):
    """Rev 5 P2-0 fix (found while adopting P3-1b's mutation registry for
    suppliers.py): void a StockBatch that has never been consumed — e.g. a
    supplier invoice edited or deleted before any of its receipted stock was
    touched — WITHOUT hard-deleting the row.

    Before P2-0, api_supplier_invoice_update/_delete freely did
    db.session.delete(batch) here, which was safe because nothing referenced
    a StockBatch row. Since P2-0's dual-write, every batch purchase_run/
    api_stock_receive creates also gets a stock_movements row, and
    stock_movements.batch_id deliberately has no ON DELETE CASCADE (the
    ledger is append-only — see StockMovement's docstring) — so that same
    delete now raises psycopg.errors.ForeignKeyViolation for any batch
    created after P2-0 shipped, i.e. effectively every new supplier invoice.

    Fix: write a compensating RECEIPT_VOID movement bringing the batch's net
    ledger position to zero, zero both quantity columns so every reader that
    already filters/sums on qty_remaining_base > 0 (consume_fifo,
    get_stock_level, valuation) or qty_purchased_base > 0 (consumed_pct,
    supplier invoice/batch listings) treats it as gone, and detach it from
    its invoice (so the invoice itself can still be deleted/replaced without
    an invoice_id FK pointing at a voided batch). The row survives for FK
    integrity and audit trail — same principle P2-2/P2-2b used for returns
    and invoice-undo: reverse with a movement, never delete history.

    Caller must have already verified no StockConsumption references this
    batch (both call sites do, as a precondition for allowing the edit).
    """
    remaining = Decimal(str(batch.qty_remaining_base))
    if remaining != Decimal('0'):
        write_stock_movement(
            batch, movement_type='RECEIPT_VOID', qty_delta=-remaining,
            unit_cost=batch.cost_per_base_unit, source_type=source_type,
            source_id=source_id, note=note, user_id=user_id, when=when,
        )
    batch.qty_remaining_base = Decimal('0')
    batch.qty_purchased_base = Decimal('0')
    batch.invoice_id = None
    batch.cost_adjustment_reason = note or batch.cost_adjustment_reason
    batch.updated_at = when or datetime.utcnow()
    batch.updated_by = user_id


def consume_fifo(ingredient_id, qty_needed_base, sale_id, now, _depth=0, sale_unit_price=None, is_writeoff=False,
                  movement_source_type=None):
    """
    Consume qty_needed_base units of ingredient_id from FIFO batches.
    Recursive for compound ingredients (recipe within recipe).
    Returns total COGS as Decimal. Never raises - consumes what's available.

    sale_unit_price: selling price per base unit — required for PCT_OF_SALE consignment products.
    is_writeoff: when True, no ConsignmentLiability is created (write-offs are absorbed loss, not owed to supplier).
    movement_source_type: Rev 5 P2-0 dual-write classification — 'sale' | 'writeoff' | 'production'.
    Defaults to 'writeoff' when is_writeoff else 'sale' when not given explicitly; production callers
    must pass 'production' since is_writeoff alone can't distinguish a sale from a production input.
    """
    if movement_source_type is None:
        movement_source_type = 'writeoff' if is_writeoff else 'sale'

    if _depth > 10:
        return Decimal('0')

    qty_needed = Decimal(str(qty_needed_base))

    sub_lines = RecipeLine.query.filter_by(product_id=ingredient_id).all()
    if sub_lines:
        prod = db.session.get(Product, ingredient_id)
        if not (prod and prod.is_produced):
            # Made-to-order recipe: consume raw ingredients recursively.
            # qty_needed is in "portions/units" of the recipe output.
            # batch_size defines how many portions one recipe run produces, so divide to get
            # the correct fraction of ingredients (default batch_size=1 preserves old behaviour).
            batch_sz = Decimal(str(prod.batch_size or 1)) if prod else Decimal('1')
            if batch_sz <= 0:
                batch_sz = Decimal('1')
            total_cost = Decimal('0')
            for sub in sub_lines:
                sub_qty = sub.qty_base * qty_needed / batch_sz
                total_cost += consume_fifo(sub.ingredient_id, sub_qty, sale_id, now, _depth + 1,
                                            is_writeoff=is_writeoff, movement_source_type=movement_source_type)
            return total_cost
        # Batch-produced recipe: fall through to consume from its own finished-goods batch.

    qty_to_consume = qty_needed
    total_cost = Decimal('0')

    batch_q = (StockBatch.query
               .filter_by(product_id=ingredient_id)
               .filter(StockBatch.qty_remaining_base > 0)
               .with_for_update()
               .order_by(StockBatch.sort_order.asc().nulls_last(),
                         StockBatch.purchased_at.asc(), StockBatch.id.asc()))

    batches = batch_q.filter(StockBatch.purchased_at <= now).all()
    if not batches:
        batches = batch_q.all()

    for batch in batches:
        if qty_to_consume <= 0:
            break
        take = min(Decimal(str(batch.qty_remaining_base)), qty_to_consume)
        batch.qty_remaining_base = Decimal(str(batch.qty_remaining_base)) - take
        cost = take * Decimal(str(batch.cost_per_base_unit))
        total_cost += cost
        db.session.add(StockConsumption(
            sale_id=sale_id,
            ingredient_id=ingredient_id,
            batch_id=batch.id,
            qty_consumed_base=take,
            cost_per_base_unit=batch.cost_per_base_unit,
            consumed_at=now
        ))
        write_stock_movement(
            batch, movement_type=movement_source_type.upper(), qty_delta=-take,
            unit_cost=batch.cost_per_base_unit, source_type=movement_source_type,
            source_id=sale_id, when=now,
        )

        # Consignment liability: generate on every FIFO sale of a consignment batch.
        # Write-offs are absorbed as your own loss — supplier is not owed for spoilage/damage.
        if not is_writeoff and getattr(batch, 'ownership_type', 'NORMAL') == 'CONSIGNMENT':
            _prod = db.session.get(Product, ingredient_id)
            # Batch supplier takes priority; fall back to product-level consignment_supplier_id
            _eff_supplier = batch.supplier_id or (getattr(_prod, 'consignment_supplier_id', None) if _prod else None)
            if not _eff_supplier:
                logger.warning(
                    f'consume_fifo: consignment batch {batch.id} for product {ingredient_id} '
                    f'has no supplier_id and product has no consignment_supplier_id — liability NOT created.'
                )
            else:
                _basis = getattr(_prod, 'settlement_basis', 'FIXED_COST') if _prod else 'FIXED_COST'
                _sale_price_snap = None
                _pct_snap = None
                if _basis == 'PCT_OF_SALE' and sale_unit_price is not None and _prod:
                    _pct = Decimal(str(_prod.consignment_pct or 0)) / Decimal('100')
                    _unit_cost = (Decimal(str(sale_unit_price)) * _pct).quantize(Decimal('0.000001'))
                    _sale_price_snap = float(sale_unit_price)
                    _pct_snap = float(_prod.consignment_pct or 0)
                elif _basis == 'FIXED_COST':
                    # Manually-set fixed price on product overrides batch cost
                    _fixed = getattr(_prod, 'consignment_cost_per_unit', None) if _prod else None
                    if _fixed is not None:
                        _unit_cost = Decimal(str(_fixed))
                    else:
                        _cuc = getattr(batch, 'consignment_unit_cost', None)
                        _unit_cost = Decimal(str(_cuc)) if _cuc is not None else Decimal(str(batch.cost_per_base_unit))
                else:  # UNIT_COST — purchase cost per unit, excludes shipping/additional costs
                    _cuc = getattr(batch, 'consignment_unit_cost', None)
                    _unit_cost = Decimal(str(_cuc)) if _cuc is not None else Decimal(str(batch.cost_per_base_unit))
                _amount = (take * _unit_cost).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                db.session.add(ConsignmentLiability(
                    supplier_id=_eff_supplier,
                    product_id=ingredient_id,
                    batch_id=batch.id,
                    sale_id=sale_id,
                    qty_consumed=float(take),
                    unit_cost=float(_unit_cost),
                    amount_owed=float(_amount),
                    sale_price_at_time=_sale_price_snap,
                    settlement_percent_at_time=_pct_snap,
                ))

        qty_to_consume -= take

    # Stock exhausted before qty satisfied (Rev 5 P2-1). The shortfall posts to a
    # negative-placeholder batch — one aggregate per product, matching how every other
    # oversell path in the app already represents "we owe stock" — never to a historical
    # batch. Previously this wrote a StockConsumption against the last historical batch
    # WITHOUT decrementing its qty_remaining_base, corrupting that batch's own position;
    # this is the fix. Centralizing it here (rather than in each of consume_fifo's
    # callers, as before) means every caller gets correct oversell handling, including
    # the made-to-order and nested-recipe paths that previously had no placeholder logic
    # at all and silently hit the same historical-batch bug.
    if qty_to_consume > 0:
        last_batch = (StockBatch.query
                      .filter_by(product_id=ingredient_id)
                      .filter(StockBatch.batch_type != 'negative_placeholder')
                      .filter(StockBatch.cost_per_base_unit > 0)
                      .order_by(StockBatch.purchased_at.desc(), StockBatch.id.desc())
                      .first())
        if last_batch:
            est_cost   = Decimal(str(last_batch.cost_per_base_unit))
            est_method = 'last_known_batch'
        else:
            est_cost   = Decimal('0')
            est_method = 'none'
        total_cost += qty_to_consume * est_cost

        neg_batch = (StockBatch.query
                     .filter_by(product_id=ingredient_id, batch_type='negative_placeholder')
                     .filter(StockBatch.qty_remaining_base < 0)
                     .with_for_update()
                     .first())
        if neg_batch:
            neg_batch.qty_remaining_base = Decimal(str(neg_batch.qty_remaining_base)) - qty_to_consume
            neg_batch.qty_purchased_base = Decimal(str(neg_batch.qty_purchased_base)) - qty_to_consume
            # Last-estimate-wins, not a running weighted average — simple and matches how
            # a normal batch already carries exactly one cost_per_base_unit, not a blend.
            neg_batch.estimated_unit_cost    = est_cost
            neg_batch.cost_estimation_method = est_method
            neg_batch.cost_reconciled        = False
        else:
            neg_batch = StockBatch(
                product_id=ingredient_id,
                qty_purchased_base=-qty_to_consume,
                qty_remaining_base=-qty_to_consume,
                cost_per_base_unit=Decimal('0'),
                estimated_unit_cost=est_cost,
                cost_estimation_method=est_method,
                cost_reconciled=False,
                purchased_at=now,
                batch_type='negative_placeholder',
            )
            db.session.add(neg_batch)
            db.session.flush()

        db.session.add(StockConsumption(
            sale_id=sale_id,
            ingredient_id=ingredient_id,
            batch_id=neg_batch.id,
            qty_consumed_base=qty_to_consume,
            cost_per_base_unit=est_cost,
            consumed_at=now,
        ))
        write_stock_movement(
            neg_batch, movement_type=movement_source_type.upper(), qty_delta=-qty_to_consume,
            unit_cost=est_cost, source_type=movement_source_type, source_id=sale_id, when=now,
        )

    return total_cost


def reverse_fifo(sale_id, movement_source_type='sale'):
    """Restore all batch quantities consumed by this sale_id. Delete consumption records.
    IMPORTANT: For consignment products, also call reverse_consignment_liabilities(sale_id)
    to void the corresponding liabilities. reverse_fifo alone leaves a ghost liability.

    movement_source_type: Rev 5 P2-0 dual-write classification for the restoring movement.
    Defaults to 'sale' (covers void and edit, both of which reverse a real sale_id); callers
    reversing a produce run (undoing a StockBatch.produce_ref) pass 'production'.
    """
    records = StockConsumption.query.filter_by(sale_id=sale_id).all()
    for r in records:
        batch = db.session.get(StockBatch, r.batch_id, with_for_update=True)
        if batch:
            batch.qty_remaining_base = (
                Decimal(str(batch.qty_remaining_base)) + Decimal(str(r.qty_consumed_base))
            )
            write_stock_movement(
                batch,
                movement_type=('PRODUCTION_REVERSAL' if movement_source_type == 'production' else 'SALE_REVERSAL'),
                qty_delta=Decimal(str(r.qty_consumed_base)), unit_cost=r.cost_per_base_unit,
                source_type=movement_source_type, source_id=sale_id,
            )
        db.session.delete(r)


def reverse_consignment_liabilities(sale_id):
    """Mark all outstanding consignment liabilities for this sale as voided.
    Already-settled liabilities are left intact (financial audit trail)."""
    from datetime import datetime as _dt
    liabilities = ConsignmentLiability.query.filter_by(
        sale_id=sale_id, status='outstanding'
    ).all()
    now = _dt.utcnow()
    for lib in liabilities:
        lib.status = 'voided'
        lib.settled_at = now


def reverse_consignment_liabilities_partial(sale_id, product_id, returned_qty):
    """Rev 5 P2-2 — reverses ONLY the liability attributable to a partial return,
    pro-rata against the original per-batch consumption, instead of voiding every
    outstanding liability for the whole sale (that stays correct for a full
    void/edit — see reverse_consignment_liabilities above, still used there).

    Writes a compensating credit row per original liability row rather than
    mutating the originals — same "append, never rewrite" principle as the rest
    of this ledger. A credit row carries the SAME sale_id/product_id/batch_id as
    the liability it offsets, but negative qty_consumed/amount_owed. Every
    existing caller that sums amount_owed for status='outstanding' rows (supplier
    balance, settlement runs, write-off reports — see blueprints/consignment.py)
    nets out correctly with no changes needed there.

    original_total is derived from the POSITIVE-qty_consumed outstanding rows
    only, so repeated partial returns of the same sale+product stay correct:
    each call's ratio is against the true original quantity, not a shrinking
    remainder, and earlier credit rows (negative qty_consumed) are excluded
    from the denominator by construction.
    """
    originals = (ConsignmentLiability.query
                 .filter_by(sale_id=sale_id, product_id=product_id, status='outstanding')
                 .filter(ConsignmentLiability.qty_consumed > 0)
                 .all())
    if not originals:
        return
    original_total = sum(Decimal(str(l.qty_consumed)) for l in originals)
    if original_total <= 0:
        return
    ratio = Decimal(str(returned_qty)) / original_total
    now = datetime.utcnow()
    for l in originals:
        credit_qty = (Decimal(str(l.qty_consumed)) * ratio).quantize(Decimal('0.0001'))
        if credit_qty == 0:
            continue
        credit_amount = (Decimal(str(l.amount_owed)) * ratio).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        db.session.add(ConsignmentLiability(
            supplier_id=l.supplier_id, product_id=l.product_id, batch_id=l.batch_id,
            sale_id=sale_id, qty_consumed=-credit_qty, unit_cost=l.unit_cost,
            amount_owed=-credit_amount, status='outstanding', created_at=now,
        ))


def get_stock_level(product_id):
    from sqlalchemy import func
    result = db.session.query(
        func.sum(StockBatch.qty_remaining_base)
    ).filter_by(product_id=product_id).scalar()
    return float(result or 0)


def backfill_consignment_liabilities(product_id, qty_absorbed, supplier_id_override, cuc):
    """Create ConsignmentLiability records for units already sold before stock was received.
    Called when receiving consignment stock absorbs a negative placeholder.
    For PCT_OF_SALE: creates per-sale records using actual sale prices.
    For FIXED_COST/UNIT_COST: creates one aggregate record.
    """
    qty_to_cover = Decimal(str(qty_absorbed))
    if qty_to_cover <= 0:
        return

    prod = db.session.get(Product, product_id)
    if not prod or not prod.is_consignment:
        return

    supplier_id = supplier_id_override or getattr(prod, 'consignment_supplier_id', None)
    if not supplier_id:
        return

    _basis = getattr(prod, 'settlement_basis', 'FIXED_COST')

    if _basis == 'PCT_OF_SALE':
        _pct = Decimal(str(prod.consignment_pct or 0)) / Decimal('100')

        # Find when the negative started (creation time of the placeholder)
        neg_batch = (StockBatch.query
                     .filter_by(product_id=product_id, batch_type='negative_placeholder')
                     .order_by(StockBatch.purchased_at.asc())
                     .first())
        if not neg_batch:
            return
        neg_start_at = neg_batch.purchased_at

        # Sales already covered by existing liabilities (from previous partial receives)
        covered = set(
            row[0] for row in
            db.session.query(ConsignmentLiability.sale_id)
            .filter_by(product_id=product_id)
            .filter(ConsignmentLiability.sale_id.isnot(None))
            .filter(ConsignmentLiability.status != 'voided')
            .all()
        )

        uncovered = (Sale.query
                     .filter_by(product_id=product_id, voided=False)
                     .filter(Sale.date_time >= neg_start_at)
                     .filter(Sale.sale_id.notin_(covered) if covered else db.true())
                     .order_by(Sale.date_time.asc())
                     .all())

        for sale in uncovered:
            if qty_to_cover <= 0:
                break
            take = min(Decimal(str(sale.qty)), qty_to_cover)
            unit_price = Decimal(str(sale.unit_price or 0))
            unit_cost  = (unit_price * _pct).quantize(Decimal('0.000001'))
            amount     = (take * unit_cost).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            if amount > 0:
                db.session.add(ConsignmentLiability(
                    supplier_id=supplier_id, product_id=product_id,
                    batch_id=None, sale_id=sale.sale_id,
                    qty_consumed=float(take), unit_cost=float(unit_cost),
                    amount_owed=float(amount),
                    sale_price_at_time=float(unit_price),
                    settlement_percent_at_time=float(prod.consignment_pct or 0),
                ))
            qty_to_cover -= take
    else:
        # FIXED_COST: manually-set price. UNIT_COST: purchase cost (no shipping).
        if _basis == 'FIXED_COST':
            _fixed = getattr(prod, 'consignment_cost_per_unit', None)
            unit_cost = Decimal(str(_fixed)) if _fixed is not None else Decimal(str(cuc or 0))
        else:
            unit_cost = Decimal(str(cuc or 0))

        if unit_cost <= 0:
            return

        amount = (qty_to_cover * unit_cost).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        db.session.add(ConsignmentLiability(
            supplier_id=supplier_id, product_id=product_id,
            batch_id=None, sale_id=None,
            qty_consumed=float(qty_to_cover), unit_cost=float(unit_cost),
            amount_owed=float(amount),
        ))


def absorb_neg_placeholder(product_id, incoming_qty_dec, incoming_unit_cost=None):
    """When receiving stock, absorb any existing negative-placeholder batch first.
    Returns the amount absorbed (Decimal). The new batch's qty_remaining_base
    should be reduced by this amount so the visible available qty is correct.

    Rev 5 P2-1: incoming_unit_cost, when given, is compared against the placeholder's
    own estimated_unit_cost (stamped by consume_fifo when the shortfall was posted).
    The estimate is never silently overwritten or discarded — if it disagrees with what
    stock actually cost on arrival, the difference is written as an explicit COST_VARIANCE
    movement so it is visible in the P&L rather than absorbed invisibly into inventory.
    """
    _neg = (StockBatch.query
            .filter_by(product_id=product_id, batch_type='negative_placeholder')
            .filter(StockBatch.qty_remaining_base < 0)
            .with_for_update()
            .first())
    if not _neg:
        return Decimal('0')
    _neg_qty  = abs(Decimal(str(_neg.qty_remaining_base)))
    _absorbed = min(_neg_qty, incoming_qty_dec)
    _neg.qty_remaining_base = Decimal(str(_neg.qty_remaining_base)) + _absorbed
    if _absorbed > 0:
        # Rev 5 P2-0: the receiving batch doesn't exist yet at this point in every
        # caller (it's created after this returns), so there's no batch_id to link
        # as source_id — recorded via `note` instead of guessing at a reference.
        write_stock_movement(
            _neg, movement_type='RECEIPT_ABSORB_SHORTFALL', qty_delta=_absorbed,
            unit_cost=Decimal('0'), source_type='receipt', source_id=None,
            note=f'absorbed by a receipt of product_id={product_id}',
        )
        if incoming_unit_cost is not None and _neg.estimated_unit_cost is not None:
            _variance_per_unit = Decimal(str(incoming_unit_cost)) - Decimal(str(_neg.estimated_unit_cost))
            if _variance_per_unit != 0:
                _variance_amount = (_variance_per_unit * _absorbed).quantize(Decimal('0.0001'))
                write_stock_movement(
                    _neg, movement_type='COST_VARIANCE', qty_delta=Decimal('0'),
                    unit_cost=_variance_per_unit, source_type='receipt', source_id=None,
                    note=(f'estimated {_neg.estimated_unit_cost} vs actual {incoming_unit_cost} '
                          f'per unit, {_absorbed} units absorbed, variance {_variance_amount}'),
                )
        _neg.cost_reconciled = True
    return _absorbed


def get_fifo_cost_per_unit(product_id):
    batch = (StockBatch.query
             .filter_by(product_id=product_id)
             .filter(StockBatch.qty_remaining_base > 0)
             .order_by(StockBatch.sort_order.asc().nulls_last(),
                       StockBatch.purchased_at.asc(), StockBatch.id.asc())
             .first())
    if batch:
        return float(batch.cost_per_base_unit)
    # No stock remaining — fall back to last known cost from most recently received batch
    last_batch = (StockBatch.query
                  .filter_by(product_id=product_id)
                  .filter(StockBatch.batch_type != 'negative_placeholder')
                  .filter(StockBatch.cost_per_base_unit > 0)
                  .order_by(StockBatch.purchased_at.desc(), StockBatch.id.desc())
                  .first())
    return float(last_batch.cost_per_base_unit) if last_batch else 0.0


def auto_produce_on_negative(product_id, shortfall, now, u):
    """Auto-produce enough batches of a produced recipe to cover a negative shortfall.
    Consumes ingredients, creates a finished-goods StockBatch, and reconciles any
    existing negative placeholder. Runs within the caller's DB transaction (no commit).
    Returns (batches_produced: int, units_added: int).

    Rev 5 P2-0: the ingredient consumption below goes through consume_fifo, which is
    dual-written. The finished-goods StockBatch this function creates, and the negative-
    placeholder bookkeeping around it, are now dual-written too — same PRODUCTION_OUTPUT /
    PRODUCTION_ABSORB_SHORTFALL movement types as the manual produce endpoint in
    blueprints/products.py, so the two paths reconcile identically under INV-11.
    """
    from decimal import ROUND_CEILING
    p = db.session.get(Product, product_id)
    if not p or p.product_type != 'recipe' or not p.is_produced:
        return 0, 0
    batch_sz = Decimal(str(p.batch_size or 1))
    if batch_sz <= 0:
        batch_sz = Decimal('1')
    batches_needed = (Decimal(str(shortfall)) / batch_sz).to_integral_value(rounding=ROUND_CEILING)
    if batches_needed <= 0:
        return 0, 0

    produce_uuid     = str(uuid.uuid4())
    total_cost       = Decimal('0')
    available_before = Decimal(str(get_stock_level(product_id)))

    for rl in RecipeLine.query.filter_by(product_id=product_id).all():
        _ing_needed = Decimal(str(rl.qty_base)) * batches_needed
        # Rev 5 P2-1: consume_fifo posts any shortfall to the negative placeholder
        # itself now — no separate caller-side shortfall/placeholder logic needed
        # (and keeping it would double-count the debt against consume_fifo's own).
        total_cost += consume_fifo(rl.ingredient_id, _ing_needed, produce_uuid, now, movement_source_type='production')

    units_added = int((batch_sz * batches_needed).to_integral_value())
    cost_per    = total_cost / units_added if units_added > 0 else Decimal('0')

    # Reconcile any existing negative placeholder so net stock is correct
    _neg_ph = (StockBatch.query
               .filter_by(product_id=product_id, batch_type='negative_placeholder')
               .filter(StockBatch.qty_remaining_base < 0)
               .with_for_update()
               .first())
    reconciled = 0
    if _neg_ph:
        _neg_qty  = abs(Decimal(str(_neg_ph.qty_remaining_base)))
        _cancel   = min(_neg_qty, Decimal(str(units_added)))
        _neg_ph.qty_remaining_base = Decimal(str(_neg_ph.qty_remaining_base)) + _cancel
        reconciled = int(_cancel.to_integral_value())
        if _cancel > 0:
            write_stock_movement(
                _neg_ph, movement_type='PRODUCTION_ABSORB_SHORTFALL', qty_delta=_cancel,
                unit_cost=Decimal('0'), source_type='production', source_id=produce_uuid, when=now,
            )

    _out_batch = StockBatch(
        product_id=product_id,
        qty_purchased_base=units_added,
        qty_remaining_base=units_added - reconciled,
        cost_per_base_unit=cost_per,
        base_cost_total=total_cost,
        purchased_at=now,
        user_id=u.id if u else None,
        produce_ref=produce_uuid,
        produce_cost=total_cost,
    )
    db.session.add(_out_batch)
    db.session.flush()
    write_stock_movement(
        _out_batch, movement_type='PRODUCTION_OUTPUT', qty_delta=Decimal(str(units_added - reconciled)),
        unit_cost=cost_per, source_type='production', source_id=produce_uuid, when=now,
    )
    db.session.add(StockAdjustment(
        product_id=product_id,
        adjustment_type='produce',
        qty_change_base=units_added,
        system_qty_before=available_before,
        cost_written_off=total_cost,
        base_unit=p.base_unit,
        reason=f'Auto-produce (sold at zero stock): {int(batches_needed)} batch(es)',
        adjusted_at=now,
        user_id=u.id if u else None,
    ))
    return int(batches_needed), units_added


def _auto_price_products(product_ids, min_drift_pct=0):
    """Calculate auto-price for products with auto_price=True and store as pending_price.
    The pending price must be explicitly applied by the user before the till uses it.
    min_drift_pct: only flag when actual markup has drifted more than this many pct
    points from the target markup. 0 = flag any price change (original behaviour)."""
    from decimal import Decimal as _D
    import logging as _logging
    _log = _logging.getLogger('pos')
    if not product_ids:
        return
    global_markup = _D(str(get_setting('markup_percent', 20) or 20))
    changed = False
    for pid in product_ids:
        try:
            p = db.session.get(Product, pid)
            if not p or not getattr(p, 'auto_price', True):
                continue
            batches = (StockBatch.query
                       .filter_by(product_id=pid)
                       .filter(StockBatch.qty_remaining_base > 0)
                       .all())
            if not batches:
                continue
            total_qty  = sum(_D(str(b.qty_remaining_base)) for b in batches)
            total_cost = sum(_D(str(b.qty_remaining_base)) * _D(str(b.cost_per_base_unit)) for b in batches)
            if total_qty <= 0:
                continue
            cost = total_cost / total_qty  # WAC — full Decimal precision
            if cost <= 0:
                # WAC is zero (zero-cost batches) — clear any stale pending suggestion
                if p.pending_price is not None or p.pending_price_per_unit is not None:
                    p.pending_price = None
                    p.pending_price_per_unit = None
                    changed = True
                continue
            markup = _D(str(p.margin_pct)) if p.margin_pct is not None else global_markup
            new_price = (cost * (1 + markup / 100)).quantize(_D('0.0001'))
            if p.sold_by_weight and p.unit_type in ('weight', 'volume'):
                current = _D(str(p.price_per_unit or 0))
                if min_drift_pct > 0 and cost > 0 and current > 0:
                    actual_markup = (current / cost - 1) * 100
                    if abs(actual_markup - markup) <= _D(str(min_drift_pct)):
                        if p.pending_price_per_unit is not None:
                            p.pending_price_per_unit = None
                            changed = True
                        continue
                if abs(new_price - current) > _D('0.0001'):
                    p.pending_price_per_unit = new_price
                    changed = True
            else:
                new_price_r = new_price.quantize(_D('0.01'))
                current = _D(str(p.price or 0))
                if min_drift_pct > 0 and cost > 0 and current > 0:
                    actual_markup = (current / cost - 1) * 100
                    if abs(actual_markup - markup) <= _D(str(min_drift_pct)):
                        if p.pending_price is not None:
                            p.pending_price = None
                            changed = True
                        continue
                if abs(new_price_r - current) > _D('0.005'):
                    p.pending_price = new_price_r
                    changed = True
        except Exception as e:
            _log.warning(f'[auto_price] product {pid}: {e}')
    if changed:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()


def _run_markup_drift_check(min_drift_pct=None):
    """Scan all active auto_price=True products and flag those whose actual markup
    has drifted more than min_drift_pct points from their target markup.
    min_drift_pct defaults to the 'markup_drift_pct' setting (fallback 5)."""
    import logging as _logging
    _log = _logging.getLogger('pos')
    try:
        if min_drift_pct is None:
            min_drift_pct = float(get_setting('markup_drift_pct', 5) or 5)
        ids = [
            p.id for p in Product.query.filter(
                Product.is_archived == False,
                Product.auto_price == True,
                Product.is_for_sale == True,
            ).all()
        ]
        if ids:
            _auto_price_products(ids, min_drift_pct=min_drift_pct)
            _log.info(f'[markup_drift] scanned {len(ids)} products (threshold {min_drift_pct}%)')
    except Exception as e:
        _log.warning(f'[markup_drift] scan failed: {e}')


_markup_scheduler_started = False


def _start_markup_drift_scheduler(app):
    """Start hourly background markup-drift check. Call once from create_app()."""
    global _markup_scheduler_started
    if _markup_scheduler_started:
        return
    _markup_scheduler_started = True

    import threading
    import time

    def _loop():
        time.sleep(300)  # 5-min warm-up delay
        while True:
            try:
                with app.app_context():
                    _run_markup_drift_check()
            except Exception as e:
                app.logger.warning(f'[markup_drift] scheduler error: {e}')
            time.sleep(3600)  # check every hour

    t = threading.Thread(target=_loop, daemon=True, name='markup-drift-check')
    t.start()


_backup_scheduler_started = False


def _start_backup_scheduler(app):
    """Start the backup scheduler daemon thread. Call once from _register_routes()."""
    global _backup_scheduler_started
    if _backup_scheduler_started:
        return
    _backup_scheduler_started = True

    import threading
    import time

    def _loop():
        time.sleep(60)  # short warm-up
        while True:
            try:
                with app.app_context():
                    _run_scheduled_backup(app)
            except Exception as e:
                app.logger.warning(f'[backup] scheduler error: {e}')
            time.sleep(60)  # check every minute

    t = threading.Thread(target=_loop, daemon=True, name='backup-scheduler')
    t.start()


def _run_scheduled_backup(app):
    from datetime import datetime, timedelta
    from sqlalchemy import text
    enabled = get_setting('backup_enabled', 'false') == 'true'
    if not enabled:
        return

    frequency        = get_setting('backup_schedule_frequency', 'daily')
    target_time      = get_setting('backup_schedule_time', '12:00')
    target_day       = get_setting('backup_schedule_day', 'monday').lower()
    retry_max        = int(get_setting('backup_retry_count', '4') or 4)
    retry_interval   = int(get_setting('backup_retry_interval_minutes', '30') or 30)
    catchup_hours    = int(get_setting('backup_catchup_hours', '24') or 24)
    last_success_at  = get_setting('backup_last_run_at', '')       # set only on success
    last_attempt_at  = get_setting('backup_last_attempt_at', '')   # set on every attempt
    last_status      = get_setting('backup_last_run_status', '')
    fail_count       = int(get_setting('backup_fail_count', '0') or 0)
    now              = datetime.utcnow()

    def _parse(s):
        try:
            return datetime.fromisoformat(s) if s else None
        except Exception:
            return None

    last_success_dt = _parse(last_success_at)
    last_attempt_dt = _parse(last_attempt_at)

    # Helper: seconds since a datetime (None → infinity)
    def _age(dt):
        return (now - dt).total_seconds() if dt else float('inf')

    triggered_by = None

    # 1. Scheduled time
    hhmm = now.strftime('%H:%M')
    if hhmm == target_time:
        if frequency == 'daily':
            triggered_by = 'schedule'
        elif frequency == 'weekly':
            day_names = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
            if day_names[now.weekday()] == target_day:
                triggered_by = 'schedule'

    # 2. Catch-up: last successful backup is older than catchup_hours
    if triggered_by is None and catchup_hours > 0:
        if _age(last_success_dt) > catchup_hours * 3600:
            # Only catch-up if we're not mid-retry sequence (fail_count == 0 means last attempt succeeded or never ran)
            if fail_count == 0 or last_success_dt is None:
                triggered_by = 'catchup'

    # 3. Retry: last backup failed, under the retry limit, enough time since last attempt
    if triggered_by is None and last_status == 'failed' and 0 < fail_count <= retry_max:
        if _age(last_attempt_dt) >= retry_interval * 60:
            triggered_by = 'retry'

    if triggered_by is None:
        return

    # Dedup: don't fire if last attempt was < 55s ago (catches scheduler drift on exact-time match)
    if _age(last_attempt_dt) < 55:
        return

    # Cross-worker guard: pg advisory lock ensures only one of N workers enqueues.
    # pg_try_advisory_xact_lock is atomic and auto-releases on commit/rollback.
    try:
        locked = db.session.execute(text("SELECT pg_try_advisory_xact_lock(557700)")).scalar()
        if not locked:
            return
        # Re-check last_attempt_at inside the lock to close the TOCTOU window
        if _age(_parse(get_setting('backup_last_attempt_at', ''))) < 55:
            db.session.commit()
            return
        from blueprints.backup import _enqueue_backup
        _enqueue_backup(app=app, triggered_by=triggered_by)
        db.session.commit()
    except Exception as e:
        app.logger.warning(f'[backup] scheduler lock error: {e}')
        try:
            db.session.rollback()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Product helpers
# ---------------------------------------------------------------------------

def sync_sell_packages(product_id, packages):
    """Create/update/delete auto-managed package products for a stock_item."""
    existing = Product.query.filter_by(parent_stock_item_id=product_id).all()
    existing_by_name = {p.name: p for p in existing}
    submitted_names = {pkg['name'] for pkg in packages}

    for name, prod in existing_by_name.items():
        if name not in submitted_names:
            RecipeLine.query.filter_by(product_id=prod.id).delete()
            if Sale.query.filter_by(product_id=prod.id).count() == 0:
                db.session.delete(prod)

    parent = db.session.get(Product, product_id)  # noqa: F841 - kept for future use

    for pkg in packages:
        pkg_name = pkg.get('name', '').strip()
        qty_base = Decimal(str(pkg.get('qty_base', 0)))
        price    = Decimal(str(pkg.get('price', 0)))
        barcode  = pkg.get('barcode', '').strip() or None

        if not pkg_name or qty_base <= 0:
            continue

        if pkg_name in existing_by_name:
            prod = existing_by_name[pkg_name]
            prod.price = price
            if barcode:
                clash = Product.query.filter(Product.barcode == barcode, Product.id != prod.id).first()
                if not clash:
                    prod.barcode = barcode
            RecipeLine.query.filter_by(product_id=prod.id).delete()
            db.session.add(RecipeLine(
                product_id=prod.id,
                ingredient_id=product_id,
                qty_base=qty_base
            ))
        else:
            if not barcode:
                barcode = _gen_barcode(product_id)
            if Product.query.filter_by(barcode=barcode).first():
                barcode = _gen_barcode(product_id)
            prod = Product(
                name=pkg_name,
                price=price,
                barcode=barcode,
                product_type='recipe',
                is_for_sale=True,
                sold_by_weight=False,
                parent_stock_item_id=product_id
            )
            db.session.add(prod)
            db.session.flush()
            db.session.add(RecipeLine(
                product_id=prod.id,
                ingredient_id=product_id,
                qty_base=qty_base
            ))


def _ean13_check(code12):
    s = sum(int(code12[i]) * (1 if i % 2 == 0 else 3) for i in range(12))
    return str((10 - s % 10) % 10)


def _gen_barcode_from_code(product_code):
    """Generate deterministic EAN-13 for fixed-price products from product_code.
    Format: 1 + PPPPP (5-digit code) + 000000 (6 zeros) + check digit.
    Weight/volume products don't get a stored barcode - scale generates dynamically.
    """
    core = f"1{str(product_code).zfill(5)}000000"
    return core + _ean13_check(core)


def _plu_range(sold_by_weight, unit_type, product_type):
    """Return (lo, hi) range for product_code based on type."""
    if sold_by_weight and unit_type == 'volume':
        return 30000, 39999
    elif sold_by_weight:
        return 1, 19999
    elif product_type in ('simple', 'stock_item'):
        return 20000, 29999
    else:
        return 40000, 49999


def _assign_product_code(sold_by_weight, unit_type, product_type):
    """Assign the smallest available product_code gap for the given product type.
    For fixed-price products also skips codes whose auto-generated barcode is
    already taken by another product (guards against barcode/product_code mismatch
    in existing data).
    Uses table lock - caller must be inside a transaction.
    """
    lo, hi = _plu_range(sold_by_weight, unit_type, product_type)
    from sqlalchemy import text as _text
    db.session.execute(_text("LOCK TABLE products IN SHARE ROW EXCLUSIVE MODE"))

    used_codes = {r[0] for r in db.session.execute(_text(
        "SELECT product_code FROM products "
        "WHERE product_code >= :lo AND product_code <= :hi"
    ), {'lo': lo, 'hi': hi}).fetchall()}

    # For fixed-price products the barcode is derived from the product_code.
    # Pre-load all barcodes so we can skip any code whose barcode is already taken.
    check_barcode = not sold_by_weight and unit_type != 'volume'
    used_barcodes = set()
    if check_barcode:
        used_barcodes = {r[0] for r in db.session.execute(_text(
            "SELECT barcode FROM products WHERE barcode IS NOT NULL"
        )).fetchall()}

    for code in range(lo, hi + 1):
        if code in used_codes:
            continue
        if check_barcode and _gen_barcode_from_code(code) in used_barcodes:
            continue
        return code

    raise ValueError(f"Product code range {lo}-{hi} exhausted")


def validate_product_code(new_code, product_id=None):
    """Check product_code is available. Returns (ok, conflict_product_name or None)."""
    if not new_code or new_code <= 0 or new_code > 99999:
        return False, "Product code must be between 1 and 99999"
    conflict = Product.query.filter(
        Product.product_code == new_code,
        Product.id != product_id if product_id else True
    ).first()
    if conflict:
        return False, f"PLU {new_code} already used by '{conflict.name}'"
    return True, None


def _gen_barcode(seed_id):
    """Legacy fallback - generates random EAN-13 with prefix 100."""
    for _ in range(30):
        rnd = str(random.randint(0, 99999)).zfill(5)
        core = f"100{str(seed_id).zfill(5)}{rnd}"[:12]
        check = _ean13_check(core)
        candidate = core + check
        if not Product.query.filter_by(barcode=candidate).first():
            return candidate
    return str(uuid.uuid4().int)[:13]


# ---------------------------------------------------------------------------
# Date parsing helper (used by kitchen and stats routes)
# ---------------------------------------------------------------------------

def _parse_dt(value: str, is_end: bool = False):
    if not value:
        return None
    v = value.strip()
    try:
        if len(v) == 10 and v[4] == '-' and v[7] == '-':
            d = datetime.strptime(v, "%Y-%m-%d")
            return d.replace(hour=23, minute=59, second=59, microsecond=999999) if is_end else d
        v2 = v.replace('Z', '')
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(v2, fmt)
            except ValueError:
                pass
        d = datetime.strptime(v[:10], "%Y-%m-%d")
        return d.replace(hour=23, minute=59, second=59, microsecond=999999) if is_end else d
    except Exception:
        return None


def _serialize_product(p, include_recipe=False, include_packages=False, image_cache=None, supplier_cache=None,
                        stock_cache=None, purchase_options_cache=None, latest_batch_cache=None, purchase_cost_cache=None,
                        recipe_lines_cache=None, sell_packages_cache=None):
    d = {
        'id':           p.id,
        'name':         p.name,
        'price':        float(p.price) if p.price is not None else None,
        'barcode':      p.barcode,
        'product_code': p.product_code,
        'stock_qty':    p.stock_qty,
        'product_type': p.product_type,
        'unit_type':    p.unit_type,
        'base_unit':    p.base_unit,
        'sold_by_weight':       p.sold_by_weight,
        'is_for_sale':          p.is_for_sale,
        'price_per_unit':       float(p.price_per_unit) if p.price_per_unit is not None else None,
        'low_stock_threshold':  float(p.low_stock_threshold) if p.low_stock_threshold is not None else None,
        'package_size':         float(p.package_size) if p.package_size is not None else None,
        'package_size_unit':    p.package_size_unit,
        'package_unit':         p.package_unit,
        'parent_stock_item_id': p.parent_stock_item_id,
        'margin_pct':      float(p.margin_pct) if p.margin_pct is not None else None,
        'is_prepared':          p.is_prepared,
        'is_available_online':  p.is_available_online,
        'image_url':            p.image_url,
        'description':          p.description,
        'is_archived':          p.is_archived,
        'archived_reason':      p.archived_reason,
        'category_id':          p.category_id,
        'category_name':        p.category.name if p.category else None,
        'sub_category_id':      p.sub_category_id,
        'sub_category_name':    p.sub_category.name if getattr(p, 'sub_category', None) else None,
        'product_family_id':    p.product_family_id,
        'family_name':          p.family.name if getattr(p, 'family', None) else None,
        'is_default_variant':   p.is_default_variant,
        # Scale sync fields
        'sync_to_scale':           p.sync_to_scale,
        'scale_tare':              float(p.scale_tare) if p.scale_tare is not None else 0,
        'scale_shelf_life':        p.scale_shelf_life or 0,
        'scale_pack_qty':          p.scale_pack_qty or 0,
        'scale_open_price':        p.scale_open_price,
        'scale_msg1':              p.scale_msg1 or '',
        'scale_msg2':              p.scale_msg2 or '',
        # Barcode config - used by POS scanner to decode scale labels
        'scale_barcode_prefix':    20,    # scale always uses prefix 20
        'scale_barcode_format':    'price_cents',  # VVVVVV = total price in cents
        'scale_last_synced_at':    p.scale_last_synced_at.isoformat() if p.scale_last_synced_at else None,
        'scale_last_sync_status':  p.scale_last_sync_status,
        'scale_last_sync_error':   p.scale_last_sync_error,
        'stat_unit_size':          float(p.stat_unit_size) if p.stat_unit_size is not None else None,
        'is_produced':             p.is_produced,
        'batch_size':              float(p.batch_size) if p.batch_size is not None else 1.0,
        'stock_unit':              p.stock_unit,
        'last_overhead_costs':     p.last_overhead_costs,
        'inventory_policy':        p.inventory_policy or 'ALLOW_NEGATIVE',
        # Consignment fields
        'is_consignment':             p.is_consignment,
        'settlement_basis':           p.settlement_basis,
        'consignment_pct':            float(p.consignment_pct) if p.consignment_pct is not None else None,
        'consignment_supplier_id':    p.consignment_supplier_id,
        'consignment_cost_per_unit':  float(p.consignment_cost_per_unit) if p.consignment_cost_per_unit is not None else None,
        'auto_price':        p.auto_price if getattr(p, 'auto_price', None) is not None else True,
        'supplier_names':    ', '.join(supplier_cache[p.id]) if supplier_cache and p.id in supplier_cache else '',
        'pending_price':          float(p.pending_price) if getattr(p, 'pending_price', None) is not None else None,
        'pending_price_per_unit': float(p.pending_price_per_unit) if getattr(p, 'pending_price_per_unit', None) is not None else None,
        'packaging_capacity':     p.packaging_capacity,
        'purchase_options': [
            {
                'id':               opt.id,
                'package_size':     float(opt.package_size),
                'package_size_unit': opt.package_size_unit,
                'package_unit':     opt.package_unit,
                'sort_order':       opt.sort_order,
            }
            for opt in sorted(
                purchase_options_cache[p.id] if purchase_options_cache is not None
                else ProductPurchaseOption.query.filter_by(product_id=p.id).all(),
                key=lambda o: (o.sort_order, o.id)
            )
        ],
        'cost_per_base_unit':     (
            latest_batch_cache.get(p.id) if latest_batch_cache is not None
            else (lambda _b: float(_b.cost_per_base_unit) if _b and _b.cost_per_base_unit else None)(
                StockBatch.query.filter_by(product_id=p.id)
                .order_by(StockBatch.purchased_at.desc(), StockBatch.id.desc()).first()
            )
        ),
        'images': image_cache[p.id] if image_cache is not None else [{
            'id':            img.id,
            'filename':      img.filename,
            'is_primary':    img.is_primary,
            'display_order': img.display_order,
        } for img in ProductImage.query.filter_by(product_id=p.id).order_by(ProductImage.display_order).all()],
    }
    if p.product_type == 'stock_item':
        d['stock_level'] = stock_cache[p.id] if stock_cache is not None else get_stock_level(p.id)
        d['low_stock']   = (
            p.low_stock_threshold is not None and
            d['stock_level'] < float(p.low_stock_threshold)
        )
    if p.product_type == 'recipe' and p.is_produced:
        d['stock_level'] = stock_cache[p.id] if stock_cache is not None else get_stock_level(p.id)
    if p.product_type == 'simple':
        # Weighted-average purchase cost per unit - lets the product page show
        # margin/markup for resale goods (no stock batches, costed from purchases).
        if purchase_cost_cache is not None:
            d['unit_cost'] = purchase_cost_cache.get(p.id)
        else:
            total_value, total_qty = db.session.query(
                func.coalesce(func.sum(Purchase.qty_added * Purchase.purchase_price), 0),
                func.coalesce(func.sum(Purchase.qty_added), 0),
            ).filter(Purchase.product_id == p.id).one()
            d['unit_cost'] = float(total_value) / float(total_qty) if total_qty else None
    if include_recipe and p.product_type in ('recipe',):
        lines = recipe_lines_cache[p.id] if recipe_lines_cache is not None else RecipeLine.query.filter_by(product_id=p.id).all()
        d['recipe_lines'] = []
        for ln in lines:
            ing = db.session.get(Product, ln.ingredient_id)
            d['recipe_lines'].append({
                'ingredient_id':   ln.ingredient_id,
                'ingredient_name': ing.name if ing else None,
                'unit_type':       ing.unit_type if ing else None,
                'base_unit':       ing.base_unit if ing else None,
                'qty_base':        float(ln.qty_base),
            })
    if include_packages and p.product_type == 'stock_item':
        pkgs = sell_packages_cache[p.id] if sell_packages_cache is not None else Product.query.filter_by(parent_stock_item_id=p.id).all()
        d['sell_packages'] = []
        for pkg in pkgs:
            pkg_rl = (sell_packages_cache.get('_rl', {}).get(pkg.id)
                      if sell_packages_cache is not None
                      else RecipeLine.query.filter_by(product_id=pkg.id).first())
            d['sell_packages'].append({
                'id':       pkg.id,
                'name':     pkg.name,
                'price':    float(pkg.price) if pkg.price is not None else None,
                'barcode':  pkg.barcode,
                'qty_base': float(pkg_rl.qty_base) if pkg_rl else None,
            })
    return d


def collect_kitchen_items(product_id, qty, depth=0, subs=None, extras=None):
    """Resolve a product into [(Product, qty, ingredients)] for kitchen orders.
    Recurses into recipes. Returns [] for non-kitchen products."""
    from decimal import Decimal as _D
    if depth > 10:
        return []
    p = db.session.get(Product, int(product_id))
    if not p:
        return []
    subs = subs or {}
    extras = extras or []
    if p.is_prepared:
        ingredients = []
        for rl in RecipeLine.query.filter_by(product_id=product_id).all():
            actual_id = subs.get(rl.ingredient_id, rl.ingredient_id)
            if actual_id == -1:
                orig = db.session.get(Product, rl.ingredient_id)
                ingredients.append({'name': orig.name if orig else str(rl.ingredient_id), 'qty': 0, 'base_unit': '', 'substituted': True, 'removed': True})
                continue
            ing      = db.session.get(Product, actual_id)
            orig_ing = db.session.get(Product, rl.ingredient_id) if actual_id != rl.ingredient_id else ing
            if not ing:
                continue
            substituted = actual_id != rl.ingredient_id
            if ing.product_type == 'stock_item':
                entry = {'name': ing.name, 'qty': float(rl.qty_base) * float(qty), 'base_unit': ing.base_unit or 'unit', 'substituted': substituted}
                if substituted and orig_ing:
                    entry['original_name'] = orig_ing.name
                ingredients.append(entry)
            elif ing.product_type == 'recipe':
                ingredients.append({'name': ing.name, 'qty': float(qty), 'base_unit': 'portion', 'substituted': substituted})
        for ex in extras:
            ex_id  = int(ex.get('ingredient_id', 0))
            ex_qty = float(ex.get('qty_base', 0)) * float(qty)
            if ex_id and ex_qty > 0:
                ex_ing = db.session.get(Product, ex_id)
                if ex_ing:
                    ingredients.append({'name': ex_ing.name, 'qty': ex_qty, 'base_unit': ex_ing.base_unit or 'unit', 'extra': True})
        return [(p, qty, ingredients)]
    elif p.product_type == 'recipe':
        results = []
        for rl in RecipeLine.query.filter_by(product_id=product_id).all():
            results.extend(collect_kitchen_items(rl.ingredient_id, _D(str(rl.qty_base)) * qty, depth + 1, subs))
        return results
    return []


def build_full_products_list():
    """Serialize all products with bulk caches — same result as GET /api/products?full=1.
    Used by the login endpoint so the client gets products in one round-trip."""
    from collections import defaultdict

    products = Product.query.order_by(Product.name.asc()).all()
    if not products:
        return []
    pid_list = [p.id for p in products]

    all_images = (ProductImage.query
                  .filter(ProductImage.product_id.in_(pid_list))
                  .order_by(ProductImage.product_id, ProductImage.display_order).all())
    image_cache = defaultdict(list)
    for img in all_images:
        image_cache[img.product_id].append({
            'id': img.id, 'filename': img.filename,
            'is_primary': img.is_primary, 'display_order': img.display_order,
        })

    supplier_cache: dict = {}
    for pid, sname in (db.session.query(StockBatch.product_id, Supplier.name)
                       .join(Supplier, Supplier.id == StockBatch.supplier_id)
                       .filter(StockBatch.supplier_id.isnot(None)).all()):
        if pid not in supplier_cache:
            supplier_cache[pid] = []
        if sname and sname not in supplier_cache[pid]:
            supplier_cache[pid].append(sname)

    stock_cache = defaultdict(float)
    for pid, total in (db.session.query(StockBatch.product_id, func.sum(StockBatch.qty_remaining_base))
                       .filter(StockBatch.product_id.in_(pid_list))
                       .group_by(StockBatch.product_id).all()):
        stock_cache[pid] = float(total or 0)

    purchase_options_cache = defaultdict(list)
    for opt in ProductPurchaseOption.query.filter(
            ProductPurchaseOption.product_id.in_(pid_list)).all():
        purchase_options_cache[opt.product_id].append(opt)

    subq = (db.session.query(StockBatch.product_id, func.max(StockBatch.id).label('max_id'))
            .filter(StockBatch.product_id.in_(pid_list))
            .group_by(StockBatch.product_id).subquery())
    latest_batch_cache: dict = {}
    for pid, cost in (db.session.query(StockBatch.product_id, StockBatch.cost_per_base_unit)
                      .join(subq, (StockBatch.product_id == subq.c.product_id) &
                                  (StockBatch.id == subq.c.max_id)).all()):
        latest_batch_cache[pid] = float(cost) if cost else None

    purchase_cost_cache: dict = {}
    for pid, total_value, total_qty in (db.session.query(
        Purchase.product_id,
        func.coalesce(func.sum(Purchase.qty_added * Purchase.purchase_price), 0),
        func.coalesce(func.sum(Purchase.qty_added), 0),
    ).filter(Purchase.product_id.in_(pid_list)).group_by(Purchase.product_id).all()):
        purchase_cost_cache[pid] = float(total_value) / float(total_qty) if total_qty else None

    recipe_lines_cache = defaultdict(list)
    for rl in RecipeLine.query.filter(RecipeLine.product_id.in_(pid_list)).all():
        recipe_lines_cache[rl.product_id].append(rl)

    # sell_packages_cache: product_id → [Package products]; '_rl' key → {pkg_id: RecipeLine}
    sell_packages_cache = defaultdict(list)
    sell_packages_cache['_rl'] = {}
    pkg_ids = []
    for pkg in Product.query.filter(Product.parent_stock_item_id.in_(pid_list)).all():
        sell_packages_cache[pkg.parent_stock_item_id].append(pkg)
        pkg_ids.append(pkg.id)
    if pkg_ids:
        for rl in RecipeLine.query.filter(RecipeLine.product_id.in_(pkg_ids)).all():
            sell_packages_cache['_rl'][rl.product_id] = rl

    return [_serialize_product(p, include_recipe=True, include_packages=True,
                               image_cache=image_cache,
                               supplier_cache=supplier_cache,
                               stock_cache=stock_cache,
                               purchase_options_cache=purchase_options_cache,
                               latest_batch_cache=latest_batch_cache,
                               purchase_cost_cache=purchase_cost_cache,
                               recipe_lines_cache=recipe_lines_cache,
                               sell_packages_cache=sell_packages_cache)
            for p in products]
