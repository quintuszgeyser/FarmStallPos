# Changelog

All notable changes to Farm POS are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) — versions track `APP_VERSION` in `app.py`.

---

## [Unreleased]

### Added
- **Returns workflow (ISSUE-32)** — `POST /api/transactions/<id>/return` accepts partial
  or full item returns after a session ends. Stock is restored to FIFO batches at the
  original cost. An immutable `audit_log` entry is written before any mutation.
  Admin-only `↩ Return` button on each transaction card opens the return modal.
- **End-of-day cash-up / Z-report (ISSUE-33)** — `TillSession` model, migration, and
  blueprint at `/api/till/sessions`. `POST` closes the till (records opening float,
  counted cash, computes over/under vs POS cash sales). `GET /summary` powers the
  Close Till modal. `🧾 Close Till` button added to the Transactions tab header.
- **Opening stock import (ISSUE-34)** — `POST /api/stock/opening-import?mode=preview|import`
  seeds `stock_batches` for `stock_item` products from a CSV (columns: `product_code`,
  `qty`, `unit`, `unit_cost`, `received_date`). Preview shows what will be created before
  committing. `⬆ Opening Stock` button on the Products tab. Template download at
  `GET /api/stock/opening-import-template`.
- **CI upgrade-path smoke test (ISSUE-46)** — new `upgrade-smoke` job in `release.yml`.
  Boots the previous released tag against a fresh Postgres DB, then hot-swaps the new
  image on the same populated DB and asserts `/health` + key columns
  (`sales.payment_method`, `sales.cash_tendered`) exist. Catches migration regressions
  that only surface on real store upgrades (not fresh installs).

---

## [2.1.6] — 2026-07-02

### Fixed
- Product images 404 on online shop — repointed appliance compose mount to
  `/app/static/product_images` (ISSUE-47)
- `update.sh` only updated the POS container; now health-gates the web container too
  when `web_shop.enabled=true` (ISSUE-48)
- `store.example.yml` and `register-store.sh` defaulted to pre-fix image tags; bumped
  to v2.1.6 (ISSUE-49 🔴)

---

## [2.1.5] — 2026-07-02

### Fixed
- Recognition cards and Monitor tab shown on appliance (POS-only) boxes — now hidden
  when `STORE_ID` is set; Configuration tab still visible for Branding/Kiosk (ISSUE-43)

---

## [2.1.4] — 2026-07-02

### Fixed
- Web container had no GHCR image and wrong DB env vars (`DATABASE_URL` vs
  `POSTGRES_HOST/USER/PW/DB`) — added `build-web` CI job and correct env (ISSUE-44)
- Uploaded logo didn't show on the online shop — added read-only bind mount for
  `./data/branding` into web container (ISSUE-45)

---

## [2.1.3] — 2026-07-02

### Fixed
- `/api/till/active_customer` 500 on appliance boxes — `till_detections` table absent
  on POS-only boxes; wrapped in try/except, returns `{customer_id: null}` (ISSUE-42)

---

## [2.1.2] — 2026-07-02

### Fixed
- `strong_migrate()` silently skipped `ALTER TABLE` DDL on upgrade — `pg_try()` used a
  hardcoded `SAVEPOINT sp`; concurrent gunicorn workers clobbered each other's savepoint.
  Fixed with per-call unique savepoint names (`sp_1`, `sp_2`, …). Also hardens the Lady
  Coleen web container. (ISSUE-41 🔴)

---

## [2.1.1] — 2026-07-02

### Fixed
- `backup.sh` size guard aborted first backup on a fresh store (4 products ≈ 7.5 KB <
  10 KB floor) — lowered guard to 2 KB (ISSUE-37)
- `restore.sh` only read first 4 KB — `CREATE TABLE|COPY|INSERT` check never matched
  pg_dump's comment header; expanded to 64 KB + added `PostgreSQL database dump` marker
  (ISSUE-38)
- `backup.sh` retention glob crashed on `.age`-only stores — replaced `ls *.sql.gz`
  with `find` (ISSUE-39)
- `register-store.sh` cron install failed non-interactively — grep matched full path;
  fixed to grep on `backup.sh` alone (ISSUE-40)

---

## [2.1.0] — 2026-07-02

### Added
- White-label branding (ISSUE-35) — DB-backed runtime branding (logo, colours, fonts,
  invoice footer) editable from the Configuration tab. Branding applies to both POS and
  web shop. `branding_bg` key for POS background colour.
