# Quick Start Script - Simplified version
Write-Host "`nFarm POS Auto-Enrollment - Quick Start`n" -ForegroundColor Cyan

# Step 1: Pull latest code
Write-Host "Step 1: Pulling latest code..." -ForegroundColor Yellow
git pull origin main

# Step 2: Download face models
Write-Host "`nStep 2: Checking face models..." -ForegroundColor Yellow
$modelDir = "$env:USERPROFILE\.insightface\models\buffalo_l"
if (-not (Test-Path "$modelDir\det_10g.onnx")) {
    Write-Host "  Downloading face models..." -ForegroundColor Yellow
    & .venv\Scripts\python.exe download_face_models.py
} else {
    Write-Host "  Face models already installed" -ForegroundColor Green
}

# Step 3: Restart services
Write-Host "`nStep 3: Restarting services..." -ForegroundColor Yellow
Restart-Service FarmPOS-qa
Restart-Service FarmPOS-Recognition
Start-Sleep -Seconds 5

# Step 4: Test
Write-Host "`nStep 4: Running diagnostics..." -ForegroundColor Yellow
& .\test_full_system.ps1

Write-Host "`n=== System Ready! ===" -ForegroundColor Green
Write-Host "Next: Walk in front of indoor camera`n" -ForegroundColor Yellow
