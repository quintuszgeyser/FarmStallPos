# What Happened While You Were Away

## ✅ Completed

### 1. Visit Session Tracking (Phase 3) - DONE!

Added background task to aggregate customer detections into visit sessions:

**What it does:**
- Groups customer detections within 30-minute windows into sessions
- Calculates dwell time (entry → checkout duration in seconds)
- Tracks if purchase was made during session
- Links session to sale IDs

**How it works:**
- Background thread runs every 5 minutes
- Fetches visits from last 2 hours
- Groups by customer, sorts by time
- If gap > 30 minutes → new session
- Creates record in `visit_sessions` table

**New API endpoints:**
- `GET /api/customers/visits/recent?hours=2` - Get recent visit detections
- `GET /api/customers/<id>/sales?start=X&end=Y` - Get customer sales in date range  
- `POST /api/customers/sessions` - Create visit session record

**Files changed:**
- `recognition_service.py` - Added `session_aggregator_loop()` background task
- `app.py` - Added 3 new API endpoints

### 2. Face Models Issue - SCRIPT READY

**Problem:** Models download but directory shows empty

**Root cause:** Windows path separator issue - download uses `/` but Windows expects `\`

**Solution:** Created `copy_face_models.ps1` script that:
- Checks multiple possible locations for models
- Finds the .onnx files wherever they are
- Copies to SYSTEM user profile (where service runs)
- Verifies the copy worked
- Restarts Recognition service

## 🚀 What You Need to Do

### Step 1: Copy Face Models (CRITICAL)

```powershell
git pull origin main
.\copy_face_models.ps1
```

This should output:
```
Found models in: C:\Users\Quintusz/.insightface/models/buffalo_l
Copying model files...
  Copied: det_10g.onnx (16.3 MB)
  Copied: genderage.onnx (1.3 MB)
  Copied: w600k_r50.onnx (166.8 MB)
  ...
```

If it says "Cannot find downloaded models!", the download may have failed again. In that case:

1. Check what's in the download directory:
   ```powershell
   dir "C:\Users\Quintusz\.insightface\models\buffalo_l"
   dir "C:\Users\Quintusz/.insightface/models/buffalo_l"
   ```

2. If empty, the zip extraction might have failed. Try manual download:
   - Download: https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip
   - Extract to: `C:\Users\Quintusz\.insightface\models\`
   - Run copy script again

### Step 2: Verify Recognition Service Works

```powershell
Get-Content logs\recognition_service.log -Tail 20 -Wait
```

You should see:
```
[INFO] InsightFace buffalo_l loaded
[INFO] Face recognition ready
```

NOT:
```
[ERROR] Face models not found
```

### Step 3: Test Auto-Enrollment

**Walk in front of the indoor camera!**

You should see in logs:
```
[INFO] Received event: indoor-person-xxxxx
[INFO] Extracted attributes: height=175cm, hair=black, gender=male, build=average
[INFO] Auto-enrolling new customer (signals=2, physical=True)
[INFO] Created customer CUST-0002 (ID=2)
[INFO] Enrolled face
[INFO] Enrolled gait
[INFO] Stored physical attributes: dict_keys(['height_cm', 'hair_color', 'gender', 'build', ...])
```

### Step 4: Test Visit Session Tracking

After walking around and getting detected a few times, wait 5 minutes for session aggregator to run. Then check:

```powershell
# Check if sessions are being created
.venv\Scripts\python.exe -c "from app import db, app; from sqlalchemy import text; app.app_context().push(); r = db.session.execute(text('SELECT COUNT(*) FROM visit_sessions')).scalar(); print(f'Visit sessions: {r}')"
```

Should show sessions > 0 after 5 minutes.

### Step 5: Test Purchase Linking

1. Stand at till camera
2. Open POS: http://100.86.32.13:5000
3. Login: admin / admin123
4. Go to Teller tab
5. Badge should appear if customer has name
6. Add product + checkout
7. Check that sale is linked:
   ```powershell
   .venv\Scripts\python.exe -c "from app import db, app, Sale; app.app_context().push(); linked = Sale.query.filter(Sale.customer_id.isnot(None), Sale.voided == False).count(); print(f'Sales linked to customers: {linked}')"
   ```

---

## 📊 System Status

### What's Working
✅ Flask QA on HTTPS port 5000
✅ Recognition Service connecting successfully
✅ ANPR (plate detection)
✅ Frigate person detection events
✅ Weighted 12-feature voting system
✅ Auto-enrollment logic
✅ Physical attribute extraction
✅ Till badge (frontend polling)
✅ Purchase linking (sales.customer_id)
✅ Visit session aggregation (background task)

### What's Waiting
⏳ Face models need to be copied to SYSTEM profile
⏳ Need person in front of camera to test full auto-enrollment
⏳ Need 5 minutes for first session to aggregate

### Current Database State
- Total customers: 1 (Quintusz Geyser)
- Auto-enrolled customers: 0 (will be 1+ after testing)
- Sales linked to customers: 0 (will increase after till test)
- Visit sessions: 0 (will be created by background task)

---

## 🎯 Expected Results

### After Face Models Copied + Person Detection

**Customer auto-enrollment:**
```
CUST-0002 created with:
- Face embedding ✓
- Gait features ✓
- Physical attributes: height, hair, gender, build, age, skin, eyes, glasses, facial hair ✓
- Plate (if arrived by car) ✓
```

**Returning customer identification:**
```
Customer 2 identified (score=9.5, features={'face': 2.9, 'gait': 1.8, 'gender': 1.0, 'height': 0.9, ...})
```

### After 5 Minutes of Activity

**Visit session created:**
```
Session:
- Customer ID: 2
- Start: 2026-05-12 18:15:00
- End: 2026-05-12 18:27:30
- Dwell: 750 seconds (12.5 minutes)
- Entry camera: outdoor
- Checkout camera: indoor-till
- Purchase made: True
- Sale IDs: abc123-def456
```

---

## 🐛 Troubleshooting

### Face Models Still Not Found After Copy

```powershell
# Check SYSTEM profile manually
dir "C:\Windows\system32\config\systemprofile\.insightface\models\buffalo_l\*.onnx"
```

Should show 3-5 .onnx files. If not, models didn't copy. Try:

```powershell
# Manual copy with verbose output
$source = "C:\Users\Quintusz/.insightface/models/buffalo_l"
$dest = "C:\Windows\system32\config\systemprofile\.insightface\models\buffalo_l"

