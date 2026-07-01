# Server Room Detection - Quick Fix Reference Card

## 🚨 **TL;DR - The Problem**

Server room detection **NOT saving to database** because:
- ❌ Cameras failing DSS authorization (code 2084)
- ❌ No frames reach worker
- ❌ No detections made
- ❌ No database inserts

Frontend **loads slowly** because:
- ❌ Missing database indexes
- ❌ Heavy queries without optimization

---

## ⚡ **Quick Fix (5 Minutes)**

### **1. Apply Performance Indexes**
```bash
mysql -u root -p cctv_monitoring < cctv_backend/src/migrations/add_detection_indexes.sql
```

### **2. Restart Backend**
```bash
cd cctv_backend && pm2 restart cctv-backend
```

### **3. Run Diagnostics**
```bash
./fix_server_room_cameras.sh
```

### **4. Fix Camera Authorization**
```sql
-- Check failing cameras
SELECT device_id, camera_no, status, cctv_url
FROM camera_links_rtsp
WHERE device_id IN ('1000648', '1000728')
  AND camera_no IN ('6', '4', '8');

-- Update status if needed
UPDATE camera_links_rtsp
SET status = 'accessible'
WHERE device_id IN ('1000648', '1000728')
  AND camera_no IN ('6', '4', '8');
```

### **5. Restart Worker**
```bash
pm2 restart server-room-worker
pm2 logs server-room-worker --lines 50
```

---

## 🔍 **Quick Diagnostics**

### **Check if detections are being saved:**
```sql
SELECT COUNT(*) FROM camera_report_rtsp
WHERE detection_type = 'server_room_detection'
  AND created_at > NOW() - INTERVAL 1 HOUR;
```
**Expected:** > 0 (if persons detected)

### **Check worker status:**
```bash
pm2 list | grep server-room-worker
```
**Expected:** Status = "online"

### **Check for authorization errors:**
```bash
pm2 logs server-room-worker --lines 100 | grep "authorization"
```
**Expected:** No errors

### **Check frontend performance:**
```bash
curl -w "@-" -o /dev/null -s "http://localhost:5000/api/client-dashboard/stats" <<'EOF'
time_total: %{time_total}s
EOF
```
**Expected:** < 2 seconds

---

## 📊 **Verification Commands**

```bash
# 1. Check indexes were created
mysql -u root -p cctv_monitoring -e "
  SHOW INDEX FROM camera_report_hls WHERE Key_name LIKE 'idx_%';
"

# 2. Check recent detections
mysql -u root -p cctv_monitoring -e "
  SELECT detection_type, COUNT(*) as count
  FROM camera_report_rtsp
  WHERE created_at > NOW() - INTERVAL 24 HOUR
  GROUP BY detection_type;
"

# 3. Check worker logs
pm2 logs server-room-worker --lines 20

# 4. Check backend logs
pm2 logs cctv-backend --lines 20
```

---

## 🎯 **Expected Results After Fix**

✅ **Worker logs show:**
```
[ServerRoom] 🚨 SAVED persons=2 → server_1000648_6_exam19_20260504_130045_abc123.jpg
[DB-REPORT-RTSP] ✅ SUCCESS: server_room_detection report inserted
```

✅ **Database query returns:**
```
+---------------------------+-------+
| detection_type            | count |
+---------------------------+-------+
| server_room_detection     |    15 |
| entry_exit_detection      |    42 |
| ...                       |   ... |
+---------------------------+-------+
```

✅ **Frontend loads in < 2 seconds**

✅ **No authorization errors in logs**

---

## 🚫 **Common Mistakes**

❌ **Don't restart worker before fixing authorization**
   → Will fail again immediately

❌ **Don't skip database indexes**
   → Frontend will remain slow

❌ **Don't forget to restart backend after index creation**
   → Changes won't take effect

❌ **Don't assume cameras are configured correctly**
   → Always verify with SQL query

---

## 📞 **If Still Not Working**

1. **Run full diagnostic:**
   ```bash
   ./fix_server_room_cameras.sh > diagnostic_output.txt 2>&1
   ```

2. **Collect logs:**
   ```bash
   pm2 logs server-room-worker --lines 500 > worker_logs.txt
   ```

3. **Check database:**
   ```bash
   mysql -u root -p cctv_monitoring < diagnose_server_room_cameras.sql > db_diagnostic.txt
   ```

4. **Share all three files for analysis**

---

## 📚 **Full Documentation**

- **Complete Diagnosis:** `SERVER_ROOM_DETECTION_DIAGNOSIS.md`
- **Fix Summary:** `SERVER_ROOM_FIX_SUMMARY.md`
- **SQL Diagnostics:** `diagnose_server_room_cameras.sql`
- **Auto Fix Script:** `fix_server_room_cameras.sh`

---

**Last Updated:** 2026-05-04  
**Estimated Fix Time:** 5-10 minutes  
**Difficulty:** Easy (mostly automated)
