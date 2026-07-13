#!/bin/bash
# quick-deploy-qa.sh — Hot-patch QA container. ~30 seconds.
# Usage: bash quick-deploy-qa.sh

set -e
APP="$(cd "$(dirname "$0")" && pwd)"  # farm_pos_web/

echo ""
echo "=== Quick QA Deploy ==="
echo "Hot-patching qa-farmpos-app (no Docker rebuild)..."
echo ""

echo "[1/4] Preparing server staging dir..."
ssh farmpc 'rm -rf /tmp/fq && mkdir -p /tmp/fq/static /tmp/fq/templates /tmp/fq/blueprints /tmp/fq/services'

echo "[2/4] Uploading source files..."
scp "$APP/app.py" "$APP/models.py" "$APP/helpers.py" farmpc:/tmp/fq/
scp "$APP/blueprints/"*.py farmpc:/tmp/fq/blueprints/
scp "$APP/templates/"*.html farmpc:/tmp/fq/templates/
# Static root files only (skip volume-mounted subdirs)
find "$APP/static" -maxdepth 1 -type f | xargs -I{} scp {} farmpc:/tmp/fq/static/
# Services if present
[ -d "$APP/services" ] && scp "$APP/services/"*.py farmpc:/tmp/fq/services/ 2>/dev/null || true

echo "[3/4] Patching container files..."
ssh farmpc '
set -e
docker cp /tmp/fq/templates/. qa-farmpos-app:/app/templates/
docker cp /tmp/fq/blueprints/. qa-farmpos-app:/app/blueprints/
docker cp /tmp/fq/static/.    qa-farmpos-app:/app/static/
for f in app.py models.py helpers.py; do
    [ -f "/tmp/fq/$f" ] && docker cp /tmp/fq/$f qa-farmpos-app:/app/$f && echo "  copied $f"
done
ls /tmp/fq/services/*.py 2>/dev/null | head -1 | grep -q . && docker cp /tmp/fq/services/. qa-farmpos-app:/app/services/ && echo "  copied services/" || true
echo "  Files patched."
'

echo "[4/4] Restarting QA container..."
ssh farmpc 'docker restart qa-farmpos-app'
sleep 4
ssh farmpc 'docker inspect qa-farmpos-app --format "  Container: {{.State.Status}} (health: {{.State.Health.Status}})"'

echo ""
echo "QA ready: http://10.0.0.101:5100"
echo ""
echo "When happy, full build:"
echo "  git add -A && git commit -m '...' && git push origin main"
echo "  ssh farmpc 'cd ~/farmpos-docker && bash deploy.sh qa'"
echo ""
