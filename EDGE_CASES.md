# Edge Cases & Test Scenarios

## Identified Edge Cases

### 1. Empty Customer Cache During Identification
**Problem:** If `_customers_cache` is empty (service just started, or refresh failed), weighted matching will return no results.
**Impact:** All customers will fail to identify and new customers will be created for existing customers.
**Fix:** Add validation in `identify_customer_weighted()` to check cache size and log warning.

### 2. Partial Physical Attribute Extraction
**Problem:** If MediaPipe fails to detect pose or face region is too small, physical attributes will be incomplete.
**Impact:** Auto-enrollment might not trigger (missing gender/height/hair), or matching will have reduced confidence.
**Fix:** Add graceful degradation - accept partial attributes, don't fail entire extraction.

### 3. Simultaneous Detection on Multiple Cameras
**Problem:** Same person detected on indoor + outdoor cameras within seconds.
**Impact:** Two `process_event()` threads running simultaneously, could create duplicate customers.
**Fix:** Add deduplication based on feature similarity within 10-second window.

### 4. Database Write Failure During Auto-Enrollment
**Problem:** If POS API call fails during customer creation, partial state (customer created but no signals enrolled).
**Impact:** Customer exists but can't be identified (no biometrics).
**Fix:** Add retry logic and cleanup on failure.

### 5. Face Models Not Loaded Yet
**Problem:** Service starts before face models copied to SYSTEM profile.
**Impact:** Face extraction returns None for all detections.
**Fix:** Already handled - `run_face()` returns None gracefully, auto-enrollment uses remaining signals.

### 6. Session Aggregator Running While Customer Still in Store
**Problem:** Customer detected at 10:00, 10:15, aggregator runs at 10:05 and closes session prematurely.
**Impact:** Customer will have multiple short sessions instead of one long session.
**Fix:** Only aggregate sessions where last detection is >30 minutes ago.

### 7. Two Customers with Identical Physical Attributes
**Problem:** Two males, both 175cm, black hair, average build, same age range.
**Impact:** Physical attribute matching gives both customers high scores even though they're different people.
**Fix:** Physical attributes alone can't reach 5.0 threshold (max ~5.2 without biometrics), requires at least one biometric match.

### 8. Customer Cache Refresh During Matching
**Problem:** `refresh_customers()` called while `identify_customer_weighted()` is reading cache.
**Impact:** Cache could be modified mid-iteration, causing race condition.
**Fix:** Already handled - `_cache_lock` protects all cache access.

### 9. API Timeout During Peak Load
**Problem:** Recognition service makes many API calls to POS (faces_raw, gaits_raw, attributes_bulk, etc).
**Impact:** If POS is under load, requests could timeout, causing `pos_get()` to return None.
**Fix:** Add timeout handling with retry logic.

### 10. Duplicate Session Creation
**Problem:** Session aggregator runs multiple times on same visit data.
**Impact:** Multiple session records created for same customer visit.
**Fix:** Add "processed" flag to customer_visits or check if session already exists for time range.

### 11. Physical Attributes Confidence Too Low
**Problem:** Camera angle bad, lighting poor - extracted attributes unreliable.
**Impact:** Wrong hair color, wrong gender could cause mis-identification or duplicate customers.
**Fix:** Add confidence threshold - only use attributes with >0.7 confidence.

### 12. Plate OCR Misread
**Problem:** ANPR reads "ABC123" as "ABC125" (3→5 common OCR error).
**Impact:** Customer not identified despite being in database.
**Fix:** Add fuzzy plate matching (Levenshtein distance ≤1) with partial score (2.5 instead of 3.0).

### 13. Customer Leaves Before Till Detection Expires
**Problem:** Customer detected at till at 10:00, leaves at 10:00:15, till badge expires at 10:00:30.
**Impact:** Next customer arrives at 10:00:20, sees previous customer's badge.
**Fix:** Already handled - badge shows detected_at timestamp, teller can clear manually if wrong customer.

### 14. Multiple Customers at Checkout Simultaneously
**Problem:** 3 customers near checkout area, system picks wrong one.
**Impact:** Purchase linked to wrong customer.
**Fix:** Provide manual override in POS UI to select correct customer from recent detections.

### 15. Attribute Cache Out of Sync
**Problem:** New attributes added to customer in database, but `_attributes_cache` not refreshed.
**Impact:** Physical attribute matching uses stale data, lower scores.
**Fix:** Refresh attributes cache whenever `refresh_customers()` called (already implemented).

## Test Scenarios

### Scenario 1: Cold Start (Empty Database)
**Steps:**
1. Clear all customers from database
2. Start Recognition service
3. Walk in front of outdoor camera (car + person)
4. Walk in front of indoor camera
5. Stand at till camera

**Expected:**
- Customer auto-enrolled with CUST-0001
- Plate + face + gait enrolled
- Physical attributes stored
- Visit logged at each camera
- Till detection logged

**Edge Cases Tested:**
- Empty customer cache (first customer)
- Multiple camera detections in sequence
- Auto-enrollment threshold (2+ biometrics)

### Scenario 2: Returning Customer (All Signals Match)
**Steps:**
1. Existing customer CUST-0001 returns
2. Arrives by car (plate detected)
3. Walks indoor (face + gait detected)
4. Goes to till

**Expected:**
- Customer identified at each stage (score ~8-10)
- Visit count incremented
- No duplicate customer created
- Till badge appears (if name populated)

**Edge Cases Tested:**
- Weighted matching with all signals
- Cache hit performance
- Till detection

### Scenario 3: Returning Customer (Partial Signals)
**Steps:**
1. Customer CUST-0001 returns on foot (no car)
2. Wearing mask (no face detection)
3. Only gait + physical attributes available

