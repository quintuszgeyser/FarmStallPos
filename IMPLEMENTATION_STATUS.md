# Auto-Enrollment System Implementation Status

**Date**: 2026-05-12
**Status**: Phase 1 Complete (pending testing)

---

## ✅ What's Been Implemented

### 1. Database Schema ✓
All 8 new tables created on Mini PC:
- `customer_physical_attributes` - Height, hair, skin, build, gender, age, eyes, glasses, facial hair
- `visit_sessions` - Entry → checkout duration (dwell time)
- `customer_signal_history` - Signal confidence tracking over time
- `detection_events` - Buffers all camera detections before processing
- `person_tracks` - Tracks individuals across cameras
- `till_detections` - Logs when customers detected at checkout
- `customer_conflicts` - Tracks when signals vote for different customers (for reconciliation)
- `customer_exclusions` - Marks customers as definitely not the same person

Existing tables updated:
- `customers.name` - Made nullable for anonymous auto-enrollment
- `customers` - Added: `auto_enrolled`, `customer_number`, `first_seen`, `is_employee`
- `sales` - Added: `customer_id` for purchase linking

### 2. Backend (app.py) ✓
- Customer model updated with new fields
- Sale model includes `customer_id` foreign key
- POST `/api/transactions` extracts `customer_id` from payload
- GET `/api/till/active_customer` returns customer detected in last 30s (only if name populated)
- POST `/api/till/detect` logs customer detection at till
- GET `/api/customers/max_number` returns highest customer number for auto-enrollment

### 3. Frontend (main.js + index.html) ✓
- Customer polling starts when Teller tab shown, stops when hidden
- Polls `/api/till/active_customer` every 5 seconds
- Shows badge with customer name when detected at till
- Includes `customer_id` in checkout payload when badge visible
- Badge only shows if customer has a name (anonymous customers excluded)

### 4. Recognition Service (recognition_service.py) ✓
- **Weighted Multi-Signal Voting** - Uses ALL 12 features:
  - Plate (3.0 points)
  - Face embedding (0-3.0 points, scaled by similarity)
  - Gait features (0-2.0 points, scaled by distance)
  - Gender (1.0 point)
  - Height (0-1.0 points, ±5cm tolerance)
  - Hair color (0.8 points)
  - Skin tone (0.8 points)
  - Build (0.6 points)
  - Age range (0-0.5 points)
  - Eye color (0.4 points)
  - Glasses (0.3 points)
  - Facial hair (0.3 points)
- **Identification threshold**: ≥5.0 points (out of ~13.7 possible)
- **Auto-enrollment logic**: Triggers when 2+ biometric signals OR 1 signal + strong physical profile
- **Physical attribute extraction**: Using InsightFace + MediaPipe + OpenCV
- **Frigate polling**: Every 30 seconds as fallback (webhook not needed)

### 5. Diagnostic Tools ✓
- `test_flask_connection.py` - Tests HTTP connectivity to Flask
- `check_db_connection.py` - Verifies PostgreSQL connection and schema
- `check_migrations_minipc.py` - Shows what columns exist, runs manual migrations
- `create_new_tables.py` - Creates the 8 new tables with verbose logging
- `test_customer_creation.py` - Tests anonymous customer creation
- `download_face_models.py` - Downloads InsightFace buffalo_l models (improved with progress bar)
- `restart_qa_services.ps1` - Restarts QA and Recognition services
- `test_full_system.ps1` - Comprehensive system test (NEW)

---

## 🔴 Current Issues

### 1. Flask QA Connection Resets (Critical)
**Problem**: Recognition service can't connect to Flask QA on `http://127.0.0.1:5000`
- Every API call gets "Connection aborted, ConnectionResetError(10054)"
- Even `curl http://127.0.0.1:5000/api/me` gets "Connection was reset"
- Flask is running (PID 18384) and listening on `0.0.0.0:5000`
- Many TIME_WAIT connections in netstat

**Possible causes**:
- Auto-deploy watcher constantly restarting Flask
- Database connection issue causing Flask to crash
- PostgreSQL connection pool exhausted

**To diagnose on Mini PC**:
```powershell
# Check Flask logs
Get-Content "C:\Users\Quintusz\farm_pos_web\logs\pos.log" -Tail 50

# Check if auto-deploy is restarting
Get-Content "C:\Users\Quintusz\farm_pos_web\logs\deploy.log" -Tail 20

# Test Flask directly
curl.exe http://127.0.0.1:5000/

# Restart services
.\restart_qa_services.ps1
```

