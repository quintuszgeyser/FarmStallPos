🐄 Farm Stall POS
Lightweight Browser‑Based POS for Farm Stalls, Markets & Small Shops
PWA • Barcode Scanner • Products • Users & Roles • Transactions • CSV Export

📋 Table of Contents

#overview
#features
#technology-stack
#user-roles--authentication
#product-management
#barcode-scanning
#sales--transactions
#api-reference
#database-structure
#pwa--fullscreen-mode
#environment-variables
#role-based-ui-logic
#deployment-checklist
#service-worker-notes
#future-features--roadmap
#files-of-interest


1) Overview
Farm Stall POS is a lightweight, fully browser‑based point‑of‑sale system designed for:

farm stalls
market stands
small retail shops
kiosk devices and tablets

It runs entirely in the browser (no native app required) and can be installed as a PWA for fullscreen operation on mobile or desktop.
Key capabilities include:

Barcode scanning (camera-based)
Product & user management (admin only)
Teller checkout screen
Transaction history
CSV exports
Offline asset caching (via service worker)


2) Features
✔ Simple POS for touch devices
✔ Barcode scanning with ZXing
✔ Users & roles (admin/teller)
✔ Products with unique EAN‑13 barcodes
✔ Full transaction history
✔ CSV exports (products, transactions, lines)
✔ Works as fullscreen PWA
✔ Designed for low-inventory environments

3) Technology Stack
Backend

Python Flask
SQLAlchemy ORM
Flask‑SQLAlchemy

Database

PostgreSQL (DATABASE_URL)

Frontend

HTML + Bootstrap 5
Vanilla JavaScript
ZXing UMD build (camera barcode scanning)

PWA

manifest.json
sw.js (versioned cache)

Deployment

Render Web Service + Render PostgreSQL


4) User Roles & Authentication
Session-based authentication using hashed passwords (Werkzeug).
Roles

















RoleAccessadminFull access: Teller, Transactions, Manage Products, Users, CSV ExportstellerTeller + Transactions only
First-Run Auto Admin
If no users exist, the system creates a first admin from env vars:
ADMIN_USER
ADMIN_PASS


5) Product Management
Each product includes:

name (unique)
price
barcode (unique EAN‑13)
Optional auto-generated EAN‑13 with checksum

Example API response:
JSON{  "Apples": { "id": 3, "price": 12.5, "barcode": "2004428073279" },  "Milk":   { "id": 4, "price": 18.0, "barcode": "2004428073286" }}Show more lines
Admins can create, update, and delete products.

6) Barcode Scanning
Scanner uses ZXing BrowserMultiFormatReader.
Supports:

EAN‑13
Multiple formats via ZXing

UX Enhancements:

Beep on successful scan
700 ms cooldown to prevent duplicates
Green visual flash around video feed

Matches scanned code against:

name
product ID
barcode


7) Sales & Transactions
Process

Add items to cart (qty automatically increments)
Checkout creates:

Transaction entry
TransactionLine rows



Transactions View

Grouped by transaction ID
Shows date/time, totals, line items


8) API Reference
Authentication
POST /api/login
POST /api/logout
GET  /api/me

Users (admin only)
GET    /api/users
POST   /api/users
POST   /api/users/update
DELETE /api/users/<username>

Products (admin only)
GET    /api/products
POST   /api/products
POST   /api/products/update
DELETE /api/products/<name>

Transactions
GET /api/transactions
POST /api/transactions

CSV Exports (admin)
/admin/export/products
/admin/export/transactions
/admin/export/transaction_lines

Diagnostics
/api/db-health
/__version


9) Database Structure
users

id
username
password_hash
role
active

products

id
name
price
barcode

transactions

id
date_time

transaction_lines

id
transaction_id
product_id
qty
unit_price


10) PWA & Fullscreen Mode
PWA Features

Installable on Android / iOS / Windows
Fullscreen (display: fullscreen)
Offline caching for assets

Kiosk Mode Examples
Windows + Chrome
chrome.exe --kiosk --app=https://your-app-url


11) Environment Variables



































VariableRequiredPurposeDATABASE_URL✔Postgres connection stringSECRET_KEY✔Flask session signingADMIN_USERoptionalFirst admin usernameADMIN_PASSoptionalFirst admin passwordADMIN_TOKENoptionalSecures CSV exports

12) Role-Based UI Logic
Before Login

Only login card visible
Tabs + main UI hidden

Teller View

Visible: Teller, Transactions
Hidden: Products, Users, Exports

Admin View

All tabs visible
Users admin panel enabled
Products + exports available

Frontend JavaScript adjusts visibility based on /api/me.

13) Deployment Checklist
🟩 Step 1 — Configure Environment Variables
Set at minimum:

DATABASE_URL
SECRET_KEY

Optional:

ADMIN_USER + ADMIN_PASS
ADMIN_TOKEN


🟩 Step 2 — Deploy to Render
Render detects Flask → deploys with:
gunicorn app:app


🟩 Step 3 — First Login

Log in as seeded admin
Change password
Create teller users


🟩 Step 4 — Add Products

Add products
Generate barcodes
Export CSV for labels


🟩 Step 5 — Test Scanner
On a phone:

Add to Home Screen
Allow camera
Test scanning


🟩 Step 6 — Confirm PWA & Cache
If UI stale → bump SW version.

14) Service Worker Notes

Cache name is versioned: pos-cache-vX
Bump version when changing:

main.js
any static assets


Avoid caching "/" in development
Hard refresh + SW unregister when debugging


15) Future Features & Roadmap
Authentication

PIN login
Auto-logout timer
Multi-teller fast switching

Sales & Reporting

Printable receipts (thermal/PDF)
Transaction filtering
End-of-day Z-reports

Inventory

Stock tracking
Product categories
Bulk CSV import/export
A4/label barcode printing

UI

Dark mode
Customizable layout
Large touch-friendly buttons

Offline & Multi-Store

Full offline db with sync
Multi-branch support


16) Files of Interest
app.py                # Flask app, routes, ORM, RBAC, exports, barcode generation
templates/index.html  # App UI skeleton, tabs, login
static/main.js        # Client logic: auth, users, products, transactions, scanning
static/manifest.json  # PWA metadata
static/sw.js          # Service worker, cache versioning