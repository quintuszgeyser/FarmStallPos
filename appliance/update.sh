#!/bin/bash
# update.sh - pull-based in-place update of a store box to the version pinned in store.yml.
#
#   1. edit /opt/farmpos/store.yml  ->  farmpos_version: "v2.1.2"
#   2. sudo /opt/farmpos/appliance/update.sh
#
# Re-renders .env from store.yml (secrets untouched), pulls the new pinned image, restarts
# ONLY the pos container, and health-gates. If the new image never passed CI it was never
# pushed, so 'docker compose pull' fails loudly and the old container keeps running. If the
# new container fails its health check, this exits non-zero - the operator can roll back by
# restoring the previous farmpos_version and re-running.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FARMPOS_HOME="${FARMPOS_HOME:-/opt/farmpos}"
. "$HERE/lib/common.sh"
cd "$FARMPOS_HOME"

NEW_VER="$(yaml_get store.yml farmpos_version)"
[ -n "$NEW_VER" ] || die "farmpos_version missing in store.yml"
echo "$NEW_VER" | grep -qv 'latest' || die "refusing to run ':latest' - pin a real version"

c_bold "Updating $(yaml_get store.yml store_id) -> $NEW_VER"

# Snapshot before a version change (cheap insurance; backup.sh has the size/validity guards).
if command -v "$HERE/backup.sh" >/dev/null 2>&1 || [ -x "$HERE/backup.sh" ]; then
  c_bold "Taking a pre-update backup..."
  "$HERE/backup.sh" || c_red "WARN: pre-update backup failed - continuing, but no fresh snapshot."
fi

# Re-render .env + compose from current store.yml (no secret regen, no DB init).
"$HERE/register-store.sh" --update-only

# Update the web container too if the store runs the web shop, so a version bump
# ships POS + web together (matches how register-store.sh brings the stack up).
WEB_ENABLED="$(yaml_get store.yml web_shop.enabled)"
if [ "$WEB_ENABLED" = "True" ] || [ "$WEB_ENABLED" = "true" ]; then
  docker compose --profile web pull pos web
  docker compose --profile web up -d --no-deps pos web
else
  docker compose pull pos
  docker compose up -d --no-deps pos
fi

c_bold "Waiting for POS to become healthy..."
ok=0
for i in $(seq 1 45); do
  code="$(curl -s -o /dev/null -w '%{http_code}' http://localhost:5000/health || true)"
  [ "$code" = "200" ] && { ok=1; break; }
  sleep 2
done
if [ "$ok" != "1" ]; then
  docker compose logs --tail 40 pos
  die "POS unhealthy after update to $NEW_VER. Roll back: set farmpos_version back in store.yml and re-run update.sh."
fi

# Web health-gate (only if the web shop runs on this box).
if [ "$WEB_ENABLED" = "True" ] || [ "$WEB_ENABLED" = "true" ]; then
  c_bold "Waiting for web shop to become healthy..."
  wok=0
  for i in $(seq 1 45); do
    code="$(curl -s -o /dev/null -w '%{http_code}' http://localhost:5001/health || true)"
    [ "$code" = "200" ] && { wok=1; break; }
    sleep 2
  done
  if [ "$wok" != "1" ]; then
    docker compose logs --tail 40 web
    die "Web shop unhealthy after update to $NEW_VER. POS is up; investigate web, or roll back."
  fi
  c_green "Updated POS + web to $NEW_VER and healthy."
else
  c_green "Updated to $NEW_VER and healthy."
fi
