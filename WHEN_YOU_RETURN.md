# 🚀 Quick Start - When You Return

## TL;DR
Run this ONE command on the Mini PC:
```powershell
cd C:\Users\Quintusz\farm_pos_web
.\quick_start.ps1
```

That's it! The script will:
1. Pull latest code from GitHub ✓
2. Download face models (if missing) ✓
3. Restart services ✓
4. Run system diagnostics ✓

Then walk in front of the indoor camera to test auto-enrollment.

---

## What I Did While You Were Away

### ✅ Completed

1. **Full 12-Feature Auto-Enrollment System**
   - Weighted multi-signal voting across plate, face, gait + 9 physical attributes
   - Auto-enrollment when 2+ signals or 1 signal + strong physical profile
   - Physical attribute extraction (height, gender, hair, skin, build, age, eyes, glasses, facial hair)
   - Threshold: ≥5.0 points for identification (out of ~13.7 possible)

2. **Database Schema**
   - All 8 new tables created
   - Existing tables updated (customers, sales)
   - Migrations added to app.py

3. **Purchase Linking**
   - Sales.customer_id foreign key added
   - POST /api/transactions accepts customer_id
   - Frontend includes customer_id in checkout payload

4. **Till Badge (Frontend)**
   - Polls /api/till/active_customer every 5 seconds when Teller tab active
   - Shows badge with customer name when detected (only if name populated)
   - Includes customer_id in transaction automatically

5. **Recognition Service**
   - Already had Frigate polling (every 30s)
   - Added physical attribute extraction
   - Added weighted 12-feature voting
   - Added auto-enrollment logic

6. **Diagnostic Tools**
   - 7 new test scripts created
   - Comprehensive system test: `test_full_system.ps1`
   - Quick start: `quick_start.ps1`

---

## Current Status

### ✓ Working
- Person detection (Frigate sees people on indoor camera)
- Plate detection (ANPR working)
- Database schema (all tables and columns exist)
- Frontend polling (till badge code ready)
- Auto-deployment (picks up GitHub changes in ~60 seconds)

### ⚠ Needs Fixing (Quick Start Script Handles This)
- Face models need downloading (~100MB, one-time)
- Flask might need restart (connection was resetting earlier)
- Services need restart to pick up latest code

---

## What to Expect After Quick Start

When you walk in front of the **indoor camera**, you should see in the logs:

```
2026-05-12 18:00:00 [INFO] Received event: indoor-person-1234567
2026-05-12 18:00:01 [INFO] Extracted attributes: height=175cm, hair=black, gender=male, build=average
2026-05-12 18:00:02 [INFO] Auto-enrolling new customer (signals=2, physical=True)
2026-05-12 18:00:02 [INFO] Created customer CUST-0002 (ID=2)
2026-05-12 18:00:02 [INFO] Enrolled face embedding
2026-05-12 18:00:02 [INFO] Enrolled gait features
2026-05-12 18:00:02 [INFO] Stored physical attributes
```

When you return to the camera later:
```
2026-05-12 18:30:00 [INFO] Customer 2 identified (score=9.5, features={'face': 2.9, 'gait': 1.8, 'gender': 1.0, 'height': 0.9, ...})
```

When you stand at the **till camera**:
- Badge appears in POS: "Customer: [Your Name]" (if you added a name to the customer)
- Or no badge if customer still anonymous (CUST-0002)

When you **complete a sale** while badge is showing:
- Sale is linked to customer automatically
- Customer profile shows purchase history

---

## Testing Checklist

After running `quick_start.ps1`:

### Basic Tests
- [ ] Walk in front of indoor camera
- [ ] Check logs: `Get-Content logs\recognition_service.log -Tail 20 -Wait`
- [ ] See auto-enrollment message
- [ ] Walk in front of camera again → see identification message (not re-enrollment)

### Purchase Linking
- [ ] Stand at till camera
- [ ] Open POS: http://100.86.32.13:5000
- [ ] Login: admin / admin123
- [ ] Go to Teller tab
- [ ] Badge should appear if customer has name
- [ ] Add product to cart
- [ ] Complete sale
- [ ] Go to Customers tab → view profile → purchase should appear

### Multi-Signal Recognition
- [ ] Test with car (plate + face + gait)
- [ ] Test on foot (face + gait + physical attributes)
- [ ] Test with different car (face + gait should still identify)
- [ ] Test with mask/sunglasses (plate + gait + physical attributes)

---

## If Something's Not Working

### Flask Not Responding
```powershell
Restart-Service FarmPOS-qa
curl.exe http://127.0.0.1:5000/api/me
```

### Face Models Missing
```powershell
.venv\Scripts\python.exe download_face_models.py
Restart-Service FarmPOS-Recognition
```

### No Person Detections
```powershell
# Check Frigate events
curl http://127.0.0.1:8971/api/events?limit=10

# Should see "indoor - person" events
# If not, check camera angles in Frigate web UI
```

### Recognition Service Not Processing Events
```powershell
# Check logs
Get-Content logs\recognition_service.log -Tail 50

# Look for:
# - "Received event" messages
# - "Face models not found" (means models need downloading)
# - "POS login error" (means Flask connection issue)
```

---

## Files You Should Know About

| File | Purpose |
|------|---------|
| `quick_start.ps1` | **Run this first** - sets up everything |
| `test_full_system.ps1` | Comprehensive system test |
| `IMPLEMENTATION_STATUS.md` | **Read this** - detailed status |
| `check_db_connection.py` | Test database connectivity |
| `download_face_models.py` | Download InsightFace models |
| `restart_qa_services.ps1` | Restart Flask + Recognition |

---

## Architecture Quick Reference

```
Frigate (port 8971)
    ↓ polls every 30s
Recognition Service (port 8080)
    ├─ ANPR: fast-plate-ocr
    ├─ Face: InsightFace buffalo_l
    ├─ Gait: MediaPipe Pose
    └─ Physical: OpenCV + heuristics
    ↓ API calls
Flask QA (port 5000)
    ├─ Customer management
    ├─ Transaction processing
    └─ Till detection API
    ↓
PostgreSQL (farm_pos database)
    ├─ customers (with new fields)
    ├─ sales (with customer_id)
    └─ 8 new tables
```

---

## What's Next (Future Phases)

Once basic auto-enrollment works, next phases:

**Phase 2**: Visit Session Tracking
- Dwell time calculation (entry → checkout duration)
- Customer analytics (avg basket, visit frequency)

**Phase 3**: Customer Reconciliation
- Detect duplicate customers (same person enrolled twice)
- Auto-merge at ≥8.0 weighted overlap score
- Flag for manual review at 4.5-7.9 score

**Phase 4**: Advanced Analytics
- Product preferences
- Customer Lifetime Value (CLV)
- Browse-to-purchase conversion rate

---

## 🎉 You're All Set!

Just run:
```powershell
cd C:\Users\Quintusz\farm_pos_web
.\quick_start.ps1
```

Then walk in front of the indoor camera and watch the magic happen! 🚀

**Questions?** Check `IMPLEMENTATION_STATUS.md` for detailed information.

**Logs**: `Get-Content logs\recognition_service.log -Tail 20 -Wait`
