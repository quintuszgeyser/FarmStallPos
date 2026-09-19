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

'till_sessions' was adopted fifth: it's small (three routes) but the primary
till-fraud detection surface — the Z-report close records counted cash vs.
expected cash and the over/under, the same kind of record P2-4 made void
reasons mandatory to protect.

'suppliers' was adopted sixth: purchase_run is the other major costing-input
surface besides stock.py's receive/adjust — it's what P2-0's write_stock_
movement and absorb_neg_placeholder calls in stock.py are shared with, and it
carries the VAT/discount/shipping allocation waterfall P0-2's INV-9 checks.
The invoice PUT/DELETE routes and supplier CRUD are audited for the same
reason invoices.py's routes are: they change billed amounts and vendor
records with no prior trail.

'products' was adopted seventh: create/update/archive/delete/copy and
produce all change price, margin, or costing inputs that every other
blueprint's cost calculations (FIFO, recipe cost, VAT) read from — the
product record is the thing being protected, not just another mutation
surface. Image management (upload/delete/reorder/set-primary) is the one
deliberate EXPLICITLY_EXEMPT block in this blueprint: cosmetic product
photography with no financial, inventory, or costing impact, current state
always visible directly on the product.

'customers' was adopted eighth: customer CRUD, merges/unmerges, exclusions,
and biometric enrollment (face/gait/plate) are all admin-gated identity
decisions with real POPIA consequences, so they're AUDITED. The recognition
service's own detection telemetry (identify, log_plate, till/detect,
attributes POST, sessions) is EXPLICITLY_EXEMPT instead — high-frequency
machine writes where the created row (visit/detection/attribute/session) is
already the record, so a duplicate AuditLog entry per camera event would be
log spam with no decision to review. Visit-notification acknowledgement gets
the same exemption as products.py's image routes: a UI-only flag with no
financial or PII impact beyond what the row already carries.

Adopted from here on, terser: 'branding' (one route — rare admin config
change, AUDITED). 'packaging' (list/suggestions read-only; record is high-
frequency teller usage-counter telemetry, EXPLICITLY_EXEMPT — same reasoning
as customers.py's recognition-telemetry routes). 'subcategories',
'categories', 'cost_categories', 'families' (small taxonomy CRUD blueprints —
create/update/delete/merge AUDITED, matching suppliers.py's CRUD precedent;
reads NO_STATE_CHANGE). 'kitchen' (all mutations EXPLICITLY_EXEMPT — routine
kitchen-queue workflow, no financial/inventory impact, the KitchenOrder row
is the record). 'specials' (promo pricing CRUD, AUDITED — changes what
customers pay). 'settings' and 'recognition' (settings save AUDITED;
recognition's control/<action> proxy AUDITED too — includes purge_customer).
'kiosk' (tablet fleet config and remote control actions AUDITED — includes
reboot; status/query/screenshot reads NO_STATE_CHANGE).
"""
ADOPTED_BLUEPRINTS = {'transactions', 'auth', 'stock', 'invoices', 'till_sessions', 'suppliers',
                      'products', 'customers', 'branding', 'packaging', 'subcategories',
                      'categories', 'cost_categories', 'families', 'kitchen', 'specials',
                      'settings', 'recognition', 'kiosk'}


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
    assert 'till_sessions.api_till_close' in by_policy.get('AUDITED', [])
    assert 'till_sessions.api_till_summary' in by_policy.get('NO_STATE_CHANGE', [])
    assert 'till_sessions.api_till_sessions_list' in by_policy.get('NO_STATE_CHANGE', [])
    assert 'suppliers.api_suppliers_purchase_run' in by_policy.get('AUDITED', [])
    assert 'suppliers.api_supplier_invoice_delete' in by_policy.get('AUDITED', [])
    assert 'suppliers.api_suppliers_delete' in by_policy.get('AUDITED', [])
    assert 'suppliers.api_suppliers_get' in by_policy.get('NO_STATE_CHANGE', [])
    assert 'products.api_products_post' in by_policy.get('AUDITED', [])
    assert 'products.api_products_update' in by_policy.get('AUDITED', [])
    assert 'products.api_product_archive' in by_policy.get('AUDITED', [])
    assert 'products.api_products_delete' in by_policy.get('AUDITED', [])
    assert 'products.api_products_get' in by_policy.get('NO_STATE_CHANGE', [])
    assert 'products.api_product_image_upload' in by_policy.get('EXPLICITLY_EXEMPT', [])
    assert 'customers.api_customers_merge' in by_policy.get('AUDITED', [])
    assert 'customers.api_customers_enroll_face' in by_policy.get('AUDITED', [])
    assert 'customers.api_customers_delete_permanent' in by_policy.get('AUDITED', [])
    assert 'customers.api_customers_get' in by_policy.get('NO_STATE_CHANGE', [])
    assert 'customers.api_customers_identify' in by_policy.get('EXPLICITLY_EXEMPT', [])
    assert len(by_policy.get('NO_STATE_CHANGE', [])) >= 3
    assert len(by_policy.get('EXPLICITLY_EXEMPT', [])) >= 2
