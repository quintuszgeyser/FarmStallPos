#!/bin/bash
# Smart deployment — caches recognition model/package layers, only rebuilds what changed.
#
# QA  : build the artifact from ./pos (git clone, fresh) and run it as qa-farmpos-app (own DB farm_pos_qa).
# PROD: PROMOTE THE EXACT QA-TESTED IMAGE (no rebuild) so prod is functionally identical to QA,
#       differing only by APP_ENV / DATABASE_URL / volumes. Includes pre-deploy DB snapshot,
#       previous-image tagging for rollback, and a post-deploy schema parity check vs QA.
#
# Install on server at ~/farmpos-docker/deploy.sh
set -e
cd ~/farmpos-docker

TARGET="${1:-}"  # pos | recognition | web | scale-sync | qa | prod | (empty = all)

PROJECT="farmpos-docker"          # docker compose project prefix (image name prefix)
QA_IMAGE="${PROJECT}-qa-pos:latest"
PROD_IMAGE="${PROJECT}-pos:latest"
PROD_PREV_IMAGE="${PROJECT}-pos:previous"
PG_CONTAINER="farmpos-postgres"
PG_USER="farmstall"
BACKUP_DIR="$HOME/backups/pre-deploy"

PARITY_FAIL=0   # set to 1 if the post-deploy schema parity check finds drift

wait_healthy() {  # $1 = container name, $2 = label. Never fails the script (warn only).
  # 120s window — startup runs the DB migration first, which can push health past 60s.
  for i in $(seq 1 24); do
    sleep 5
    if docker ps | grep -q "$1.*healthy"; then echo "[deploy] $2 is healthy"; return 0; fi
    echo "[deploy] Waiting for $2... ($((i*5))s)"
  done
  echo "[deploy] WARNING: $2 did not report healthy in 120s"; return 0
}

# Compare prod schema against qa schema. Anything QA has that prod lacks = drift = failure.
schema_parity_check() {
  echo "[deploy] Schema parity check (prod vs qa)..."
  local Q="SELECT table_name||'.'||column_name FROM information_schema.columns WHERE table_schema='public' UNION ALL SELECT 'TBL:'||tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1"
  docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d farm_pos_qa   -t -A -c "$Q" | sort > /tmp/parity_qa.txt
  docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d farm_pos_prod -t -A -c "$Q" | sort > /tmp/parity_prod.txt
  local missing
  missing=$(comm -23 /tmp/parity_qa.txt /tmp/parity_prod.txt)
  if [ -n "$missing" ]; then
    echo "[deploy] !!! SCHEMA DRIFT — prod is missing objects QA has:"
    echo "$missing" | sed 's/^/    /'
    echo "[deploy] !!! Investigate or run rollback.sh. (DB snapshot is in $BACKUP_DIR)"
    return 1
  fi
  echo "[deploy] Schema parity OK — prod schema matches qa."
  return 0
}

backup_prod_db() {
  mkdir -p "$BACKUP_DIR"
  local ts; ts=$(date +%Y-%m-%d_%H%M%S)
  local f="$BACKUP_DIR/farm_pos_prod_${ts}.sql.gz"
  echo "[deploy] Backing up prod DB -> $f"
  docker exec "$PG_CONTAINER" pg_dump -U "$PG_USER" farm_pos_prod | gzip > "$f"
  ls -1t "$BACKUP_DIR"/farm_pos_prod_*.sql.gz | tail -n +15 | xargs -r rm -f   # keep last 14
  echo "$f" > "$BACKUP_DIR/.last"
}

deploy_pos() {
  echo "[deploy] Building POS (git clone, always fresh)..."
  docker rm -f farmpos-app 2>/dev/null || true
  docker compose build --no-cache pos
  docker compose up -d --remove-orphans
  wait_healthy farmpos-app "POS"
}

deploy_recognition() {
  echo "[deploy] Building recognition (cached layers, code-only rebuild)..."
  docker rm -f farmpos-recognition 2>/dev/null || true
  docker compose build recognition
  docker compose up -d --remove-orphans
}

deploy_qa() {
  echo "[deploy] Deploying QA POS (qa-pos container, port 5100)..."
  docker rm -f qa-farmpos-app 2>/dev/null || true
  docker compose build --no-cache qa-pos
  docker compose up -d qa-pos
  wait_healthy qa-farmpos-app "QA POS"
  # Record the artifact that QA validated, for traceable promotion.
  docker image inspect "$QA_IMAGE" --format '{{.Id}}' > /tmp/qa_image_id.txt 2>/dev/null || true
  echo "[deploy] QA artifact: $QA_IMAGE ($(cat /tmp/qa_image_id.txt 2>/dev/null))"
}

# PROMOTE the exact QA-tested image to prod — no rebuild. Prod == QA functionally, own DB.
deploy_prod() {
  echo "[deploy] Promoting QA artifact to PROD (no rebuild)..."
  if ! docker image inspect "$QA_IMAGE" >/dev/null 2>&1; then
    echo "[deploy] ERROR: $QA_IMAGE not found — deploy QA first (deploy.sh qa)."; exit 1
  fi

  # 1) Pre-deploy DB snapshot (rollback safety net)
  backup_prod_db

  # 2) Tag current prod image as :previous for image rollback
  if docker image inspect "$PROD_IMAGE" >/dev/null 2>&1; then
    docker tag "$PROD_IMAGE" "$PROD_PREV_IMAGE"
    echo "[deploy] Saved rollback image: $PROD_PREV_IMAGE ($(docker image inspect "$PROD_PREV_IMAGE" --format '{{.Id}}'))"
  else
    echo "[deploy] No existing prod image to save as previous (first promotion)."
  fi

  # 3) Promote the exact tested bits
  docker tag "$QA_IMAGE" "$PROD_IMAGE"
  echo "[deploy] Promoted $QA_IMAGE -> $PROD_IMAGE ($(docker image inspect "$PROD_IMAGE" --format '{{.Id}}'))"

  # 4) Restart prod from the promoted image (no build). Startup migration runs against farm_pos_prod.
  docker rm -f farmpos-app 2>/dev/null || true
  docker compose up -d --no-build pos
  wait_healthy farmpos-app "PROD POS"

  # 5) Post-deploy parity gate — fail loudly on drift so it shows in the Deploy tab
  schema_parity_check || PARITY_FAIL=1
}

deploy_scale_sync() {
  echo "[deploy] Building scale-sync (SCP source, always fresh)..."
  docker rm -f farmpos-scale-sync 2>/dev/null || true
  docker compose build --no-cache scale-sync
  docker compose up -d --remove-orphans
}

deploy_web() {
  echo "[deploy] Building ladycoleen-web (git clone from GitHub, always fresh)..."
  docker rm -f ladycoleen-web 2>/dev/null || true
  docker compose build --no-cache ladycoleen-web
  docker compose up -d --remove-orphans
}

case "$TARGET" in
  pos)          deploy_pos ;;
  recognition)  deploy_recognition ;;
  web)          deploy_web ;;
  scale-sync)   deploy_scale_sync ;;
  qa)           deploy_qa ;;
  prod)         deploy_prod ;;
  *)            deploy_pos && deploy_recognition && deploy_web ;;
esac

sleep 5
docker ps --format 'table {{.Names}}\t{{.Status}}'

# Propagate parity result so deploy_cron.sh reports prod drift as a failed deploy.
if [ "$PARITY_FAIL" = "1" ]; then
  echo "[deploy] RESULT: FAILED — schema drift detected (see above). Prod is up but NOT in parity with QA."
  exit 1
fi
echo "[deploy] RESULT: OK"

