# Farm Stall POS

Lightweight browser-based point-of-sale system for farm stalls, markets, and small shops.

**Stack:** Python Flask · SQLAlchemy · PostgreSQL · Vanilla JS · Bootstrap 5 · PWA

---

## Environments

| Environment | Script | URL | Database |
|---|---|---|---|
| QA (testing) | `start-qa.ps1` | `https://127.0.0.1:5000` | `farm_pos` |
| Production | `start-prod.ps1` | `https://localhost:5443` | `farm_pos_prod` |

```powershell
# Start QA
powershell -ExecutionPolicy Bypass -File start-qa.ps1

# Start Production
powershell -ExecutionPolicy Bypass -File start-prod.ps1

# Promote QA → Production
powershell -ExecutionPolicy Bypass -File promote.ps1
```

Default login: `admin` / `admin123`

QA shows a **yellow banner** at the top — you can never confuse the two environments.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | `.venv\` in project root |
| PostgreSQL | Installed at `C:\Users\<you>\PostgreSQL\pgsql\` — NOT a Windows service |
| Windows 11 | Start scripts are PowerShell only |

PostgreSQL is started manually by the start scripts via `pg_ctl`. It is not registered as a Windows service.

---

## First-Time Setup

1. Run `start-qa.ps1` — creates all tables and seeds the admin user automatically on first run.
2. Trust the self-signed HTTPS certificate (required once per machine for camera scanning):
   ```powershell
   # Run as Administrator
   powershell -ExecutionPolicy Bypass -File install-cert.ps1
   ```
3. Open `https://127.0.0.1:5000` in Edge or Chrome. No certificate warning after step 2.

---

## Installing Packages

The Capitec corporate SSL proxy requires trusted-host flags:

```powershell
.venv\Scripts\pip install <package> --trusted-host pypi.org --trusted-host files.pythonhosted.org
```

---

## Architecture

Single-file Flask backend + single-page frontend.

```
app.py                   Flask backend — all routes, ORM models, migrations
templates/index.html     SPA shell — tabs, modals, login form
static/main.js           All client-side logic (no framework)
start-qa.ps1             QA startup script
start-prod.ps1           Production startup script
promote.ps1              Promote QA → Production branch
install-cert.ps1         Trust self-signed cert in Windows (run once as admin)
cert.pem / cert.key      Self-signed TLS certificate (SAN: 127.0.0.1, 192.168.1.4)
cert.conf                OpenSSL config used to regenerate the cert
```

### Backend (`app.py`)

- All API routes are under `/api/`. Root `/` serves the SPA. `/admin/export/` serves CSV downloads.
- **Auth:** Flask session-based. `require_login()` / `require_role('admin')`. No JWT.
- **Session tracking:** `UserSession` records login/logout/last_active. Sessions auto-close after 10 min idle (for accurate time stats) and force-logout after 2 h total inactivity.
- **DB:** SQLAlchemy ORM. All money columns are `Numeric(10,2)` — never `Float`.
- **Migration:** `strong_migrate()` runs on every startup. Idempotent via `SAVEPOINT`/rollback per DDL. Add all schema changes there.
- **Concurrency:** All stock-mutating routes use `with_for_update=True` to prevent race conditions under concurrent users.
- **Sales model:** Each receipt is a group of `sales` rows sharing the same `sale_id` (UUID string). Voided sales have `voided=True`.

### Frontend (`static/main.js`)

- All UI state lives in the `STATE` object at the top.
- `api(path, opts)` — single fetch wrapper, throws on non-2xx.
- `toast(msg, type, durationMs)` — all notifications. Never `alert()`.
- `displayQty(qty_base, unitType)` — converts g/ml to kg/L with auto-threshold at 1000.
- `displayCost(cost_per_base, qty_base, unitType)` — scales cost to the same display unit.
- **USB barcode scanner:** global `keydown` listener buffers rapid keystrokes ending in Enter. Active only on Teller tab when no input is focused.
- **Camera scanner:** ZXing `BrowserMultiFormatReader`, toggled on-demand, 1.5 s cooldown. Requires HTTPS.

---

## User Roles

| Role | Access |
|---|---|
| `admin` | All tabs including Products, Users, Suppliers, Stats, CSV exports |
| `teller` | Teller tab + last 5 own transactions only. No COGS/margin visibility. |

---

## Product Types

| Type | Stock tracked by | Sold by | COGS |
|---|---|---|---|
| `simple` | `stock_qty` (integer) | unit | none |
| `stock_item` | `stock_batches` (FIFO) | weight/volume/unit | FIFO |
| `recipe` | ingredients' batches | unit (portions) | sum of ingredient COGS |

---

## Key Behaviours

- **Edit vs Void:** editing restores original stock then deducts new atomically. Voiding requires a reason and fully restores all stock.
- **Archive with stock:** when archiving a stock_item or simple product with remaining stock, you choose: **Keep** (stock stays, visible in Archived tab) or **Write off** (FIFO consumed, adjustment logged).
- **Transactions tab is role-aware:** tellers see last 5 only; admins get a full date-range picker.
- **Tellers never see COGS or margin** — these are admin-only in the transaction drilldown.
- **`sale_id` is a UUID string** — always display as `sale_id.slice(0, 8)`.

---

## Regenerating the TLS Certificate

If the cert expires or you change the server's IP address:

```powershell
# Regenerate (updates cert.pem and cert.key)
openssl req -x509 -newkey rsa:2048 -keyout cert.key -out cert.pem -days 3650 -nodes -config cert.conf

# Re-trust in Windows (as Administrator)
powershell -ExecutionPolicy Bypass -File install-cert.ps1
```

Then restart the app and restart Edge.

---

## PWA / Kiosk Mode

Install as a PWA via Edge or Chrome "Add to Home Screen" / "Install App".

For dedicated kiosk mode on Windows:
```
msedge.exe --kiosk --app=https://127.0.0.1:5000
```

---

## Promoting to Production

```powershell
powershell -ExecutionPolicy Bypass -File promote.ps1
```

This merges `main` → `production` branch on GitHub and pushes both. Type `yes` to confirm.