- Gap remediation Phase 1.1 — `payment_method` (cash/card/split/qr) + `cash_tendered`
  on `Sale` rows; cash/card toggle in teller checkout; payment split in stats and CSV
  export (ISSUE-29)
- Gap remediation Phase 1.2 — append-only `audit_log` table for voids and edits; edit
  endpoint marks originals as `voided='superseded by edit'` instead of DELETE-ing them
  (SARS s29 compliance) (ISSUE-31)
- Gap remediation Phase 2.1–2.3 — `.manifest` row-count file written by `backup.sh`;
  `restore.sh` aborts if key tables are >20% below manifest after restore; disk guard
  (warn ≥80%, abort ≥95%); backup health banner in POS admin (ISSUE-30)
- Appliance first-drill: golden-image onboarding tested end-to-end on bare Ubuntu 24.04;
  13 bugs found and fixed (v2.1.1–v2.1.6) (ISSUE-37–49)
- QA logo mount fixed — `qa-web` repointed to `./data/qa-branding` (ISSUE-36)

---

## [1.6.0] — 2026-06-30

### Added
- BC-4000 scale PLU write confirmed end-to-end — three bugs fixed: wrong TCP frame
  structure, raw CR byte vs literal `\x0d` separator, SLP-V PLU exclusion range
  (ISSUE-25)
- Barcode scanner HID mode support — global `keydown` listener, `#barcode-trap` for
  reliable Teller tab scanning; BLE path removed
- Teller Scale tab — read-only PLU/price/tare/shelf-life view for teller role
- BC-4000 peel sensor workaround documented (Setup Menu B01 → step 08 → Disable)

### Fixed
- Supplier documents not persisted across restarts — added `./data/supplier-docs` volume
  mount (ISSUE-24)

---

## [1.5.0] — 2026-06-26

### Added
- Deploy hardening — artifact promotion (QA image promoted byte-identical to prod), pre-
  deploy backup, parity gate, rollback button (ISSUE-17/18)
- QA/PROD dual-environment — separate DBs (`farm_pos_qa` / `farm_pos_prod`), env files,
  3-layer QA scale block, yellow QA banner, `/api/env` endpoint (ISSUE-15/16)
- CSV bulk product import — `POST /api/products/import` (preview/import/strict),
  versioned template download, field-level diff preview, `product_import_runs` audit log
- Scheduled deployments — cron polls QA at `/api/deploy-schedule/poll` every minute;
  lockfile-protected; supports scheduled and immediate deploy from Deploy tab
- Face recognition fresh-start wipe (2026-06-26) — all embeddings cleared; re-enrol as
  customers visit

### Fixed
- Online orders silently lost after PayFast payment — `ensure_pos_customer()` raw INSERT
  omitted NOT NULL columns causing rollback of in-flight orders (ISSUE-23)
- PayFast signature mismatch — prod was posting live creds to sandbox URL; `jsonify(dict)`
  sorted fields alphabetically breaking signature order (ISSUE-21)
- Customer permanent-delete 500 — missing FK cleanup before DELETE (ISSUE-20)
- Deploy stuck in "running" — scheduler race between Flask thread and host cron (ISSUE-19)
- Prod schema drift — 8 missing tables + columns; `pg_try()` SAVEPOINT isolation (ISSUE-16)

---

## [1.4.0] — 2026-06-24

### Fixed
- Product add 500 — `::int` cast in `_assign_product_code()` broken by SQLAlchemy named
  param parser; fixed to `CAST(:lo AS int)` (ISSUE-14)
- `qa.ladycoleen.co.za` routing to prod — nginx `map $http_host $site_backend` +
  `resolver 127.0.0.11` (ISSUE-15)

---

## [1.3.0] — 2026-06-23

### Added
- 8 GB swap file (`/swap8.img`) replacing exhausted `/swap.img` (ISSUE-01)
- Daily `backup.sh` cron at 02:00, 14-day retention (ISSUE-02)
- Scale sync rewritten — outbound poll loop, correct MsgNo 1001 protocol (ISSUE-03)
- mt7902e WiFi driver: blacklisted conflicting in-kernel mt76 modules, added to
  `modules-load.d`, rebuilt initramfs for boot persistence (ISSUE-13)
