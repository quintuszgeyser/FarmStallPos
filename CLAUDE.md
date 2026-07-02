# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the App

| Environment | Script | URL | Database |
|---|---|---|---|
| QA (dev/testing) | `start-qa.ps1` | `http://localhost:5000` | `farm_pos` |
| Production | `start-prod.ps1` | `https://localhost:5443` | `farm_pos_prod` |

```powershell
powershell -ExecutionPolicy Bypass -File start-qa.ps1
powershell -ExecutionPolicy Bypass -File start-prod.ps1
```

QA shows a yellow banner. Default login (both): `admin` / `admin123`.

Promote QA → Production branch:
```powershell
powershell -ExecutionPolicy Bypass -File promote.ps1
```

pip installs require SSL bypass (Capitec corporate proxy).
The venv `python.exe` stub is broken - use `C:\Python314\python.exe` directly:
```powershell
C:\Python314\python.exe -m pip install <package> --trusted-host pypi.org --trusted-host files.pythonhosted.org
```

## Deployment (Ubuntu Server via Docker)

The production system runs on an Ubuntu 24.04 server (`farmpc` in `~/.ssh/config`) via Docker Compose at `~/farmpos-docker/`. SSH requires a jump host - see `~/.ssh/config`.

**Monorepo:** Both apps live in `https://github.com/quintuszgeyser/FarmStallPos.git`
- `farm_pos_web/` - POS (this folder, git root)
- `ladycoleen_web/` - Lady Coleen website (subfolder)

```bash
# Deploy POS - push to GitHub first, then:
ssh farmpc 'cd ~/farmpos-docker && bash deploy.sh pos'

# Deploy website - push to GitHub first, then:
ssh farmpc 'cd ~/farmpos-docker && bash deploy.sh web'

# Deploy recognition - SCP file first, then:
cat recognition_service_v2.py | ssh farmpc 'cat > ~/farmpos-docker/recognition/recognition_service_v2.py'
ssh farmpc 'cd ~/farmpos-docker && bash deploy.sh recognition'

# Check container health
ssh farmpc 'docker compose -f ~/farmpos-docker/docker-compose.yml ps'
```

**Critical deploy rules:**
- POS and web pull from GitHub on every build (`--no-cache`) - always push to `main` before deploying
- Recognition uses local SCP - GitHub push alone does nothing for recognition
- NEVER `docker compose up` directly - always use `deploy.sh`
- After deploy, verify new code: `docker exec ladycoleen-web grep -c "some_new_string" /app/blueprints/farmshop.py`

## Environment

