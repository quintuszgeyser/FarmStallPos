# Recognition v2.0 - Quick Start

## What Changed?

**Old system (v1):**
- ❌ Instant enrollment from single frame
- ❌ Single embedding per customer
- ❌ Blurry faces, occluded bodies counted
- ❌ Plate alone could link identity
- ❌ Fixed 10.0 max score (unfair when features missing)
- ❌ Guessed thresholds

**New system (v2):**
- ✅ 30-60s observation period before enrollment
- ✅ 3-5 embeddings per customer (multi-pose)
- ✅ Quality gates (blurry/occluded rejected)
- ✅ Plate requires biometric first (support only)
- ✅ Normalized scoring (earned/available)
- ✅ Calibrated thresholds from ground truth

**Result:** Dramatically fewer duplicates, better accuracy, explainable decisions.

---

## Installation (5 minutes)

### 1. Run Database Migration

```bash
# From project directory
psql -U farmstall -d farm_pos_prod -f migrate_recognition_v2.sql
```

Output:
```
ALTER TABLE
ALTER TABLE
CREATE TABLE
CREATE INDEX
...
INSERT 0 1  (threshold config)
INSERT 0 1  (weights config)
```

### 2. Test New Service

```bash
# Stop old service
# (Ctrl+C if running in terminal, or stop Windows service)

# Start v2
python recognition_service_v2.py
```

Expected output:
```
2026-05-13 10:30:00 [INFO] Recognition service v2.0 starting
2026-05-13 10:30:00 [INFO] Weights version: v2.0_production
2026-05-13 10:30:00 [INFO] Threshold version: v1.0_initial
2026-05-13 10:30:01 [INFO] Logged in to POS API
2026-05-13 10:30:01 [INFO] Customer cache refreshed: 45 customers
2026-05-13 10:30:01 [INFO] InsightFace loaded (SCRFD + ArcFace)
2026-05-13 10:30:01 [INFO] MediaPipe Pose loaded
2026-05-13 10:30:01 [INFO] Webhook server listening on port 8080
```

### 3. Verify Working

Trigger a person detection (walk in front of camera).

Check logs:
```bash
tail -f logs/recognition_service_v2.log
```

Look for:
```
[INFO] Face extracted successfully: quality=0.87
[INFO] Gait extracted successfully: quality=0.72
[INFO] Track abc123 pending (age=5.2s, confidence=0.68)
```

---

## Next Steps

### Step 1: Monitor for 24 Hours

Let the system run and observe:
- Are duplicates still being created?
- Are existing customers being recognized?
- Check decision audit log

```sql
-- Daily summary
SELECT
    decision_type,
    COUNT(*),
    AVG(confidence),
    AVG(biometric_ratio)
FROM decision_audit_log
WHERE timestamp >= CURRENT_DATE
GROUP BY decision_type;
```

Expected (first day):
```
decision_type | count | avg_confidence | avg_biometric_ratio
--------------+-------+----------------+--------------------
linked        |   120 |          0.832 |               0.91
pending       |    45 |          0.623 |               0.67
enroll        |     8 |          0.551 |               0.74
```

### Step 2: Collect Calibration Data (Week 1)

**Goal:** 100+ ground truth pairs

When browsing recent detections in admin UI:

1. **For true matches:** "This IS customer #123" ✅
   - Same person appearing at different times
   - Different angles, lighting, clothing

2. **For false matches:** "This is NOT customer #123" ❌
   - Different people who look similar
   - System incorrectly suggested match

Aim for **50/50 split** (50 true, 50 false).

### Step 3: Run Calibration (After 100 pairs)

```python
from calibration import CalibrationDataset

cal = CalibrationDataset()

# Check data quality
print(f"Ground truth pairs: {cal.get_pair_count()}")

# Compute ROC
roc = cal.compute_roc_curve()
print(f"AUC: {roc['auc']:.3f}")  # Should be > 0.90

# Derive optimal thresholds
result = cal.derive_thresholds_from_roc(
    target_fpr=0.01,  # 1% false positive rate
    target_fnr=0.05   # 5% false negative rate
)

print(f"Link threshold: {result['link_threshold']:.3f}")
print(f"Pending threshold: {result['pending_threshold']:.3f}")
```

### Step 4: Update Thresholds

```sql
INSERT INTO threshold_config (
    version,
    global_link_threshold,
    global_pending_threshold,
    active,
    calibration_metadata
)
VALUES (
    'v1.1_calibrated_2026_05_20',
    0.82,  -- From calibration
    0.58,
    TRUE,
    '{"auc": 0.94, "true_count": 105, "false_count": 98}'::JSON
);

-- Deactivate old
UPDATE threshold_config SET active = FALSE WHERE version != 'v1.1_calibrated_2026_05_20';
```

Restart service to pick up new thresholds.

---

## Troubleshooting

### Still seeing duplicates?

**Check decision audit:**
```sql
SELECT
    customer_id,
    confidence,
    biometric_ratio,
    feature_breakdown
FROM decision_audit_log
WHERE decision_type = 'enroll'
ORDER BY timestamp DESC
LIMIT 20;
```

**Common causes:**
1. **Threshold too low** → raise `global_link_threshold`
2. **Poor quality biometrics** → check camera angles, lighting
3. **Need more calibration data** → collect more ground truth

### Too many pending (not enrolling)?

**Check pending reasons:**
```sql
SELECT
    reason,
    COUNT(*),
    AVG(frame_count)
FROM decision_audit_log
WHERE decision_type = 'pending'
  AND timestamp >= CURRENT_DATE
GROUP BY reason;
```

**Common causes:**
1. `insufficient_quality` → camera quality issues
2. `insufficient_time` → tracks too short (< 60s)
3. `ambiguous_match` → in gray zone (calibrate thresholds)

### False positives (wrong customer matched)?

**Find low-confidence matches:**
```sql
SELECT * FROM decision_audit_log
WHERE decision_type = 'linked'
  AND confidence < 0.78
ORDER BY confidence ASC
LIMIT 10;
```

**Fix:** Raise `global_link_threshold` (e.g., 0.75 → 0.80).

---

## Performance Expectations

**First week (with initial thresholds):**
- 60-80% of existing customers recognized
- 10-20% new customers enrolled
- 20-30% pending (waiting for more evidence)
- ~5% false positives (acceptable during calibration)

**After calibration (week 2+):**
- 85-95% of existing customers recognized
- 15-25% new customers enrolled
- 5-10% pending
- <1% false positives

**Multi-embedding enrichment:**
- Existing customers gain 1-3 new embeddings per week
- Recognition accuracy improves over time (more poses covered)

---

## Rolling Back

If v2 has issues, roll back to v1:

```bash
# Stop v2
pkill -f recognition_service_v2

# Start v1
python recognition_service.py
```

Database schema is backward-compatible (new columns are optional).

---

## Questions?

- Logs: `logs/recognition_service_v2.log`
- Full docs: `RECOGNITION_V2_README.md`
- Decision audit: `SELECT * FROM decision_audit_log ORDER BY timestamp DESC LIMIT 50;`
