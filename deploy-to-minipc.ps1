# deploy-to-minipc.ps1
# Quick deployment script to copy recognition service files to Mini PC via Tailscale
# Run this from your dev machine, NOT on the Mini PC
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File deploy-to-minipc.ps1

$MINI_PC_IP = "100.86.32.13"
$REMOTE_PATH = "\\$MINI_PC_IP\C$\path\to\farm_pos_web"  # Update this path!

Write-Host "Deploying recognition service to Mini PC at $MINI_PC_IP..." -ForegroundColor Yellow
Write-Host ""

# Files to copy
$files = @(
    "recognition_service.py",
    "requirements_recognition.txt",
    "install-recognition.ps1",
    "start-recognition.ps1",
    "register-recognition-service.ps1",
    "RECOGNITION_SETUP.md"
)

Write-Host "Files to copy:" -ForegroundColor Cyan
$files | ForEach-Object { Write-Host "  - $_" }
Write-Host ""

# Prompt for remote path if not set
if ($REMOTE_PATH -eq "\\$MINI_PC_IP\C$\path\to\farm_pos_web") {
    Write-Host "ERROR: Update the REMOTE_PATH variable in this script first!" -ForegroundColor Red
    Write-Host ""
    Write-Host "Set it to the UNC path of your farm_pos_web folder, e.g.:" -ForegroundColor Yellow
    Write-Host "  \\$MINI_PC_IP\C$\Users\YourUser\farm_pos_web" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Or use Remote Desktop to copy files manually:" -ForegroundColor Yellow
    Write-Host "  mstsc /v:$MINI_PC_IP" -ForegroundColor Cyan
    exit 1
}

# Check if remote is accessible
if (-not (Test-Path $REMOTE_PATH)) {
    Write-Host "ERROR: Cannot access $REMOTE_PATH" -ForegroundColor Red
    Write-Host ""
    Write-Host "Make sure:" -ForegroundColor Yellow
    Write-Host "  1. File sharing is enabled on the Mini PC"
    Write-Host "  2. You have admin access to the Mini PC"
    Write-Host "  3. Tailscale is connected on both machines"
    Write-Host ""
    Write-Host "Alternatively, use Remote Desktop:" -ForegroundColor Yellow
    Write-Host "  mstsc /v:$MINI_PC_IP" -ForegroundColor Cyan
    exit 1
}

# Copy files
$success = 0
$failed = 0

foreach ($file in $files) {
    if (Test-Path $file) {
        try {
            Copy-Item $file $REMOTE_PATH -Force
            Write-Host "[OK] $file" -ForegroundColor Green
            $success++
        } catch {
            Write-Host "[FAIL] $file - $_" -ForegroundColor Red
            $failed++
        }
    } else {
        Write-Host "[SKIP] $file - not found" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "Deployment complete: $success copied, $failed failed" -ForegroundColor Cyan
Write-Host ""
Write-Host "Next steps on the Mini PC ($MINI_PC_IP):" -ForegroundColor Yellow
Write-Host "  1. Remote Desktop: mstsc /v:$MINI_PC_IP" -ForegroundColor Cyan
Write-Host "  2. Open PowerShell as Administrator"
Write-Host "  3. cd to farm_pos_web folder"
Write-Host "  4. Run: .\install-recognition.ps1" -ForegroundColor Cyan
Write-Host "  5. Test: .\start-recognition.ps1" -ForegroundColor Cyan
Write-Host "  6. Register service: .\register-recognition-service.ps1" -ForegroundColor Cyan
Write-Host ""
Write-Host "See RECOGNITION_SETUP.md for full guide" -ForegroundColor Green
