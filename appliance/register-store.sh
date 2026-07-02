#!/bin/bash
# register-store.sh - provision (or repair) a Farm POS appliance box.
#
#   sudo ./register-store.sh              # interactive; or reads existing store.yml
#   sudo ./register-store.sh --restore    # restore mode (existing data dir / from backup)
#
# What it does (idempotent):
#   1. Ensure/collect store.yml identity  2. Generate per-box secrets (once)
#   3. Render .env + docker-compose.yml   4. Provision DB + bring up pinned image
#   5. Health-gate                        6. Print the ready banner
#
# Local-first, POS-only. No recognition/Frigate/web. Does not touch the LC box.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FARMPOS_HOME="${FARMPOS_HOME:-/opt/farmpos}"
. "$HERE/lib/common.sh"

REGISTRY="${REGISTRY:-ghcr.io}"
IMAGE_REPO="${IMAGE_REPO:-quintuszgeyser/farmpos-pos}"
RESTORE=0; UPDATE_ONLY=0
[ "${1:-}" = "--restore" ] && RESTORE=1
# --update-only: re-render .env + compose from the current store.yml, then exit BEFORE
# bringing up the stack / installing cron. Used by update.sh (which does the restart).
[ "${1:-}" = "--update-only" ] && UPDATE_ONLY=1

need_cmd docker; need_cmd openssl; need_cmd python3
docker compose version >/dev/null 2>&1 || die "docker compose plugin not installed"

c_bold "== Farm POS store provisioning =="
mkdir -p "$FARMPOS_HOME" "$SECRETS_DIR" \
         "$FARMPOS_HOME/data" "$FARMPOS_HOME/data/branding" "$FARMPOS_HOME/store" "$FARMPOS_HOME/postgres-init"
chmod 700 "$SECRETS_DIR"
STORE_YML="$FARMPOS_HOME/store.yml"

# --- 1. Identity ----------------------------------------------------------------
if [ ! -f "$STORE_YML" ]; then
  if [ -t 0 ]; then
    echo "No store.yml found - let's create one."
    read -rp "Store ID (kebab-case, e.g. boer-and-butcher): " IN_ID
    read -rp "Store display name: " IN_NAME
    read -rp "Scale IP (blank = no scale): " IN_SCALE
    read -rp "Image version tag [v2.1.0]: " IN_VER
    cp "$HERE/store.example.yml" "$STORE_YML"
    python3 - "$STORE_YML" "$IN_ID" "$IN_NAME" "${IN_SCALE:-}" "${IN_VER:-v2.1.0}" <<'PY'
import sys, yaml
f, sid, name, scale, ver = sys.argv[1:6]
d = yaml.safe_load(open(f))
d['store_id'] = sid; d['store_name'] = name
d['store_tagline'] = name; d['store_legal'] = name; d['store_subtitle'] = ''
d['farmpos_version'] = ver
d['scale'] = {'enabled': bool(scale), 'ip': scale, 'port': 7061}
yaml.safe_dump(d, open(f,'w'), sort_keys=False)
PY
  else
    die "no store.yml at $STORE_YML and not interactive - pre-seed it first."
  fi
fi

STORE_ID="$(yaml_get "$STORE_YML" store_id)"
[ -n "$STORE_ID" ] || die "store_id missing in store.yml"
echo "$STORE_ID" | grep -Eq '^[a-z0-9-]{1,40}$' || die "store_id must be kebab-case, <=40 chars"

STORE_NAME="$(yaml_or "$STORE_YML" store_name "$STORE_ID")"
STORE_TAGLINE="$(yaml_or "$STORE_YML" store_tagline "$STORE_NAME")"
STORE_LEGAL="$(yaml_or "$STORE_YML" store_legal "$STORE_NAME")"
STORE_SUBTITLE="$(yaml_get "$STORE_YML" store_subtitle)"
TZ="$(yaml_or "$STORE_YML" timezone 'Africa/Johannesburg')"
FARMPOS_VERSION="$(yaml_or "$STORE_YML" farmpos_version 'latest')"
SCALE_ENABLED="$(yaml_get "$STORE_YML" scale.enabled)"
SCALE_IP=""; SCALE_PORT="7061"
if [ "$SCALE_ENABLED" = "True" ] || [ "$SCALE_ENABLED" = "true" ]; then
  SCALE_IP="$(yaml_get "$STORE_YML" scale.ip)"
  SCALE_PORT="$(yaml_or "$STORE_YML" scale.port 7061)"
fi
POS_IMAGE="$REGISTRY/$IMAGE_REPO:$FARMPOS_VERSION"
c_green "Store: $STORE_NAME ($STORE_ID)  image: $POS_IMAGE  scale: ${SCALE_IP:-<none>}"

# --- 2. Secrets (generate once; never regenerate on re-run) ----------------------
secret_file() {  # $1 = name -> ensures file exists with a generated value, echoes it
  local f="$SECRETS_DIR/$1"
  [ -s "$f" ] || { gen_secret > "$f"; chmod 600 "$f"; }
  cat "$f"
}
POSTGRES_PASSWORD="$(secret_file postgres_password)"
SECRET_KEY="$(secret_file secret_key)"
ADMIN_PASS="$(secret_file admin_pass)"
POSTGRES_PASSWORD_URLENC="$(urlencode "$POSTGRES_PASSWORD")"

