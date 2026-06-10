
<# ================================
  Farm Stall POS → local Postgres
  Sync script (PowerShell) — tailored to your models
================================ #>

# --------- CONFIG: EDIT THESE ----------
$BaseUrl      = "https://farmstallpos.onrender.com"   # your app URL
$AdminToken   = "YOUR_ADMIN_TOKEN"                    # or "" if not set
$OutDir       = "$PSScriptRoot\_exports"
$LocalDbName  = "farmstall_local"
$LocalDbUser  = "postgres"
$LocalDbHost  = "localhost"
$LocalDbPort  = 5432
# ---------------------------------------

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

function Download-Csv($endpoint, $outfile) {
    $headers = @{ }
    if ($AdminToken -and $AdminToken.Trim() -ne "") {
        $headers["X-Admin-Token"] = $AdminToken
    }
    Write-Host "→ Downloading $endpoint ..."
    try {
        Invoke-RestMethod -Uri "$BaseUrl$endpoint" -Headers $headers -OutFile $outfile -Method GET
    } catch {
        Write-Error "Failed to download $endpoint. $_"
        exit 1
    }
    Write-Host "   Saved: $outfile"
}

Write-Host "Checking DB health via app ..."
try {
    $hc = Invoke-RestMethod -Uri "$BaseUrl/api/db-health" -Method GET
    if (-not $hc.ok) { Write-Error "DB health failed: $($hc.error)"; exit 1 }
    Write-Host "   OK."
} catch {
    Write-Error "Could not call /api/db-health. $_"; exit 1
}

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$ProductsCsv  = Join-Path $OutDir "products_$stamp.csv"
$TxCsv        = Join-Path $OutDir "transactions_$stamp.csv"
$LinesCsv     = Join-Path $OutDir "transaction_lines_$stamp.csv"

Download-Csv "/admin/export/products"           $ProductsCsv
Download-Csv "/admin/export/transactions"       $TxCsv
Download-Csv "/admin/export/transaction_lines"  $LinesCsv

Write-Host "Ensuring local database '$LocalDbName' exists ..."
$existsCmd = "SELECT 1 FROM pg_database WHERE datname = '$LocalDbName';"
$exists = & psql -h $LocalDbHost -p $LocalDbPort -U $LocalDbUser -d postgres -t -c $existsCmd
if (-not $exists) {
    Write-Host "   Creating DB '$LocalDbName' ..."
    & psql -h $LocalDbHost -p $LocalDbPort -U $LocalDbUser -d postgres -c "CREATE DATABASE $LocalDbName;"
} else {
    Write-Host "   DB exists."
}

Write-Host "Ensuring tables exist for your current models ..."
$SchemaSql = @"
CREATE TABLE IF NOT EXISTS products (
  id     SERIAL PRIMARY KEY,
  name   VARCHAR(120) NOT NULL UNIQUE,
  price  NUMERIC(10,2) NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions (
  id        BIGSERIAL PRIMARY KEY,
  date_time TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS transaction_lines (
  id             BIGSERIAL PRIMARY KEY,
  transaction_id BIGINT  NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
  product_id     INTEGER NOT NULL REFERENCES products(id),
  qty            INTEGER NOT NULL CHECK (qty > 0),
  unit_price     NUMERIC(10,2) NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lines_tx   ON transaction_lines(transaction_id);
CREATE INDEX IF NOT EXISTS idx_lines_prod ON transaction_lines(product_id);
"@
& psql -h $LocalDbHost -p $LocalDbPort -U $LocalDbUser -d $LocalDbName -v ON_ERROR_STOP=1 -c $SchemaSql

Write-Host "Importing CSVs into '$LocalDbName' ..."
& psql -h $LocalDbHost -p $LocalDbPort -U $LocalDbUser -d $LocalDbName -v ON_ERROR_STOP=1 -c "\copy products             FROM '$ProductsCsv'  CSV HEADER;"
& psql -h $LocalDbHost -p $LocalDbPort -U $LocalDbUser -d $LocalDbName -v ON_ERROR_STOP=1 -c "\copy transactions         FROM '$TxCsv'        CSV HEADER;"
& psql -h $LocalDbHost -p $LocalDbPort -U $LocalDbUser -d $LocalDbName -v ON_ERROR_STOP=1 -c "\copy transaction_lines    FROM '$LinesCsv'     CSV HEADER;"

Write-Host "Resetting sequences to MAX(id) ..."
$ResetSql = @"
SELECT setval(pg_get_serial_sequence('products','id'),          COALESCE((SELECT MAX(id) FROM products), 0), true);
SELECT setval(pg_get_serial_sequence('transactions','id'),      COALESCE((SELECT MAX(id) FROM transactions), 0), true);
SELECT setval(pg_get_serial_sequence('transaction_lines','id'), COALESCE((SELECT MAX(id) FROM transaction_lines), 0), true);
"@
& psql -h $LocalDbHost -p $LocalDbPort -U $LocalDbUser -d $LocalDbName -v ON_ERROR_STOP=1 -c $ResetSql

Write-Host "✅ Sync complete."
Write-Host "Local DB: $LocalDbName"
Write-Host "CSV folder: $OutDir"
