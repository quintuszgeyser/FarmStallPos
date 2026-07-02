# Farm POS Appliance — new-store onboarding

Turns a bare mini-PC into a trading POS. **POS-only** (no recognition/Frigate/web shop).
Local-first: the box owns its Postgres and trades fully offline.

> This does **not** touch the original Lady Coleen box. LC builds from its own
> server-side `~/farmpos-docker/pos/Dockerfile` and has no `STORE_ID` set, so it keeps
> its exact current branding, scale IP, and admin login. The appliance path only
> activates when `STORE_ID` is present (which only `register-store.sh` sets).

## What makes a box "an appliance"
`STORE_ID` in its `.env`. That single switch flips the app into strict mode:
generated `SECRET_KEY`/`ADMIN_PASS` are **required** (boot fails loudly otherwise),
branding comes from `store.yml`, and the scale IP is the store's own (or none).

## One-time prerequisites on the box
```bash
# Ubuntu 24.04 + Docker Engine + compose plugin + age + rclone (+ openssl, python3)
sudo apt-get install -y age rclone
# GHCR login so the pinned image can be pulled (private repo):
echo "$GHCR_PAT" | docker login ghcr.io -u quintuszgeyser --password-stdin
```

## Onboard a store
```bash
sudo mkdir -p /opt/farmpos && cd /opt/farmpos
# put the appliance/ folder here (git clone or copy), then:
sudo ./appliance/register-store.sh
```
Answer: Store ID, display name, scale IP (blank = none), image tag. The script:
1. writes `store.yml`  2. generates secrets in `/opt/farmpos/secrets/`
3. renders `.env` + `docker-compose.yml`  4. pulls the pinned image + starts
5. health-gates  6. prints the POS URL + generated admin password.

**Zero-touch variant:** pre-seed `/opt/farmpos/store.yml` (from `store.example.yml`)
before first boot and run the script non-interactively.

## Before you leave site (the honest checklist)
- [ ] Scale: set a **router DHCP reservation** for the scale MAC → the `scale.ip` in `store.yml` (~15–30 min; the real reason a visit runs long).
- [ ] Change the admin password shown at the end (or force it on first login).
- [ ] Confirm off-box backup: set `backup.target` (rclone remote) + generate the age key (below), then run `./appliance/backup.sh` once and verify the push.
- [ ] Load the product catalog (CSV import in the POS).

## Backups (per-store encryption)
```bash
# once per box — PUBLIC key encrypts on the box; PRIVATE key is escrowed OFF the box.
age-keygen -o /opt/farmpos/secrets/backup_age.key
grep 'public key:' /opt/farmpos/secrets/backup_age.key | awk '{print $NF}' \
  > /opt/farmpos/secrets/backup_age.pub
# ⚠️ copy backup_age.key to TWO offline locations. Lose it = backups unrecoverable.
```
Cron: `0 2 * * * /opt/farmpos/appliance/backup.sh >> /opt/farmpos/data/backup.log 2>&1`

## Hardware replacement / DR (<15 min if a spare is pre-flashed)
```bash
sudo ./appliance/register-store.sh --restore   # brings the stack up
sudo ./appliance/restore.sh                     # newest local, or pass a pulled .age
# (restore backup_age.key from escrow first if backups are encrypted)
```

## Updating a box
Bump `farmpos_version` in `store.yml`, then:
```bash
cd /opt/farmpos && docker compose pull && docker compose up -d
```
Never use `:latest` in production — always a pinned `vX.Y.Z` that passed CI.

## Files
| File | Purpose |
|---|---|
| `store.example.yml` | template for per-store identity/flags |
| `env.template` | rendered → `/opt/farmpos/.env` (secrets injected) |
| `compose.template.yml` | the POS-only stack (postgres + pos, pinned image) |
| `postgres-init/01-create-db.sh` | one-time DB creation on empty data dir |
| `register-store.sh` | the installer |
| `backup.sh` / `restore.sh` | age-encrypted backup + restore |
| `lib/common.sh` | shared shell helpers |
