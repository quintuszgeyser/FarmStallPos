# watch-deploy.ps1
# Watches GitHub for new commits and restarts the POS app automatically.
#
# Usage (run on the Mini PC, leave it running):
#   powershell -ExecutionPolicy Bypass -File watch-deploy.ps1 -Env qa
#   powershell -ExecutionPolicy Bypass -File watch-deploy.ps1 -Env prod
#
# -Env qa   -> tracks branch 'main',       runs start-qa.ps1
# -Env prod -> tracks branch 'production', runs start-prod.ps1

param(
    [Parameter(Mandatory)][ValidateSet("qa","prod")] [string]$Env,
    [int]$PollSeconds = 60
)

$ErrorActionPreference = "Continue"
$ScriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { "C:\Users\Quintusz\farm_pos_web" }

$Branch      = if ($Env -eq "prod") { "production" } else { "main" }
$StartScript = if ($Env -eq "prod") { "$ScriptDir\start-prod.ps1" } else { "$ScriptDir\start-qa.ps1" }
$LogFile     = "$ScriptDir\logs\watch-deploy-$Env.log"

New-Item -ItemType Directory -Force -Path "$ScriptDir\logs" | Out-Null

function Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line
}

function Get-RemoteHash {
    git -C $ScriptDir fetch origin $Branch --quiet 2>&1 | Out-Null
    return (git -C $ScriptDir rev-parse "origin/$Branch").Trim()
}

function Get-LocalHash {
    return (git -C $ScriptDir rev-parse HEAD).Trim()
}

function Start-App {
    Log "Starting app ($Env)..."
    $AppOut = "$ScriptDir\logs\app-$Env.log"
    $AppErr = "$ScriptDir\logs\app-$Env-err.log"
    $global:AppProcess = Start-Process powershell `
        -ArgumentList "-ExecutionPolicy Bypass -File `"$StartScript`"" `
        -WorkingDirectory $ScriptDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput $AppOut `
        -RedirectStandardError  $AppErr `
        -PassThru
    Log "App started (pid $($global:AppProcess.Id)). Logs: $AppOut / $AppErr"
}

function Stop-App {
    if ($global:AppProcess -and -not $global:AppProcess.HasExited) {
        Log "Stopping app (pid $($global:AppProcess.Id))..."
        Stop-Process -Id $global:AppProcess.Id -Force -ErrorAction SilentlyContinue
        $global:AppProcess = $null
    }
    $port = if ($Env -eq "prod") { 5443 } else { 5000 }
    $pids = (netstat -ano | Select-String ":$port ") |
            ForEach-Object { ($_ -split '\s+')[-1] } |
            Sort-Object -Unique
    foreach ($p in $pids) {
        if ($p -match '^\d+$' -and $p -ne "0") {
            try { Stop-Process -Id $p -Force -ErrorAction SilentlyContinue } catch {}
        }
    }
    Start-Sleep -Seconds 2
}

function Deploy-Latest {
    Log "Pulling latest from origin/$Branch..."
    git -C $ScriptDir pull origin $Branch --quiet
    Log "Installing/updating Python packages..."
    & "$ScriptDir\.venv\Scripts\pip" install -r "$ScriptDir\requirements.txt" `
        --quiet `
        --trusted-host pypi.org `
        --trusted-host files.pythonhosted.org
    Stop-App
    Start-Sleep -Seconds 2
    Start-App
}

Log "=== watch-deploy started | env=$Env | branch=$Branch | poll=${PollSeconds}s ==="

try {
    try {
        Deploy-Latest
    } catch {
        Log "WARNING: initial deploy failed - $_. App may not be running."
    }
    $lastHash = Get-LocalHash
    Log "Running at commit $($lastHash.Substring(0,8))."

    while ($true) {
        Start-Sleep -Seconds $PollSeconds

        try {
            $remote = Get-RemoteHash
            if ($remote -ne $lastHash) {
                Log "New commit detected: $($lastHash.Substring(0,8)) -> $($remote.Substring(0,8))"
                Deploy-Latest
                $lastHash = Get-LocalHash
                Log "Deployed. Now at $($lastHash.Substring(0,8))."
            }
        } catch {
            Log "WARNING: poll failed - $_"
        }
    }
} finally {
    Log "Watcher stopping — killing app..."
    Stop-App
    Log "App stopped."
}
