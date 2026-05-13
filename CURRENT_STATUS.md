# Recognition Service v2 - Current Status
**Last Updated:** 2026-05-13 22:30
**Status:** BLOCKED - Frigate poller not processing events

## What We're Trying to Achieve
Deploy Recognition Service v2.0 with quality-gated multi-biometric identification that:
- Collects observations for 30-60s before enrolling new customers
- Stores multiple embeddings (3-5) per customer for different angles/lighting
- Uses normalized scoring (earned/available) so missing features don't unfairly penalize
- Has quality gates to reject poor signals (face >=0.5, gait >=0.6, plate >=0.8)
- Implements three-state decision: linked (match existing), pending (insufficient evidence), enroll (new customer)

## Current Problem
**Recognition service runs but does NOT process Frigate detection events.**

### Symptoms
1. Service starts successfully, models load, webhook server listens on port 8080
2. Customer cache refreshes every ~60 seconds (working)
3. Frigate IS detecting people (verified via `curl http://127.0.0.1:8971/api/events`)
4. **BUT**: No "Processing new event" log entries appear
5. **AND**: No "Frigate poller thread started" log message (thread not starting at all)

### What We've Tried
1. ✅ Fixed database - cleared all customers from both QA (farm_pos) and PROD (farm_pos_prod)
2. ✅ Added 60-second time window to poller (only process events that ended <60s ago)
3. ✅ Fixed time module conflict (used `time_module` alias in poller)
4. ✅ Added debug logging (INFO level for startup, DEBUG for poll attempts)
5. ✅ Changed logging level to DEBUG
6. ❌ Thread still not starting - no "Frigate poller thread started" message in logs

### Code Location
- Main service: `recognition_service_v2.py`
- Poller function: lines 1120-1149
- Thread start: line 1203 in main block

### Recent Frigate Event (Verified Working)
```
Event ID: 1778703581.193841-a8oyml
Camera: indoor
Label: person
End time: 1778703585.196684 (Unix timestamp)
Has snapshot: true
```

### Next Debugging Steps
1. Check if there's an exception when starting the poller thread (not visible in logs)
2. Verify the thread is actually created (might be silently failing)
3. Add try-except around thread creation with logging
4. Consider running service in foreground to see stderr output
5. Check if `threading.Thread()` is working at all (test with simple thread first)

## Environment Details
- **Mini PC IP:** 10.0.0.101
- **User:** Quintusz
- **Password:** 2311 (still required for SSH - passwordless setup incomplete)
- **Frigate URL:** http://127.0.0.1:8971
- **POS QA URL:** http://127.0.0.1:5000
- **Webhook Port:** 8080
- **Python:** C:/Users/Quintusz/farm_pos_web/.venv/Scripts/python.exe
- **Log File:** C:/Users/Quintusz/farm_pos_web/logs/recognition_service_v2.log

## Database State
### QA (farm_pos)
```sql
customers: 0
customer_faces: 0
customer_gaits: 0
customer_plates: 0
```

### Production (farm_pos_prod)
```sql
customers: 0
customer_faces: 0
customer_gaits: 0
```

Both databases are clean and ready for first enrollment test.

## Key Commands

### Check if service is running
```powershell
ssh Quintusz@10.0.0.101 'powershell -Command "Get-Process python -ErrorAction SilentlyContinue | Measure-Object | Select-Object -ExpandProperty Count"'
```

### View recent logs
```powershell
ssh Quintusz@10.0.0.101 'powershell -Command "Get-Content C:/Users/Quintusz/farm_pos_web/logs/recognition_service_v2.log -Tail 30"'
```

### Restart recognition service
```powershell
ssh Quintusz@10.0.0.101 'powershell -Command "Stop-Process -Name python -Force -ErrorAction SilentlyContinue; Start-Sleep -Seconds 2; Start-Process -FilePath C:/Users/Quintusz/farm_pos_web/.venv/Scripts/python.exe -ArgumentList recognition_service_v2.py -WorkingDirectory C:/Users/Quintusz/farm_pos_web -WindowStyle Hidden"'
```

