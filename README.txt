# Farm Stall POS

Lightweight browser-based point-of-sale system for farm stalls, markets, and small shops.

Stack: Python Flask · SQLAlchemy · PostgreSQL · Vanilla JS · Bootstrap 5 · PWA

---

QUICK START

Run the app with the start script (starts PostgreSQL, activates venv, sets env vars, launches Flask):

  powershell -ExecutionPolicy Bypass -File start.ps1

App runs at: http://127.0.0.1:5000

Default login: admin / admin123

On the local network, also accessible at http://<your-IP>:5000 from tablets/phones on the same Wi-Fi.

---

PREREQUISITES

  Python 3.11+     .venv\ in project root
  PostgreSQL       Installed at C:\Users\<you>\PostgreSQL\pgsql\ — NOT a Windows service
  Windows 11       start.ps1 is PowerShell only

PostgreSQL is started manually by start.ps1 via pg_ctl. It is not registered as a Windows service.

---

WHAT start.ps1 DOES

  1. Checks if PostgreSQL is running via pg_ctl status -D %USERPROFILE%\PostgreSQL\data
  2. Starts it if not running (log at %USERPROFILE%\PostgreSQL\pg.log)
  3. Activates .venv\Scripts\Activate.ps1
  4. Sets environment variables:
       SECRET_KEY   = local-dev-secret
       DATABASE_URL = postgresql://frauduser:Fraud@localhost:5432/farm_pos
       LOCAL_TZ     = Africa/Johannesburg
       ADMIN_USER   = admin
       ADMIN_PASS   = admin123
  5. Runs python app.py

---

INSTALLING PACKAGES

The Capitec corporate SSL proxy requires trusted-host flags:

  .venv\Scripts\pip install <package> --trusted-host pypi.org --trusted-host files.pythonhosted.org

---

ARCHITECTURE

Single-file Flask backend + single-page frontend.

  app.py                   Flask backend — all routes, ORM models, migrations
  templates/index.html     SPA shell — tabs, login form
  static/main.js           All client-side logic (no framework)
  static/sw.js             Service worker — versioned asset cache
  static/manifest.json     PWA manifest
  start.ps1                Dev startup script

Backend (app.py)
  - All API routes are under /api/. Root / serves the SPA. /admin/export/ serves CSV downloads.
  - Auth: Flask session-based. require_login() / require_role('admin') decorators. No JWT.
  - DB: SQLAlchemy ORM. All money columns are Numeric(10,2) — never Float.
  - Migration: strong_migrate() runs on every startup. Idempotent — uses SAVEPOINT/rollback per
    DDL statement. Add all schema changes there, not via Alembic.
  - Sales model: Each receipt is a group of sales rows sharing the same sale_id (UUID string).
    Voided sales have voided=True and are excluded from all queries.

Frontend (static/main.js)
  - All UI state lives in the STATE object at the top of the file.
  - api(path, opts) is the single fetch wrapper — throws on non-2xx with the server's error message.
  - toast(msg, type, durationMs) is used for all user notifications — never alert().
  - USB barcode scanner: global keydown listener buffers rapid keystrokes ending in Enter
    (scanner wedge mode). Active only on the Teller tab when no input is focused.
  - Camera scanner: ZXing BrowserMultiFormatReader, toggled on-demand, 1.5s cooldown.

---

USER ROLES

  admin    Teller, Transactions (full date range), Products, Users, Settings, CSV Exports
  teller   Teller tab + last 5 transactions only

On first run, if no users exist, the system creates an admin from ADMIN_USER / ADMIN_PASS env vars.

---

DATABASE SCHEMA

  users           — id, username, password_hash, role, active
  products        — id, name, price (Numeric), barcode, stock_qty
  purchases       — id, product_id, qty_added, purchase_price (Numeric), date_time, user_id
  settings        — id, key, value
  sales           — id, sale_id (UUID), date_time, product_id, qty, unit_price (Numeric),
                    user_id, voided, voided_by, voided_at, void_reason

Upcoming (recipe/FIFO system in progress):
  recipe_lines      — id, product_id, ingredient_id, qty_base (Numeric)
  stock_batches     — id, product_id, qty_purchased_base, qty_remaining_base,
                      cost_per_base_unit (Numeric 10,6), purchased_at, user_id
  stock_consumption — id, sale_id, ingredient_id, batch_id, qty_consumed,
                      cost_per_unit, consumed_at

---

API REFERENCE

  POST   /api/login                           { username, password }
  POST   /api/logout
  GET    /api/me

  GET    /api/products
  POST   /api/products                        admin — create product
  POST   /api/products/update                 admin — edit product
  DELETE /api/products/<name>                 admin
  GET    /api/products/<id>/suggested_price   ?markup=

  POST   /api/purchases                       admin — receive stock

  GET    /api/transactions                    admin: ?start=&end= date range / teller: last 5
  POST   /api/transactions                    { cart: [{product_id, qty, unit_price}] }
  POST   /api/transactions/<sale_id>/void     admin — { reason }
  POST   /api/transactions/<sale_id>/edit     admin — { lines: [{product_id, qty, unit_price}] }

  GET    /api/stats/today                     admin
  GET    /api/settings                        admin
  POST   /api/settings                        admin

  GET    /admin/export/products               CSV
  GET    /admin/export/transactions           ?start=&end= CSV

---

KEY BEHAVIOURS

  - Edit vs Void: editing a sale restores original stock then deducts new stock atomically.
    Voiding requires a reason and fully restores stock.
  - sale_id is a UUID string — always display as sale_id.slice(0, 8) in the UI.
  - Stock is integer for simple products (stock_qty). Will be Numeric for ingredient products
    once the recipe/FIFO system lands.
  - Transactions tab is role-aware: tellers see last 5 only; admins get a full date-range
    picker defaulting to today.

---

PWA / KIOSK MODE

The app can be installed as a PWA (Add to Home Screen on Android/iOS, or Install in Chrome on Windows).

For dedicated kiosk use on Windows + Chrome:
  chrome.exe --kiosk --app=http://127.0.0.1:5000

Service worker caches static assets under pos-cache-vX. Bump the version in static/sw.js when
deploying changes to main.js or other static files.

---

ROADMAP

  - Recipe-based products (ingredients with FIFO cost tracking)
  - Variable-weight products
  - Thermal/PDF receipt printing
  - End-of-day Z-reports
  - PIN login + auto-logout timer
  - A4/label barcode printing
