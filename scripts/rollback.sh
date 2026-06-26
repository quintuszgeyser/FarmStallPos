#!/bin/bash
# rollback.sh — revert PROD POS to the previously-promoted image (fast code rollback).
# The pre-deploy DB snapshot is NOT auto-restored (image-only rollback by design); if you
# also need to restore the DB, the latest snapshot path is printed below — restore manually.
#
# Install on server at ~/farmpos-docker/rollback.sh
set -e
cd ~/farmpos-docker

PROJECT="farmpos-docker"
PROD_IMAGE="${PROJECT}-pos:latest"
PROD_PREV_IMAGE="${PROJECT}-pos:previous"
BACKUP_DIR="$HOME/backups/pre-deploy"

if ! docker image inspect "$PROD_PREV_IMAGE" >/dev/null 2>&1; then
  echo "[rollback] ERROR: no $PROD_PREV_IMAGE found — nothing to roll back to."; exit 1
fi

echo "[rollback] Current prod image:  $(docker image inspect "$PROD_IMAGE" --format '{{.Id}}' 2>/dev/null)"
echo "[rollback] Rolling back to:     $(docker image inspect "$PROD_PREV_IMAGE" --format '{{.Id}}')"

# Keep a handle on what we're rolling back FROM, so a re-deploy is still possible.
docker tag "$PROD_IMAGE" "${PROJECT}-pos:rolledback" 2>/dev/null || true

# Swap previous -> latest and restart prod from it (no build).
docker tag "$PROD_PREV_IMAGE" "$PROD_IMAGE"
docker rm -f farmpos-app 2>/dev/null || true
docker compose up -d --no-build pos

for i in $(seq 1 12); do
  sleep 5
  if docker ps | grep -q "farmpos-app.*healthy"; then echo "[rollback] PROD POS healthy on previous image"; break; fi
  echo "[rollback] Waiting for PROD POS... ($((i*5))s)"
done

echo "[rollback] Done."
if [ -f "$BACKUP_DIR/.last" ]; then
  echo "[rollback] If you also need to restore the DB, latest pre-deploy snapshot:"
  echo "    $(cat "$BACKUP_DIR/.last")"
  echo "    Restore with: gunzip -c <file> | docker exec -i farmpos-postgres psql -U farmstall -d farm_pos_prod"
fi
docker ps --format 'table {{.Names}}\t{{.Status}}'
