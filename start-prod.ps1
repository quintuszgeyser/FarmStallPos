# Start PostgreSQL if not running
$pgBin   = "$env:USERPROFILE\PostgreSQL\pgsql\bin"
$pgCtl   = "$pgBin\pg_ctl.exe"
$pgData  = "$env:USERPROFILE\PostgreSQL\data"
$pgLog   = "$env:USERPROFILE\PostgreSQL\pg.log"
$python  = "$PSScriptRoot\.venv\Scripts\python.exe"

$status = & $pgCtl status -D $pgData 2>&1
if ($status -match "server is running") {
    Write-Host "PostgreSQL already running." -ForegroundColor Green
} else {
    $pidFile = "$pgData\postmaster.pid"
    if (Test-Path $pidFile) { Remove-Item $pidFile -Force }

    Write-Host "Starting PostgreSQL..." -ForegroundColor Yellow
    $result = & $pgCtl start -D $pgData -l $pgLog 2>&1
    if ($result -match "Permission denied") {
        $tmpLog = "$env:TEMP\pg_start.log"
        & $pgCtl start -D $pgData -l $tmpLog
    }
    Start-Sleep -Seconds 2

    $check = & $pgCtl status -D $pgData 2>&1
    if ($check -match "server is running") {
        Write-Host "PostgreSQL started." -ForegroundColor Green
    } else {
        Write-Host "WARNING: PostgreSQL may not have started. Check logs." -ForegroundColor Red
    }
}

# Set environment variables
$env:APP_ENV      = "production"
$env:SECRET_KEY   = "Nicolene0729021560"
$env:DATABASE_URL = "postgresql://farmstall:FarmStall@localhost:5432/farm_pos_prod"
$env:LOCAL_TZ     = "Africa/Johannesburg"
$env:ADMIN_USER   = "admin"
$env:ADMIN_PASS   = "admin123"
$env:PORT         = "5443"

Write-Host "Starting Farm POS PRODUCTION on https://localhost:5443 ..." -ForegroundColor Green
& $python "$PSScriptRoot\app.py"