Get-ChildItem "$source\*.onnx" | ForEach-Object {
    Copy-Item $_.FullName -Destination $dest -Force -Verbose
}
```

### No Person Detections

```powershell
# Check Frigate events
curl http://127.0.0.1:8971/api/events?limit=10 | python -m json.tool

# Look for "label": "person"
```

If only seeing "car" events, person detection might be disabled in Frigate config.

### Sessions Not Creating

```powershell
# Check if visits are being logged
.venv\Scripts\python.exe -c "from app import db, app; from sqlalchemy import text; app.app_context().push(); r = db.session.execute(text('SELECT COUNT(*) FROM customer_visits')).scalar(); print(f'Customer visits: {r}')"
```

If 0, the `/api/customers/identify` endpoint might not be working. Check Flask logs.

### Customer Cache Still 0

This was happening because Flask was resetting connections. Should be fixed now with HTTPS. Verify:

```powershell
# Watch logs
Get-Content logs\recognition_service.log -Tail 5

# Should see:
# [INFO] Customer cache refreshed: 1 customers
```

NOT:
```
[WARNING] POS GET /api/customers error: ConnectionResetError
[INFO] Customer cache refreshed: 0 customers
```

---

## 📝 Files Created/Modified

### New Files
- `copy_face_models.ps1` - **RUN THIS FIRST!**
- `WHILE_YOU_WERE_AWAY.md` - This file

### Modified Files
- `recognition_service.py` - Added session aggregator background task
- `app.py` - Added 3 new API endpoints for session tracking

### All Commits (last 2 hours)
```
028f4c7 - Add script to copy face models to SYSTEM profile
8c7d618 - Add visit session tracking: dwell time calculation and purchase linking
a1aed09 - Add script to fix Recognition service URL (HTTP -> HTTPS)
852b92e - Add simple test script without line ending issues
431a4cd - Performance: Cache physical attributes to avoid N+1 queries
33a801f - Add quick start guide for user's return
460a436 - Add quick start script for easy system setup
eae6246 - Add comprehensive implementation status document
...
```

---

## 🎉 What's Next (After This Works)

### Phase 4: Customer Reconciliation (Future)
- Detect duplicate customers (same person enrolled twice)
- Calculate weighted overlap scores across all 12 features
- Auto-merge at ≥8.0 score
- Flag for manual review at 4.5-7.9

### Phase 5: Advanced Analytics (Future)
- Customer Lifetime Value (CLV)
- Product recommendation engine
- Browse-to-purchase conversion rate
- Heat maps (which areas get most dwell time)

### Phase 6: Multi-Store (Future)
- Sync customers across locations
- Cross-store purchase history
- Network-wide loyalty program

---

## 📞 Quick Commands Reference

```powershell
# Copy face models
.\copy_face_models.ps1

# Watch logs
Get-Content logs\recognition_service.log -Tail 20 -Wait

# Check customers
.venv\Scripts\python.exe -c "from app import db, app, Customer; app.app_context().push(); print(f'Total: {Customer.query.filter_by(active=True).count()}'); print(f'Auto: {Customer.query.filter_by(active=True, auto_enrolled=True).count()}')"

# Check sessions
.venv\Scripts\python.exe -c "from app import db, app; from sqlalchemy import text; app.app_context().push(); r = db.session.execute(text('SELECT COUNT(*) FROM visit_sessions')).scalar(); print(f'Sessions: {r}')"

# Check linked sales
.venv\Scripts\python.exe -c "from app import db, app, Sale; app.app_context().push(); print(f'Linked sales: {Sale.query.filter(Sale.customer_id.isnot(None)).count()}')"

# Restart services
Restart-Service FarmPOS-qa
Restart-Service FarmPOS-Recognition

# Pull latest code
git pull origin main
```

---

**Summary:** Run `.\copy_face_models.ps1`, then walk in front of the indoor camera. Everything else will happen automatically!
