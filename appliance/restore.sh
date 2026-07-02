#!/bin/bash
# restore.sh — restore a store's DB from a backup (local or pulled from off-box).
#   sudo ./restore.sh                      # restore newest local backup
#   sudo ./restore.sh /path/to/dump.sql.gz[.age]
#
# SAFE-BY-DESIGN: the target DB is DROPPED and recreated before loading, so a restore
# always produces the backup's exact state (never a silent merge into existing rows).
# Load runs with ON_ERROR_STOP=1 and no output suppression, so a bad dump fails loudly.
#
# DRILL THIS on a box that ALREADY HAS DATA before trusting it — an empty-DB drill
# hides the merge/idempotency failure mode. An untested restore is not a backup.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FARMPOS_HOME="${FARMPOS_HOME:-/opt/farmpos}"
. "$HERE/lib/common.sh"
cd "$FARMPOS_HOME"

PG="${PG_CONTAINER:-farmpos-postgres}"   # override for repro stacks
DB="${PG_DB:-farmpos}"
BK="$FARMPOS_HOME/data/backups"
SRC="${1:-}"
[ -z "$SRC" ] && SRC="$(cat "$BK/.last" 2>/dev/null || true)"
[ -n "$SRC" ] && [ -f "$SRC" ] || die "no backup file (pass a path, or ensure $BK/.last exists)"

c_bold "Restoring $DB from: $SRC"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
chmod 700 "$TMP"                         # decrypted PII must not be world-readable
PLAIN="$TMP/restore.sql.gz"

# 1. Decrypt if .age (needs a private key — store's own, or the central support key).
if [[ "$SRC" == *.age ]]; then
  KEY=""
  [ -f "$SECRETS_DIR/backup_age.key" ] && KEY="$SECRETS_DIR/backup_age.key"
  [ -z "$KEY" ] && [ -n "${SUPPORT_AGE_KEY:-}" ] && [ -f "$SUPPORT_AGE_KEY" ] && KEY="$SUPPORT_AGE_KEY"
  [ -n "$KEY" ] || die "encrypted backup but no age private key (store key at $SECRETS_DIR/backup_age.key, or set SUPPORT_AGE_KEY)"
  age -d -i "$KEY" -o "$PLAIN" "$SRC"
else
  cp "$SRC" "$PLAIN"
fi

# 2. Validate the dump BEFORE we touch the database (fail before destroying anything).
gzip -t "$PLAIN" 2>/dev/null || die "backup is not a valid gzip — refusing to restore"
SIZE=$(stat -c%s "$PLAIN" 2>/dev/null || echo 0)
[ "$SIZE" -gt 1024 ] || die "decrypted dump is suspiciously small ($SIZE bytes) — refusing to restore"
gunzip -c "$PLAIN" | head -c 4096 | grep -qE 'CREATE TABLE|COPY |INSERT INTO' \
  || die "dump does not look like a pg_dump (no CREATE/COPY/INSERT in header) — refusing"

# 3. Bring up Postgres; stop the app so nothing writes mid-restore.
docker compose up -d postgres
for i in $(seq 1 30); do docker exec "$PG" pg_isready -U farmstall >/dev/null 2>&1 && break; sleep 2; done
docker compose stop pos 2>/dev/null || true

BEFORE="$(docker exec "$PG" psql -U farmstall -d "$DB" -t -A -c 'SELECT count(*) FROM products' 2>/dev/null || echo 'n/a')"
c_bold "products rows BEFORE restore: $BEFORE"

# 4. Clean slate: DROP + CREATE the DB, then load. This is the fix for the silent-merge
#    bug — loading a plain-SQL dump into a populated DB errors on every existing table.
c_bold "Dropping + recreating $DB (clean restore)..."
docker exec "$PG" psql -U farmstall -d postgres -v ON_ERROR_STOP=1 -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$DB' AND pid<>pg_backend_pid();" >/dev/null
docker exec "$PG" psql -U farmstall -d postgres -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS $DB;"
docker exec "$PG" psql -U farmstall -d postgres -v ON_ERROR_STOP=1 -c "CREATE DATABASE $DB OWNER farmstall;"

c_bold "Loading dump (errors are fatal)..."
gunzip -c "$PLAIN" | docker exec -i "$PG" psql -U farmstall -d "$DB" -v ON_ERROR_STOP=1 -q

# 5. Bring the app back and report before/after so the operator can SEE it changed.
docker compose up -d
AFTER="$(docker exec "$PG" psql -U farmstall -d "$DB" -t -A -c 'SELECT count(*) FROM products' 2>/dev/null || echo '?')"
c_green "Restore complete. products rows: $BEFORE -> $AFTER  — verify the POS."
