#!/bin/bash
# bootstrap.sh - Farm POS first-boot installer.
#
# Invoked by the onboard-store workflow output command:
#
#   sudo bash -c 'echo "<base64-inner-script>" | base64 -d | bash'
#
# Secrets are passed via environment variables (never process arguments):
#   FARMPOS_STORE_YML     — base64-encoded store.yml
#   FARMPOS_SUPPORT_PUB   — base64-encoded age public key
#   FARMPOS_GHCR_PAT_B64  — base64-encoded GHCR PAT
#   FARMPOS_SSH_PUBKEY    — base64-encoded operator SSH public key
#   FARMPOS_VERSION       — image version tag (e.g. v2.2.0)
#
# Idempotent: safe to re-run if interrupted. If the session was lost mid-run,
# save secrets to a tempfile and re-source them:
#   sudo bash -c 'echo "<INNER_B64>" | base64 -d > /tmp/farmpos-resume.sh && bash /tmp/farmpos-resume.sh'
set -euo pipefail

# ── Root guard ────────────────────────────────────────────────────────────────
[ "$(id -u)" = "0" ] || { echo "ERROR: Must run as root (use sudo)" >&2; exit 1; }

# ── Helpers ───────────────────────────────────────────────────────────────────
c_red()   { printf '\033[31m%s\033[0m\n' "$*"; }
c_green() { printf '\033[32m%s\033[0m\n' "$*"; }
c_bold()  { printf '\033[1m%s\033[0m\n'  "$*"; }
die()     { c_red "ERROR: $*" >&2; exit 1; }

FARMPOS_HOME="${FARMPOS_HOME:-/opt/farmpos}"
SECRETS_DIR="$FARMPOS_HOME/secrets"
APPLIANCE_DIR="$FARMPOS_HOME/appliance"
REPO="quintuszgeyser/FarmStallPos"
RAW="https://raw.githubusercontent.com/${REPO}/main/appliance"

# ── Read secrets from environment (never from args) ───────────────────────────
STORE_YML_B64="${FARMPOS_STORE_YML:-}"
SUPPORT_PUB_B64="${FARMPOS_SUPPORT_PUB:-}"
GHCR_PAT_B64="${FARMPOS_GHCR_PAT_B64:-}"
SSH_PUBKEY_B64="${FARMPOS_SSH_PUBKEY:-}"
VERSION="${FARMPOS_VERSION:-}"

[ -n "$STORE_YML_B64" ] || die "FARMPOS_STORE_YML not set — re-paste the full bootstrap command"
[ -n "$GHCR_PAT_B64" ]  || die "FARMPOS_GHCR_PAT_B64 not set — re-paste the full bootstrap command"
[ -n "$VERSION" ]       || die "FARMPOS_VERSION not set — re-paste the full bootstrap command"

# Decode PAT immediately, clear the encoded form
GHCR_PAT=$(echo "$GHCR_PAT_B64" | base64 -d)
unset FARMPOS_GHCR_PAT_B64 GHCR_PAT_B64

c_bold "=== Farm POS Bootstrap ==="
echo "  Version:  $VERSION"
echo "  Home:     $FARMPOS_HOME"
echo ""

# ── Step 1 — Swap file (prevent OOM during image pull on low-RAM boxes) ───────
c_bold "Step 1/10 — Checking swap..."
TOTAL_RAM_MB=$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)
if [ "$(swapon --show | wc -l)" -le 1 ] && [ "$TOTAL_RAM_MB" -lt 4096 ]; then
  c_bold "  RAM: ${TOTAL_RAM_MB}MB — creating 2GB swap file..."
  if [ ! -f /swapfile ]; then
    fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
    chmod 600 /swapfile
    mkswap /swapfile -q
  fi
  swapon /swapfile 2>/dev/null || true
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  c_green "  Swap: $(free -h | awk '/Swap/ {print $2}')"
else
  c_green "  Swap OK (RAM: ${TOTAL_RAM_MB}MB, swap: $(free -h | awk '/Swap/ {print $2}'))"
fi

# ── Step 2 — Install system dependencies ─────────────────────────────────────
c_bold "Step 2/10 — Installing system dependencies..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq

if ! command -v docker >/dev/null 2>&1; then
  c_bold "  Installing Docker..."
  curl -fsSL https://get.docker.com | sh || true
  if ! command -v docker >/dev/null 2>&1; then
    c_bold "  Official script failed — falling back to docker.io (Ubuntu repos)..."
    apt-get install -y -qq docker.io docker-compose-v2 || apt-get install -y -qq docker.io
  fi
fi
if command -v docker >/dev/null 2>&1; then
  c_green "  Docker: $(docker --version)"
else
  die "Docker installation failed — cannot continue."
fi

apt-get install -y -qq git gettext-base python3-yaml age openssl curl rclone screen openssh-server