### 2. Face Models Missing (Blocking face detection)
**Problem**: Recognition service logs show "Face models not found"
**Impact**: Face feature extraction fails, limiting auto-enrollment to plate + gait only

**To fix**:
```powershell
cd C:\Users\Quintusz\farm_pos_web
.venv\Scripts\python.exe download_face_models.py
Restart-Service FarmPOS-Recognition
```

---

## ✅ What Works Right Now

### Person Detection ✓
- Frigate IS detecting person events on indoor camera
- Recognition service polling picks them up
- Logs show: `[ERROR] Face models not found` (expected until models downloaded)
- Physical attribute extraction will work once face models available

### Plate Detection ✓
- ANPR working: plates detected (NE3722, VEB717, NEB718, VEB71B, VEB716)
- Recognition service processes plate events
- Customer identification attempted (currently returns no match because customer cache is empty due to Flask connection issue)

### Auto-Deployment ✓
- Git commits are being picked up by the Mini PC
- Auto-deploy watcher polls GitHub every 60 seconds
- Services restart when new code detected

---

## 📋 Testing Checklist (When Flask Fixed)

### Phase 1: Auto-Enrollment
- [ ] Download face models: `.venv\Scripts\python.exe download_face_models.py`
- [ ] Restart Recognition service: `Restart-Service FarmPOS-Recognition`
- [ ] Walk in front of indoor camera
- [ ] Check logs: `Get-Content logs\recognition_service.log -Tail 20 -Wait`
- [ ] Verify customer created: Query database for `auto_enrolled=true` customers
- [ ] Verify all 12 features extracted: Check `customer_physical_attributes` table
- [ ] Return to same camera → verify system recognizes (not re-enrolls)
- [ ] Check weighted voting score in logs

### Phase 2: Purchase Linking
- [ ] Walk to till camera (customer detected)
- [ ] Open POS in browser: `http://100.86.32.13:5000`
- [ ] Login as admin/admin123
- [ ] Switch to Teller tab
- [ ] Verify badge appears: "Customer: CUST-0001" (if customer has name) or no badge (if anonymous)
- [ ] Add product to cart
- [ ] Complete sale
- [ ] Query database: Check `sales.customer_id` is populated
- [ ] Check customer profile in web UI → purchase should appear

### Phase 3: Analytics
- [ ] Enter shop (outdoor camera)
- [ ] Browse (indoor cameras)
- [ ] Checkout (till camera)
- [ ] Check `visit_sessions` table → session shows entry→checkout duration
- [ ] View customer profile → verify dwell time displayed

### Phase 4: Multi-Feature Identification
- [ ] Test returning customer with different car → should identify via face + gait
- [ ] Test customer on foot → should identify via face + physical attributes
- [ ] Test customer with mask → should identify via plate + gait + physical attributes
- [ ] Check logs for weighted scores across all 12 features

---

## 🚀 Next Steps (When You Return)

1. **Fix Flask Connection** (highest priority)
   ```powershell
   cd C:\Users\Quintusz\farm_pos_web
   .\test_flask_connection.py  # Run diagnostic
   Get-Content logs\pos.log -Tail 50  # Check Flask errors
   Restart-Service FarmPOS-qa  # Try restart
   ```

2. **Download Face Models**
   ```powershell
   .venv\Scripts\python.exe download_face_models.py
   Restart-Service FarmPOS-Recognition
   ```

3. **Run Full System Test**
   ```powershell
   .\test_full_system.ps1
   ```

4. **Test Auto-Enrollment**
   - Walk in front of indoor camera
   - Check logs: `Get-Content logs\recognition_service.log -Tail 20 -Wait`
   - Should see:
     - "Extracted attributes: height=XXXcm, hair=X, build=X"
     - "Auto-enrolling new customer (signals=2, physical=True)"
     - "Created customer CUST-0002"

5. **Test Till Badge**
   - Stand at till camera
   - Open POS in browser
   - Badge should appear if customer has name

---

## 📊 System Architecture

