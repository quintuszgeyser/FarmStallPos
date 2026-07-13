# quick-deploy-qa.ps1 — Hot-patch QA container with local changes. ~30 seconds.
#
# Copies changed files directly into the running qa-farmpos-app container.
# The Docker image is NOT rebuilt — changes are ephemeral (lost on next full QA build).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File quick-deploy-qa.ps1
#
# When done testing, do the real QA build:
#   git add -A && git commit -m "..." && git push origin main
#   ssh farmpc 'cd ~/farmpos-docker && bash deploy.sh qa'

$ErrorActionPreference = 'Stop'
$App = $PSScriptRoot   # = farm_pos_web/

Write-Host ""
Write-Host "=== Quick QA Deploy ===" -ForegroundColor Cyan
Write-Host "Hot-patching qa-farmpos-app (no Docker rebuild)..." -ForegroundColor Gray
Write-Host ""

# ── 1. Create temp staging dir on server ─────────────────────────────────────
Write-Host "[1/4] Preparing server staging dir..." -ForegroundColor Yellow
& ssh farmpc 'rm -rf /tmp/fq && mkdir -p /tmp/fq/static /tmp/fq/templates /tmp/fq/blueprints /tmp/fq/services'

# ── 2. Upload source files ────────────────────────────────────────────────────
Write-Host "[2/4] Uploading source files..." -ForegroundColor Yellow

# Core Python files
& scp "$App\app.py" "$App\models.py" "$App\helpers.py" "farmpc:/tmp/fq/"

# Blueprints (all .py files)
& scp "$App\blueprints\*.py" "farmpc:/tmp/fq/blueprints/"

# Services (if exists)
if (Test-Path "$App\services") {
    & scp "$App\services\*.py" "farmpc:/tmp/fq/services/" 2>$null
}

# Templates
& scp "$App\templates\*.html" "farmpc:/tmp/fq/templates/"

# Static root files only (product_images / supplier_docs / branding are volume mounts — skip them)
$staticFiles = Get-ChildItem "$App\static" -File
foreach ($f in $staticFiles) {
    & scp $f.FullName "farmpc:/tmp/fq/static/"
}

# ── 3. docker cp into the running QA container ───────────────────────────────
Write-Host "[3/4] Patching container files..." -ForegroundColor Yellow
& ssh farmpc @'
set -e
docker cp /tmp/fq/templates/. qa-farmpos-app:/app/templates/
docker cp /tmp/fq/blueprints/. qa-farmpos-app:/app/blueprints/
docker cp /tmp/fq/static/.    qa-farmpos-app:/app/static/
for f in app.py models.py helpers.py; do
    [ -f "/tmp/fq/$f" ] && docker cp /tmp/fq/$f qa-farmpos-app:/app/$f && echo "  copied $f"
done
if ls /tmp/fq/services/*.py 2>/dev/null | head -1 | grep -q .; then
    docker cp /tmp/fq/services/. qa-farmpos-app:/app/services/
    echo "  copied services/"
fi
echo "  Files patched."
'@

# ── 4. Restart QA container (fast — no rebuild, just restarts Flask) ──────────
Write-Host "[4/4] Restarting QA container..." -ForegroundColor Yellow
& ssh farmpc 'docker restart qa-farmpos-app'

# Wait a moment and show status
Start-Sleep -Seconds 4
$status = & ssh farmpc 'docker inspect qa-farmpos-app --format "{{.State.Status}} (health: {{.State.Health.Status}})"' 2>&1
Write-Host ""
Write-Host "Container: $status" -ForegroundColor Gray
Write-Host ""
Write-Host "QA ready at: http://10.0.0.101:5100" -ForegroundColor Green
Write-Host ""
Write-Host "NOTE: This patches the live container only — Docker image unchanged." -ForegroundColor DarkGray
Write-Host "When happy, do the full build:" -ForegroundColor DarkGray
Write-Host "  git add -A && git commit -m '...' && git push origin main" -ForegroundColor DarkGray
Write-Host "  ssh farmpc 'cd ~/farmpos-docker && bash deploy.sh qa'" -ForegroundColor DarkGray
Write-Host ""
