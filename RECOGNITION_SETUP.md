# Recognition Service Setup Guide

## Overview

The customer recognition service runs on the Mini PC alongside the Farm POS app and Frigate NVR. It identifies customers using a **2-of-3 voting system**:

- **License plate** (ANPR via fast-plate-ocr)
- **Face recognition** (InsightFace buffalo_sc)
- **Body proportions** (MediaPipe Pose)

When 2+ signals agree, the customer is identified and a "Welcome back!" notification appears on the teller screen.

## Prerequisites

✅ Mini PC with Frigate NVR running on port 8971  
✅ Tapo cameras feeding into Frigate  
✅ Farm POS QA/Prod services running  
✅ Tailscale for remote access (Mini PC at `100.86.32.13`)

## Installation Steps

### 1. Connect to Mini PC

**Via Remote Desktop:**
```
mstsc /v:100.86.32.13
```

**Or via SSH (if enabled):**
```bash
ssh user@100.86.32.13
```

### 2. Navigate to Project Folder

```powershell
cd C:\path\to\farm_pos_web
```

### 3. Install Python Dependencies

**IMPORTANT:** Run this in PowerShell as Administrator:

```powershell
powershell -ExecutionPolicy Bypass -File install-recognition.ps1
```

This installs:
- `fast-plate-ocr` — ANPR model (~200MB)
- `insightface` — Face recognition (~100MB buffalo_sc model)
- `onnxruntime` — ONNX inference engine
- `opencv-python` — Image processing
- `mediapipe` — Body keypoint detection

**Expected time:** 5-10 minutes (downloads models on first run)

### 4. Test Recognition Service Manually

Before registering as a service, test it:

```powershell
powershell -ExecutionPolicy Bypass -File start-recognition.ps1
```

**You should see:**
```
[INFO] Recognition service starting
[INFO] Logged in to POS API
[INFO] Customer cache refreshed: 0 customers
[INFO] Webhook server listening on port 8080
```

**Test webhook endpoint:**
```powershell
curl http://127.0.0.1:8080/webhook/frigate -Method POST -Body '{"type":"test"}'
```

Press `Ctrl+C` to stop. If it works, proceed to step 5.

### 5. Register as Windows Service

**Run as Administrator:**

```powershell
powershell -ExecutionPolicy Bypass -File register-recognition-service.ps1
```

- Enter your Windows password when prompted
- Service will install and auto-start
- Logs to `logs\recognition_service.log`

**Manage service:**
```powershell
Start-Service FarmPOS-Recognition
Stop-Service FarmPOS-Recognition
Restart-Service FarmPOS-Recognition
Get-Service FarmPOS-Recognition  # check status
```

### 6. Configure Frigate Webhooks

Edit Frigate's `config.yml` (usually at `C:\ProgramData\Frigate\config.yml`):

```yaml
mqtt:
  enabled: false

notifications:
  webhook:
    url: http://127.0.0.1:8080/webhook/frigate
    method: POST
    event_types:
      - end  # fire webhook when tracking completes (best snapshot available)
```

Restart Frigate to apply changes.

**Verify webhook delivery:**
- Trigger a car/person detection
- Check `logs\recognition_service.log` for:
  ```
  [INFO] Plate detected: ABC123GP (0.95)
  [INFO] Customer 1 identified via ['plate', 'face']
  ```

## Configuration

Service environment variables (edit `tools\FarmPOS-Recognition.xml` after registration):

| Variable | Default | Description |
|---|---|---|
| `POS_URL` | `http://127.0.0.1:5000` | Farm POS API endpoint |
| `POS_USER` | `admin` | Admin username for API login |
| `POS_PASS` | `admin123` | Admin password |
| `FRIGATE_URL` | `http://127.0.0.1:8971` | Frigate NVR URL |
| `WEBHOOK_PORT` | `8080` | Port to listen for Frigate webhooks |
| `FACE_THRESHOLD` | `0.40` | Face similarity threshold (0-1, lower = stricter) |
| `GAIT_THRESHOLD` | `0.25` | Body proportion distance threshold (lower = stricter) |

**After editing XML, restart the service:**
```powershell
Restart-Service FarmPOS-Recognition
```

## How It Works

### 1. Detection Flow

```
Frigate detects car/person → Webhook to port 8080 → Recognition service downloads snapshot
                                                    ↓
                          ANPR / Face / Body extraction
                                                    ↓
                          Match against enrolled customers (2-of-3 vote)
                                                    ↓
                          POST /api/customers/identify → CustomerVisit logged
                                                    ↓
                          Teller polls /api/customers/pending_visits every 5s
                                                    ↓
                          "Welcome back, Jane!" toast appears
```

