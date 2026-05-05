# start-watchers.ps1
# Launches both QA and Prod watch-deploy processes fully hidden in the background.
# Logs go to logs\watch-deploy-qa.log and logs\watch-deploy-prod.log
# Run stop-watchers.ps1 to shut them down.

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$PidFile   = "$ScriptDir\logs\watchers.pid"

New-Item -ItemType Directory -Force -Path "$ScriptDir\logs" | Out-Null

function Start-Watcher($envName) {
    $existing = Get-Content $PidFile -ErrorAction SilentlyContinue |
                Where-Object { $_ -match "^$envName=" } |
                ForEach-Object { ($_ -split "=")[1] }
    if ($existing -and (Get-Process -Id $existing -ErrorAction SilentlyContinue)) {
        Write-Host "[$envName] Watcher already running (pid $existing). Skipping." -ForegroundColor Yellow
        return
    }

    $proc = Start-Process powershell `
        -ArgumentList "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ScriptDir\watch-deploy.ps1`" -Env $envName" `
        -WorkingDirectory $ScriptDir `
        -WindowStyle Hidden `
        -PassThru

    Write-Host "[$envName] Watcher started (pid $($proc.Id))." -ForegroundColor Green

    # Append/replace pid entry
    $lines = (Get-Content $PidFile -ErrorAction SilentlyContinue) | Where-Object { $_ -notmatch "^$envName=" }
    $lines += "$envName=$($proc.Id)"
    Set-Content $PidFile -Value $lines
}

Start-Watcher "qa"
Start-Watcher "prod"

Write-Host ""
Write-Host "Both watchers running in the background." -ForegroundColor Cyan
Write-Host "  QA log:   $ScriptDir\logs\watch-deploy-qa.log"
Write-Host "  Prod log: $ScriptDir\logs\watch-deploy-prod.log"
Write-Host "  To stop:  powershell -ExecutionPolicy Bypass -File stop-watchers.ps1"
