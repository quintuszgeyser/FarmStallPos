# Check SSH status without installing

Write-Host "=== Checking SSH Status ===" -ForegroundColor Green

# Check if SSH is already installed
Write-Host "`nChecking OpenSSH Server..." -ForegroundColor Cyan
$sshServer = Get-WindowsCapability -Online | Where-Object { $_.Name -like 'OpenSSH.Server*' }
Write-Host "  State: $($sshServer.State)" -ForegroundColor White

# Check if service exists
Write-Host "`nChecking SSH service..." -ForegroundColor Cyan
$service = Get-Service sshd -ErrorAction SilentlyContinue
if ($service) {
    Write-Host "  Status: $($service.Status)" -ForegroundColor White
    Write-Host "  StartType: $($service.StartType)" -ForegroundColor White
} else {
    Write-Host "  Service not found" -ForegroundColor Yellow
}

# Check firewall
Write-Host "`nChecking firewall rule..." -ForegroundColor Cyan
$firewallRule = Get-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -ErrorAction SilentlyContinue
if ($firewallRule) {
    Write-Host "  Rule exists: $($firewallRule.Enabled)" -ForegroundColor White
} else {
    Write-Host "  Rule not found" -ForegroundColor Yellow
}

# Get connection info
Write-Host "`nConnection info..." -ForegroundColor Cyan
$currentUser = $env:USERNAME
$computerName = $env:COMPUTERNAME
$tailscaleIP = (Get-NetIPAddress | Where-Object { $_.IPAddress -like "100.*" -and $_.AddressFamily -eq "IPv4" }).IPAddress

Write-Host "  Computer: $computerName" -ForegroundColor White
Write-Host "  User: $currentUser" -ForegroundColor White
Write-Host "  Tailscale IP: $tailscaleIP" -ForegroundColor White

Write-Host "`nDone!" -ForegroundColor Green
