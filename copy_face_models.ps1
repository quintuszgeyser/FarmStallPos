# Copy face models from user profile to SYSTEM profile
# Run on Mini PC after downloading models

Write-Host "`nCopying InsightFace models to SYSTEM profile..." -ForegroundColor Yellow

# Source: where models are downloaded
$userProfile = $env:USERPROFILE
$sourceDir = "$userProfile\.insightface\models\buffalo_l"

# Alternative source locations to check
$altSources = @(
    "$userProfile/.insightface/models/buffalo_l",
    "C:\Users\Quintusz\.insightface\models\buffalo_l",
    "C:\Users\Quintusz/.insightface/models/buffalo_l"
)

# Find where models actually are
$actualSource = $null
foreach ($src in @($sourceDir) + $altSources) {
    if (Test-Path $src) {
        $files = Get-ChildItem $src -Filter "*.onnx" -ErrorAction SilentlyContinue
        if ($files.Count -gt 0) {
            $actualSource = $src
            Write-Host "Found models in: $src" -ForegroundColor Green
            break
        }
    }
}

if (-not $actualSource) {
    Write-Host "`nError: Cannot find downloaded models!" -ForegroundColor Red
    Write-Host "Checked locations:" -ForegroundColor Yellow
    foreach ($src in @($sourceDir) + $altSources) {
        Write-Host "  - $src"
    }
    Write-Host "`nPlease run: .venv\Scripts\python.exe download_face_models.py" -ForegroundColor Yellow
    exit 1
}

# Destination: SYSTEM user profile
$destDir = "C:\Windows\system32\config\systemprofile\.insightface\models\buffalo_l"

# Create destination
New-Item -ItemType Directory -Force -Path $destDir | Out-Null

# Copy all .onnx files
Write-Host "`nCopying model files..." -ForegroundColor Yellow
$files = Get-ChildItem $actualSource -Filter "*.onnx"
$copied = 0

foreach ($file in $files) {
    Copy-Item $file.FullName -Destination $destDir -Force
    Write-Host "  Copied: $($file.Name) ($([math]::Round($file.Length/1MB, 1)) MB)" -ForegroundColor Green
    $copied++
}

if ($copied -eq 0) {
    Write-Host "`nError: No .onnx files found in $actualSource" -ForegroundColor Red
    exit 1
}

# Verify
Write-Host "`nVerifying..." -ForegroundColor Yellow
$destFiles = Get-ChildItem $destDir -Filter "*.onnx"
Write-Host "  Destination has $($destFiles.Count) model files" -ForegroundColor Green

if ($destFiles.Count -ne $copied) {
    Write-Host "`nWarning: File count mismatch!" -ForegroundColor Yellow
}

# Restart Recognition service
Write-Host "`nRestarting Recognition Service..." -ForegroundColor Yellow
Restart-Service FarmPOS-Recognition

Write-Host "`nDone! Face models ready." -ForegroundColor Green
Write-Host "`nWatch logs:" -ForegroundColor Cyan
Write-Host "  Get-Content logs\recognition_service.log -Tail 20 -Wait" -ForegroundColor White
Write-Host "`nThen walk in front of the indoor camera!" -ForegroundColor Yellow
