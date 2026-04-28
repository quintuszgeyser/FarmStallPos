# Start PostgreSQL if not running
$pgData = "$env:USERPROFILE\PostgreSQL\data"
$pgLog  = "$env:USERPROFILE\PostgreSQL\pg.log"
$status = & pg_ctl status -D $pgData 2>&1
if ($status -match "server is running") {
    Write-Host "PostgreSQL already running." -ForegroundColor Green
} else {
    $pidFile = "$pgData\postmaster.pid"
    if (Test-Path $pidFile) { Remove-Item $pidFile -Force }

    Write-Host "Starting PostgreSQL..." -ForegroundColor Yellow
    $result = & pg_ctl start -D $pgData -l $pgLog 2>&1
    if ($result -match "Permission denied") {
        $tmpLog = "$env:TEMP\pg_start.log"
        & pg_ctl start -D $pgData -l $tmpLog
    }
    Start-Sleep -Seconds 2

    $check = & pg_ctl status -D $pgData 2>&1
    if ($check -match "server is running") {
        Write-Host "PostgreSQL started." -ForegroundColor Green
    } else {
        Write-Host "WARNING: PostgreSQL may not have started. Check logs." -ForegroundColor Red
    }
}

# Activate venv
& "$PSScriptRoot\.venv\Scripts\Activate.ps1"

# Set environment variables
$env:APP_ENV           = "production"
$env:SECRET_KEY        = "CHANGE-ME-prod-secret-key"
$env:DATABASE_URL      = "postgresql://farmstall:FarmStall@localhost:5432/farm_pos_prod"
$env:LOCAL_TZ          = "Africa/Johannesburg"
$env:ADMIN_USER        = "admin"
$env:ADMIN_PASS        = "CHANGE-ME-prod-password"
$env:PORT              = "5443"

Write-Host "Starting Farm POS PRODUCTION on https://localhost:5443 ..." -ForegroundColor Green
python app.py
