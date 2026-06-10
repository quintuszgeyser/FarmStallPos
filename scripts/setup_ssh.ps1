# Setup SSH Server on Windows Mini PC
# Run this as Administrator on the Mini PC

Write-Host "=== Setting up OpenSSH Server on Mini PC ===" -ForegroundColor Green

# 1. Install OpenSSH Server (if not already installed)
Write-Host "`n[1/5] Installing OpenSSH Server..." -ForegroundColor Cyan
$sshServer = Get-WindowsCapability -Online | Where-Object Name -like 'OpenSSH.Server*'

if ($sshServer.State -eq "Installed") {
    Write-Host "  ✓ OpenSSH Server already installed" -ForegroundColor Green
} else {
    Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
    Write-Host "  ✓ OpenSSH Server installed" -ForegroundColor Green
}

# 2. Start SSH service and set to automatic
Write-Host "`n[2/5] Configuring SSH service..." -ForegroundColor Cyan
Start-Service sshd
Set-Service -Name sshd -StartupType 'Automatic'
Write-Host "  ✓ SSH service started and set to automatic" -ForegroundColor Green

# 3. Configure firewall
Write-Host "`n[3/5] Configuring firewall..." -ForegroundColor Cyan
$firewallRule = Get-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -ErrorAction SilentlyContinue

if ($firewallRule) {
    Write-Host "  ✓ Firewall rule already exists" -ForegroundColor Green
} else {
    New-NetFirewallRule -Name sshd -DisplayName 'OpenSSH Server (sshd)' -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22
    Write-Host "  ✓ Firewall rule created" -ForegroundColor Green
}

# 4. Get current user and Mini PC info
Write-Host "`n[4/5] Getting connection info..." -ForegroundColor Cyan
$currentUser = $env:USERNAME
$computerName = $env:COMPUTERNAME

# Get Tailscale IP
$tailscaleIP = (Get-NetIPAddress | Where-Object {$_.InterfaceAlias -like "*Tailscale*" -and $_.AddressFamily -eq "IPv4"}).IPAddress

if (-not $tailscaleIP) {
    Write-Host "  ⚠ Tailscale IP not found, checking all IPs..." -ForegroundColor Yellow
    $tailscaleIP = (Get-NetIPAddress | Where-Object {$_.IPAddress -like "100.*" -and $_.AddressFamily -eq "IPv4"}).IPAddress
}

Write-Host "  Computer: $computerName" -ForegroundColor White
Write-Host "  User: $currentUser" -ForegroundColor White
Write-Host "  Tailscale IP: $tailscaleIP" -ForegroundColor White

# 5. Set PowerShell as default shell (optional, recommended for Windows)
Write-Host "`n[5/5] Setting default shell..." -ForegroundColor Cyan
New-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" -Name DefaultShell -Value "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -PropertyType String -Force | Out-Null
Write-Host "  ✓ PowerShell set as default SSH shell" -ForegroundColor Green

# Summary
Write-Host "`n========================================" -ForegroundColor Green
Write-Host "SSH Setup Complete!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host "`nFrom your dev machine, connect with:" -ForegroundColor Cyan
Write-Host "  ssh $currentUser@$tailscaleIP" -ForegroundColor Yellow
Write-Host "`nFirst time connection:" -ForegroundColor Cyan
Write-Host "  1. You'll be asked to accept the host key (type 'yes')" -ForegroundColor White
Write-Host "  2. Enter your Windows password" -ForegroundColor White
Write-Host "`nTo monitor logs:" -ForegroundColor Cyan
Write-Host "  ssh $currentUser@$tailscaleIP 'Get-Content C:\Users\$currentUser\farm_pos_web\logs\pos.log -Tail 50'" -ForegroundColor White
Write-Host "`nTest connection:" -ForegroundColor Cyan
Write-Host "  ssh $currentUser@$tailscaleIP 'echo SSH working!'" -ForegroundColor White
Write-Host ""