### Check Frigate events
```bash
ssh Quintusz@10.0.0.101 'curl -s "http://127.0.0.1:8971/api/events?limit=5&has_snapshot=1"' | python -m json.tool
```

### Pull latest code
```powershell
ssh Quintusz@10.0.0.101 "cd C:/Users/Quintusz/farm_pos_web && git pull origin main"
```

## Git Status
- **Current Branch:** main
- **Latest Commit:** e0fb6bd - "Add debug logging to diagnose poller issue"
- **Changes:**
  - Added "Frigate poller thread started" log message
  - Added "Polling Frigate for events..." DEBUG log
  - Added detailed poll result logging
  - Changed logging level from INFO to DEBUG

## What's Working
1. ✅ Recognition service starts without crashes
2. ✅ Models load successfully (InsightFace, MediaPipe, plate OCR)
3. ✅ Database connection works
4. ✅ POS API login works
5. ✅ Customer cache refresh works (every ~60s)
6. ✅ Webhook server listens on port 8080
7. ✅ Frigate NVR is running and detecting people
8. ✅ Frigate API responds to queries
9. ✅ Database migration complete (all v2 tables exist)

## What's NOT Working
1. ❌ Frigate poller thread does not start
2. ❌ No events are processed
3. ❌ No face/gait extraction happens
4. ❌ No customer enrollment occurs
5. ❌ SSH passwordless authentication (not critical, can use password)

## Files Modified Today
1. `recognition_service_v2.py` - Added event time filtering, debug logging
2. `migrate_recognition_v2.sql` - Already run, no changes needed
3. `RECOGNITION_V2_README.md` - Needs update with current status
4. `QUICK_START_V2.md` - Needs update with troubleshooting steps

## Next Session Action Plan
1. Add exception handling around thread creation with explicit logging
2. Test if threading works at all by creating a dummy test thread
3. Run service in foreground mode to capture all stderr output
4. Check if there's a race condition (thread starting before logger configured)
5. Consider using a different threading approach (ThreadPoolExecutor?)
6. If all else fails, use webhook approach instead of polling

## Critical Code Snippet (Poller)
```python
def poll_frigate_events():
    """Background poller for Frigate events"""
    import time as time_module
    logger.info('Frigate poller thread started')  # NOT APPEARING IN LOGS
    while True:
        try:
            logger.debug('Polling Frigate for events...')  # NOT APPEARING
            r = requests.get(f'{FRIGATE_URL}/api/events?limit=20&has_snapshot=1', timeout=10)
            if r.ok:
                events = r.json()
                now = time_module.time()
                new_count = 0
                recent_count = 0

                for ev in events:
                    eid = ev.get('id')
                    end_time = ev.get('end_time')

                    # Only process events that ended in the last 60 seconds
                    if end_time and (now - end_time) <= 60:
                        recent_count += 1
                        if eid and eid not in _seen_events:
                            new_count += 1
                            _seen_events.add(eid)
                            logger.info(f'Processing new event {eid[:20]}...')
                            threading.Thread(target=process_event, args=(ev,), daemon=True).start()

                logger.debug(f'Frigate poll complete: {len(events)} total, {recent_count} recent, {new_count} new')
        except Exception as e:
            logger.warning(f'Frigate poll error: %s', e)
        time_module.sleep(30)

# Thread start in main block (line 1203)
threading.Thread(target=poll_frigate_events, daemon=True).start()
```

## Hypothesis
The poller thread is either:
1. Not starting at all (no exception visible)
2. Starting but crashing immediately and silently
3. Starting but blocked/hung on first iteration
4. Starting but logger not configured when first message fires

The lack of ANY log output from the poller (not even the startup message or errors) suggests #1 or #2.