# age 1.2.0+ removed age-keygen as a standalone binary; shim it so register-store.sh
# finds it regardless of which package version is installed
if ! command -v age-keygen >/dev/null 2>&1 && command -v age >/dev/null 2>&1; then
  printf '#!/bin/bash\nexec age keygen "$@"\n' > /usr/local/bin/age-keygen
  chmod +x /usr/local/bin/age-keygen
  echo "  age-keygen: shimmed from 'age keygen'"
fi

echo "  age:     $(age --version)"
echo "  rclone:  $(rclone --version 2>/dev/null | head -1)"
python3 -c "import yaml; print('  python3-yaml: ok')"
c_green "  Dependencies ready"

# ── Step 3 — Download appliance scripts from GitHub ──────────────────────────
c_bold "Step 3/10 — Downloading appliance scripts..."
mkdir -p "$APPLIANCE_DIR/lib" "$APPLIANCE_DIR/postgres-init" \
         "$FARMPOS_HOME/data/branding" "$FARMPOS_HOME/store" \
         "$SECRETS_DIR"
chmod 700 "$SECRETS_DIR"

for f in register-store.sh update.sh backup.sh restore.sh fleet-status.sh; do
  curl -fsSL "$RAW/$f" -o "$APPLIANCE_DIR/$f"
  chmod +x "$APPLIANCE_DIR/$f"
done
curl -fsSL "$RAW/lib/common.sh"                 -o "$APPLIANCE_DIR/lib/common.sh"
curl -fsSL "$RAW/env.template"                  -o "$APPLIANCE_DIR/env.template"
curl -fsSL "$RAW/compose.template.yml"          -o "$APPLIANCE_DIR/compose.template.yml"
curl -fsSL "$RAW/postgres-init/01-create-db.sh" -o "$APPLIANCE_DIR/postgres-init/01-create-db.sh"
chmod +x "$APPLIANCE_DIR/postgres-init/01-create-db.sh"

# Strip Windows line endings (safety net)
for f in "$APPLIANCE_DIR"/*.sh "$APPLIANCE_DIR/lib/common.sh"; do
  sed -i 's/\r//' "$f" 2>/dev/null || true
done

c_green "  Scripts ready"

# ── Step 4 — Write store.yml ──────────────────────────────────────────────────
c_bold "Step 4/10 — Writing store.yml..."
echo "$STORE_YML_B64" | base64 -d > "$FARMPOS_HOME/store.yml"
unset FARMPOS_STORE_YML STORE_YML_B64
STORE_ID=$(python3 -c "import yaml; print(yaml.safe_load(open('$FARMPOS_HOME/store.yml'))['store_id'])")
STORE_NAME=$(python3 -c "import yaml; print(yaml.safe_load(open('$FARMPOS_HOME/store.yml'))['store_name'])")
c_green "  store_id: $STORE_ID  name: $STORE_NAME"

# ── Step 5 — Deploy support age public key ────────────────────────────────────
c_bold "Step 5/10 — Deploying support age public key..."
if [ -n "$SUPPORT_PUB_B64" ]; then
  echo "$SUPPORT_PUB_B64" | base64 -d > "$APPLIANCE_DIR/support_age.pub"
  unset FARMPOS_SUPPORT_PUB SUPPORT_PUB_B64
  c_green "  support_age.pub deployed (central restore will work)"
else
  c_red "  WARN: no support_age.pub — central backup restore will NOT work"
fi

# ── Step 6 — Install operator SSH public key + harden SSH ────────────────────
c_bold "Step 6/10 — Hardening SSH..."
if [ -n "$SSH_PUBKEY_B64" ]; then
  PUBKEY=$(echo "$SSH_PUBKEY_B64" | base64 -d)
  unset FARMPOS_SSH_PUBKEY SSH_PUBKEY_B64
  mkdir -p /root/.ssh
  chmod 700 /root/.ssh
  touch /root/.ssh/authorized_keys
  chmod 600 /root/.ssh/authorized_keys
  if ! grep -qF "$PUBKEY" /root/.ssh/authorized_keys 2>/dev/null; then
    echo "$PUBKEY" >> /root/.ssh/authorized_keys
    c_green "  Operator SSH key added"
  else
    c_green "  Operator SSH key already present (skipped)"
  fi
else
  c_red "  WARN: no SSH public key — password SSH will remain enabled"
fi

# Harden sshd: enable pubkey auth, disable password auth (safe only after key installed)
SSHD_CFG=/etc/ssh/sshd_config
if [ ! -f "$SSHD_CFG" ]; then
  c_bold "  openssh-server not found — installing..."
  apt-get install -y -qq openssh-server
fi
if [ -s /root/.ssh/authorized_keys ]; then
  sed -i 's/^#*PubkeyAuthentication.*/PubkeyAuthentication yes/'   "$SSHD_CFG"
  sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' "$SSHD_CFG"
  # Ensure the directives exist if sed matched nothing (fresh minimal image)
  grep -q '^PubkeyAuthentication'   "$SSHD_CFG" || echo 'PubkeyAuthentication yes'   >> "$SSHD_CFG"
  grep -q '^PasswordAuthentication' "$SSHD_CFG" || echo 'PasswordAuthentication no'  >> "$SSHD_CFG"
  systemctl reload sshd 2>/dev/null || service ssh reload 2>/dev/null || true
  c_green "  SSH hardened: PubkeyAuthentication yes, PasswordAuthentication no"
