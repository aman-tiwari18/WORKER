# Server Room Detection Issue - Complete Diagnosis

## 🔴 **Problem Summary**

Server room detection is **NOT saving to database** despite the worker running successfully. The root cause is **camera authorization failures** preventing frames from being processed.

---

## 📊 **What's Happening**

### **Detection Flow (Normal):**
```
1. master_pipeline fetches RTSP stream from DSS
2. Frames are queued to server_room_worker
3. Worker processes frames → detects persons
4. Worker saves image → calls report_to_db_rtsp()
5. Database row inserted with pushed_to_client = 0
6. Operator reviews → pushes to client (pushed_to_client = 1)
7. Frontend displays detection
```

### **What's Actually Happening:**
```
1. master_pipeline tries to fetch RTSP stream
2. ❌ DSS returns: {'code': 2084, 'desc': 'The user has no authorization.'}
3. ❌ No frames reach worker
4. ❌ No detections made
5. ❌ No database inserts
6. ❌ Nothing appears in frontend
```

---

## 🔍 **Evidence from Logs**

```log
[13:00:28] WARNING - No 'data' in DSS response for 1000648_6: 
           {'code': 2084, 'desc': 'The user has no authorization.'}
[13:00:28] WARNING - RTSP fetch failed for 1000648_6

[13:00:28] WARNING - No 'data' in DSS response for 1000728_4: 
           {'code': 2084, 'desc': 'The user has no authorization.'}
[13:00:28] WARNING - RTSP fetch failed for 1000728_4

[13:00:29] WARNING - No 'data' in DSS response for 1000648_8: 
           {'code': 2084, 'desc': 'The user has no authorization.'}
[13:00:29] WARNING - RTSP fetch failed for 1000648_8

[13:00:50] INFO - All producers finished. Stopping workers...
[13:00:50] INFO - MASTER PIPELINE COMPLETED
```

**Key Observations:**
- ✅ Worker started successfully: `🟢 [ServerRoom] RTSP worker started`
- ✅ GPU worker initialized: `🚀 [ServerRoom] GPU worker started (true batch mode)`
- ✅ YOLO model loaded: `YOLO model moved to CUDA`
- ❌ **All 3 cameras failed authorization**
- ❌ Pipeline completed with **zero frames processed**

---

## 🎯 **Root Causes**

### **1. Camera Authorization Failure (PRIMARY)**
- Cameras `1000648_6`, `1000728_4`, `1000648_8` are rejected by DSS server `d14.camview.io`
- Error code `2084` = "The user has no authorization"
- This means either:
  - Cameras don't exist in DSS system
  - Credentials lack permission for these cameras
  - Cameras are disabled/inactive in DSS

