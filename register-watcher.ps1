# register-watcher.ps1
# Registers watch-deploy.ps1 as a Windows scheduled task that starts at boot.
# Run this ONCE on the Mini PC (as Administrator).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File register-watcher.ps1 -Env qa
#   powershell -ExecutionPolicy Bypass -File register-watcher.ps1 -Env prod

param(
    [Parameter(Mandatory)][ValidateSet("qa","prod")] [string]$Env
)

$TaskName  = "FarmPOS-Watcher-$Env"
$ScriptDir = $PSScriptRoot
$Watcher   = "$ScriptDir\watch-deploy.ps1"

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Removing existing task '$TaskName'..." -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Watcher`" -Env $Env" `
    -WorkingDirectory $ScriptDir

$trigger = New-ScheduledTaskTrigger -AtStartup

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
    -RestartOnIdle:$false

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Highest

$desc = "Farm POS auto-deploy watcher ($Env) - polls GitHub every 60s"

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description $desc

Write-Host ""
Write-Host "Task '$TaskName' registered. Starting it now..." -ForegroundColor Green
Start-ScheduledTask -TaskName $TaskName
$logPath = "logs\watch-deploy-$Env.log"
Write-Host "Done. Check $logPath for status." -ForegroundColor Cyan
