#!/bin/bash
# deploy-web-fast.sh — Sync ladycoleen_web source files into the running QA container
# without rebuilding the Docker image. ~30 seconds vs ~60 minutes for a full rebuild.
#
# Usage: bash scripts/deploy-web-fast.sh [qa|prod]
#   Default target: qa  (qa-ladycoleen-web container)
#   Use 'prod' only for emergency hotfixes; prefer the in-app Deploy tab for releases.
#
# What it does:
#   1. rsync ladycoleen_web/ to a staging dir on the server
#   2. Uploads a remote helper script and runs it in a tmux session for resilience
#   3. Helper: docker cp staged files into container, clear .pyc cache, SIGHUP gunicorn
#
# Caveats:
#   - Does NOT update Python packages (requirements.txt changes → full rebuild needed)
#   - Does NOT update the Docker image — next full `deploy.sh qa` overwrites this patch
#   - Always do a full QA build before promoting to prod

set -euo pipefail

TARGET="${1:-qa}"
SSH_HOST="farmpc"
STAGING_DIR="/tmp/lcweb_fast_deploy"
REMOTE_HELPER="/tmp/lcweb_fast_helper.sh"
REMOTE_LOG="/tmp/lcweb_fast_reload.log"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEB_SRC="${REPO_ROOT}/ladycoleen_web"

case "$TARGET" in
  qa)
    CONTAINER="qa-ladycoleen-web"
    CONTAINER_PORT=5101
    ;;
  prod)
    CONTAINER="ladycoleen-web"
    CONTAINER_PORT=5001
    echo "WARNING: targeting PROD container. Use the in-app Deploy tab for real releases."
    read -r -p "Continue? [y/N] " confirm
    [[ "${confirm,,}" == "y" ]] || { echo "Aborted."; exit 1; }
    ;;
  *)
    echo "Usage: $0 [qa|prod]"; exit 1
    ;;
esac

LOG_DIR="${HOME}/.local/share/farmpos/deploy-logs"
mkdir -p "$LOG_DIR"
LOG_FILE="${LOG_DIR}/deploy-web-fast-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee "$LOG_FILE") 2>&1

echo "=== deploy-web-fast.sh ==="
echo "Target    : $TARGET ($CONTAINER)"
echo "Source    : $WEB_SRC"
echo "Local log : $LOG_FILE"
echo "Started   : $(date)"
echo ""

# ── 1. Verify source directory ──────────────────────────────────────────────
[ -d "$WEB_SRC" ] || { echo "ERROR: ladycoleen_web/ not found at $WEB_SRC"; exit 1; }

# ── 2. Check container is running ───────────────────────────────────────────
echo "[1/4] Checking $CONTAINER is running on $SSH_HOST..."
ssh -o ServerAliveInterval=15 "$SSH_HOST" \
  "docker ps --filter name=^/${CONTAINER}\$ --filter status=running --format '{{.Names}}'" \
  | grep -q "$CONTAINER" \
  || { echo "ERROR: Container '$CONTAINER' not running on $SSH_HOST"; exit 1; }
echo "      OK"

# ── 3. rsync web source to server staging dir ───────────────────────────────
echo "[2/4] Syncing ladycoleen_web/ → ${SSH_HOST}:${STAGING_DIR}/ ..."
rsync -az --delete \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '*.pyo' \
  --exclude '.git' \
  --exclude 'logs/' \
  --exclude 'uploads/' \
  --exclude 'product_images/' \
  --exclude 'brand_logos/' \
  -e "ssh -o ServerAliveInterval=15" \
  "${WEB_SRC}/" "${SSH_HOST}:${STAGING_DIR}/"
echo "      Done"

# ── 4. Upload remote helper script ──────────────────────────────────────────
echo "[3/4] Uploading and running remote helper in tmux session..."

