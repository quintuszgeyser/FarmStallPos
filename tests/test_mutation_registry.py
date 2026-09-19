"""Mutation registry coverage tests (Rev 5 P3-1b).

"Every non-GET route writes an audit row" is the wrong CI rule — HTTP method
is a signal, not a boundary (see helpers.audit_policy's docstring for the
full policy vocabulary). Every route in an ADOPTED blueprint below must
declare one via @audit_policy; tests/test_audit_service.py and the routes
exercised throughout the rest of the suite prove the runtime half (an
AUDITED route that completes 2xx without writing an AuditLog row raises via
helpers._install_audit_completeness_check, which fires on every request in
TESTING config).

Scope: this is a gradual-adoption registry, not full coverage of the app's
~370 routes. ADOPTED_BLUEPRINTS lists the ones that have been fully
classified so far; adding a new route to an adopted blueprint without a
policy fails test_every_route_in_an_adopted_blueprint_declares_a_policy.
Extending ADOPTED_BLUEPRINTS to another blueprint is the natural way to grow
coverage — do that once that blueprint's routes have actually been
classified, not by adding its name here first.

'stock' was adopted third (after transactions, auth): it's the direct
inventory/costing mutation surface the P2 ledger-integrity work (movement
ledger, FIFO reversal, stocktake variance) already hardened, so it's the
next-highest-stakes blueprint after money movement and account lifecycle.

'invoices' was adopted fourth: it's the other money-movement surface besides
`transactions` — invoice create/update/delete/copy/finalise/undo all touch
billed amounts, and finalise/undo already run through the same FIFO and
consignment-liability reversal paths P2-2b hardened. Shipping-fee updates are
audited too since they change what online customers are charged.
"""
ADOPTED_BLUEPRINTS = {'transactions', 'auth', 'stock', 'invoices'}


def _adopted_rules(app):
    for rule in app.url_map.iter_rules():
        if rule.endpoint == 'static' or '.' not in rule.endpoint:
            continue
        bp_name = rule.endpoint.split('.', 1)[0]
        if bp_name in ADOPTED_BLUEPRINTS:
            yield rule


def test_every_route_in_an_adopted_blueprint_declares_a_policy(app):
    missing = []
    for rule in _adopted_rules(app):
        fn = app.view_functions[rule.endpoint]
        if not hasattr(fn, '_audit_policy'):
            missing.append(rule.endpoint)
    assert not missing, (
        f"routes in adopted blueprints ({sorted(ADOPTED_BLUEPRINTS)}) with no "
        f"@audit_policy declared: {missing}"
    )


def test_every_explicitly_exempt_route_has_a_reason(app):
    unreasoned = []
    for rule in _adopted_rules(app):
        fn = app.view_functions[rule.endpoint]
        policy = getattr(fn, '_audit_policy', None)
        if policy == 'EXPLICITLY_EXEMPT' and not getattr(fn, '_audit_policy_reason', None):
            unreasoned.append(rule.endpoint)
    # Unreachable in practice — audit_policy() itself raises at decoration
    # time (import time) if EXPLICITLY_EXEMPT has no reason. Kept as a
    # regression guard in case that constructor-time check is ever loosened.
    assert not unreasoned, f"EXPLICITLY_EXEMPT routes with no reason: {unreasoned}"


def test_adopted_blueprints_have_the_expected_policy_mix(app):
    """Sanity check on the classification itself, not just its presence —
    catches an accidental blanket policy (e.g. everything marked
    NO_STATE_CHANGE) that would make the coverage test above vacuous."""
    by_policy = {}
    for rule in _adopted_rules(app):
        fn = app.view_functions[rule.endpoint]
        policy = getattr(fn, '_audit_policy', None)
        by_policy.setdefault(policy, []).append(rule.endpoint)

    assert 'transactions.api_transaction_void' in by_policy.get('AUDITED', [])
    assert 'transactions.api_transaction_edit' in by_policy.get('AUDITED', [])
    assert 'transactions.api_transaction_return' in by_policy.get('AUDITED', [])
    assert 'auth.api_users_delete' in by_policy.get('AUDITED', [])
    assert 'auth.api_login' in by_policy.get('SECURITY_EVENT_ONLY', [])
    assert 'stock.api_stock_receive' in by_policy.get('AUDITED', [])
    assert 'stock.api_stock_writeoff' in by_policy.get('AUDITED', [])
    assert 'stock.api_stock_adjust' in by_policy.get('AUDITED', [])
    assert 'stock.api_stock_ingredients' in by_policy.get('NO_STATE_CHANGE', [])
    assert 'invoices.api_invoices_create' in by_policy.get('AUDITED', [])
    assert 'invoices.api_invoices_finalise' in by_policy.get('AUDITED', [])
    assert 'invoices.api_invoices_undo' in by_policy.get('AUDITED', [])
    assert 'invoices.api_invoices_delete' in by_policy.get('AUDITED', [])
    assert 'invoices.api_invoices_list' in by_policy.get('NO_STATE_CHANGE', [])
    assert len(by_policy.get('NO_STATE_CHANGE', [])) >= 3
    assert len(by_policy.get('EXPLICITLY_EXEMPT', [])) >= 2
