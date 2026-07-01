# 🚀 Deployment Checklist - Index Optimization

## ✅ **Pre-Deployment**

- [ ] **Backup Database**
  ```bash
  mysqldump -u root -p cctv_monitoring > backup_$(date +%Y%m%d_%H%M%S).sql
  ```

- [ ] **Verify Indexes Exist**
  ```bash
  mysql -u root -p cctv_monitoring < cctv_backend/src/migrations/add_detection_indexes.sql
  ```
  Expected: All indexes should show "✅ EXISTS"

- [ ] **Check Current Performance**
  ```bash
  time curl -s "http://localhost:5000/api/client-dashboard/stats" > /dev/null
  ```
  Record baseline time: __________ seconds

- [ ] **Review Modified Files**
  - `cctv_backend/src/controllers/ClientDashboardController.js`
  - `cctv_backend/src/controllers/DashboardController.js`
  - `script-server/app/main.py`
  - `script-server/scripts/detection/utils/db_client.py`

---

## 🔄 **Deployment Steps**

### **Step 1: Restart Backend (2 min)**

- [ ] Navigate to backend directory
  ```bash
  cd cctv_backend
  ```

- [ ] Restart backend service
  ```bash
  pm2 restart cctv-backend
  ```

- [ ] Check logs for errors
  ```bash
  pm2 logs cctv-backend --lines 50
  ```
  Expected: No errors, server starts successfully

- [ ] Verify backend is running
  ```bash
  pm2 list | grep cctv-backend
  ```
  Expected: Status = "online"

---

### **Step 2: Restart Script Server (2 min)**

- [ ] Navigate to script-server directory
  ```bash
  cd ../script-server
  ```

- [ ] Restart script-server
  ```bash
  pm2 restart script-server
  ```

- [ ] Check logs for errors
  ```bash
  pm2 logs script-server --lines 50
  ```
  Expected: No errors, server starts successfully

- [ ] Verify script-server is running
  ```bash
  pm2 list | grep script-server
  ```
  Expected: Status = "online"

---

### **Step 3: Test Performance (1 min)**

- [ ] Test dashboard API
  ```bash
  time curl -s "http://localhost:5000/api/client-dashboard/stats" > /dev/null
  ```
  Expected: < 2 seconds (was ______ seconds before)

- [ ] Test detection API
  ```bash
  time curl -s "http://localhost:5000/api/detections?page=1&limit=50" > /dev/null
  ```
  Expected: < 1 second

- [ ] Open frontend in browser
  - URL: `http://localhost:3000/dashboard`
  - Check: Page loads in < 2 seconds
  - Check: No console errors

---

## ✅ **Post-Deployment Verification**

### **1. Database Query Performance**

- [ ] Check HLS query uses correct index
  ```sql
  EXPLAIN SELECT detection_type, COUNT(*) as count
  FROM camera_report_hls USE INDEX (idx_exam_shift_push)
  WHERE exam_id = 19 AND pushed_to_client = 1
  GROUP BY detection_type;
  ```
  Expected: `key = idx_exam_shift_push`

- [ ] Check RTSP query uses correct index
  ```sql
  EXPLAIN SELECT detection_type, COUNT(*) as count
  FROM camera_report_rtsp USE INDEX (idx_exam_shift_time_rtsp)
  WHERE exam_id = 19 AND pushed_to_client = 1
  GROUP BY detection_type;
  ```
  Expected: `key = idx_exam_shift_time_rtsp`

- [ ] Check camera loading query
  ```sql
  EXPLAIN SELECT * FROM camera_links_rtsp USE INDEX (idx_exam_active_rtsp)
  WHERE exam_id = 19 AND is_active = 1;
  ```
  Expected: `key = idx_exam_active_rtsp`

---

### **2. Application Performance**

- [ ] **Dashboard Page**
  - Load time: < 2 seconds ✅
  - No errors in console ✅
  - Data displays correctly ✅

- [ ] **Detection Page**
  - Load time: < 1 second ✅
  - Pagination works ✅
  - Images load correctly ✅