- **Database:** PostgreSQL at `postgresql://farmstall:FarmStall@localhost:5432/farm_pos`
- **PostgreSQL location:** `C:\Users\CP368103\PostgreSQL\pgsql\` - not a Windows service, started by the start scripts
- **Python (local):** `C:\Python314\python.exe` - the `.venv\Scripts\python.exe` stub is broken (points to missing Python311)
- **Platform:** Windows 11 locally, Ubuntu 24.04 in production (Docker)
- **Shell:** bash (use Unix syntax in tool calls)

## Architecture

Blueprint-based Flask backend + single-page frontend. Refactored June 2026.

**Backend structure:**
- `app.py` (1,146 lines) - pure factory: `create_app()`, `strong_migrate()`, `_register_routes()`
- `models.py` - 21 SQLAlchemy models, `db = SQLAlchemy()` unbound
- `helpers.py` - `get_setting`, `require_login`, `consume_fifo`, `_parse_dt`, etc.
- `blueprints/` - 15 Blueprint files covering all 131 routes

**Import order (never break):** `blueprints → helpers → models → db`  
**Never import from `app.py` inside a blueprint** - circular import risk.

### Backend (`app.py`) - 1,146 lines (factory only)

**Route groups** (131 routes across 15 blueprints):

| Blueprint | Routes |
|---|---|
| `blueprints/auth.py` | `/api/login\|logout\|me`, `/api/users/*` |
| `blueprints/products.py` | `/api/products/*` (images, archive, recipe_cost, fifo_price) |
| `blueprints/suppliers.py` | `/api/suppliers/*`, `purchase_run` |
| `blueprints/stock.py` | `/api/stock/*`, `/api/purchases` |
| `blueprints/customers.py` | `/api/customers/*`, `/api/till/*` |
| `blueprints/transactions.py` | `/api/transactions/*` |
| `blueprints/kitchen.py` | `/api/kitchen/*` |
| `blueprints/specials.py` | `/api/specials/*` |
| `blueprints/invoices.py` | `/api/invoices/*`, `/invoices/*/print` |
| `blueprints/stats.py` | `/api/stats/*`, `/admin/export/*` |
| `blueprints/settings.py` | `/api/settings` |
| `blueprints/recognition.py` | `/api/recognition/*` |
| `blueprints/kiosk.py` | `/api/kiosk/*` |
| `blueprints/system.py` | `/api/system/update-*` |
| `blueprints/core.py` | `/`, `/health`, `/guide`, `/__version`, `/api/logs`, `/api/db-*` |

**Key patterns:**
- All money columns are `Numeric(10,2)` - never `Float`
- `strong_migrate()` runs on every startup - idempotent via `SAVEPOINT`/rollback. Add all schema changes here, never use Alembic
- Stock-mutating routes use `with_for_update=True` to prevent race conditions
- `Sale` rows share a UUID `sale_id` - always slice to 8 chars for display
- `require_login()` / `require_role('admin')` for auth guards
- Session auto-closes after `SESSION_TIMEOUT_MINUTES` (10 min) idle; hard logout after `SESSION_LOGOUT_HOURS` (2 h)

**Database models** (21 total):
- Core: `User`, `UserSession`, `Setting`
- Products: `Product`, `ProductImage`, `RecipeLine`, `Supplier`
- Stock: `StockBatch`, `StockConsumption`, `StockAdjustment`
- Sales: `Sale`, `Purchase`, `Special`, `SpecialLine`, `Invoice`, `KitchenOrder`
- Customers: `Customer`, `CustomerPlate`, `CustomerFace`, `CustomerGait`, `CustomerVisit`, `PlateDetection`

### Frontend (`static/main.js`) - ~8,444 lines

All state lives in the `STATE` object at the top. Sections are separated by `═══` dividers. Major sections:

| Section | What it does |
|---|---|
| STATE / HELPERS / AUTH | Global state, `api()` fetch wrapper, `toast()`, `show()`/`hide()`, role-based visibility |
| PRODUCTS | Product cards, archive/restore, product editor modal |
| STOCK TAB | Ingredient stock, stocktake modal, write-off modal |
| SUPPLIERS | Supplier CRUD, purchase runs |
| CART / TELLER | Shopping cart, weight entry, barcode scanner (USB + camera) |
| TRANSACTIONS | History, void, edit, flag |
| STATS | Dashboard with drilldown charts |
| SETTINGS | App settings including kiosk tablet management |
| KITCHEN | Kitchen order queue |
| CUSTOMERS | Customer management, face/plate/gait enrollment, merge suggestions |
| INVOICES | Draft/finalised invoice management |
| DEVELOPER MONITOR | Recognition service live diagnostics |

Key conventions:
- `api(path, opts)` - single fetch wrapper, throws on non-2xx with server's error message
- `toast(msg, type, durationMs)` - never use `alert()`
- `displayQty(qty_base, unitType)` + `displayCost(cost_per_base, qty_base, unitType)` - always use these together for stock quantities
- `_globalMarkupPct` - loaded from `/api/settings`, never read from DOM
- USB barcode scanner: global `keydown` listener, only active on Teller tab when no input is focused

### Product Types

| Type | Stock tracked by | Sold by | Cost |
|---|---|---|---|
| `simple` | `stock_qty` (integer) | unit | no COGS |
| `stock_item` | `stock_batches` (FIFO) | weight/volume or unit | FIFO COGS |
| `recipe` | ingredients' batches (FIFO) | unit | sum of ingredient COGS |

`sold_by_weight=True` on a `stock_item` - weighed at point of sale.
`is_for_sale=False` - internal ingredient, hidden at teller.
`is_prepared=True` - sends to kitchen queue on sale.

### Lady Coleen Website (`ladycoleen_web/`)

Separate Flask app serving `ladycoleen.co.za` via Cloudflare Tunnel. JWT auth (Bearer tokens in localStorage as `lc_token`).

**Key files:**
- `app.py` - factory, 6 blueprints: auth, cakes, admin, farmshop, invoices, policies
- `migrate.py` - idempotent migrations, runs on startup (same pattern as POS `strong_migrate`)
- `services/payfast.py` - PayFast form builder + ITN verification. `_signature(data, passphrase, skip_empty=True)` - set `skip_empty=False` for ITN verification (PayFast sends empty fields)
- `services/stock.py` - `check_and_deduct_order()` - deducts stock and creates POS sale records
- `services/customers.py` - `ensure_pos_customer()` - finds or creates a POS `customers` record for web orders

**PayFast flow:**
1. `POST /api/farmshop/payfast/initiate` - validates cart, stores `payment_sessions` row, returns form fields
2. Browser POSTs to `https://www.payfast.co.za/eng/process` (or sandbox)
3. PayFast POSTs ITN to `POST /api/farmshop/payfast/notify` - verifies signature, creates order + invoice + POS sale

**Critical:** `ensure_pos_customer` must run **before** `check_and_deduct_order` so `web_customers.pos_customer_id` is set when the POS sale is inserted. The sale's `customer_id` is looked up via `web_customers.pos_customer_id` - NOT the web customer ID directly.

**Shared with POS:**
- Product images served from `/product_images/` (shared Docker volume `~/farmpos-docker/data/product-images/`)
- POS `customers` table - web orders create/link POS customer records
- POS `invoices` table - online orders create paid invoices visible in POS

### Recognition System

`recognition_service_v2.py` (~4,700 lines) runs as a separate Docker container (`farmpos-recognition`). It:
- Polls Frigate NVR events (12 Tapo cameras at `http://10.0.0.101:8971`)
- Runs InsightFace ArcFace (512D embeddings) for face recognition
- Runs fast-plate-ocr for ANPR
- Posts identification events to `farmpos-app:5000`
- Maintains a customer cache and visit log

`recognition_service.py` in the root is the deprecated v1 - do not modify it.

### Kiosk Tablet Control

Free Kiosk tablets are managed from the Configuration tab. The POS server acts as a proxy to the Free Kiosk HTTP API (default port 2323, configurable). Three proxy routes:
- `GET /api/kiosk/status/<ip>` - polls `/api/status`, returns unwrapped `data`
- `POST /api/kiosk/query/<ip>` - body `{endpoint}` for read-only GET endpoints (battery, screen, sensors, etc.)
- `POST /api/kiosk/control/<ip>` - body `{action, ...payload}` for control commands
- `GET /api/kiosk/screenshot/<ip>` - streams PNG

### Windows Update Engine

The update engine (`farm_pos_installer/updater/`) runs as `FarmPOS-Updater` Windows service. Releases are created manually:
1. Copy changed files → `farm_pos_installer/releases/updates/vX.Y.Z/files/`
2. Calculate SHA-256 checksums (PowerShell `Get-FileHash`)
3. Create `manifest.json` + ZIP
4. Sign: `C:\Python314\python.exe scripts\sign_manifest.py manifest.json --key <key> --output manifest.json.sig`
5. Tag git: `git tag vX.Y.Z && git push origin vX.Y.Z`
6. Upload to GitHub release via API (no `gh` CLI installed - use `curl` + GitHub REST API with credential from `git credential fill`)

Private signing key: `farm_pos_installer\.signing_keys\PRIVATE_KEY.txt`

## Database Schema

```
users             - id, username, password_hash, role, active
products          - id, name, price, barcode, stock_qty, product_type,
                    unit_type, base_unit, sold_by_weight, is_for_sale, is_prepared,
                    price_per_unit, low_stock_threshold, package_size,
                    package_size_unit, package_unit, margin_pct, is_archived
purchases         - id, product_id, qty_added, purchase_price, date_time, user_id
settings          - id, key, value
sales             - id, sale_id, date_time, product_id, qty, unit_price,
                    user_id, voided, voided_by, voided_at, void_reason, sub_log
recipe_lines      - id, product_id, ingredient_id, qty_base
stock_batches     - id, product_id, qty_purchased_base, qty_remaining_base,
                    cost_per_base_unit (Numeric 10,6), purchased_at, user_id, supplier_id
stock_consumption - id, sale_id, ingredient_id, batch_id, qty_consumed_base,
                    cost_per_base_unit, consumed_at
stock_adjustments - id, product_id, adjustment_type, qty_change_base, system_qty_before,
                    cost_written_off, reason, adjusted_at, user_id
suppliers         - id, name, phone, email, website, notes
kitchen_orders    - id, sale_id, product_id, product_name, qty, ingredients (JSON),
                    status, sort_order, queued_at, completed_at, teller_id
user_sessions     - id, user_id, logged_in, logged_out, last_active
specials          - id, name, special_price, active
special_lines     - id, special_id, product_id, qty
invoices          - id, ..., status (draft/sent/paid)
customers         - id, name, ..., face embeddings, plate list
customer_visits   - id, customer_id, ..., detection method, timestamp
```

## API Quick Reference

```
POST /api/login                              { username, password }
POST /api/logout
GET  /api/me
GET  /api/products
POST /api/products                           admin only
POST /api/products/update                    admin only
POST /api/products/<id>/archive              admin only
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
POST /api/suppliers/<id>/purchase_run        admin only
GET  /api/stock/ingredients
POST /api/stock/receive                      admin only
POST /api/stock/writeoff                     admin only
POST /api/stock/adjust                       admin only
GET  /api/users                              admin only
POST /api/users                              admin only
GET  /api/kitchen/orders
POST /api/kitchen/orders/<id>/complete       admin only
GET  /api/specials
POST /api/specials                           admin only
GET  /api/customers
POST /api/customers/<id>/enroll/face         admin only
POST /api/customers/<id>/enroll/plate        admin only
GET  /api/customers/identify
GET  /api/till/active_customer
GET  /api/kiosk/tablets
POST /api/kiosk/control/<ip>                 { action, ...payload }
POST /api/kiosk/query/<ip>                   { endpoint }
GET  /api/kiosk/screenshot/<ip>
GET  /admin/export/products                  CSV
GET  /admin/export/transactions              ?start=&end= CSV
```