**Expected:**
- Customer identified with lower score (~4-5)
- If below 5.0 threshold, not identified
- If gait + 4 physical attributes match, score ~4.8 → new customer created
- This is acceptable behavior (better safe than false positive)

**Edge Cases Tested:**
- Partial biometric data
- Physical attribute fallback
- Threshold boundary

### Scenario 4: Similar Customers (Parent + Teen)
**Steps:**
1. Parent (CUST-0001): Male, 175cm, black hair, plate ABC123
2. Teen son arrives in same car: Male, 170cm, black hair
3. Face and gait completely different

**Expected:**
- Plate matches parent (3.0)
- Face and gait don't match (0.0)
- Physical attributes partially match (gender 1.0, height 0.0, hair 0.8) = 1.8
- Total: 4.8 → below threshold → new customer CUST-0002 created
- Both customers linked to same plate

**Edge Cases Tested:**
- Shared vehicle handling
- Physical similarity but different biometrics
- Threshold prevents false positive

### Scenario 5: Appearance Change
**Steps:**
1. Customer CUST-0001 enrolled with long hair + beard
2. Week later returns with buzzcut, clean shaven
3. Same plate + face (slightly different due to facial hair)

**Expected:**
- Plate matches (3.0)
- Face still matches with lower score (2.5 instead of 2.9)
- Gait matches (1.8)
- Gender/height match (2.0)
- Hair/facial hair don't match (0.0)
- Total: 9.3 → identified
- Attributes updated with new appearance

**Edge Cases Tested:**
- Attribute changes over time
- Biometric dominance over attributes
- Attribute refresh on identification

### Scenario 6: Simultaneous Entry (Multiple People)
**Steps:**
1. Two cars arrive simultaneously
2. 4 people exit (2 from each car)
3. All 4 detected within 30 seconds

**Expected:**
- 6 events: 2 car events + 4 person events
- Each person matched to correct car based on timing
- 4 new customers created (if all new)
- Parallel processing handles load (<10s total)

**Edge Cases Tested:**
- Concurrent event processing
- Plate-person association
- Performance under burst load

### Scenario 7: Poor Image Quality
**Steps:**
1. Person walks past quickly (motion blur)
2. Camera at bad angle (face partially visible)
3. Low lighting (evening)

**Expected:**
- Face extraction may fail → returns None
- Gait extraction may succeed
- Physical attributes have low confidence (<0.5)
- Only use attributes with confidence >0.7
- If insufficient signals → no auto-enrollment (correct behavior)

**Edge Cases Tested:**
- Partial extraction failure
- Confidence filtering
- Graceful degradation

### Scenario 8: Database Connection Lost
**Steps:**
1. Customer detected
2. PostgreSQL service stopped mid-identification
3. POS API calls fail with connection error

**Expected:**
- `pos_get()` returns None or raises exception
- `identify_customer_weighted()` handles empty results
- Customer not identified (returns None)
- Event logged to recognition_service.log
- Retry on next detection (30s later)

**Edge Cases Tested:**
- Database failure handling
- API error handling
- Automatic recovery

### Scenario 9: Session Aggregation with Active Customer
**Steps:**
1. Customer enters at 10:00
2. Detected at 10:00, 10:15, 10:20
3. Aggregator runs at 10:22 (5min mark)
4. Customer still in store

**Expected:**
- Aggregator should NOT create session yet (last detection only 2 min ago)
- Only aggregate when last detection >30 min ago
- Session created when customer leaves and 30min passes

**Edge Cases Tested:**
- Premature session closure prevention
- Active customer detection
- Time window validation

### Scenario 10: Till Badge Race Condition
**Steps:**
1. Customer A at till at 10:00:00
2. Customer A leaves at 10:00:10
3. Customer B arrives at till at 10:00:15
4. Frontend polls at 10:00:20

**Expected:**
- Active customer endpoint returns most recent detection
- Returns Customer B (detected at 10:00:15)
- Customer A detection expired (>30s old would be 10:00:50)
- Both within window: returns most recent

**Edge Cases Tested:**
- Till detection ordering
- Time window expiry
- Multiple customers in checkout area

## Edge Case Fixes to Implement

### Priority 1 (Critical)
1. ✅ Add cache validation in `identify_customer_weighted()`
2. ✅ Add partial attribute handling in `extract_physical_attributes()`
3. ✅ Add deduplication for simultaneous detections
4. ✅ Add retry logic for database writes

### Priority 2 (Important)
5. ✅ Add session aggregator active customer check
6. ✅ Add confidence threshold for physical attributes
7. ✅ Add fuzzy plate matching
8. ✅ Add timeout handling for API calls

### Priority 3 (Nice to Have)
9. ✅ Add manual customer selection UI at till
10. ✅ Add attribute update detection and logging
11. ✅ Add duplicate session prevention
12. ✅ Add performance monitoring for burst loads

## Performance Benchmarks

| Scenario | Expected Time | Acceptable Max |
|----------|--------------|----------------|
| Single person detection → identification | <2s | 5s |
| Auto-enrollment (new customer) | <5s | 10s |
| 10 simultaneous detections | <10s | 20s |
| Session aggregation (100 visits) | <5s | 15s |
| Customer cache refresh | <2s | 5s |
| Attributes cache refresh | <1s | 3s |

## Monitoring Points

Add logging for:
- Cache hit rate (identified / total detections)
- Auto-enrollment rate (new customers / total detections)
- Average identification score
- Signal availability (% with face, gait, plate)
- API call failures and retries
- Session aggregation runs and records created
- Till detection accuracy (correct customer %)