# Write helper to a temp file locally, scp it, then execute in tmux
HELPER_LOCAL="$(mktemp /tmp/lcweb_helper_XXXXXX.sh)"
cat > "$HELPER_LOCAL" <<HELPER
#!/bin/bash
set -e
CONTAINER="${CONTAINER}"
STAGING="${STAGING_DIR}"
LOG="${REMOTE_LOG}"

exec > "\$LOG" 2>&1
echo "[remote] \$(date) — fast deploy started for \$CONTAINER"

echo "[remote] Copying files into container..."
docker cp "\${STAGING}/." "\${CONTAINER}:/app/"

echo "[remote] Clearing .pyc caches..."
docker exec "\$CONTAINER" bash -c "find /app -name '*.pyc' -delete; find /app -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null; true"

echo "[remote] Sending SIGHUP to gunicorn..."
GPID=\$(docker exec "\$CONTAINER" pgrep -x gunicorn 2>/dev/null | head -1 || true)
if [ -n "\$GPID" ]; then
  docker exec "\$CONTAINER" kill -HUP "\$GPID"
  echo "[remote] SIGHUP → gunicorn PID \$GPID"
else
  echo "[remote] No gunicorn master found; restarting container..."
  docker restart "\$CONTAINER"
fi

sleep 4
HEALTH=\$(docker inspect --format='{{.State.Health.Status}}' "\$CONTAINER" 2>/dev/null || echo unknown)
echo "[remote] Health: \$HEALTH"
echo "[remote] \$(date) — fast deploy complete"
HELPER

scp -q "$HELPER_LOCAL" "${SSH_HOST}:${REMOTE_HELPER}"
rm -f "$HELPER_LOCAL"
ssh "$SSH_HOST" "chmod +x ${REMOTE_HELPER}"

# Run inside a tmux session named lcweb_fast for resilience
SESSION="lcweb_fast"
ssh -o ServerAliveInterval=15 "$SSH_HOST" "
  tmux kill-session -t '$SESSION' 2>/dev/null || true
  tmux new-session -d -s '$SESSION' 'bash ${REMOTE_HELPER}; echo done > /tmp/lcweb_fast_done'
"

# Poll for completion (up to 60s)
echo "      Remote helper running in tmux session '$SESSION'..."
for i in $(seq 1 12); do
  sleep 5
  DONE=$(ssh "$SSH_HOST" "cat /tmp/lcweb_fast_done 2>/dev/null || echo ''")
  if [ "$DONE" = "done" ]; then break; fi
  echo "      Waiting... (${i}×5s)"
done
ssh "$SSH_HOST" "rm -f /tmp/lcweb_fast_done"

# Fetch remote log
echo ""
echo "=== Remote log ==="
ssh "$SSH_HOST" "cat '$REMOTE_LOG' 2>/dev/null || echo '(log not available)'"
echo "=== End remote log ==="
echo ""

# ── 5. Final health check ─────────────────────────────────────────────────────
echo "[4/4] Final health check..."
HEALTH=$(ssh "$SSH_HOST" "docker inspect --format='{{.State.Health.Status}}' '$CONTAINER' 2>/dev/null || echo unknown")
case "$HEALTH" in
  healthy) echo "      Container is HEALTHY." ;;
  unknown|no-healthcheck)
    RUNNING=$(ssh "$SSH_HOST" "docker ps --filter name=^/${CONTAINER}\$ --filter status=running --format '{{.Names}}' | grep -c . || echo 0")
    [ "$RUNNING" -ge 1 ] && echo "      Container is running (no healthcheck)." || echo "WARNING: Container appears stopped!"
    ;;
  *) echo "WARNING: Container health is '$HEALTH'. Check: ssh $SSH_HOST 'docker logs $CONTAINER --tail 50'" ;;
esac

echo ""
echo "=== Fast deploy complete ==="
echo "QA web    : http://10.0.0.101:${CONTAINER_PORT}"
echo "Local log : $LOG_FILE"
echo ""
echo "NOTE: This patched the running container only. Run 'deploy.sh qa' before promoting to prod."
