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

# Local retention: 14 normally, 7 when the disk is tight. Set by the disk guard below.
KEEP=14

# 0. Disk guard - a full disk crashes Postgres mid-write and silently kills backups.
#    Warn (owner-visible marker via .disk_warn) and tighten retention; hard-abort only
#    if there is genuinely no safe headroom left.
DISK_PCT="$(df --output=pcent "$BK" 2>/dev/null | tail -1 | tr -dc '0-9')"
if [ -n "$DISK_PCT" ] && [ "$DISK_PCT" -ge 80 ]; then
  echo "$(date -Iseconds) DISK_WARN ${DISK_PCT}% used on backup volume" | tee "$BK/.disk_warn" >&2
  KEEP=7
  [ "$DISK_PCT" -ge 95 ] && die "disk ${DISK_PCT}% full - refusing backup (no safe headroom)"
else
  rm -f "$BK/.disk_warn"
fi

# 1. Dump (compressed). set -o pipefail makes a pg_dump failure abort the whole script.
# Write a failure status immediately if pg_dump fails so /api/health shows a banner.
_write_failure_status() {
  local msg="$1"
  CFG="$FARMPOS_HOME/data/pos-config"
  if [ -d "$CFG" ]; then
    DWARN="false"; [ -f "$BK/.disk_warn" ] && DWARN="true"
    printf '{"last_backup":null,"last_push_ok":false,"disk_warn":%s,"disk_pct":%s,"error":"%s"}\n' \
      "$DWARN" "${DISK_PCT:-0}" "$msg" > "$CFG/backup_status.json" 2>/dev/null || true
  fi
}
if ! docker exec farmpos-postgres pg_dump -U farmstall farmpos | gzip > "$DUMP"; then
  rm -f "$DUMP"
  _write_failure_status "pg_dump failed"
  die "pg_dump failed - Postgres may be unreachable"
fi

# 1a. Size guard - never let a zero/tiny dump (postgres down, broken pipe) overwrite good
#     backups. 2 KB is the floor: a fresh store with no products compresses to ~7 KB, so
#     10 KB was too aggressive and aborted valid first-run backups.
SIZE=$(stat -c%s "$DUMP" 2>/dev/null || echo 0)
if [ "$SIZE" -lt 2048 ]; then
  rm -f "$DUMP"
  die "dump is suspiciously small ($SIZE bytes) - aborting, keeping previous backups"
fi
gzip -t "$DUMP" 2>/dev/null || { rm -f "$DUMP"; die "dump failed gzip integrity check - aborting"; }

# 1b. Manifest - row counts per table, so restore.sh can detect a truncated/partial dump
#     that still happens to be valid gzip (ISSUE-30). Written BEFORE encryption so it
#     reflects the real DB state at dump time.
MANIFEST="$DUMP.manifest"
docker exec farmpos-postgres psql -U farmstall -d farmpos -t -A -F',' -c \
  "SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY relname" > "$MANIFEST" 2>/dev/null || \
  echo "" > "$MANIFEST"

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
  # keep the manifest named to match the final artifact
  mv "$MANIFEST" "$FINAL.manifest"; MANIFEST="$FINAL.manifest"
else
  echo "$(date -Iseconds) WARN: no age/backup_age.pub - backup is UNENCRYPTED (POPIA risk)" >&2
fi

# 2a. Integrity checksum alongside the artifact.
sha256sum "$FINAL" > "$FINAL.sha256"

# 3. Retention: keep last $KEEP local (artifact + its .sha256 + .manifest sidecars).
# Use find so there's no glob-expands-to-nothing error when only one format exists.
find "$BK" -maxdepth 1 -name "${STORE_ID}_*.sql.gz.age" -o -name "${STORE_ID}_*.sql.gz" 2>/dev/null \
  | grep -vE '\.sha256$|\.manifest$' | xargs ls -1t 2>/dev/null \
  | tail -n +$((KEEP+1)) | while read -r old; do
    rm -f "$old" "$old.sha256" "$old.manifest"
  done
echo "$FINAL" > "$BK/.last"

# 4. Off-box push (rclone) if configured in store.yml. Verify the remote copy landed.
#    Push the manifest too so a central restore can validate row counts.
TARGET="$(yaml_get store.yml backup.target)"
if [ -n "$TARGET" ] && command -v rclone >/dev/null 2>&1; then
  DEST="$TARGET/stores/${STORE_ID}/"
  if rclone copy "$FINAL" "$DEST" && rclone copy "$FINAL.sha256" "$DEST" && rclone copy "$MANIFEST" "$DEST"; then
    echo "$(date -Iseconds) pushed $(basename "$FINAL")" >> "$BK/.push.log"
    rm -f "$BK/.push_warn"
  else
    echo "$(date -Iseconds) WARN: rclone push FAILED for $(basename "$FINAL")" | tee -a "$BK/.push.log" "$BK/.push_warn" >&2
  fi
elif [ -n "$TARGET" ]; then
  echo "$(date -Iseconds) WARN: backup.target set but rclone not installed - no off-box copy" >&2
fi

# 5. Heartbeat file for backup-health monitoring (always written, regardless of rclone).
echo "$(date -Iseconds) $(basename "$FINAL") $SIZE bytes" > "$BK/.heartbeat"

# 5b. Write a status file into the POS config volume (mounted into the container at
#     /app/config) so /api/health can surface a backup-health banner to the owner.
#     Zero new mounts needed - pos-config is already mounted.
CFG="$FARMPOS_HOME/data/pos-config"
if [ -d "$CFG" ]; then
  PUSHED="true"; [ -f "$BK/.push_warn" ] && PUSHED="false"
  DWARN="false"; [ -f "$BK/.disk_warn" ] && DWARN="true"
  printf '{"last_backup":"%s","last_push_ok":%s,"disk_warn":%s,"disk_pct":%s}\n' \
    "$(date -Iseconds)" "$PUSHED" "$DWARN" "${DISK_PCT:-0}" > "$CFG/backup_status.json" 2>/dev/null || true
fi
echo "$(date -Iseconds) backup ok: $FINAL ($SIZE bytes)"
