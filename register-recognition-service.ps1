# register-recognition-service.ps1
# Installs FarmPOS-Recognition as a Windows service using WinSW.
# Run ONCE on the Mini PC as Administrator AFTER register-services.ps1
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File register-recognition-service.ps1

$ErrorActionPreference = "Stop"
$ScriptDir  = $PSScriptRoot
$ToolsDir   = "$ScriptDir\tools"
$WinSwBase  = "$ToolsDir\winsw.exe"

# --- Check WinSW exists ---
if (-not (Test-Path $WinSwBase)) {
    Write-Host "ERROR: WinSW not found. Run register-services.ps1 first." -ForegroundColor Red
    exit 1
}

# --- Prompt for Windows account password ---
Write-Host ""
Write-Host "Service will run as: $env:USERDOMAIN\$env:USERNAME" -ForegroundColor Cyan
$cred        = Get-Credential -UserName "$env:USERDOMAIN\$env:USERNAME" `
               -Message "Enter your Windows password so the service can run as your account"
$accountUser = "$env:USERDOMAIN\$env:USERNAME"
$accountPass = $cred.GetNetworkCredential().Password

$ErrorActionPreference = "Continue"
New-Item -ItemType Directory -Force -Path "$ScriptDir\logs" | Out-Null

# --- Service config ---
$ServiceName = "FarmPOS-Recognition"
$DisplayName = "Farm POS Customer Recognition"
$LogFile     = "$ScriptDir\logs\recognition_service.log"
$SvcExe      = "$ToolsDir\$ServiceName.exe"
$XmlFile     = "$ToolsDir\$ServiceName.xml"

# Stop and uninstall existing service
$existing = & sc.exe query $ServiceName 2>&1
if ($existing -notmatch "does not exist") {
    Write-Host "Removing existing service '$ServiceName'..." -ForegroundColor Yellow
    & sc.exe stop $ServiceName 2>&1 | Out-Null
    Start-Sleep -Seconds 2
    & $SvcExe uninstall 2>&1 | Out-Null
    & sc.exe delete $ServiceName 2>&1 | Out-Null
    Start-Sleep -Seconds 2
}

# Copy WinSW exe
Copy-Item $WinSwBase $SvcExe -Force

# Write WinSW XML config
@"
<service>
  <id>$ServiceName</id>
  <name>$DisplayName</name>
  <description>Farm POS customer recognition service - ANPR, face, and body identification via Frigate NVR</description>
  <executable>$ScriptDir\.venv\Scripts\python.exe</executable>
  <arguments>recognition_service.py</arguments>
  <workingdirectory>$ScriptDir</workingdirectory>
  <logpath>$ScriptDir\logs</logpath>
  <logmode>append</logmode>
  <onfailure action="restart" delay="30000 ms"/>
  <startmode>Automatic</startmode>
  <env name="POS_URL" value="http://127.0.0.1:5000"/>
  <env name="POS_USER" value="admin"/>
  <env name="POS_PASS" value="admin123"/>
  <env name="FRIGATE_URL" value="http://127.0.0.1:8971"/>
  <env name="WEBHOOK_PORT" value="8080"/>
  <env name="FACE_THRESHOLD" value="0.40"/>
  <env name="GAIT_THRESHOLD" value="0.25"/>
  <serviceaccount>
    <username>$accountUser</username>
    <password>$accountPass</password>
  </serviceaccount>
</service>
"@ | Set-Content -Path $XmlFile -Encoding UTF8

Write-Host "Installing service '$ServiceName'..." -ForegroundColor Green
& $SvcExe install

Write-Host "Starting service..." -ForegroundColor Green
& $SvcExe start
Start-Sleep -Seconds 3

$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -eq "Running") {
    Write-Host "Running. Log: $LogFile" -ForegroundColor Cyan
} else {
    Write-Host "WARNING: service did not start. Check: $LogFile" -ForegroundColor Red
}

Write-Host ""
Write-Host "Done. Manage from services.msc or PowerShell:" -ForegroundColor Green
Write-Host "  Start-Service $ServiceName    |  Stop-Service $ServiceName"
Write-Host "  Restart-Service $ServiceName"
Write-Host ""
Write-Host "IMPORTANT: Configure Frigate to send webhooks to:" -ForegroundColor Yellow
Write-Host "  http://127.0.0.1:8080/webhook/frigate" -ForegroundColor Cyan
Write-Host ""
Write-Host "Frigate config snippet (add to config.yml):" -ForegroundColor Yellow
Write-Host "  mqtt:"
Write-Host "    enabled: false"
Write-Host "  notifications:"
Write-Host "    webhook:"
Write-Host "      url: http://127.0.0.1:8080/webhook/frigate"
Write-Host "      method: POST"
Write-Host "      event_types:"
Write-Host "        - end"
