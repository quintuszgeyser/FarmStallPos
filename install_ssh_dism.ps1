# Install OpenSSH using DISM (faster method)
# Run as Administrator

Write-Host "=== Installing OpenSSH Server ===" -ForegroundColor Green

Write-Host "`n[1/3] Installing via DISM..." -ForegroundColor Cyan
dism /Online /Add-Capability /CapabilityName:OpenSSH.Server~~~~0.0.1.0 /NoRestart

if ($LASTEXITCODE -eq 0) {
    Write-Host "  Installation successful!" -ForegroundColor Green
} else {
    Write-Host "  Installation may have failed (code: $LASTEXITCODE)" -ForegroundColor Yellow
    Write-Host "  Continuing anyway..." -ForegroundColor Yellow
}

Write-Host "`n[2/3] Starting service..." -ForegroundColor Cyan
Start-Service sshd
Set-Service -Name sshd -StartupType 'Automatic'
Write-Host "  Service started" -ForegroundColor Green

Write-Host "`n[3/3] Configuring firewall..." -ForegroundColor Cyan
New-NetFirewallRule -Name sshd -DisplayName 'OpenSSH Server (sshd)' -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22
Write-Host "  Firewall configured" -ForegroundColor Green

Write-Host "`nSetting default shell..." -ForegroundColor Cyan
New-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" -Name DefaultShell -Value "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -PropertyType String -Force | Out-Null

Write-Host "`n========================================" -ForegroundColor Green
Write-Host "SSH Setup Complete!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host "`nConnect with:" -ForegroundColor Cyan
Write-Host "  ssh Quintusz@100.86.32.13" -ForegroundColor Yellow
Write-Host ""
