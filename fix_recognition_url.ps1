# Fix Recognition Service to use HTTPS for QA
# Run on Mini PC

Write-Host "Fixing Recognition Service URL configuration..." -ForegroundColor Yellow

$xmlPath = "C:\Users\Quintusz\farm_pos_web\tools\FarmPOS-Recognition.xml"

if (-not (Test-Path $xmlPath)) {
    Write-Host "Error: Cannot find $xmlPath" -ForegroundColor Red
    exit 1
}

# Read the XML
$content = Get-Content $xmlPath -Raw

# Replace HTTP with HTTPS
$newContent = $content -replace 'http://127.0.0.1:5000', 'https://127.0.0.1:5000'

# Write back
Set-Content $xmlPath -Value $newContent

Write-Host "Updated POS_URL to: https://127.0.0.1:5000" -ForegroundColor Green

# Show the change
Write-Host "`nRelevant lines:" -ForegroundColor Cyan
Get-Content $xmlPath | Select-String "POS_URL"

# Restart service
Write-Host "`nRestarting Recognition Service..." -ForegroundColor Yellow
Restart-Service FarmPOS-Recognition

Write-Host "`nDone! Check logs in a few seconds:" -ForegroundColor Green
Write-Host "  Get-Content logs\recognition_service.log -Tail 20 -Wait" -ForegroundColor White
