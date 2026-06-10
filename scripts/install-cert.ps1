# install-cert.ps1 — Install the self-signed cert into Windows Trusted Root so
# Edge/Chrome trust it without a warning. Run once (as admin) after any cert regeneration.
#
# Usage: powershell -ExecutionPolicy Bypass -File install-cert.ps1

$certPath = Join-Path $PSScriptRoot "cert.pem"

if (-not (Test-Path $certPath)) {
    Write-Host "cert.pem not found at $certPath" -ForegroundColor Red
    exit 1
}

Write-Host "Installing certificate into Trusted Root store..." -ForegroundColor Cyan

# Import into LocalMachine\Root (requires admin) — trusted by all users & all browsers
try {
    Import-Certificate -FilePath $certPath -CertStoreLocation Cert:\LocalMachine\Root | Out-Null
    Write-Host "Done. Edge and Chrome will now trust https://localhost:5443 and https://192.168.1.4:5443 without warnings." -ForegroundColor Green
    Write-Host "You may need to restart Edge once for the change to take effect." -ForegroundColor Yellow
} catch {
    Write-Host "Failed (not running as admin?): $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Re-run this script as Administrator:" -ForegroundColor Yellow
    Write-Host "  Right-click PowerShell -> Run as Administrator, then run:" -ForegroundColor Yellow
    Write-Host "  powershell -ExecutionPolicy Bypass -File install-cert.ps1" -ForegroundColor White
}
