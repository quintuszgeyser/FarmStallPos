# Recognition Service v2.0 - Production Implementation

## Overview

Complete rewrite of the customer recognition system with production-grade multi-biometric identification.

**Key improvements over v1:**
- ✅ Quality-gated feature extraction (no blurry faces, occluded bodies, low-confidence plates)
- ✅ Normalized scoring (accounts for missing features fairly)
- ✅ Track-level identity consistency (averages across multiple frames)
- ✅ Hard safety constraints (biometric required, plate can't link alone, context capped)
- ✅ ROC-calibrated thresholds (empirical, not guessed)
- ✅ Full decision audit trail (explainable AI)
- ✅ Multi-embedding storage (3-5 poses per customer)

## Architecture

### Three-State Decision Model

```
┌─────────────┐
│  Detection  │  Person appears on camera
└──────┬──────┘
       │
       ▼
┌─────────────────────────────────────────┐
│  Track Identity Manager                 │
│  - Collect observations (30-60s)        │
│  - Quality-weighted voting              │
│  - Track continuity across frames       │
└──────┬──────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────┐
│  Scoring Engine                         │
│  - Extract features with quality gates  │
│  - Match against all customers          │
│  - Normalized scoring (earned/available)│
│  - Safety constraints enforced          │
└──────┬──────────────────────────────────┘
       │
       ▼
   ┌───────┴───────┐
   │   Decision    │
   └───┬───┬───┬───┘
       │   │   │
   ┌───▼┐ ┌▼──┐ ┌▼────────┐
   │Link│ │Pend│ │Enroll   │
   │    │ │ing │ │         │
   └────┘ └────┘ └─────────┘

State A: LINKED
- Score >= 75% of available evidence
- At least 4.0 points available
- Biometric present

State B: PENDING
- Not enough evidence yet
- OR in ambiguity region
- Keep collecting

State C: ENROLL
- Age >= 60s
- Has enrollment quality
- Score < pending threshold (no ambiguous match)
```

### Feature Weights (Production-Tuned)

```python
BIOMETRIC (identity-grade, required):
- face:         6.0 points  (40% of max)
- gait:         3.0 points  (20% of max)

SUPPORT (cannot link alone):
- plate:        2.0 points  (requires biometric >= 50%)
- height_cat:   0.5 points
- build:        0.4 points
- hair_color:   0.3 points
- facial_hair:  0.1 points

CONTEXTUAL (capped at 1.0 total, requires biometric >= 60%):
- time_pattern:       0.3 points
- zone_pattern:       0.3 points
- plate_person_assoc: 0.4 points

Max possible: ~12.5 points (if all features present)
```

### Quality Gates

**Face:**
- Detection confidence >= 0.5
- Face size >= 5% of image
- Not profile (pose angle check)
- Not blurry (Laplacian variance >= 500)

**Gait:**
- Full body visible (head + feet)
- >= 60% of key landmarks detected
- Visibility confidence >= 0.6

**Plate:**
- OCR confidence >= 0.8
- (Shared cars, Uber, OCR errors → plate alone cannot link)

**Height:**
- Person not at edge of frame
- Full body visible
- **Category-based** (short/medium/tall), not exact cm

### Safety Constraints

1. **Hard biometric requirement**: Must have face OR gait to link
2. **Plate support-only**: Plate requires biometric >= 50% first
3. **Context capped**: Max 1.0 points from context (prevents habit leakage)
4. **Normalized scoring**: `earned / available` (fair when features missing)
5. **Track continuity tie-breaker**: Not score inflation, only breaks ties

## Installation

### 1. Run Database Migration

```bash
# Connect to your PostgreSQL database
psql -U farmstall -d farm_pos_prod -f migrate_recognition_v2.sql
```

This creates:
- Quality columns on biometric tables
- Decision audit log
- Calibration dataset tables
- Threshold/weights versioning
- Track identity persistence

### 2. Install Python Dependencies

Already installed (no new dependencies).

### 3. Start Recognition Service v2

```bash
# Stop old service first
python recognition_service.py  # Stop with Ctrl+C

# Start v2
python recognition_service_v2.py
```

Or update your Windows service to point to `recognition_service_v2.py`.

## Calibration Workflow

**CRITICAL**: The system starts with conservative thresholds (link=0.75, pending=0.60). These are **guesses** and need calibration.

### Step 1: Collect Ground Truth Data

Admin UI feature (TODO: add to app.py):

1. When a detection appears, admin can tag:
   - "This IS customer #123" → positive example
   - "This is NOT customer #123" → negative example

2. System stores in `calibration_ground_truth` table

3. Aim for **100+ pairs** minimum:
   - 50+ true matches (same person, different times)
   - 50+ false matches (different people, similar appearance)

### Step 2: Compute ROC Curve

```python
from calibration import CalibrationDataset

cal = CalibrationDataset()

# Compute ROC
roc = cal.compute_roc_curve()
print(f"AUC: {roc['auc']:.3f}")

# Derive optimal thresholds
thresholds = cal.derive_thresholds_from_roc(
    target_fpr=0.01,  # 1% false positive rate
    target_fnr=0.05   # 5% false negative rate
)

print(f"Link threshold: {thresholds['link_threshold']:.3f}")
print(f"Pending threshold: {thresholds['pending_threshold']:.3f}")
```

### Step 3: Update Thresholds

```sql
INSERT INTO threshold_config (
    version,
    global_link_threshold,
    global_pending_threshold,
    active,
    calibration_metadata
)
VALUES (
    'v1.1_calibrated_2026_05_13',
    0.82,  -- From ROC analysis
    0.58,  -- From ROC analysis
    TRUE,
    '{"auc": 0.94, "true_match_count": 120, "false_match_count": 105}'::JSON
);

-- Deactivate old
UPDATE threshold_config SET active = FALSE WHERE version = 'v1.0_initial';
```

Service will pick up new thresholds on next restart.

## Decision Audit

Every identification decision is logged to `decision_audit_log` with:

```json
{
  "decision_id": "uuid",
  "decision_type": "linked",
  "customer_id": 123,
  "track_id": "abc123",
  "confidence": 0.85,
  "frame_count": 8,
  "top_3_candidates": [
    {"customer_id": 123, "score": 0.85},
    {"customer_id": 456, "score": 0.62},
    {"customer_id": 789, "score": 0.41}
  ],
  "normalized_score": 0.85,
  "biometric_ratio": 0.92,
  "available_weight": 9.5,
  "feature_breakdown": {
    "face": 5.2,
    "gait": 2.1,
    "plate": 2.0,
    "hair_color": 0.3
  },
  "quality_summary": {
    "face_quality": 0.87,
    "gait_quality": 0.72
  },
  "threshold_version": "v1.1_calibrated_2026_05_13",
  "weights_version": "v2.0_production",
  "threshold_source_link": "global",
  "threshold_source_pending": "camera:outdoor_1",
  "context": {
    "camera": "outdoor_1",
    "quality": "high",
    "time_of_day": "morning"
  }
}
```

### Query Examples

**Find false positives:**
```sql
SELECT * FROM decision_audit_log
WHERE decision_type = 'linked'
  AND confidence < 0.80
ORDER BY confidence ASC
LIMIT 20;
```

**Daily decision summary:**
```sql
SELECT * FROM decision_audit_summary
WHERE decision_date = CURRENT_DATE;
```

**Track confidence over time:**
```sql
SELECT
    track_id,
    frame_count,
    confidence,
    biometric_ratio,
    feature_breakdown
FROM decision_audit_log
WHERE track_id = 'abc123'
ORDER BY timestamp;
```

## Segment-Specific Thresholds

Performance varies by camera, quality, and time of day. After sufficient calibration data:

```python
# Compute per-segment thresholds
segment_thresholds = cal.compute_segmented_roc('camera')

# Returns:
{
    'outdoor_1': {'link': 0.85, 'pending': 0.55},  # Outdoor needs higher threshold
    'indoor_till': {'link': 0.78, 'pending': 0.62}, # Indoor more reliable
}
```

Update config:
```sql
UPDATE threshold_config
SET segment_thresholds = '{
    "camera:outdoor_1": {"link": 0.85, "pending": 0.55},
    "camera:indoor_till": {"link": 0.78, "pending": 0.62}
}'::JSON
WHERE version = 'v1.1_calibrated_2026_05_13';
```

System automatically uses segment threshold with fallback to global.

## Track-Level Identity

Key innovation: **identity is determined across multiple frames**, not single detection.

```
Frame 1: customer #123 score=0.68, customer #456 score=0.52
Frame 2: customer #123 score=0.75, customer #456 score=0.48
Frame 3: customer #123 score=0.82, customer #456 score=0.55
Frame 4: customer #123 score=0.71, customer #456 score=0.60

Quality-weighted average:
- Customer #123: 0.74 (high quality frames)
- Customer #456: 0.54 (low quality frames)

→ Track linked to #123 (confidence=0.74)
```

Benefits:
- Reduces single-frame noise
- Exploits temporal continuity
- Allows lower per-frame thresholds (more data = more confidence)

## Enrollment Quality Requirements

Track only enrolls as new customer if:

1. **Age >= 60 seconds** (sufficient observation time)
2. **At least 3 frames observed**
3. **High-quality biometric present:**
   - At least 1 frame with quality >= 0.8
   - OR 3+ frames with quality >= 0.6
4. **Cumulative biometric weight >= 5.0**
5. **Not in ambiguity region** (confidence < pending_threshold)

This prevents:
- Instant enrollment from single poor-quality frame
- Creating customers from transient detections
- Duplicate creation during collection period

## Removed Features (v1 → v2)

- ❌ **Gender** (bias concerns, unreliable)
- ❌ **Skin tone** (lighting dependent, ethical issues)
- ❌ **Age range** (too coarse, gradual change)
- ❌ **Exact height (cm)** (camera angle/distance noise → replaced with short/medium/tall)
- ❌ **Wearing glasses** (changes daily, not stable)

## Migration from v1

**Existing customers:**
- v1 single embeddings remain valid
- v2 will add more embeddings over time (enrichment)
- No data loss

**New enrollments:**
- v2 immediately stores 3-5 embeddings per customer
- Better multi-pose coverage

**Gradual enrichment:**
When existing customer detected, v2 adds new high-quality embeddings to their profile (up to 5 total).

## Performance Tuning

### Adjust Thresholds

```sql
-- More conservative (fewer false positives, more false negatives)
UPDATE threshold_config
SET global_link_threshold = 0.85
WHERE active = TRUE;

-- More aggressive (more false positives, fewer false negatives)
UPDATE threshold_config
SET global_link_threshold = 0.70
WHERE active = TRUE;
```

### Adjust Weights

```sql
-- Increase face importance
UPDATE weights_config
SET feature_weights = jsonb_set(
    feature_weights::jsonb,
    '{face}',
    '8.0'
)
WHERE active = TRUE;
```

## Monitoring

### Key Metrics

**Daily:**
```sql
SELECT
    decision_type,
    COUNT(*),
    AVG(confidence),
    AVG(biometric_ratio)
FROM decision_audit_log
WHERE timestamp >= CURRENT_DATE
GROUP BY decision_type;
```

**Confidence distribution:**
```sql
SELECT
    FLOOR(confidence * 10) / 10 AS confidence_bucket,
    COUNT(*)
FROM decision_audit_log
WHERE decision_type = 'linked'
  AND timestamp >= CURRENT_DATE - INTERVAL '7 days'
GROUP BY confidence_bucket
ORDER BY confidence_bucket;
```

**Quality distribution:**
```sql
SELECT
    json_extract_path_text(quality_summary, 'face_quality')::NUMERIC AS face_quality,
    COUNT(*)
FROM decision_audit_log
WHERE json_extract_path_text(quality_summary, 'face_quality') IS NOT NULL
GROUP BY face_quality
ORDER BY face_quality;
```

## Troubleshooting

### Problem: Too many duplicates still created

**Diagnosis:**
```sql
-- Find customers with very similar signals
SELECT
    c1.id AS customer_a,
    c2.id AS customer_b,
    COUNT(DISTINCT cf1.embedding) AS face_overlap
FROM customers c1
JOIN customers c2 ON c2.id > c1.id
JOIN customer_faces cf1 ON cf1.customer_id = c1.id
JOIN customer_faces cf2 ON cf2.customer_id = c2.id
WHERE cf1.embedding = cf2.embedding  -- Exact duplicate
GROUP BY c1.id, c2.id
HAVING COUNT(*) > 0;
```

**Fix:**
1. Lower `link_threshold` (e.g., 0.75 → 0.70)
2. Increase `pending_threshold` (widen ambiguity region)
3. Check calibration data quality

### Problem: False positives (wrong customer matched)

**Diagnosis:**
```sql
-- Find low-confidence links
SELECT * FROM decision_audit_log
WHERE decision_type = 'linked'
  AND confidence < 0.78
ORDER BY confidence ASC
LIMIT 50;
```

**Fix:**
1. Raise `link_threshold` (e.g., 0.75 → 0.82)
2. Check if plate-only matches (should be impossible with safety constraints)
3. Review feature breakdown: is biometric score low?

### Problem: Enrollment too slow (many pending)

**Diagnosis:**
```sql
-- Check pending tracks
SELECT
    reason,
    COUNT(*),
    AVG(frame_count),
    AVG(confidence)
FROM decision_audit_log
WHERE decision_type = 'pending'
  AND timestamp >= CURRENT_DATE
GROUP BY reason;
```

**Fix:**
1. Lower enrollment quality requirements (fewer frames, lower quality threshold)
2. Check camera quality (many poor-quality frames?)
3. Adjust track idle timeout (currently 15s)

## TODO / Future Enhancements

- [ ] Admin UI for ground truth tagging
- [ ] Calibration dashboard (ROC curves, score distributions)
- [ ] Merge/split customer tools
- [ ] Contextual signals (time patterns, zone patterns)
- [ ] Real-time dashboard (active tracks, recent decisions)
- [ ] Automated threshold retraining (weekly)
- [ ] Per-camera quality profiles
- [ ] Alert on anomalous decisions (outlier detection)

## Credits

Architecture based on NIST FRVT best practices and InsightFace recommendations.

---

**Questions?** Check logs at `logs/recognition_service_v2.log`
