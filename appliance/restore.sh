#!/bin/bash
# restore.sh — restore a store's DB from a backup (local or pulled from off-box).
#   sudo ./restore.sh                      # restore newest local backup
#   sudo ./restore.sh /path/to/dump.sql.gz[.age]
#
# DRILL THIS on a throwaway box before trusting it. An untested restore is not a backup.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FARMPOS_HOME="${FARMPOS_HOME:-/opt/farmpos}"
. "$HERE/lib/common.sh"
cd "$FARMPOS_HOME"

BK="$FARMPOS_HOME/data/backups"
SRC="${1:-}"
[ -z "$SRC" ] && SRC="$(cat "$BK/.last" 2>/dev/null || true)"
[ -n "$SRC" ] && [ -f "$SRC" ] || die "no backup file (pass a path, or ensure $BK/.last exists)"

c_bold "Restoring from: $SRC"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
PLAIN="$TMP/restore.sql.gz"

# Decrypt if .age (needs the PRIVATE key — from escrow).
if [[ "$SRC" == *.age ]]; then
  [ -f "$SECRETS_DIR/backup_age.key" ] || die "encrypted backup but no private key at $SECRETS_DIR/backup_age.key (restore it from escrow)"
  age -d -i "$SECRETS_DIR/backup_age.key" -o "$PLAIN" "$SRC"
else
  cp "$SRC" "$PLAIN"
fi

docker compose up -d postgres
for i in $(seq 1 30); do docker exec farmpos-postgres pg_isready -U farmstall >/dev/null 2>&1 && break; sleep 2; done

c_bold "Loading dump into farmpos..."
gunzip -c "$PLAIN" | docker exec -i farmpos-postgres psql -U farmstall -d farmpos >/dev/null

docker compose up -d
ROWS="$(docker exec farmpos-postgres psql -U farmstall -d farmpos -t -A -c 'SELECT count(*) FROM products' 2>/dev/null || echo '?')"
c_green "Restore complete. products rows: $ROWS  — verify the POS at :5000."
