# Farm Stall POS

Browser-based point-of-sale system for Lady Coleen's farm stall.

**Stack:** Python Flask · SQLAlchemy · PostgreSQL · Vanilla JS · Bootstrap 5 · PWA

---

## Production Deployment (Ubuntu Docker)

Production runs on an Ubuntu 24.04 server via Docker Compose.

```bash
# Deploy latest code (run after every push to main)
ssh -J root@172.20.10.1 quintusz@100.72.83.107 \
  'cd ~/farmpos-docker && bash deploy.sh pos'

# Verify deploy
ssh -J root@172.20.10.1 quintusz@100.72.83.107 \
  'sleep 25 && docker exec farmpos-app python scripts/smoke_test.py http://localhost:5000'
```

The Docker image clones the latest `main` branch from GitHub on every build - push first, then deploy.

---

## Local Development (Windows)

| Environment | Script | URL | Database |
|---|---|---|---|
| QA (testing) | `start-qa.ps1` | `http://localhost:5000` | `farm_pos` |
| Production | `start-prod.ps1` | `https://localhost:5443` | `farm_pos_prod` |

```powershell
# Start QA
powershell -ExecutionPolicy Bypass -File start-qa.ps1

# Start Production
powershell -ExecutionPolicy Bypass -File start-prod.ps1

# Promote main → production branch on GitHub
powershell -ExecutionPolicy Bypass -File promote.ps1
```

Default login: `admin` / `admin123`

QA shows a **yellow banner** - you can never confuse the two environments.

### Prerequisites

| Requirement | Notes |
|---|---|
| Python | Use `C:\Python314\python.exe` directly - the `.venv` stub is broken |
| PostgreSQL | Installed at `C:\Users\<you>\PostgreSQL\pgsql\` - started by the scripts, not a Windows service |
| Windows 11 | Start scripts are PowerShell only |

### Installing packages (Capitec corporate proxy)

```powershell
C:\Python314\python.exe -m pip install <package> --trusted-host pypi.org --trusted-host files.pythonhosted.org
```

### First-time local setup

1. Run `start-qa.ps1` - creates all tables and seeds the admin user on first run.
2. Trust the self-signed cert once (required for camera barcode scanning on HTTPS):
   ```powershell
   # Run as Administrator
   powershell -ExecutionPolicy Bypass -File scripts\install-cert.ps1
   ```

---

## Architecture

Blueprint-based Flask backend + single-page frontend.

```
app.py                   Factory: create_app(), strong_migrate(), _register_routes()
models.py                21 SQLAlchemy models
helpers.py               Shared utilities (FIFO, auth, date parsing, etc.)
blueprints/              15 Blueprint files - all 131 API routes
  auth.py                /api/login|logout|me, /api/users/*
  products.py            /api/products/*
  stock.py               /api/stock/*, /api/purchases
  suppliers.py           /api/suppliers/*
  customers.py           /api/customers/*, /api/till/*
  transactions.py        /api/transactions/*
  kitchen.py             /api/kitchen/*
  specials.py            /api/specials/*
  invoices.py            /api/invoices/*
  stats.py               /api/stats/*, /admin/export/*
  settings.py            /api/settings
  recognition.py         /api/recognition/*
  kiosk.py               /api/kiosk/*
  system.py              /api/system/update-*
  core.py                /, /health, /guide, /__version, /api/logs
templates/index.html     SPA shell - tabs, modals, login form
static/main.js           All client-side logic - 8,443 lines, no framework
static/main.css          Utility CSS classes
```

### Backend patterns

- **Import order:** `blueprints → helpers → models → db` - never import from `app.py` in a blueprint
- **Auth:** Flask session-based. `require_login()` / `require_role('admin')`. No JWT.
- **Money:** All columns are `Numeric(10,2)` - never `Float`.
- **Migrations:** `strong_migrate()` runs on every startup - idempotent DDL via `SAVEPOINT`/rollback. Add all schema changes there. Never use Alembic.
- **Concurrency:** All stock-mutating routes use `with_for_update=True`.
- **Sales:** Each receipt is a group of `sales` rows sharing one `sale_id` (UUID). Always display as `sale_id.slice(0, 8)`.
- **Sessions:** Auto-close after 10 min idle (time tracking); hard logout after 2 h inactivity.

### Frontend (`static/main.js`)

- All state in `STATE` object at top.
- `api(path, opts)` - single fetch wrapper, throws on non-2xx.
- `toast(msg, type, ms)` - all notifications. Never `alert()`.
- `displayQty(qty_base, unitType)` / `displayCost(cost_per_base, qty_base, unitType)` - always use these together for stock quantities.
- **USB barcode scanner:** global `keydown` listener, active on Teller tab when no input focused.
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
| `stock_item` | `stock_batches` (FIFO) | weight / volume / unit | FIFO |
| `recipe` | ingredients' batches | unit (portions) | sum of ingredient COGS |

- `sold_by_weight=true` - teller enters qty at till (biltong, cheese, etc.)
- `is_for_sale=false` - internal ingredient only, hidden at teller
- `is_prepared=true` - sends to kitchen queue on checkout

---

## Key Behaviours

- **Edit vs Void:** editing restores original stock then deducts the new cart atomically. Voiding requires a reason and fully restores all stock.
- **Archive with stock:** when archiving a product with remaining stock, choose **Keep** or **Write off** (FIFO consumed, adjustment logged).
- **Role-aware transactions:** tellers see last 5 only; admins get a full date-range picker. Tellers never see COGS or margin.
- **Kiosk tablets:** two Android tablets run Free Kiosk and are managed from the Configuration tab (`/api/kiosk/*` proxy to Free Kiosk REST API).

---

## Safety Net

After every deploy, run the smoke test:

```bash
ssh -J root@172.20.10.1 quintusz@100.72.83.107 \
  'docker exec farmpos-app python scripts/smoke_test.py http://localhost:5000'
```

For schema verification:

```bash
ssh -J root@172.20.10.1 quintusz@100.72.83.107 \
  'docker exec farmpos-app python scripts/db_integrity.py'
```

---

## TLS Certificate (local dev only)

```powershell
# Regenerate cert.pem and cert.key
openssl req -x509 -newkey rsa:2048 -keyout cert.key -out cert.pem -days 3650 -nodes -config cert.conf

# Trust in Windows (as Administrator)
powershell -ExecutionPolicy Bypass -File scripts\install-cert.ps1
```

---

## PWA / Kiosk Mode

Install via Edge or Chrome "Install App". For dedicated kiosk mode on Windows:

```
msedge.exe --kiosk --app=https://127.0.0.1:5000
```