else
  c_red "  WARN: no authorized_keys — leaving PasswordAuthentication enabled"
fi

# ── Step 7 — Log in to GHCR ───────────────────────────────────────────────────
c_bold "Step 7/10 — Logging in to GHCR..."
echo "$GHCR_PAT" | docker login ghcr.io -u quintuszgeyser --password-stdin
unset GHCR_PAT
# Lock down the docker credential file (base64-encoded PAT, root-only)
chmod 600 /root/.docker/config.json 2>/dev/null || true
c_green "  GHCR login OK"

# ── Step 8 — Install Tailscale ────────────────────────────────────────────────
c_bold "Step 8/10 — Installing Tailscale..."
if ! command -v tailscale >/dev/null 2>&1; then
  curl -fsSL https://tailscale.com/install.sh | sh
else
  echo "  Tailscale already installed: $(tailscale --version | head -1)"
fi
c_green "  Tailscale ready"

# ── Step 9 — Run register-store.sh ───────────────────────────────────────────
c_bold "Step 9/10 — Running register-store.sh..."
FARMPOS_HOME="$FARMPOS_HOME" bash "$APPLIANCE_DIR/register-store.sh"

# ── Step 10 — Handover checks ─────────────────────────────────────────────────
c_bold "Step 10/10 — Running handover checks..."
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
check "Operator SSH key installed" \
  "test -s /root/.ssh/authorized_keys"
check "Password SSH disabled" \
  "grep -q '^PasswordAuthentication no' /etc/ssh/sshd_config"
check "rclone available" \
  "command -v rclone"

echo ""
echo "  $PASS / $((PASS+FAIL)) checks passed"

# ── Log rotation ──────────────────────────────────────────────────────────────
c_bold "Installing log rotation..."
cat > /etc/logrotate.d/farmpos <<LOGROTATE
${FARMPOS_HOME}/data/backup.log
${FARMPOS_HOME}/data/pos-logs/*.log
${FARMPOS_HOME}/data/web-logs/*.log
{
    daily
    missingok
    rotate 14
    compress
    delaycompress
    notifempty
    copytruncate
}
LOGROTATE
c_green "  Log rotation configured (14-day daily)"

# ── First backup ──────────────────────────────────────────────────────────────
c_bold "Running first backup..."
FARMPOS_HOME="$FARMPOS_HOME" bash "$APPLIANCE_DIR/backup.sh" && \
  c_green "  First backup complete" || \
  c_red   "  WARN: first backup failed — run backup.sh manually"

# ── Ready banner ──────────────────────────────────────────────────────────────
# Pick the LAN IP — prefer the first non-Docker, non-loopback IPv4 address
LAN_IP=$(ip -4 addr show scope global | \
  awk '/inet / {print $2}' | \
  cut -d/ -f1 | \
  grep -v '^172\.' | \
  grep -v '^10\.255\.' | \
  head -1)
LAN_IP="${LAN_IP:-$(hostname -I 2>/dev/null | awk '{print $1}')}"

TS_IP="$(tailscale ip -4 2>/dev/null || echo 'pending — check: tailscale status')"
ADMIN_PASS="$(cat "$SECRETS_DIR/admin_pass" 2>/dev/null || echo "see $SECRETS_DIR/admin_pass")"

echo ""
c_green "════════════════════════════════════════════════════════"
c_green " READY — $STORE_NAME  ($VERSION)"
c_green ""
c_green "  POS (LAN):       http://${LAN_IP}:5000"
c_green "  POS (Tailscale): http://${TS_IP}:5000"
c_green "  Admin login:     admin / ${ADMIN_PASS}"
c_green ""
c_green "  SSH (LAN):       ssh root@${LAN_IP}"
c_green "  SSH (Tailscale): ssh root@${TS_IP}"
c_green "════════════════════════════════════════════════════════"
echo ""
c_red "⚠  BEFORE TRADING:"
c_red "   1. Change the admin password (Admin → Users)"
c_red "   2. ESCROW the backup key:"
c_red "        $SECRETS_DIR/backup_age.key"
c_red "      Copy to password manager, then delete:"
c_red "        rm $SECRETS_DIR/backup_age.key.escrow.txt"
c_red "   3. Load product catalog (Products → ⬆ Import CSV)"
c_red "   4. Set off-box backup target in store.yml (currently local-only)"
c_red "      Then run: bash $APPLIANCE_DIR/update.sh"
echo ""

[ "$FAIL" = "0" ] || { c_red "⚠  $FAIL check(s) failed — review above before trading"; exit 1; }
