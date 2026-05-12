# Quick Start Script - Run this when you return
# Gets the auto-enrollment system up and running

Write-Host "`n╔════════════════════════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║     Farm POS Auto-Enrollment System - Quick Start        ║" -ForegroundColor Cyan
Write-Host "╚════════════════════════════════════════════════════════════╝`n" -ForegroundColor Cyan

# Step 1: Pull latest code
Write-Host "Step 1: Pulling latest code from GitHub..." -ForegroundColor Yellow
git pull origin main
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ⚠ Git pull failed. Continuing anyway..." -ForegroundColor Yellow
}

# Step 2: Check/download face models
Write-Host "`nStep 2: Checking face models..." -ForegroundColor Yellow
$modelDir = "$env:USERPROFILE\.insightface\models\buffalo_l"
$requiredFiles = @("det_10g.onnx", "genderage.onnx", "w600k_r50.onnx")
$missing = $false

foreach ($file in $requiredFiles) {
    if (-not (Test-Path "$modelDir\$file")) {
        $missing = $true
        break
    }
}

if ($missing) {
    Write-Host "  Face models missing. Downloading..." -ForegroundColor Yellow
    & .venv\Scripts\python.exe download_face_models.py
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ✗ Download failed. You may need to run this manually later." -ForegroundColor Red
    } else {
        Write-Host "  ✓ Face models downloaded" -ForegroundColor Green
    }
} else {
    Write-Host "  ✓ Face models already installed" -ForegroundColor Green
}

# Step 3: Restart services
Write-Host "`nStep 3: Restarting services..." -ForegroundColor Yellow
Stop-Service FarmPOS-qa -ErrorAction SilentlyContinue
Stop-Service FarmPOS-Recognition -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3
Start-Service FarmPOS-qa
Start-Service FarmPOS-Recognition
Start-Sleep -Seconds 5

Write-Host "  ✓ Services restarted" -ForegroundColor Green

# Step 4: Run system test
Write-Host "`nStep 4: Running system test..." -ForegroundColor Yellow
& .\test_full_system.ps1

# Step 5: Monitor recognition logs
Write-Host "`n╔════════════════════════════════════════════════════════════╗" -ForegroundColor Green
Write-Host "║                    System Ready!                          ║" -ForegroundColor Green
Write-Host "╚════════════════════════════════════════════════════════════╝`n" -ForegroundColor Green

Write-Host "Next: Walk in front of indoor camera to test auto-enrollment" -ForegroundColor Yellow
Write-Host ""
Write-Host "Watch logs live:" -ForegroundColor Cyan
Write-Host "  Get-Content logs\recognition_service.log -Tail 20 -Wait" -ForegroundColor White
Write-Host ""
Write-Host "Open POS in browser:" -ForegroundColor Cyan
Write-Host "  http://100.86.32.13:5000" -ForegroundColor White
Write-Host "  Login: admin / admin123" -ForegroundColor White
Write-Host ""
Write-Host "Check implementation status:" -ForegroundColor Cyan
Write-Host "  notepad IMPLEMENTATION_STATUS.md" -ForegroundColor White
Write-Host ""
