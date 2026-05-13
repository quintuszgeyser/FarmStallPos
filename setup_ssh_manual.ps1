# Manual SSH Setup - Step by step
# Run this as Administrator

Write-Host "=== Manual SSH Setup ===" -ForegroundColor Green
Write-Host "This will guide you through each step manually`n" -ForegroundColor Yellow

# Step 1: Install (if needed)
Write-Host "[Step 1] Install OpenSSH Server" -ForegroundColor Cyan
Write-Host "Run this command in a separate admin PowerShell:" -ForegroundColor White
Write-Host "  Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0" -ForegroundColor Yellow
Write-Host ""
Read-Host "Press Enter when installation is complete (or if already installed)"

# Step 2: Start service
Write-Host "`n[Step 2] Start SSH Service" -ForegroundColor Cyan
try {
    Start-Service sshd -ErrorAction Stop
    Write-Host "  Service started successfully" -ForegroundColor Green
} catch {
    Write-Host "  Error: $_" -ForegroundColor Red
    Write-Host "  Try manually: Start-Service sshd" -ForegroundColor Yellow
}

# Step 3: Set to automatic
Write-Host "`n[Step 3] Set to Automatic" -ForegroundColor Cyan
try {
    Set-Service -Name sshd -StartupType 'Automatic' -ErrorAction Stop
    Write-Host "  Set to automatic successfully" -ForegroundColor Green
} catch {
    Write-Host "  Error: $_" -ForegroundColor Red
}

# Step 4: Firewall
Write-Host "`n[Step 4] Configure Firewall" -ForegroundColor Cyan
$rule = Get-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -ErrorAction SilentlyContinue
if ($rule) {
    Write-Host "  Firewall rule already exists" -ForegroundColor Green
} else {
    try {
        New-NetFirewallRule -Name sshd -DisplayName 'OpenSSH Server (sshd)' -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 -ErrorAction Stop
        Write-Host "  Firewall rule created" -ForegroundColor Green
    } catch {
        Write-Host "  Error: $_" -ForegroundColor Red
    }
}

# Step 5: Default shell
Write-Host "`n[Step 5] Set Default Shell" -ForegroundColor Cyan
try {
    New-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" -Name DefaultShell -Value "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -PropertyType String -Force -ErrorAction Stop | Out-Null
    Write-Host "  PowerShell set as default shell" -ForegroundColor Green
} catch {
    Write-Host "  Warning: $_" -ForegroundColor Yellow
}

# Show connection info
Write-Host "`n========================================" -ForegroundColor Green
Write-Host "Connection Info" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
$currentUser = $env:USERNAME
$tailscaleIP = (Get-NetIPAddress | Where-Object { $_.IPAddress -like "100.*" -and $_.AddressFamily -eq "IPv4" }).IPAddress
Write-Host "  ssh $currentUser@$tailscaleIP" -ForegroundColor Yellow
Write-Host ""