### **2. Missing Database Reporting in Core Script**
- `Server_room_det.py` saves images but **never calls `report_to_db_rtsp()`**
- This is intentional (it's a standalone test script)
- The worker (`server_room_worker.py`) handles DB reporting
- But if no frames arrive, worker never triggers DB insert

### **3. Frontend Performance Issues (SECONDARY)**
- `ClientDashboardController.js` runs heavy queries without indexes
- Multiple JOINs across large tables
- No caching layer
- Causes slow page loads when refreshing

---

## ✅ **Why Other Scripts Work**

Entry/Exit detection works because:
1. Those cameras **ARE authorized** on their DSS servers
2. Frames successfully reach the worker
3. Detections are made and saved to DB
4. Database inserts complete successfully

---

## 🛠️ **Solutions**

### **IMMEDIATE (Fix Authorization)**

#### **Step 1: Verify Camera Configuration**
Run this SQL query to check camera setup:

```sql
SELECT 
    device_id, 
    camera_no, 
    classification, 
    status,
    cctv_url,
    is_active
FROM camera_links_rtsp 
WHERE device_id IN ('1000648', '1000728') 
  AND camera_no IN ('6', '4', '8')
  AND exam_id = 19;
```

**Expected Output:**
- `classification` should contain "Server Room" or "Strong Room"
- `status` should be "accessible"
- `is_active` should be `1`
- `cctv_url` should point to valid DSS server

#### **Step 2: Check DSS Server Permissions**
1. Log into DSS admin panel: `https://d14.camview.io/admin`
2. Navigate to: **Devices → Device List**
3. Search for devices: `1000648`, `1000728`
4. Verify:
   - Devices exist and are active
   - Channels 6, 4, 8 are enabled
   - User account has "View" permission for these cameras

#### **Step 3: Test RTSP Stream Manually**
```bash
cd script-server/scripts/detection
python3 test_rtsp_from_excel.py
```

Enter camera details when prompted:
- Server: `d14.camview.io`
- Device ID: `1000648`
- Camera No: `6`

**Expected:** Stream should open successfully
**If fails:** Authorization issue confirmed

#### **Step 4: Fix Credentials (if needed)**
If credentials are wrong, update in database:

```sql
-- Check current credentials
SELECT server_address, username, password 
FROM camera_links_rtsp 
WHERE device_id = '1000648';

-- Update if needed (use actual credentials)
UPDATE camera_links_rtsp 
SET username = 'correct_username',
    password = 'correct_password'
WHERE device_id IN ('1000648', '1000728');
```

---

### **SHORT-TERM (Performance Optimization)**

#### **Add Database Indexes**
```sql
-- Speed up detection queries
CREATE INDEX idx_exam_pushed_created 
ON camera_report_hls (exam_id, pushed_to_client, created_at);

CREATE INDEX idx_exam_pushed_created 
ON camera_report_rtsp (exam_id, pushed_to_client, created_at);

-- Speed up detection type filtering
CREATE INDEX idx_detection_type 
ON camera_report_hls (detection_type, pushed_to_client);

CREATE INDEX idx_detection_type 
ON camera_report_rtsp (detection_type, pushed_to_client);
```

#### **Add Query Caching**
The dashboard queries are already optimized with index hints (just applied).

---

### **LONG-TERM (Architecture Improvements)**

#### **1. Add Redis Caching**
```javascript
// Cache dashboard stats for 30 seconds
const cacheKey = `dashboard:stats:${userCompanyName}:${timeRange}`;
const cached = await redis.get(cacheKey);
if (cached) return JSON.parse(cached);

// ... run queries ...

await redis.setex(cacheKey, 30, JSON.stringify(result));
```

#### **2. Add Health Check Endpoint**
Create endpoint to monitor camera authorization status:

```javascript
// GET /api/cameras/health
exports.getCameraHealth = async (req, res) => {
  const cameras = await query(`
    SELECT device_id, camera_no, status, last_check
    FROM camera_links_rtsp
    WHERE exam_id = ? AND is_active = 1
  `, [examId]);
  
  const failing = cameras.filter(c => c.status !== 'accessible');
  
  res.json({
    total: cameras.length,
    accessible: cameras.length - failing.length,
    failing: failing.length,
    failing_cameras: failing
  });
};
```

#### **3. Add Retry Logic for Failed Streams**
```python
# In master_pipeline.py
MAX_RETRIES = 3
RETRY_DELAY = 10  # seconds

for attempt in range(MAX_RETRIES):
    rtsp_url = fetch_rtsp_url(device_id, camera_no)
    if rtsp_url:
        break
    if attempt < MAX_RETRIES - 1:
        logger.warning(f"Retry {attempt+1}/{MAX_RETRIES} for {device_id}_{camera_no}")
        time.sleep(RETRY_DELAY)
```

---

## 🧪 **Testing Checklist**

### **After Fixing Authorization:**

- [ ] Run SQL query to verify camera configuration
- [ ] Test RTSP stream manually with `test_rtsp_from_excel.py`
- [ ] Restart detection workers: `pm2 restart server-room-worker`
- [ ] Monitor logs: `pm2 logs server-room-worker --lines 100`
- [ ] Verify detections appear in database:
  ```sql
  SELECT COUNT(*) FROM camera_report_rtsp 
  WHERE detection_type = 'server_room_detection' 
  AND created_at > NOW() - INTERVAL 1 HOUR;
  ```
- [ ] Check frontend displays detections
- [ ] Verify "Push to Client" workflow works

### **After Performance Optimization:**

- [ ] Run `EXPLAIN` on dashboard queries to verify index usage
- [ ] Measure page load time (should be < 2 seconds)
- [ ] Test with 10,000+ detections in database
- [ ] Monitor database CPU usage

---

## 📝 **Quick Reference**

### **Key Files:**
- **Detection Core:** `script-server/scripts/detection/Server_room_det.py`
- **Worker:** `script-server/scripts/detection/workers/server_room_worker.py`
- **DB Client:** `script-server/scripts/detection/utils/db_client.py`
- **Pipeline:** `script-server/scripts/detection/pipeline/master_pipeline.py`
- **Backend Controller:** `cctv_backend/src/controllers/ClientDashboardController.js`

### **Key Database Tables:**
- **Camera Config:** `camera_links_rtsp`, `camera_links_hls`
- **Detections:** `camera_report_rtsp`, `camera_report_hls`
- **Push Status:** `pushed_to_client` column (0 = not pushed, 1 = pushed)

### **Key Environment Variables:**
- `EXAM_ID` - Current exam being monitored
- `SHIFT_ID` - Current shift
- `DB_HOST`, `DB_USER`, `DB_PASSWORD` - Database credentials

### **Useful Commands:**
```bash
# Check worker status
pm2 list

# View worker logs
pm2 logs server-room-worker --lines 100

# Restart worker
pm2 restart server-room-worker

# Test RTSP stream
cd script-server/scripts/detection
python3 test_rtsp_from_excel.py

# Check database
mysql -u root -p cctv_monitoring
```

---

## 🎯 **Expected Outcome**

After fixing authorization:
1. ✅ RTSP streams connect successfully
2. ✅ Frames reach server_room_worker
3. ✅ Detections are made and saved to database
4. ✅ Images appear in results folder
5. ✅ Database rows inserted with `pushed_to_client = 0`
6. ✅ Operator can review and push to client
7. ✅ Frontend displays detections
8. ✅ Page loads quickly (< 2 seconds)

---

## 📞 **Support**

If issues persist after following this guide:
1. Collect full logs: `pm2 logs server-room-worker --lines 500 > server_room_debug.log`
2. Run diagnostic query:
   ```sql
   SELECT * FROM camera_links_rtsp 
   WHERE device_id IN ('1000648', '1000728') 
   AND camera_no IN ('6', '4', '8');
   ```
3. Test RTSP manually and save output
4. Share all outputs for further analysis

---

**Last Updated:** 2026-05-04  
**Status:** Authorization issue identified, performance optimizations applied