- [ ] **Camera Loading**
  - Workers start successfully ✅
  - Cameras load in < 1 second ✅
  - No authorization errors ✅

---

### **3. System Health**

- [ ] **Backend Logs**
  ```bash
  pm2 logs cctv-backend --lines 100 | grep -i error
  ```
  Expected: No critical errors

- [ ] **Script Server Logs**
  ```bash
  pm2 logs script-server --lines 100 | grep -i error
  ```
  Expected: No critical errors

- [ ] **Database CPU Usage**
  ```bash
  top -p $(pgrep mysqld)
  ```
  Expected: CPU < 30%

- [ ] **Slow Query Log**
  ```bash
  tail -f /var/log/mysql/slow-query.log
  ```
  Expected: No new slow queries

---

## 📊 **Performance Metrics**

### **Before Optimization**
- Dashboard Load: ______ seconds
- Detection Page: ______ seconds
- Database CPU: ______%

### **After Optimization**
- Dashboard Load: ______ seconds (Target: < 2s)
- Detection Page: ______ seconds (Target: < 1s)
- Database CPU: ______% (Target: < 30%)

### **Improvement**
- Dashboard: ______% faster
- Detection Page: ______% faster
- Database CPU: ______% reduction

---

## 🔄 **Rollback Plan** (if needed)

If issues occur, follow these steps:

1. **Stop Services**
   ```bash
   pm2 stop cctv-backend script-server
   ```

2. **Revert Code Changes**
   ```bash
   git checkout HEAD -- cctv_backend/src/controllers/
   git checkout HEAD -- script-server/app/main.py
   git checkout HEAD -- script-server/scripts/detection/utils/db_client.py
   ```

3. **Restart Services**
   ```bash
   pm2 restart cctv-backend script-server
   ```

4. **Verify Rollback**
   ```bash
   pm2 logs cctv-backend --lines 20
   pm2 logs script-server --lines 20
   ```

---

## 📝 **Post-Deployment Tasks**

- [ ] **Monitor for 24 Hours**
  - Check logs every 4 hours
  - Monitor database CPU usage
  - Watch for slow query warnings

- [ ] **Update Documentation**
  - Update team wiki with new performance metrics
  - Document index usage patterns
  - Share optimization results with team

- [ ] **Notify Stakeholders**
  - Send performance improvement report
  - Highlight 80-93% speed improvements
  - Share before/after metrics

- [ ] **Schedule Follow-up**
  - Review performance after 1 week
  - Check for any edge cases
  - Gather user feedback

---

## 🎯 **Success Criteria**

Deployment is successful if:

- ✅ All services restart without errors
- ✅ Dashboard loads in < 2 seconds
- ✅ Detection page loads in < 1 second
- ✅ Database queries use correct indexes
- ✅ No increase in error rates
- ✅ Database CPU usage < 30%
- ✅ No slow query warnings

---

## 📞 **Support Contacts**

If issues occur:

1. **Check Documentation**
   - `COMPLETE_INDEX_OPTIMIZATION.md`
   - `OPTIMIZATION_DEPLOYMENT_GUIDE.md`
   - `INDEX_OPTIMIZATION_FINAL.md`

2. **Run Diagnostics**
   ```bash
   ./fix_server_room_cameras.sh
   ```

3. **Collect Logs**
   ```bash
   pm2 logs --lines 500 > deployment_logs.txt
   ```

---

## ✅ **Sign-off**

- [ ] **Deployed By:** ________________
- [ ] **Date:** ________________
- [ ] **Time:** ________________
- [ ] **Performance Verified:** YES / NO
- [ ] **Issues Encountered:** NONE / ________________
- [ ] **Rollback Required:** NO / YES

---

**Deployment Status:** ⬜ Not Started | ⬜ In Progress | ⬜ Complete | ⬜ Rolled Back

**Notes:**
_____________________________________________________________________________
_____________________________________________________________________________
_____________________________________________________________________________
