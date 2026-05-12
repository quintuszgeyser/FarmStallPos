# Comprehensive test script for auto-enrollment system
# Run on Mini PC after services restart

Write-Host "=" -ForegroundColor Cyan
Write-Host "Auto-Enrollment System Test" -ForegroundColor Cyan
Write-Host "=" * 80 -ForegroundColor Cyan

# 1. Check services
Write-Host "`n1. Service Status:" -ForegroundColor Yellow
Get-Service FarmPOS-qa, FarmPOS-Recognition | Select-Object Name, Status

# 2. Test Flask connection
Write-Host "`n2. Flask Connection Test:" -ForegroundColor Yellow
try {
    $response = Invoke-RestMethod -Uri "http://127.0.0.1:5000/api/me" -Method GET
    Write-Host "  ✓ Flask responding" -ForegroundColor Green
} catch {
    Write-Host "  ✗ Flask not responding: $_" -ForegroundColor Red
}

# 3. Test database connection
Write-Host "`n3. Database Test:" -ForegroundColor Yellow
& .venv\Scripts\python.exe check_db_connection.py

# 4. Check face models
Write-Host "`n4. Face Models Check:" -ForegroundColor Yellow
$modelDir = "$env:USERPROFILE\.insightface\models\buffalo_l"
if (Test-Path $modelDir) {
    $files = Get-ChildItem $modelDir
    Write-Host "  ✓ Model directory exists: $($files.Count) files" -ForegroundColor Green
} else {
    Write-Host "  ✗ Models not found. Run: .venv\Scripts\python.exe download_face_models.py" -ForegroundColor Red
}

# 5. Check Frigate events
Write-Host "`n5. Recent Frigate Events:" -ForegroundColor Yellow
try {
    $events = Invoke-RestMethod -Uri "http://127.0.0.1:8971/api/events?limit=5"
    foreach ($event in $events) {
        $label = $event.label
        $camera = $event.camera
        $time = [System.DateTimeOffset]::FromUnixTimeSeconds($event.start_time).LocalDateTime
        Write-Host "  - $camera : $label at $time"
    }
} catch {
    Write-Host "  ✗ Frigate not responding: $_" -ForegroundColor Red
}

# 6. Check recognition service logs
Write-Host "`n6. Recognition Service (last 10 lines):" -ForegroundColor Yellow
Get-Content "C:\Users\Quintusz\farm_pos_web\logs\recognition_service.log" -Tail 10 -ErrorAction SilentlyContinue

# 7. Check customers in database
Write-Host "`n7. Customer Count:" -ForegroundColor Yellow
& .venv\Scripts\python.exe -c @"
from app import db, app, Customer
with app.app_context():
    total = Customer.query.filter_by(active=True).count()
    auto = Customer.query.filter_by(active=True, auto_enrolled=True).count()
    manual = total - auto
    print(f'  Total customers: {total}')
    print(f'    Auto-enrolled: {auto}')
    print(f'    Manual: {manual}')
"@

# 8. Test till detection API
Write-Host "`n8. Till Detection Test:" -ForegroundColor Yellow
try {
    $response = Invoke-RestMethod -Uri "http://127.0.0.1:5000/api/till/active_customer" -Method GET `
        -Headers @{"Cookie" = "session=test"}  # Will fail auth but tests endpoint exists
} catch {
    if ($_.Exception.Response.StatusCode -eq 401) {
        Write-Host "  ✓ Endpoint exists (auth required)" -ForegroundColor Green
    } else {
        Write-Host "  ✗ Endpoint error: $_" -ForegroundColor Red
    }
}

Write-Host "`n" + "=" * 80 -ForegroundColor Cyan
Write-Host "Test Complete" -ForegroundColor Cyan
Write-Host "=" * 80 -ForegroundColor Cyan

Write-Host "`nNext Steps:" -ForegroundColor Yellow
Write-Host "1. If Flask not responding: Restart-Service FarmPOS-qa"
Write-Host "2. If face models missing: .venv\Scripts\python.exe download_face_models.py"
Write-Host "3. If no person detections: Check Frigate camera zones"
Write-Host "4. Walk in front of indoor camera and check logs for auto-enrollment"
