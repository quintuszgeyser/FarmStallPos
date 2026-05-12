# Install recognition service dependencies
# Run from the farm_pos_web folder on the Mini PC

$pip = ".\.venv\Scripts\pip"

Write-Host "Installing recognition service dependencies..."
& $pip install fast-plate-ocr --trusted-host pypi.org --trusted-host files.pythonhosted.org
& $pip install insightface --trusted-host pypi.org --trusted-host files.pythonhosted.org
& $pip install onnxruntime --trusted-host pypi.org --trusted-host files.pythonhosted.org
& $pip install opencv-python --trusted-host pypi.org --trusted-host files.pythonhosted.org
& $pip install mediapipe --trusted-host pypi.org --trusted-host files.pythonhosted.org

Write-Host ""
Write-Host "Attempting to install insightface (may need Visual C++ Build Tools)..." -ForegroundColor Yellow
$installResult = & $pip install insightface --only-binary=:all: --trusted-host pypi.org --trusted-host files.pythonhosted.org 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "WARNING: Could not find pre-built insightface wheel." -ForegroundColor Red
    Write-Host "You need to install Microsoft C++ Build Tools:" -ForegroundColor Yellow
    Write-Host "  1. Download: https://visualstudio.microsoft.com/visual-cpp-build-tools/" -ForegroundColor Cyan
    Write-Host "  2. Run installer, select 'Desktop development with C++'" -ForegroundColor Cyan
    Write-Host "  3. After install, re-run this script" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "OR use older Python (3.11) which has pre-built wheels" -ForegroundColor Yellow
} else {
    Write-Host "InsightFace installed successfully" -ForegroundColor Green

    Write-Host ""
    Write-Host "Pre-downloading InsightFace model (~100MB)..."
    & .\.venv\Scripts\python -c "import insightface; app = insightface.app.FaceAnalysis(); app.prepare(ctx_id=-1, nms=0.4)"
}

Write-Host ""
Write-Host "Pre-downloading ANPR model..."
& .\.venv\Scripts\python -c "from fast_plate_ocr import LicensePlateRecognizer; LicensePlateRecognizer('global-plates-mobile-vit-v2-model')"

Write-Host ""
Write-Host "Installation complete!" -ForegroundColor Green
