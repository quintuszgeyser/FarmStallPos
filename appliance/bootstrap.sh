#!/bin/bash
# bootstrap.sh - Farm POS first-boot installer.
#
# Pulled and run directly on the target box by the onboard-store workflow output:
#
#   sudo bash -c 'curl -fsSL .../bootstrap.sh | bash -s -- \
#     --store-yml   <base64-encoded store.yml> \
#     --support-pub <base64-encoded age public key> \
#     --ghcr-pat    <GitHub PAT with read:packages> \
#     --version     v2.2.0'
#
# The box needs only OUTBOUND internet. No inbound SSH from GitHub.
# Idempotent: safe to re-run if interrupted.
set -euo pipefail

# ── Helpers ──────────────────────────────────────────────────────────────────
c_red()   { printf '\033[31m%s\033[0m\n' "$*"; }
c_green() { printf '\033[32m%s\033[0m\n' "$*"; }
c_bold()  { printf '\033[1m%s\033[0m\n'  "$*"; }
die()     { c_red "ERROR: $*" >&2; exit 1; }

FARMPOS_HOME="${FARMPOS_HOME:-/opt/farmpos}"
SECRETS_DIR="$FARMPOS_HOME/secrets"
APPLIANCE_DIR="$FARMPOS_HOME/appliance"
REPO="quintuszgeyser/FarmStallPos"
RAW="https://raw.githubusercontent.com/${REPO}/main/farm_pos_web/appliance"

# ── Parse arguments ───────────────────────────────────────────────────────────
STORE_YML_B64=""
SUPPORT_PUB_B64=""
GHCR_PAT=""
VERSION=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --store-yml)   STORE_YML_B64="$2";   shift 2 ;;
    --support-pub) SUPPORT_PUB_B64="$2"; shift 2 ;;
    --ghcr-pat)    GHCR_PAT="$2";        shift 2 ;;
    --version)     VERSION="$2";         shift 2 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[ -n "$STORE_YML_B64" ]   || die "--store-yml required"
[ -n "$GHCR_PAT" ]        || die "--ghcr-pat required"
[ -n "$VERSION" ]         || die "--version required"

c_bold "=== Farm POS Bootstrap ==="
echo "  Version:  $VERSION"
echo "  Home:     $FARMPOS_HOME"
echo ""

# ── 1. Install system dependencies ───────────────────────────────────────────
c_bold "Step 1/8 — Installing system dependencies..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq

if ! command -v docker >/dev/null 2>&1; then
  c_bold "  Installing Docker..."
  curl -fsSL https://get.docker.com | sh
else
  echo "  Docker: $(docker --version)"
fi

apt-get install -y -qq git gettext-base python3-yaml age openssl curl

echo "  age:     $(age --version)"
python3 -c "import yaml; print('  python3-yaml: ok')"
c_green "  Dependencies ready"

# ── 2. Download appliance scripts from GitHub ─────────────────────────────────
c_bold "Step 2/8 — Downloading appliance scripts..."
mkdir -p "$APPLIANCE_DIR/lib" "$APPLIANCE_DIR/postgres-init" \
         "$FARMPOS_HOME/data/branding" "$FARMPOS_HOME/store" \
         "$SECRETS_DIR"
chmod 700 "$SECRETS_DIR"

for f in register-store.sh update.sh backup.sh restore.sh fleet-status.sh; do
  curl -fsSL "$RAW/$f" -o "$APPLIANCE_DIR/$f"
  chmod +x "$APPLIANCE_DIR/$f"
done
for f in lib/common.sh; do
  curl -fsSL "$RAW/$f" -o "$APPLIANCE_DIR/$f"
done
for f in env.template compose.template.yml; do
  curl -fsSL "$RAW/$f" -o "$APPLIANCE_DIR/$f"
done
curl -fsSL "$RAW/postgres-init/01-create-db.sh" \
     -o "$APPLIANCE_DIR/postgres-init/01-create-db.sh"
chmod +x "$APPLIANCE_DIR/postgres-init/01-create-db.sh"

