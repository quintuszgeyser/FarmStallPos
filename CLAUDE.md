# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the App

Two environments, two scripts:

| Environment | Script | URL | Database |
|---|---|---|---|
| QA (dev/testing) | `start-qa.ps1` | `http://localhost:5000` | `farm_pos` |
| Production | `start-prod.ps1` | `https://localhost:5443` | `farm_pos_prod` |

```powershell
# QA
powershell -ExecutionPolicy Bypass -File start-qa.ps1

# Production
powershell -ExecutionPolicy Bypass -File start-prod.ps1
```

QA shows a yellow banner at the top so you can never confuse the two.
Default login (both): `admin` / `admin123` (change prod password in `start-prod.ps1`).

To promote QA → Production:

```powershell
powershell -ExecutionPolicy Bypass -File promote.ps1
```

This merges `main` into the `production` GitHub branch and pushes both.

To install new packages add `--trusted-host pypi.org --trusted-host files.pythonhosted.org` (Capitec corporate SSL proxy):

```powershell
.venv\Scripts\pip install <package> --trusted-host pypi.org --trusted-host files.pythonhosted.org
```

## Environment

- **Database:** PostgreSQL at `postgresql://farmstall:FarmStall@localhost:5432/farm_pos`
- **PostgreSQL location:** `C:\Users\CP368103\PostgreSQL\pgsql\` — not a Windows service, started manually by the start scripts
- **Python venv:** `.venv\` in project root
- **Platform:** Windows 11, shell is bash (use Unix syntax in tool calls)

## Architecture

Single-file Flask backend (`app.py`) + single-page frontend (`templates/index.html` + `static/main.js`).

### Backend (`app.py`)

- All routes are REST JSON API under `/api/` except the root `/` which serves the SPA and `/admin/export/` for CSV downloads
- **Auth:** Flask session-based. `require_login()` checks session, `require_role('admin')` checks role. No JWT.
- **Session tracking:** `UserSession` records login/logout/last_active. `before_request` stamps `last_active` and auto-closes idle sessions after `SESSION_TIMEOUT_MINUTES` (10 min), creating a new one transparently. Hard logout fires after `SESSION_LOGOUT_HOURS` (2 h) of total inactivity via `require_login()`.
- **DB:** SQLAlchemy ORM. All money columns are `Numeric(10,2)` — never `Float`. Decimal→float conversion handled by custom `_JSONProvider` at the top of the file.
- **Migration:** `strong_migrate()` runs on every startup. It is idempotent — uses `SAVEPOINT`/rollback per DDL statement for PostgreSQL so failures don't abort the transaction. Add all schema changes here, never use Alembic.
- **Concurrency:** All stock-mutating routes use `with_for_update=True` on `db.session.get()` and `.with_for_update()` on batch queries to prevent race conditions under concurrent users.
- **`Sale` model** is the single source of truth for transactions. Each receipt is a group of rows sharing the same `sale_id` (UUID string). Voided sales have `voided=True` and are excluded from all queries.
- **`Purchase` model** is the legacy stock-receiving path for `simple` products. `stock_batches` is the current path for `stock_item` products.

### Frontend (`static/main.js`)

- Vanilla JS, no framework. All state lives in the `STATE` object at the top.
- `api(path, opts)` is the single fetch wrapper — throws on non-2xx with the server's error message.
- `toast(msg, type, durationMs)` replaces all `alert()` calls — never use `alert()`.
- `displayQty(qty_base, unitType)` converts base units (g/ml) to friendly display (kg/L) with auto-thresholding at 1000. `displayCost(cost_per_base, qty_base, unitType)` returns `{cost, unit}` scaled to the same display unit — always use these together when showing stock quantities and costs.
- `_globalMarkupPct` holds the default margin loaded from `/api/settings` — do not use a DOM element for this.
- **USB barcode scanner support:** global `keydown` listener buffers rapid keystrokes ending in Enter. Only active on the Teller tab when no input is focused.
- **Camera scanner:** ZXing `BrowserMultiFormatReader`, on-demand only (button toggle), 1.5s cooldown.

### Product Types

`product_type` drives all stock, pricing, and FIFO behaviour:

| Type | Stock tracked by | Sold by | Cost |
|---|---|---|---|
| `simple` | `stock_qty` (integer) | unit | no COGS |
| `stock_item` | `stock_batches` (FIFO) | weight/volume or unit | FIFO COGS |
| `recipe` | ingredients' batches (FIFO) | unit | sum of ingredient COGS |

- `sold_by_weight=True` on a `stock_item` means it is weighed at point of sale (price × weight).
- `is_for_sale=False` marks internal ingredients that don't appear at the teller.
- `is_prepared=True` sends the product to the kitchen queue on sale.

### Key Design Decisions

- **Transactions tab is role-aware:** tellers see last 5 only and never see COGS/margin; admins get a date-range picker and full financials.
- **Edit vs Void:** editing a transaction restores original stock then deducts new stock atomically. Voiding requires a reason and fully restores stock.
- **`sale_id` is a UUID string** — always slice to 8 chars for display (`String(id).slice(0,8)`).
- **Product editor modal** (`#productEditorModal`) shows only "Add" when creating, only "Update"+"Delete" when editing. `openProductEditor(p)` handles this — always call it, never show the modal directly.
- **Supplier contact fields** are three separate columns: `phone`, `email`, `website` — not a single `contact` string.

