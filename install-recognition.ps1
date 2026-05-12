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
Write-Host "Pre-downloading InsightFace buffalo_sc model (~100MB)..."
& .\.venv\Scripts\python -c "from insightface.app import FaceAnalysis; app = FaceAnalysis(name='buffalo_sc', providers=['CPUExecutionProvider']); app.prepare(ctx_id=-1)"

Write-Host ""
Write-Host "Pre-downloading ANPR model..."
& .\.venv\Scripts\python -c "from fast_plate_ocr import ONNXPlateRecognizer; ONNXPlateRecognizer('global-plates-mobile-vit-v2-model')"

Write-Host ""
Write-Host "All done. Run recognition_service.py to start."
