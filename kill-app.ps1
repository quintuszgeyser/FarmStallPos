# kill-app.ps1 — called by WinSW when stopping the service to kill the Flask process
param([Parameter(Mandatory)][ValidateSet("qa","prod")] [string]$Env)

$port = if ($Env -eq "prod") { 5443 } else { 5000 }
$pids = (netstat -ano | Select-String ":$port ") |
        ForEach-Object { ($_ -split '\s+')[-1] } |
        Sort-Object -Unique
foreach ($p in $pids) {
    if ($p -match '^\d+$' -and $p -ne "0") {
        Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
    }
}