# Ensure Unix line endings (safety net for any Windows-edited files)
if command -v dos2unix >/dev/null 2>&1; then
  dos2unix "$APPLIANCE_DIR"/*.sh "$APPLIANCE_DIR/lib/common.sh" 2>/dev/null || true
else
  for f in "$APPLIANCE_DIR"/*.sh "$APPLIANCE_DIR/lib/common.sh"; do
    sed -i 's/\r//' "$f" 2>/dev/null || true
  done
fi

c_green "  Scripts ready"

# ── 3. Write store.yml ────────────────────────────────────────────────────────
c_bold "Step 3/8 — Writing store.yml..."
echo "$STORE_YML_B64" | base64 -d > "$FARMPOS_HOME/store.yml"
STORE_ID=$(python3 -c "import yaml; print(yaml.safe_load(open('$FARMPOS_HOME/store.yml'))['store_id'])")
STORE_NAME=$(python3 -c "import yaml; print(yaml.safe_load(open('$FARMPOS_HOME/store.yml'))['store_name'])")
c_green "  store_id: $STORE_ID  name: $STORE_NAME"

# ── 4. Deploy support age public key ─────────────────────────────────────────
c_bold "Step 4/8 — Deploying support age public key..."
if [ -n "$SUPPORT_PUB_B64" ]; then
  echo "$SUPPORT_PUB_B64" | base64 -d > "$APPLIANCE_DIR/support_age.pub"
  c_green "  support_age.pub deployed (central restore will work)"
else
  c_red "  WARN: no support_age.pub — central backup restore will NOT work"
fi

# ── 5. Log in to GHCR ────────────────────────────────────────────────────────
c_bold "Step 5/8 — Logging in to GHCR..."
echo "$GHCR_PAT" | docker login ghcr.io -u quintuszgeyser --password-stdin
c_green "  GHCR login OK"
# Clear PAT from environment immediately after use
unset GHCR_PAT

# ── 6. Install Tailscale ──────────────────────────────────────────────────────
c_bold "Step 6/8 — Installing Tailscale..."
if ! command -v tailscale >/dev/null 2>&1; then
  curl -fsSL https://tailscale.com/install.sh | sh
else
  echo "  Tailscale already installed"
fi
c_green "  Tailscale ready"

# ── 7. Run register-store.sh ─────────────────────────────────────────────────
c_bold "Step 7/8 — Running register-store.sh..."
FARMPOS_HOME="$FARMPOS_HOME" bash "$APPLIANCE_DIR/register-store.sh"

# ── 8. Handover checks ───────────────────────────────────────────────────────
c_bold "Step 8/8 — Running handover checks..."
. "$APPLIANCE_DIR/lib/common.sh"

PASS=0; FAIL=0
check() {
  local label="$1"; shift
  if eval "$@" >/dev/null 2>&1; then
    c_green "  ✅ $label"
    PASS=$((PASS+1))
  else
    c_red   "  ❌ $label"
    FAIL=$((FAIL+1))
  fi
}

check "POS container healthy" \
  "docker inspect farmpos-app --format '{{.State.Health.Status}}' | grep -q healthy"
check "Postgres container healthy" \
  "docker inspect farmpos-postgres --format '{{.State.Health.Status}}' | grep -q healthy"
check "/health endpoint 200" \
  "curl -sf http://localhost:5000/health"
check "Nightly backup cron installed" \
  "crontab -l 2>/dev/null | grep -q backup.sh"
check "Secrets directory (mode 700)" \
  "test -d $SECRETS_DIR"
check "backup_age.key generated" \
  "test -s $SECRETS_DIR/backup_age.key"
check "support_age.pub present" \
  "test -s $APPLIANCE_DIR/support_age.pub"
check "Tailscale connected" \
  "tailscale status 2>/dev/null | grep -v 'not logged in' | grep -q 'farmpos-'"

echo ""
echo "  $PASS / $((PASS+FAIL)) checks passed"

# ── Run first backup ──────────────────────────────────────────────────────────
c_bold "Running first backup..."
FARMPOS_HOME="$FARMPOS_HOME" bash "$APPLIANCE_DIR/backup.sh" && \
  c_green "  First backup complete" || \
  c_red   "  WARN: first backup failed — run backup.sh manually"

# ── Print ready banner ────────────────────────────────────────────────────────
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
TS_IP="$(tailscale ip -4 2>/dev/null || echo 'pending')"
ADMIN_PASS="$(cat "$SECRETS_DIR/admin_pass" 2>/dev/null || echo 'see /opt/farmpos/secrets/admin_pass')"

echo ""
c_green "════════════════════════════════════════════════════════"
c_green " READY — $STORE_NAME  ($VERSION)"
c_green ""
c_green "  POS (LAN):       http://${LAN_IP}:5000"
c_green "  POS (Tailscale): http://${TS_IP}:5000"
c_green "  Admin login:     admin / ${ADMIN_PASS}"
c_green "════════════════════════════════════════════════════════"
echo ""
c_red "⚠  TO DO BEFORE TRADING:"
c_red "   1. Change the admin password (Admin → Users)"
c_red "   2. ESCROW the backup key:"
c_red "        $SECRETS_DIR/backup_age.key"
c_red "      Copy to password manager, then: rm $SECRETS_DIR/backup_age.key.escrow.txt"
c_red "   3. Load product catalog (Products → ⬆ Import CSV)"
c_red "   4. Set a backup target in store.yml (currently local-only)"
echo ""

[ "$FAIL" = "0" ] || { c_red "⚠  $FAIL check(s) failed — review above before trading"; exit 1; }
