# register-services.ps1
# Installs FarmPOS-QA and FarmPOS-Prod as Windows services using NSSM.
# Run ONCE on the Mini PC as Administrator.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File register-services.ps1

$ScriptDir = $PSScriptRoot
$NssmExe   = "$ScriptDir\tools\nssm.exe"

# --- Download NSSM if not present ---
if (-not (Test-Path $NssmExe)) {
    Write-Host "Downloading NSSM..." -ForegroundColor Yellow
    New-Item -ItemType Directory -Force -Path "$ScriptDir\tools" | Out-Null
    $zip     = "$env:TEMP\nssm.zip"
    $extract = "$env:TEMP\nssm_extract"
    Invoke-WebRequest "https://nssm.cc/release/nssm-2.24.zip" -OutFile $zip -UseBasicParsing
    Expand-Archive $zip -DestinationPath $extract -Force
    $arch = if ([Environment]::Is64BitOperatingSystem) { "win64" } else { "win32" }
    Copy-Item "$extract\nssm-2.24\$arch\nssm.exe" $NssmExe
    Remove-Item $zip, $extract -Recurse -Force
    Write-Host "NSSM ready." -ForegroundColor Green
}

# --- Prompt once for the Windows account password ---
Write-Host ""
Write-Host "Services will run as: $env:USERDOMAIN\$env:USERNAME" -ForegroundColor Cyan
$cred = Get-Credential -UserName "$env:USERDOMAIN\$env:USERNAME" `
        -Message "Enter your Windows password so the services can run as your account"
$accountPass = $cred.GetNetworkCredential().Password

New-Item -ItemType Directory -Force -Path "$ScriptDir\logs" | Out-Null

# --- Remove old scheduled tasks if present ---
foreach ($envName in @("qa", "prod")) {
    $oldTask = "FarmPOS-Watcher-$envName"
    if (Get-ScheduledTask -TaskName $oldTask -ErrorAction SilentlyContinue) {
        Write-Host "Removing scheduled task '$oldTask'..." -ForegroundColor Yellow
        Unregister-ScheduledTask -TaskName $oldTask -Confirm:$false
    }
}

# --- Register services ---
foreach ($envName in @("qa", "prod")) {
    $ServiceName = "FarmPOS-$envName"
    $DisplayName = "Farm POS $($envName.ToUpper())"
    $LogFile     = "$ScriptDir\logs\watch-deploy-$envName.log"

    # Remove existing service
    $existing = & sc.exe query $ServiceName 2>&1
    if ($existing -notmatch "does not exist") {
        Write-Host "Removing existing service '$ServiceName'..." -ForegroundColor Yellow
        & $NssmExe stop $ServiceName confirm 2>&1 | Out-Null
        & $NssmExe remove $ServiceName confirm 2>&1 | Out-Null
        Start-Sleep -Seconds 2
    }

    Write-Host "[$envName] Installing service '$ServiceName'..." -ForegroundColor Green

    & $NssmExe install $ServiceName powershell.exe
    & $NssmExe set $ServiceName AppParameters "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ScriptDir\watch-deploy.ps1`" -Env $envName"
    & $NssmExe set $ServiceName AppDirectory $ScriptDir
    & $NssmExe set $ServiceName DisplayName $DisplayName
    & $NssmExe set $ServiceName Description "Farm POS auto-deploy watcher ($envName) - polls GitHub, restarts on new commits"
    & $NssmExe set $ServiceName Start SERVICE_AUTO_START

    # Run as the current user so it can access PostgreSQL, git, venv, etc.
    & $NssmExe set $ServiceName ObjectName "$env:USERDOMAIN\$env:USERNAME" $accountPass

    # Log stdout + stderr to the same log file (append)
    & $NssmExe set $ServiceName AppStdout $LogFile
    & $NssmExe set $ServiceName AppStderr $LogFile
    & $NssmExe set $ServiceName AppStdoutCreationDisposition 4
    & $NssmExe set $ServiceName AppStderrCreationDisposition 4

    # Restart the service automatically if it crashes
    & $NssmExe set $ServiceName AppExit Default Restart
    & $NssmExe set $ServiceName AppRestartDelay 5000

    Write-Host "[$envName] Starting service..." -ForegroundColor Green
    & $NssmExe start $ServiceName
    Start-Sleep -Seconds 2

    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($svc.Status -eq "Running") {
        Write-Host "[$envName] Running. Log: $LogFile" -ForegroundColor Cyan
    } else {
        Write-Host "[$envName] WARNING: service did not start. Check: $LogFile" -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "Done. Manage from services.msc or PowerShell:" -ForegroundColor Green
Write-Host "  Start-Service FarmPOS-qa    |  Stop-Service FarmPOS-qa"
Write-Host "  Start-Service FarmPOS-prod  |  Stop-Service FarmPOS-prod"
Write-Host "  Restart-Service FarmPOS-qa  |  Restart-Service FarmPOS-prod"