### 2. Enrollment (Admin Only)

**License Plate:**
- Admin opens Customers tab → Edit customer → Add plate number manually
- Or auto-enroll from plate log (unmatched detections appear in plate log)

**Face:**
- Admin opens customer → "Enroll Face" → Upload photo or capture from camera
- Service extracts 512-dim embedding, stores as binary

**Body Proportions:**
- Admin → "Enroll Gait" → Upload full-body photo
- MediaPipe extracts 6 ratios (shoulder/hip width, torso/leg height, etc.)

### 3. Matching Logic

**Plate:** Exact string match (normalized: uppercase, no spaces)  
**Face:** Cosine similarity ≥ `FACE_THRESHOLD` (default 0.40)  
**Body:** Euclidean distance < `GAIT_THRESHOLD` (default 0.25)

**Vote:** If 2+ signals agree on the same customer ID → identified ✅

### 4. Fallback Poller

If webhooks fail, service polls Frigate's `/api/events` every 30s for missed detections.

## Troubleshooting

### Service won't start

```powershell
Get-Service FarmPOS-Recognition  # check status
Get-Content logs\recognition_service.log -Tail 50  # check logs
```

**Common issues:**
- Python venv not found → verify `.venv\Scripts\python.exe` exists
- Models not downloaded → re-run `install-recognition.ps1`
- Port 8080 in use → change `WEBHOOK_PORT` in XML config

### Webhooks not arriving

1. Check Frigate logs: `C:\ProgramData\Frigate\logs\frigate.log`
2. Test webhook manually:
   ```powershell
   curl http://127.0.0.1:8080/webhook/frigate -Method POST -Body '{"type":"test"}'
   ```
3. Verify service is listening:
   ```powershell
   netstat -ano | findstr :8080
   ```

### Low identification rate

- **Plate confidence too low:** South African plates work best in daylight. Check `plate_log` table for detection confidence scores.
- **Face threshold too high:** Lower `FACE_THRESHOLD` (try 0.35). Check `/api/customers/faces_raw` to see stored embeddings.
- **Body proportions unstable:** Requires full-body visible + standing upright. Gait features are experimental — disable by setting `GAIT_THRESHOLD=999`.

### Models not loading

```powershell
.\.venv\Scripts\python -c "from insightface.app import FaceAnalysis; FaceAnalysis(name='buffalo_sc')"
.\.venv\Scripts\python -c "from fast_plate_ocr import ONNXPlateRecognizer; ONNXPlateRecognizer('global-plates-mobile-vit-v2-model')"
```

If these fail, re-run `install-recognition.ps1`.

## Performance Notes

- **First detection:** 2-3s (models load on-demand)
- **Subsequent detections:** 200-500ms per frame
- **CPU usage:** ~15% idle, 40-60% during detection (Intel N100)
- **RAM usage:** ~1.2GB (InsightFace + ANPR models in memory)

## Security

- Service runs as your Windows user account (same as QA/Prod services)
- POS API credentials stored in XML config (plaintext) — file permissions restricted to admin
- Face embeddings are binary vectors (not reversible to original photos)
- Snapshots downloaded from Frigate are deleted after processing

## API Endpoints (for reference)

**Enrollment (admin only):**
- `POST /api/customers/<id>/enroll/plate` — `{plate_number}`
- `POST /api/customers/<id>/enroll/face` — `{image_data}` (base64 JPEG)
- `POST /api/customers/<id>/enroll/gait` — `{image_data}` (base64 JPEG, full-body)

**Recognition service (internal, no auth):**
- `POST /api/customers/identify` — Called when 2+ signals agree
- `POST /api/customers/log_plate` — Logs every plate detection
- `GET /api/customers/faces_raw` — Returns all face embeddings as base64
- `GET /api/customers/gaits_raw` — Returns all gait features as base64

**Teller polling:**
- `GET /api/customers/pending_visits` — Returns unacknowledged visits from last 5min
- `POST /api/customers/visits/<id>/acknowledge` — Marks visit as acknowledged (stops toasts)

**Admin:**
- `GET /api/customers/plate_log` — View all plate detections (matched + unmatched)

## Next Steps

1. Enroll your first customer with plate + face
2. Drive into camera view → check logs for plate detection
3. Walk into indoor camera view → check logs for face/body detection
4. Verify "Welcome back!" toast appears on teller screen
5. Adjust thresholds if needed
