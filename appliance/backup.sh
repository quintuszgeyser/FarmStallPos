#!/bin/bash
# backup.sh - nightly per-store backup: pg_dump -> age-encrypt (2 recipients) -> off-box push.
# Cron:  0 2 * * *  /opt/farmpos/appliance/backup.sh >> /opt/farmpos/data/backup.log 2>&1
#
# Encryption uses TWO age recipients so either key can decrypt independently:
#   1. the store's OWN key   (secrets/backup_age.pub) - store self-recovery
#   2. the CENTRAL support key (secrets/support_age.pub) - lets the operator restore/repro
#      ANY store's backup with one private key, without collecting 50 store keys.
# Private keys live OFF the box (escrow / support machine). Public keys encrypt only.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FARMPOS_HOME="${FARMPOS_HOME:-/opt/farmpos}"
. "$HERE/lib/common.sh"

cd "$FARMPOS_HOME"
STORE_ID="$(yaml_get store.yml store_id)"
[ -n "$STORE_ID" ] || die "store_id missing in store.yml - cannot name backup"
BK="$FARMPOS_HOME/data/backups"; mkdir -p "$BK"
TS="$(date +%Y-%m-%d_%H%M%S)"
DUMP="$BK/${STORE_ID}_${TS}.sql.gz"

# 1. Dump (compressed). set -o pipefail makes a pg_dump failure abort the whole script.
docker exec farmpos-postgres pg_dump -U farmstall farmpos | gzip > "$DUMP"

# 1a. Size guard - never let a zero/tiny dump (postgres down, broken pipe) overwrite good
#     backups. A real farmpos dump compresses to well over 10 KB even when nearly empty.
SIZE=$(stat -c%s "$DUMP" 2>/dev/null || echo 0)
if [ "$SIZE" -lt 10240 ]; then
  rm -f "$DUMP"
  die "dump is suspiciously small ($SIZE bytes) - aborting, keeping previous backups"
fi
gzip -t "$DUMP" 2>/dev/null || { rm -f "$DUMP"; die "dump failed gzip integrity check - aborting"; }

# 2. Encrypt with age to BOTH recipients. Warn (don't fail) if a key is missing so a box
#    provisioned before the support key existed keeps producing (store-only) backups.
FINAL="$DUMP"
if command -v age >/dev/null 2>&1 && [ -f "$SECRETS_DIR/backup_age.pub" ]; then
  RCPT=(-R "$SECRETS_DIR/backup_age.pub")
  if [ -f "$SECRETS_DIR/support_age.pub" ]; then
    RCPT+=(-R "$SECRETS_DIR/support_age.pub")
  else
    echo "$(date -Iseconds) WARN: support_age.pub absent - central restore/repro will NOT work for this backup" >&2
  fi
  age "${RCPT[@]}" -o "$DUMP.age" "$DUMP"
  rm -f "$DUMP"; FINAL="$DUMP.age"
else
  echo "$(date -Iseconds) WARN: no age/backup_age.pub - backup is UNENCRYPTED (POPIA risk)" >&2
fi

# 2a. Integrity checksum alongside the artifact.
sha256sum "$FINAL" > "$FINAL.sha256"

# 3. Retention: keep last 14 local (both the artifact and its .sha256).
ls -1t "$BK/${STORE_ID}_"*.sql.gz*[!6] 2>/dev/null | grep -v '\.sha256$' | tail -n +15 | while read -r old; do
  rm -f "$old" "$old.sha256"
done
echo "$FINAL" > "$BK/.last"

# 4. Off-box push (rclone) if configured in store.yml. Verify the remote copy landed.
TARGET="$(yaml_get store.yml backup.target)"
if [ -n "$TARGET" ] && command -v rclone >/dev/null 2>&1; then
  DEST="$TARGET/stores/${STORE_ID}/"
  if rclone copy "$FINAL" "$DEST" && rclone copy "$FINAL.sha256" "$DEST"; then
    echo "$(date -Iseconds) pushed $(basename "$FINAL")" >> "$BK/.push.log"
  else
    echo "$(date -Iseconds) WARN: rclone push FAILED for $(basename "$FINAL")" | tee -a "$BK/.push.log" >&2
  fi
elif [ -n "$TARGET" ]; then
  echo "$(date -Iseconds) WARN: backup.target set but rclone not installed - no off-box copy" >&2
fi

# 5. Heartbeat file for backup-health monitoring (support machine can check its freshness).
echo "$(date -Iseconds) $(basename "$FINAL") $SIZE bytes" > "$BK/.heartbeat"
echo "$(date -Iseconds) backup ok: $FINAL ($SIZE bytes)"
