# register-watcher.ps1
# Registers both QA and Prod watch-deploy tasks to start at boot (hidden, no window).
# Run ONCE on the Mini PC as Administrator.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File register-watcher.ps1

$ScriptDir = $PSScriptRoot
$Watcher   = "$ScriptDir\watch-deploy.ps1"

foreach ($envName in @("qa", "prod")) {
    $TaskName = "FarmPOS-Watcher-$envName"

    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Write-Host "Removing existing task '$TaskName'..." -ForegroundColor Yellow
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }

    $action = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Watcher`" -Env $envName" `
        -WorkingDirectory $ScriptDir

    $trigger = New-ScheduledTaskTrigger -AtStartup

    $settings = New-ScheduledTaskSettingsSet `
        -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
        -RestartOnIdle:$false

    $principal = New-ScheduledTaskPrincipal `
        -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive `
        -RunLevel Highest

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description "Farm POS auto-deploy watcher ($envName) - polls GitHub every 60s" | Out-Null

    Write-Host "[$envName] Task '$TaskName' registered. Starting now..." -ForegroundColor Green
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "[$envName] Started. Log: $ScriptDir\logs\watch-deploy-$envName.log" -ForegroundColor Cyan
}

Write-Host ""
Write-Host "Both watchers registered and running. They will restart automatically on reboot." -ForegroundColor Green
