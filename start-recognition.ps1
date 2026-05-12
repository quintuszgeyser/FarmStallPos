# start-recognition.ps1
# Manual test script for the recognition service (not for production use)
# For production, use the Windows service via register-recognition-service.ps1

$ErrorActionPreference = "Stop"

Write-Host "Starting recognition service in test mode..." -ForegroundColor Yellow
Write-Host "Press Ctrl+C to stop" -ForegroundColor Yellow
Write-Host ""

$env:POS_URL         = "http://127.0.0.1:5000"
$env:POS_USER        = "admin"
$env:POS_PASS        = "admin123"
$env:FRIGATE_URL     = "http://127.0.0.1:8971"
$env:WEBHOOK_PORT    = "8080"
$env:FACE_THRESHOLD  = "0.40"
$env:GAIT_THRESHOLD  = "0.25"

& .\.venv\Scripts\python.exe recognition_service.py
