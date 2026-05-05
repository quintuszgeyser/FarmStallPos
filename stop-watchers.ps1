# stop-watchers.ps1
# Kills the background QA and Prod watch-deploy processes and their Flask children.

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$PidFile   = "$ScriptDir\logs\watchers.pid"

function Stop-Port($port) {
    $pids = (netstat -ano | Select-String ":$port ") |
            ForEach-Object { ($_ -split '\s+')[-1] } |
            Sort-Object -Unique
    foreach ($p in $pids) {
        if ($p -match '^\d+$' -and $p -ne "0") {
            try { Stop-Process -Id $p -Force -ErrorAction SilentlyContinue } catch {}
        }
    }
}

if (-not (Test-Path $PidFile)) {
    Write-Host "No watchers.pid found — nothing to stop." -ForegroundColor Yellow
    exit 0
}

Get-Content $PidFile | ForEach-Object {
    $parts = $_ -split "="
    if ($parts.Count -ne 2) { return }
    $envName = $parts[0]; $pid = $parts[1]
    if ($pid -match '^\d+$') {
        Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
        Write-Host "[$envName] Stopped watcher (pid $pid)." -ForegroundColor Green
    }
}

# Kill Flask processes on both ports
Stop-Port 5000
Stop-Port 5443
Write-Host "Flask processes on ports 5000 and 5443 stopped." -ForegroundColor Green

Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
Write-Host "Done." -ForegroundColor Cyan