# 2a. Backup encryption keypair (generate ONCE per box; private key must be escrowed).
if command -v age-keygen >/dev/null 2>&1; then
  if [ ! -s "$SECRETS_DIR/backup_age.key" ]; then
    age-keygen -o "$SECRETS_DIR/backup_age.key" 2>/dev/null
    chmod 600 "$SECRETS_DIR/backup_age.key"
    grep 'public key:' "$SECRETS_DIR/backup_age.key" | awk '{print $NF}' > "$SECRETS_DIR/backup_age.pub"
    cp "$SECRETS_DIR/backup_age.key" "$SECRETS_DIR/backup_age.key.escrow.txt"
    KEY_JUST_MADE=1
  fi
else
  c_red "WARN: 'age' not installed - backups will be UNENCRYPTED. apt-get install age, then re-run."
fi

# 2b. Central support public key -> lets you restore/repro ANY store centrally.
if [ -f "$HERE/support_age.pub" ]; then
  cp "$HERE/support_age.pub" "$SECRETS_DIR/support_age.pub"
else
  c_red "WARN: appliance/support_age.pub missing - central restore/repro will NOT work."
  c_red "      Generate it on your support machine and commit it (see support_age.pub.example)."
fi

# --- 3. Render .env + compose + init --------------------------------------------
export STORE_ID STORE_NAME STORE_TAGLINE STORE_LEGAL STORE_SUBTITLE TZ \
       FARMPOS_VERSION POS_IMAGE POSTGRES_PASSWORD POSTGRES_PASSWORD_URLENC \
       SECRET_KEY ADMIN_PASS SCALE_IP SCALE_PORT
envsubst < "$HERE/env.template" > "$FARMPOS_HOME/.env"
chmod 600 "$FARMPOS_HOME/.env"
cp "$HERE/compose.template.yml" "$FARMPOS_HOME/docker-compose.yml"
cp "$HERE/postgres-init/01-create-db.sh" "$FARMPOS_HOME/postgres-init/"
chmod +x "$FARMPOS_HOME/postgres-init/01-create-db.sh"
# Ship a default logo if the store didn't supply one, so the bind-mount resolves.
[ -f "$FARMPOS_HOME/store/logo.svg" ] || : > "$FARMPOS_HOME/store/logo.svg"

# --update-only stops here: .env + compose are re-rendered from store.yml; update.sh
# owns the pull + restart. Skips DB init, stack start, health-gate, and cron install.
if [ "$UPDATE_ONLY" = "1" ]; then
  c_green "Re-rendered .env + docker-compose.yml from store.yml (update-only)."
  exit 0
fi

# --- 4. Bring up ----------------------------------------------------------------
cd "$FARMPOS_HOME"
c_bold "Pulling pinned image + starting stack..."
docker compose pull
docker compose up -d

# --- 5. Health-gate -------------------------------------------------------------
c_bold "Waiting for POS to become healthy..."
ok=0
for i in $(seq 1 45); do
  code="$(curl -s -o /dev/null -w '%{http_code}' http://localhost:5000/health || true)"
  [ "$code" = "200" ] && { ok=1; break; }
  sleep 2
done
[ "$ok" = "1" ] || { docker compose logs --tail 40 pos; die "POS did not become healthy."; }

# --- 5b. Install the nightly backup cron (idempotent) ---------------------------
CRON_LINE="0 2 * * * $HERE/backup.sh >> $FARMPOS_HOME/data/backup.log 2>&1"
if ! crontab -l 2>/dev/null | grep -qF "$HERE/backup.sh"; then
  ( crontab -l 2>/dev/null; echo "$CRON_LINE" ) | crontab -
  c_green "Installed nightly backup cron (02:00)."
fi

# --- 6. Ready -------------------------------------------------------------------
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
c_green "================================================================"
c_green " READY - $STORE_NAME is trading."
c_green "   POS:      http://${LAN_IP:-<lan-ip>}:5000"
c_green "   Admin:    admin  /  $ADMIN_PASS   (change on first login)"
[ -n "$SCALE_IP" ] && c_green "   Scale:    $SCALE_IP:$SCALE_PORT"
c_green "   Secrets:  $SECRETS_DIR (mode 600 - back these up)"
c_green "================================================================"
if [ "${KEY_JUST_MADE:-0}" = "1" ]; then
  c_red "⚠  ESCROW THE BACKUP KEY NOW - this store's backups cannot be self-recovered without it:"
  c_red "     $SECRETS_DIR/backup_age.key.escrow.txt"
  c_red "   Copy it to a password manager + one offline location, then delete the .escrow.txt copy."
fi
[ -f "$SECRETS_DIR/support_age.pub" ] || c_red "⚠  No support key deployed - you will NOT be able to restore/repro this store centrally."
[ "$RESTORE" = "1" ] && echo "(restore mode: existing DB preserved; init skipped by Postgres.)"
