# Recognition Service

`recognition_service_v2.py` - runs as the `farmpos-recognition` Docker container. Polls Frigate NVR events, identifies customers via face + plate + gait, and pushes visits to the POS API.

## Architecture

```
Frigate NVR (port 8971) → Frigate poller (every 30s) → feature extraction
                                                        ↓
                                     Track Identity Manager (30-60s window)
                                                        ↓
                                     Scoring Engine (normalized, safety-gated)
                                                        ↓
                              ┌────────┬──────────┬──────────┐
                              │ LINKED │  PENDING │  ENROLL  │
                              └────────┴──────────┴──────────┘
                                   ↓
                        POST /api/customers/identify → CustomerVisit
                                   ↓
                        Teller polls /api/customers/pending_visits (5s)
```

**Cameras:** 12 Tapo C510W via Frigate at `http://10.0.0.101:8971`  
**Service URL:** `http://farmpos-recognition:8080` (Docker internal) / `http://localhost:8080` (host)  
**POS URL (Docker):** `http://farmpos-app:5000`

## Feature Weights (v2 Production)

**Biometric - required to link:**

| Feature | Points | Notes |
|---|---|---|
| Face | 6.0 | InsightFace ArcFace buffalo_l, 512D cosine similarity |
| Gait | 3.0 | MediaPipe Pose, Euclidean distance |

**Support - cannot link alone:**

| Feature | Points | Notes |
|---|---|---|
| Plate | 2.0 | fast-plate-ocr, requires biometric ≥ 50% |
| Height category | 0.5 | short/medium/tall from body proportions |
| Build | 0.4 | shoulder-to-hip ratio |
| Hair colour | 0.3 | pixel analysis |
| Facial hair | 0.1 | chin darkness heuristic |

**Contextual - capped at 1.0 total, requires biometric ≥ 60%:**

| Feature | Points |
|---|---|
| Time pattern | 0.3 |
| Zone pattern | 0.3 |
| Plate-person association | 0.4 |

**Max possible:** ~12.5 points

## Decision States

| State | Condition |
|---|---|
| **LINKED** | Normalised score ≥ 75% of available evidence AND ≥ 4.0 available AND biometric present |
| **PENDING** | Not enough evidence yet, or in ambiguity region - keep collecting |
| **ENROLL** | Track age ≥ 60s + has enrollment-quality biometric + score below pending threshold |

## Quality Gates

**Face:** detection confidence ≥ 0.5, face ≥ 5% of image, not profile, Laplacian variance ≥ 500  
**Gait:** full body visible, ≥ 60% landmarks detected, visibility confidence ≥ 0.6  
**Plate:** OCR confidence ≥ 0.8 (shared cars / Uber / OCR errors → plate never links alone)

## Safety Constraints

1. Must have face OR gait to link (plate is support-only)
2. Plate requires biometric ≥ 50% first
3. Context capped at 1.0 (prevents habit leakage)
4. Scoring is normalised: `earned / available` (fair when features are missing)

## Auto-Enrollment Logic

New customer created when:
- **2+ biometric signals** detected (plate + face, face + gait), OR
- **1 biometric + strong physical profile** (gender + height + hair)
- Track age ≥ 60s, ≥ 3 frames, quality ≥ 0.8 on at least 1 frame

Customers start anonymous (`name = NULL`, assigned `customer_number = CUST-0001` etc.). Till badge only appears if a name is set.

## Purchase Linking

1. Recognition posts visit → `CustomerVisit` row created
2. Teller polls `/api/customers/pending_visits` every 5s
3. Badge appears in POS if customer has a name
4. Checkout includes `customer_id` - stored in `sales.customer_id`

## Configuration (Docker env vars)

Set in `~/farmpos-docker/docker-compose.yml` under the `recognition` service:

| Variable | Default | Description |
|---|---|---|
| `FRIGATE_URL` | `http://10.0.0.101:8971` | Frigate NVR |
| `POS_URL` | `http://farmpos-app:5000` | Flask POS API |
| `DATABASE_URL` | set in compose | PostgreSQL |
| `FACE_THRESHOLD` | `0.40` | Cosine similarity minimum |
| `GAIT_THRESHOLD` | `0.25` | Euclidean distance maximum |

Rebuild after env changes:
```bash
ssh farmpc 'cd ~/farmpos-docker && bash deploy.sh recognition'
```

## API Endpoints

**Called by recognition service (no auth required):**
```
POST /api/customers/identify        - submit identification result
POST /api/customers/log_plate       - log every plate detection
GET  /api/customers/faces_raw       - all face embeddings (base64)
GET  /api/customers/gaits_raw       - all gait features (base64)
```

**Teller polling:**
```
GET  /api/customers/pending_visits          - unacknowledged visits, last 5 min
POST /api/customers/visits/<id>/acknowledge - stop toast
```

**Admin enrollment:**
```
POST /api/customers/<id>/enroll/plate  { plate_number }
POST /api/customers/<id>/enroll/face   { image_data }  base64 JPEG
POST /api/customers/<id>/enroll/gait   { image_data }  base64 JPEG, full body
```

## Decision Audit

Every decision is logged to `decision_audit_log` with full feature breakdown, confidence, threshold version, and top-3 candidates. Useful queries:

```sql
-- Low-confidence links (false positive candidates)
SELECT * FROM decision_audit_log
WHERE decision_type = 'linked' AND confidence < 0.80
ORDER BY confidence ASC LIMIT 20;

-- Daily summary
SELECT decision_type, COUNT(*), AVG(confidence)
FROM decision_audit_log
WHERE timestamp >= CURRENT_DATE
GROUP BY decision_type;
```

## Calibration

The system starts with conservative defaults (`link=0.75`, `pending=0.60`). After collecting ≥100 ground truth pairs:

```python
# scripts/calibrate_recognition.py
from calibration import CalibrationDataset
cal = CalibrationDataset()
roc = cal.compute_roc_curve()
thresholds = cal.derive_thresholds_from_roc(target_fpr=0.01, target_fnr=0.05)
```

Update thresholds in `threshold_config` table, then restart the recognition container.

## Troubleshooting

**No events processed:**
```bash
ssh farmpc 'docker logs farmpos-recognition --tail 50'
# Look for "Frigate poller thread started"
# If missing, check FRIGATE_URL is reachable from the container
ssh farmpc 'docker exec farmpos-recognition curl -s http://10.0.0.101:8971/api/events?limit=1'
```

**Face models missing:**
```bash
ssh farmpc 'docker exec farmpos-recognition python -c "from insightface.app import FaceAnalysis; FaceAnalysis(name=\"buffalo_l\")"'
# Models should be in the recognition-models volume
```

**Too many duplicates:** Lower `link_threshold` (e.g. 0.75 → 0.70)  
**False positives:** Raise `link_threshold` (e.g. 0.75 → 0.82)  
**Enrollment too slow:** Reduce track idle timeout or minimum frame count

## Logs

```bash
ssh farmpc 'docker logs farmpos-recognition --tail 100 -f'
# Or via volume:
ssh farmpc 'tail -f ~/farmpos-docker/data/recognition-logs/recognition_service.log'
```
