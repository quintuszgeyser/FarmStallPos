#!/bin/bash
# backup.sh — nightly per-store backup: pg_dump -> age-encrypt -> optional off-box push.
# Cron:  0 2 * * *  /opt/farmpos/appliance/backup.sh >> /opt/farmpos/data/backup.log 2>&1
#
# Encryption uses a PER-STORE age key (secrets/backup_age.key + .pub). The PUBLIC key
# encrypts; the PRIVATE key (escrow it OFF the box, two copies) is needed to restore.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FARMPOS_HOME="${FARMPOS_HOME:-/opt/farmpos}"
. "$HERE/lib/common.sh"

cd "$FARMPOS_HOME"
STORE_ID="$(yaml_get store.yml store_id)"
BK="$FARMPOS_HOME/data/backups"; mkdir -p "$BK"
TS="$(date +%Y-%m-%d_%H%M%S)"
DUMP="$BK/${STORE_ID}_${TS}.sql.gz"

# 1. Dump (compressed). Container name is fixed by compose.
docker exec farmpos-postgres pg_dump -U farmstall farmpos | gzip > "$DUMP"

# 2. Encrypt with age if a public key exists (recommended for any off-box copy).
FINAL="$DUMP"
if command -v age >/dev/null 2>&1 && [ -f "$SECRETS_DIR/backup_age.pub" ]; then
  age -R "$SECRETS_DIR/backup_age.pub" -o "$DUMP.age" "$DUMP"
  rm -f "$DUMP"; FINAL="$DUMP.age"
fi

# 3. Retention: keep last 14 local.
ls -1t "$BK/${STORE_ID}_"*.sql.gz* 2>/dev/null | tail -n +15 | xargs -r rm -f
echo "$FINAL" > "$BK/.last"

# 4. Off-box push (rclone) if configured in store.yml.
TARGET="$(yaml_get store.yml backup.target)"
if [ -n "$TARGET" ] && command -v rclone >/dev/null 2>&1; then
  rclone copy "$FINAL" "$TARGET/store_${STORE_ID}/" && \
    echo "$(date -Iseconds) pushed $(basename "$FINAL")" >> "$BK/.push.log"
fi
echo "$(date -Iseconds) backup ok: $FINAL"
