# Restart QA Flask and Recognition services to clear stuck state

Write-Host "Stopping services..." -ForegroundColor Yellow
Stop-Service FarmPOS-qa -ErrorAction SilentlyContinue
Stop-Service FarmPOS-Recognition -ErrorAction SilentlyContinue

Start-Sleep -Seconds 3

Write-Host "Starting services..." -ForegroundColor Yellow
Start-Service FarmPOS-qa
Start-Service FarmPOS-Recognition

Start-Sleep -Seconds 5

Write-Host "`nService Status:" -ForegroundColor Green
Get-Service FarmPOS-qa, FarmPOS-Recognition | Select-Object Name, Status

Write-Host "`nTesting Flask connection..." -ForegroundColor Green
$response = curl.exe -s http://127.0.0.1:5000/api/me 2>&1
Write-Host "Response: $response"

Write-Host "`nChecking Recognition logs..." -ForegroundColor Green
Get-Content "C:\Users\Quintusz\farm_pos_web\logs\recognition_service.log" -Tail 10
