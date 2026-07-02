#!/bin/bash
# repro.sh — reproduce ANY store's exact scenario locally on the support machine.
#
#   ./repro.sh <store_id> <version> [backup_file.age]   # spin up an isolated repro stack
#   ./repro.sh teardown <store_id>                       # tear it down + wipe plaintext data
#
# Pulls the store's encrypted backup, decrypts with the CENTRAL support key, and boots an
# ISOLATED compose stack (own project name / port / volumes) running the store's EXACT
# pinned image against its EXACT data. Never touches any live store and never collides
# with your own dev POS. Read-only: it cannot write back to any store.
#
# Config comes from repro.conf (gitignored) — see repro.conf.example.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/lib/common.sh"
[ -f "$HERE/repro.conf" ] && . "$HERE/repro.conf"

REGISTRY="${REGISTRY:-ghcr.io}"
IMAGE_REPO="${IMAGE_REPO:-quintuszgeyser/farmpos-pos}"
WORK="${REPRO_WORK_DIR:-/tmp/farmpos-repro}"
SUPPORT_AGE_KEY="${SUPPORT_AGE_KEY:-$HOME/.config/farmpos/support_age.key}"
BACKUP_REMOTE="${BACKUP_RCLONE_REMOTE:-}"   # e.g. r2:farmpos-backups

# ---- teardown -----------------------------------------------------------------
if [ "${1:-}" = "teardown" ]; then
  SID="${2:?usage: repro.sh teardown <store_id>}"
  proj="repro-${SID}"
  ( cd "$WORK/$SID" 2>/dev/null && docker compose -p "$proj" down -v 2>/dev/null ) || true
  rm -rf "${WORK:?}/$SID"
  c_green "Torn down repro-$SID and wiped $WORK/$SID (plaintext PII removed)."
  exit 0
fi

SID="${1:?usage: repro.sh <store_id> <version> [backup_file]}"
VER="${2:?usage: repro.sh <store_id> <version> [backup_file]}"
echo "$SID" | grep -Eq '^[a-z0-9-]{1,40}$' || die "bad store_id"
echo "$VER" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+' || die "version must look like v2.1.0"

need_cmd docker; need_cmd age
docker compose version >/dev/null 2>&1 || die "docker compose plugin required"
[ -f "$SUPPORT_AGE_KEY" ] || die "support private key not found at $SUPPORT_AGE_KEY (set SUPPORT_AGE_KEY in repro.conf)"

DIR="$WORK/$SID"; mkdir -p "$DIR"; chmod 700 "$WORK" "$DIR"   # decrypted PII stays private
PROJ="repro-${SID}"

# Stable, collision-free ports derived from store_id (15000-15999 / 15432-.. range).
HASH=$(( 0x$(printf '%s' "$SID" | cksum | awk '{printf "%x", $1}') % 1000 ))
POS_PORT=$(( 15000 + HASH ))
PG_PORT=$(( 15432 + HASH ))

# 1. Fetch the backup: explicit path, else newest from the rclone remote.
SRC="${3:-}"
if [ -z "$SRC" ]; then
  [ -n "$BACKUP_REMOTE" ] || die "no backup_file given and BACKUP_RCLONE_REMOTE unset in repro.conf"
  need_cmd rclone
  c_bold "Pulling latest backup for $SID from $BACKUP_REMOTE ..."
  rclone copy "$BACKUP_REMOTE/stores/$SID/" "$DIR/pull/" --include "*.sql.gz.age" 2>/dev/null || true
  SRC="$(ls -1t "$DIR/pull/"*.sql.gz.age 2>/dev/null | head -1 || true)"
  [ -n "$SRC" ] || die "no backup found for $SID at $BACKUP_REMOTE/stores/$SID/"
fi
c_green "Backup: $SRC"

# 2. Decrypt with the central support key + validate.
PLAIN="$DIR/restore.sql.gz"
age -d -i "$SUPPORT_AGE_KEY" -o "$PLAIN" "$SRC"
gzip -t "$PLAIN" 2>/dev/null || die "decrypted file is not valid gzip (wrong key or corrupt backup)"

# 3. Resolve the image by DIGEST so repro is byte-identical even if the tag moved.
IMG_TAG="$REGISTRY/$IMAGE_REPO:$VER"
c_bold "Resolving $IMG_TAG ..."
docker pull "$IMG_TAG" >/dev/null
DIGEST="$(docker inspect --format '{{index .RepoDigests 0}}' "$IMG_TAG" 2>/dev/null || echo "$IMG_TAG")"
POS_IMAGE="${DIGEST:-$IMG_TAG}"

# 4. Write an isolated compose file (own project/ports/network/volumes; scale stubbed).
cat > "$DIR/docker-compose.yml" <<YML
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: farmstall
      POSTGRES_PASSWORD: repro
      POSTGRES_DB: farmpos
    ports: ["127.0.0.1:${PG_PORT}:5432"]
    healthcheck: {test: ["CMD-SHELL","pg_isready -U farmstall"], interval: 5s, timeout: 5s, retries: 10}
  pos:
    image: ${POS_IMAGE}
    depends_on: {postgres: {condition: service_healthy}}
    environment:
      APP_ENV: repro
      STORE_ID: ${SID}
      STORE_NAME: "REPRO ${SID}"
      SECRET_KEY: repro-throwaway-key
      SCALE_IP: ""                       # scale hardware cannot be reproduced — stubbed
      DATABASE_URL: postgresql://farmstall:repro@postgres:5432/farmpos
    ports: ["127.0.0.1:${POS_PORT}:5000"]
YML

# 5. Boot Postgres, load the exact data, then boot the pinned POS image.
cd "$DIR"
docker compose -p "$PROJ" up -d postgres
for i in $(seq 1 30); do docker compose -p "$PROJ" exec -T postgres pg_isready -U farmstall >/dev/null 2>&1 && break; sleep 2; done
c_bold "Loading $SID data (errors fatal)..."
gunzip -c "$PLAIN" | docker compose -p "$PROJ" exec -T postgres psql -U farmstall -d farmpos -v ON_ERROR_STOP=1 -q
docker compose -p "$PROJ" up -d pos

# 6. Health-gate + report.
ok=0
for i in $(seq 1 45); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${POS_PORT}/health" || true)"
  [ "$code" = "200" ] && { ok=1; break; }
  sleep 2
done
ROWS="$(docker compose -p "$PROJ" exec -T postgres psql -U farmstall -d farmpos -t -A -c 'SELECT count(*) FROM products' 2>/dev/null || echo '?')"
BK_AGE="$(basename "$SRC")"
c_green "================================================================"
[ "$ok" = "1" ] && c_green " REPRO LIVE — $SID" || c_red " REPRO STARTED (POS not healthy — check logs)"
c_green "   URL:     http://localhost:${POS_PORT}"
c_green "   Image:   $POS_IMAGE"
c_green "   Data:    $BK_AGE   (products: $ROWS)"
c_green "   Note:    product images 404 (not in DB backup); scale is stubbed."
c_green "   Logs:    docker compose -p $PROJ logs -f pos"
c_green "   Done:    ./repro.sh teardown $SID     (wipes plaintext PII)"
c_green "================================================================"
[ "$ok" = "1" ] || docker compose -p "$PROJ" logs --tail 30 pos
