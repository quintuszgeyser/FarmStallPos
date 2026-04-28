# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the App

Always use `start.ps1` — it starts PostgreSQL, activates the venv, sets all env vars, and runs `python app.py`:

```powershell
powershell -ExecutionPolicy Bypass -File start.ps1
```

App runs at `http://127.0.0.1:5000`. Default login: `admin` / `admin123`.

To install new packages add `--trusted-host pypi.org --trusted-host files.pythonhosted.org` (Capitec corporate SSL proxy):

```powershell
.venv\Scripts\pip install <package> --trusted-host pypi.org --trusted-host files.pythonhosted.org
```

## Environment

- **Database:** PostgreSQL at `postgresql://farmstall:FarmStall@localhost:5432/farm_pos`
- **PostgreSQL location:** `C:\Users\CP368103\PostgreSQL\pgsql\` — not a Windows service, started manually by `start.ps1`
- **Python venv:** `.venv\` in project root
- **Platform:** Windows 11, shell is bash (use Unix syntax in tool calls)

## Architecture

Single-file Flask backend (`app.py`) + single-page frontend (`templates/index.html` + `static/main.js`).

### Backend (`app.py`)

- All routes are REST JSON API under `/api/` except the root `/` which serves the SPA and `/admin/export/` for CSV downloads
- **Auth:** Flask session-based. `require_login()` checks session, `require_role('admin')` checks role. No JWT.
- **DB:** SQLAlchemy ORM. All money columns are `Numeric(10,2)` — never `Float`. Decimal→float conversion handled by custom `_JSONProvider` at the top of the file.
- **Migration:** `strong_migrate()` runs on every startup. It is idempotent — uses `SAVEPOINT`/rollback per DDL statement for PostgreSQL so failures don't abort the transaction. Add all schema changes here, never rely on Alembic.
- **`Sale` model** is the single source of truth for transactions. Each sale receipt is a group of rows sharing the same `sale_id` (UUID string). Voided sales have `voided=True` and are excluded from all queries.
- **`Purchase` model** is the legacy stock-receiving mechanism (replaced by `stock_batches` in the recipe/FIFO system being built).

### Frontend (`static/main.js`)

- Vanilla JS, no framework. All state lives in the `STATE` object at the top.
- `api(path, opts)` is the single fetch wrapper — throws on non-2xx with the server's error message.
- `toast(msg, type, durationMs)` replaces all `alert()` calls — never use `alert()`.
- **USB barcode scanner support:** global `keydown` listener buffers rapid keystrokes ending in Enter (scanner wedge behaviour). Only active on the Teller tab when no input is focused.
- **Camera scanner:** ZXing `BrowserMultiFormatReader`, on-demand only (button toggle), 1.5s cooldown.

### Key Design Decisions

- **Transactions tab is role-aware:** tellers see last 5 only; admins default to today with a date-range picker.
- **Edit vs Void:** editing a transaction restores original stock then deducts new stock atomically. Voiding requires a reason and fully restores stock.
- **Stock is always integer for simple products (`stock_qty`)** but will be `Numeric` for ingredient products once the recipe/FIFO system is added.
- **`sale_id` is a UUID string**, not an integer — always slice to 8 chars for display (`String(id).slice(0,8)`).
- Products have a `product_type` column (being added): `simple | recipe | variable_weight | ingredient`.

## Database Schema (current)

```
users           — id, username, password_hash, role, active
products        — id, name, price (Numeric), barcode, stock_qty
purchases       — id, product_id, qty_added, purchase_price (Numeric), date_time, user_id
settings        — id, key, value
sales           — id, sale_id, date_time, product_id, qty, unit_price (Numeric),
                  user_id, voided, voided_by, voided_at, void_reason
```

Upcoming tables (recipe/FIFO system):
```
recipe_lines    — id, product_id, ingredient_id, qty_base (Numeric)
stock_batches   — id, product_id, qty_purchased_base, qty_remaining_base,
                  cost_per_base_unit (Numeric 10,6), purchased_at, user_id
stock_consumption — id, sale_id, ingredient_id, batch_id, qty_consumed,
                    cost_per_unit, consumed_at
```

## API Quick Reference

```
POST /api/login                          { username, password }
POST /api/logout
GET  /api/me
GET  /api/products
POST /api/products                       admin only
POST /api/products/update                admin only
DELETE /api/products/<name>              admin only
GET  /api/products/<id>/suggested_price  ?markup=
POST /api/purchases                      admin only
GET  /api/transactions                   ?start=&end= (admin date range), tellers get last 5
POST /api/transactions                   { cart: [{product_id, qty, unit_price}] }
POST /api/transactions/<sale_id>/void    { reason }  admin only
POST /api/transactions/<sale_id>/edit    { lines: [{product_id, qty, unit_price}] }  admin only
GET  /api/stats/today                    admin only
GET  /api/settings                       admin only
POST /api/settings                       admin only
GET  /admin/export/products              CSV
GET  /admin/export/transactions          ?start=&end= CSV
```