## Database Schema

```
users             — id, username, password_hash, role, active
products          — id, name, price (Numeric), barcode, stock_qty, product_type,
                    unit_type, base_unit, sold_by_weight, is_for_sale, is_prepared,
                    price_per_unit (Numeric), low_stock_threshold, package_size,
                    package_size_unit, package_unit, margin_pct, is_archived
purchases         — id, product_id, qty_added, purchase_price (Numeric), date_time, user_id
settings          — id, key, value
sales             — id, sale_id, date_time, product_id, qty (Numeric), unit_price (Numeric),
                    user_id, voided, voided_by, voided_at, void_reason, sub_log
recipe_lines      — id, product_id, ingredient_id, qty_base (Numeric)
stock_batches     — id, product_id, qty_purchased_base, qty_remaining_base,
                    cost_per_base_unit (Numeric 10,6), purchased_at, user_id, supplier_id
stock_consumption — id, sale_id, ingredient_id, batch_id, qty_consumed_base,
                    cost_per_base_unit, consumed_at
stock_adjustments — id, product_id, adjustment_type, qty_change_base, system_qty_before,
                    cost_written_off, reason, adjusted_at, user_id
suppliers         — id, name, phone, email, website, notes
kitchen_orders    — id, sale_id, product_id, product_name, qty, ingredients (JSON),
                    status, sort_order, queued_at, completed_at, teller_id
user_sessions     — id, user_id, logged_in, logged_out, last_active
specials          — id, name, special_price, active
special_lines     — id, special_id, product_id, qty
```

## API Quick Reference

```
POST /api/login                              { username, password }
POST /api/logout
GET  /api/me
GET  /api/products
GET  /api/products/<id>
POST /api/products                           admin only
POST /api/products/update                    admin only
POST /api/products/<id>/archive              admin only
POST /api/products/<id>/restore              admin only
DELETE /api/products/<name>                  admin only
GET  /api/products/<id>/recipe_cost
GET  /api/products/<id>/fifo_price           ?markup=
GET  /api/products/<id>/suggested_price      ?markup=
POST /api/purchases                          admin only
GET  /api/transactions                       ?start=&end=
POST /api/transactions                       { cart: [{product_id, qty, unit_price, subs, extras}] }
POST /api/transactions/<sale_id>/void        { reason }  admin only
POST /api/transactions/<sale_id>/edit        { lines }   admin only
GET  /api/stats                              ?start=&end=  admin only
GET  /api/settings                           admin only
POST /api/settings                           admin only
GET  /api/suppliers
POST /api/suppliers                          admin only
POST /api/suppliers/<id>                     admin only
DELETE /api/suppliers/<id>                   admin only
GET  /api/suppliers/<id>/products
POST /api/suppliers/<id>/purchase_run        admin only
GET  /api/stock/ingredients
POST /api/stock/receive                      admin only
POST /api/stock/writeoff                     admin only
POST /api/stock/adjust                       admin only
GET  /api/stock/adjustments
GET  /api/users                              admin only
POST /api/users                              admin only
POST /api/users/<id>                         admin only
DELETE /api/users/<id>                       admin only
GET  /api/kitchen/orders
POST /api/kitchen/orders/<id>/complete       admin only
GET  /api/specials
POST /api/specials                           admin only
GET  /admin/export/products                  CSV
GET  /admin/export/transactions              ?start=&end= CSV
```
