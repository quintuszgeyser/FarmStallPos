# Simple test script
Write-Host "`n=== Farm POS System Test ===" -ForegroundColor Cyan

# 1. Services
Write-Host "`n1. Services:" -ForegroundColor Yellow
Get-Service FarmPOS-qa, FarmPOS-Recognition | Select-Object Name, Status

# 2. Flask
Write-Host "`n2. Flask Test:" -ForegroundColor Yellow
try {
    $r = Invoke-RestMethod -Uri "http://127.0.0.1:5000/api/me"
    Write-Host "  Flask OK" -ForegroundColor Green
} catch {
    Write-Host "  Flask error: $_" -ForegroundColor Red
}

# 3. Customers
Write-Host "`n3. Customers:" -ForegroundColor Yellow
& .venv\Scripts\python.exe -c "from app import db, app, Customer; app.app_context().push(); print(f'  Total: {Customer.query.filter_by(active=True).count()}'); print(f'  Auto: {Customer.query.filter_by(active=True, auto_enrolled=True).count()}')"

# 4. Frigate
Write-Host "`n4. Recent Frigate Events:" -ForegroundColor Yellow
try {
    $events = Invoke-RestMethod -Uri "http://127.0.0.1:8971/api/events?limit=5"
    foreach ($e in $events) {
        Write-Host "  $($e.camera): $($e.label)"
    }
} catch {
    Write-Host "  Frigate error: $_" -ForegroundColor Red
}

# 5. Recognition logs
Write-Host "`n5. Recognition Service (last 10 lines):" -ForegroundColor Yellow
Get-Content logs\recognition_service.log -Tail 10

Write-Host "`n=== Next Steps ===" -ForegroundColor Cyan
Write-Host "Walk in front of indoor camera to test auto-enrollment" -ForegroundColor Yellow
Write-Host "Watch logs: Get-Content logs\recognition_service.log -Tail 20 -Wait" -ForegroundColor White
