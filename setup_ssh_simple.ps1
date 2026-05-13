# Setup SSH Server on Windows Mini PC
# Run this as Administrator on the Mini PC

Write-Host "=== Setting up OpenSSH Server on Mini PC ===" -ForegroundColor Green

# 1. Install OpenSSH Server
Write-Host "`n[1/5] Installing OpenSSH Server..." -ForegroundColor Cyan
$sshServer = Get-WindowsCapability -Online | Where-Object { $_.Name -like 'OpenSSH.Server*' }

if ($sshServer.State -eq "Installed")
{
    Write-Host "  Already installed" -ForegroundColor Green
}
else
{
    Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
    Write-Host "  Installed" -ForegroundColor Green
}

# 2. Start SSH service
Write-Host "`n[2/5] Starting SSH service..." -ForegroundColor Cyan
Start-Service sshd
Set-Service -Name sshd -StartupType 'Automatic'
Write-Host "  Service started" -ForegroundColor Green

# 3. Configure firewall
Write-Host "`n[3/5] Configuring firewall..." -ForegroundColor Cyan
$firewallRule = Get-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -ErrorAction SilentlyContinue

if ($firewallRule)
{
    Write-Host "  Firewall rule exists" -ForegroundColor Green
}
else
{
    New-NetFirewallRule -Name sshd -DisplayName 'OpenSSH Server (sshd)' -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22
    Write-Host "  Firewall rule created" -ForegroundColor Green
}

# 4. Get connection info
Write-Host "`n[4/5] Getting connection info..." -ForegroundColor Cyan
$currentUser = $env:USERNAME
$computerName = $env:COMPUTERNAME

$tailscaleIP = (Get-NetIPAddress | Where-Object { $_.InterfaceAlias -like "*Tailscale*" -and $_.AddressFamily -eq "IPv4" }).IPAddress

if (-not $tailscaleIP)
{
    Write-Host "  Checking for 100.x IP..." -ForegroundColor Yellow
    $tailscaleIP = (Get-NetIPAddress | Where-Object { $_.IPAddress -like "100.*" -and $_.AddressFamily -eq "IPv4" }).IPAddress
}

Write-Host "  Computer: $computerName" -ForegroundColor White
Write-Host "  User: $currentUser" -ForegroundColor White
Write-Host "  Tailscale IP: $tailscaleIP" -ForegroundColor White

# 5. Set PowerShell as default shell
Write-Host "`n[5/5] Setting default shell..." -ForegroundColor Cyan
New-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" -Name DefaultShell -Value "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -PropertyType String -Force | Out-Null
Write-Host "  PowerShell set as default" -ForegroundColor Green

# Summary
Write-Host "`n========================================" -ForegroundColor Green
Write-Host "SSH Setup Complete!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host "`nConnect with:" -ForegroundColor Cyan
Write-Host "  ssh $currentUser@$tailscaleIP" -ForegroundColor Yellow
Write-Host ""
