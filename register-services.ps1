# register-services.ps1
# Installs FarmPOS-QA and FarmPOS-Prod as Windows services using WinSW.
# Run ONCE on the Mini PC as Administrator.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File register-services.ps1

$ErrorActionPreference = "Stop"
$ScriptDir  = $PSScriptRoot
$ToolsDir   = "$ScriptDir\tools"
$WinSwBase  = "$ToolsDir\winsw.exe"

# --- Download WinSW if not present ---
if (-not (Test-Path $WinSwBase)) {
    New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null
    Write-Host "Downloading WinSW from GitHub..." -ForegroundColor Yellow
    try {
        Invoke-WebRequest "https://github.com/winsw/winsw/releases/latest/download/WinSW-x64.exe" `
            -OutFile $WinSwBase -UseBasicParsing
    } catch {
        Write-Host ""
        Write-Host "ERROR: Could not download WinSW automatically." -ForegroundColor Red
        Write-Host "Please download WinSW-x64.exe manually and place it at:" -ForegroundColor Red
        Write-Host "  $WinSwBase" -ForegroundColor Yellow
        Write-Host "Download from: https://github.com/winsw/winsw/releases/latest" -ForegroundColor Yellow
        exit 1
    }
    Write-Host "WinSW downloaded." -ForegroundColor Green
}

# --- Prompt once for the Windows account password ---
Write-Host ""
Write-Host "Services will run as: $env:USERDOMAIN\$env:USERNAME" -ForegroundColor Cyan
$cred        = Get-Credential -UserName "$env:USERDOMAIN\$env:USERNAME" `
               -Message "Enter your Windows password so the services can run as your account"
$accountUser = "$env:USERDOMAIN\$env:USERNAME"
$accountPass = $cred.GetNetworkCredential().Password

$ErrorActionPreference = "Continue"
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
# WinSW requires the exe and xml to share the same base name in the same folder.
# We copy winsw.exe to FarmPOS-qa.exe / FarmPOS-prod.exe alongside their XML files.
foreach ($envName in @("qa", "prod")) {
    $ServiceName = "FarmPOS-$envName"
    $DisplayName = "Farm POS $($envName.ToUpper())"
    $LogFile     = "$ScriptDir\logs\watch-deploy-$envName.log"
    $SvcExe      = "$ToolsDir\$ServiceName.exe"
    $XmlFile     = "$ToolsDir\$ServiceName.xml"

    # Stop and uninstall existing service
    $existing = & sc.exe query $ServiceName 2>&1
    if ($existing -notmatch "does not exist") {
        Write-Host "Removing existing service '$ServiceName'..." -ForegroundColor Yellow
        & $SvcExe stop    2>&1 | Out-Null
        & $SvcExe uninstall 2>&1 | Out-Null
        Start-Sleep -Seconds 2
    }

    # Each service needs its own copy of the exe next to its xml
    Copy-Item $WinSwBase $SvcExe -Force

    # Write WinSW XML config (same base name as exe, same folder)
    @"
<service>
  <id>$ServiceName</id>
  <name>$DisplayName</name>
  <description>Farm POS auto-deploy watcher ($envName) - polls GitHub, restarts on new commits</description>
  <executable>powershell.exe</executable>
  <arguments>-ExecutionPolicy Bypass -WindowStyle Hidden -File "$ScriptDir\watch-deploy.ps1" -Env $envName</arguments>
  <workingdirectory>$ScriptDir</workingdirectory>
  <logpath>$ScriptDir\logs</logpath>
  <logmode>append</logmode>
  <onfailure action="restart" delay="5000 ms"/>
  <startmode>Automatic</startmode>
  <serviceaccount>
    <username>$accountUser</username>
    <password>$accountPass</password>
  </serviceaccount>
</service>
"@ | Set-Content -Path $XmlFile -Encoding UTF8

    Write-Host "[$envName] Installing service '$ServiceName'..." -ForegroundColor Green
    & $SvcExe install

    Write-Host "[$envName] Starting service..." -ForegroundColor Green
    & $SvcExe start
    Start-Sleep -Seconds 2

    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($svc -and $svc.Status -eq "Running") {
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