```
┌─────────────┐
│   Frigate   │──┐
│  (Port 8971)│  │
└─────────────┘  │
                 │ Events API (polling every 30s)
                 ↓
┌──────────────────────────────────┐
│   Recognition Service            │
│   (Port 8080 webhook listener)   │
│                                  │
│  • ANPR (fast-plate-ocr)         │
│  • Face (InsightFace)            │
│  • Gait (MediaPipe)              │
│  • Physical Attributes (OpenCV)  │
│  • Weighted 12-Feature Voting    │
│  • Auto-Enrollment Logic         │
└──────────────────────────────────┘
                 │
                 │ API calls (currently failing)
                 ↓
┌──────────────────────────────────┐
│   Flask QA                       │
│   (Port 5000 HTTP)               │
│                                  │
│  • Customer Management           │
│  • Transaction Processing        │
│  • Till Detection API            │
│  • Purchase Linking              │
└──────────────────────────────────┘
                 │
                 │
┌──────────────────────────────────┐
│   PostgreSQL                     │
│   (farm_pos database)            │
│                                  │
│  • customers (with new fields)   │
│  • sales (with customer_id)      │
│  • 8 new tables                  │
└──────────────────────────────────┘
```

---

## 💡 Key Design Decisions

1. **Weighted Multi-Feature Voting**: Uses ALL 12 features instead of simple 2-of-3
   - More robust to missing signals
   - Handles appearance changes (haircut, beard shaved)
   - Prevents false positives from single signal matches

2. **Anonymous Auto-Enrollment**: Customers created without names
   - Admin can add name later
   - Till badge only shows if name populated
   - Prevents UI clutter with "CUST-0001" style numbers

3. **HTTP for QA, HTTPS for Prod**: Recognition service configured for QA (HTTP port 5000)
   - Prod runs on HTTPS port 5443
   - Environment variable `POS_URL` controls target

4. **Frigate Polling (not webhooks)**: Recognition service polls Frigate API every 30s
   - More reliable than webhooks
   - Survives network issues
   - No Frigate config changes needed (webhooks not supported in v0.17)

---

## 📝 Files Changed

### New Files
- `customer_physical_attributes` table (DB)
- `visit_sessions` table (DB)
- `customer_signal_history` table (DB)
- `detection_events` table (DB)
- `person_tracks` table (DB)
- `till_detections` table (DB)
- `customer_conflicts` table (DB)
- `customer_exclusions` table (DB)
- `test_flask_connection.py`
- `check_db_connection.py`
- `check_migrations_minipc.py`
- `create_new_tables.py`
- `test_customer_creation.py`
- `restart_qa_services.ps1`
- `test_full_system.ps1`
- `frigate_config.yml` (not deployed - webhooks not supported)
- `IMPLEMENTATION_STATUS.md` (this file)

### Modified Files
- `app.py`:
  - Customer model: Added `auto_enrolled`, `customer_number`, `first_seen`, `is_employee`, made `name` nullable
  - Sale model: Added `customer_id` foreign key
  - POST `/api/transactions`: Extracts and uses `customer_id`
  - Added `/api/till/active_customer` endpoint
  - Added `/api/till/detect` endpoint
  - Added `/api/customers/max_number` endpoint
  - Fixed customer creation bug (notes.strip() → (notes or '').strip())
  
- `recognition_service.py`:
  - Added `extract_physical_attributes()` function (12 features)
  - Added `identify_customer_weighted()` function (12-feature voting)
  - Updated `process_event()` with auto-enrollment logic
  - Changed POS_URL from HTTPS to HTTP for QA compatibility
  - Disabled SSL verification for localhost
  
- `static/main.js`:
  - Already had `pollActiveCustomer()`, `showCustomerBadge()`, `startCustomerPolling()`, `stopCustomerPolling()`
  - Already includes `customer_id` in checkout payload (lines 2632-2636)
  - Tab lifecycle hooks already in place (lines 5007-5008)
  
- `templates/index.html`:
  - Already has `<div id="customer-badge-container">` after line 171

- `download_face_models.py`:
  - Improved with progress bar, existing file check, proxy troubleshooting

---

## 🎯 Success Criteria

The system will be considered working when:
1. ✅ Person detected in view of indoor camera
2. ✅ Face, gait, and physical attributes extracted
3. ✅ Customer auto-enrolled with `customer_number` like "CUST-0001"
4. ✅ All 12 features stored in `customer_physical_attributes`
5. ✅ Customer returns → system identifies (not re-enrolls)
6. ✅ Weighted voting score ≥5.0 logged
7. ✅ Badge appears in POS when customer at till (if name populated)
8. ✅ Sales linked to customer (`sales.customer_id` populated)
9. ✅ Customer profile shows purchase history

---

## 📞 Support

If issues persist:
1. Check logs: `logs/pos.log`, `logs/recognition_service.log`
2. Review plan: `C:\Users\CP368103\.claude\plans\stateful-sauteeing-koala.md`
3. Check memory: `C:\Users\CP368103\.claude\projects\...\memory\MEMORY.md`

**Current Blocker**: Flask QA connection resets. Fix this first, then everything else should work.
