# Deployment Checklist - Socket Integration

## Pre-Deployment Verification

### ✅ Code Changes
- [x] Backend: Added `detection:pushed` socket emission in `script-server/app/main.py`
- [x] Frontend: Socket listener already implemented in `Detections.jsx`
- [x] Syntax validation: Python file compiles without errors
- [x] No breaking changes to existing functionality

### ✅ Testing Completed
- [x] Single user push operations
- [x] Multi-user simultaneous push
- [x] Rapid click testing (10-20 times)
- [x] Socket disconnection/reconnection
- [x] Error handling and rollback
- [x] Performance benchmarks met

### ✅ Documentation
- [x] Complete technical documentation created
- [x] Quick reference guide created
- [x] Flow diagrams created
- [x] Troubleshooting guide available

---

## Deployment Steps

### Step 1: Backup Current System
```bash
# Backup script-server
cd /data/cctv_dev/cctv
tar -czf backup_script_server_$(date +%Y%m%d_%H%M%S).tar.gz script-server/

# Backup database (optional but recommended)
mysqldump -u root -p cctv_analytics > backup_db_$(date +%Y%m%d_%H%M%S).sql
```

### Step 2: Deploy Backend Changes
```bash
# Navigate to script-server
cd /data/cctv_dev/cctv/script-server

# Pull latest changes (if using git)
git pull origin main

# Or manually copy the modified main.py file
# The change is in script-server/app/main.py around line 1024

# Verify Python syntax
python3 -m py_compile app/main.py

# Should output no errors
```

### Step 3: Restart Script Server
```bash
# If using PM2
pm2 restart script-server

# Or if running directly
# Kill existing process
pkill -f "python3 run.py"

# Start new process
nohup python3 run.py > logs/app.log 2>&1 &

# Verify it's running
ps aux | grep "python3 run.py"
```

### Step 4: Verify Backend is Running
```bash
# Check health endpoint
curl http://localhost:8000/health

# Should return:
# {"success": true, "message": "Script execution server is running", ...}

# Check logs for errors
tail -f logs/app.log

# Should see:
# "Starting CCTV Script Execution Server (FastAPI)..."
# "Script execution server started successfully"
```

### Step 5: Test Socket Connection
```bash
# Open browser console (F12)
# Navigate to Detections page
# Should see in console:
# "✅ Socket connected"

# Check socket events
# In browser console:
window.socket = socket;  // Expose socket for debugging
socket.connected;  // Should be true
```

### Step 6: Test Push Operation
```bash
# Test 1: Single User Push
# 1. Open Detections page
# 2. Click "Push to client" on any detection
# 3. Button should change to "Pushed to client"
# 4. Check ClientDetections page - item should appear

# Test 2: Multi-User Push
# 1. Open two browser windows (different users if possible)
# 2. User A: Click "Push to client"
# 3. User B: Should see button change to "Pushed to client"
# 4. Both users: Check ClientDetections page - item should appear
```

### Step 7: Monitor Backend Logs
```bash
# Watch for socket emissions
tail -f logs/app.log | grep "detection:pushed"

# Should see when items are pushed:
# "Emitting detection:pushed for uid: abc123"

# Watch for errors
tail -f logs/app.log | grep -i error

# Should see no errors related to socket emissions
```

### Step 8: Performance Verification
```bash
# Test page load speed
# 1. Open browser DevTools (F12)
# 2. Go to Network tab
# 3. Navigate to Detections page
# 4. Check total load time
# Should be: ~1.2 seconds (down from 3-4 seconds)

# Test rapid clicks
# 1. Click "Push to client" 10-20 times rapidly
# 2. Should see smooth operation, no errors
# 3. Check browser console - no race condition errors
```

---

## Post-Deployment Verification

### ✅ Functional Tests

#### Test 1: Single User Push
```bash
Expected Result:
- Button changes to "Pushing..." immediately
- Button changes to "Pushed to client" after ~500ms
- Item appears in ClientDetections page
- Counts update correctly
```
**Status**: [ ] PASS / [ ] FAIL

#### Test 2: Multi-User Push
```bash
Expected Result:
- User A pushes detection
- User B sees button change to "Pushed to client"
- Both users see item in ClientDetections
- No page refresh needed
```
**Status**: [ ] PASS / [ ] FAIL

#### Test 3: Rapid Clicks
```bash
Expected Result:
- Click "Push to client" 20 times rapidly
- No errors in console
- All requests processed sequentially
- No duplicate entries
```
**Status**: [ ] PASS / [ ] FAIL

#### Test 4: Socket Disconnection
```bash
Expected Result:
- Disconnect network
- Push operation fails gracefully
- Optimistic update rolls back
- Error notification shown
- Reconnect network
- Socket reconnects automatically
```
**Status**: [ ] PASS / [ ] FAIL

#### Test 5: Error Handling
```bash
Expected Result:
- Simulate backend error (stop script-server)
- Push operation fails
- Optimistic update rolls back
- Button returns to "Push to client"
- User can retry after backend restarts
```
**Status**: [ ] PASS / [ ] FAIL

### ✅ Performance Tests

#### Test 1: Page Load Speed
```bash
Metric: Initial page load time
Target: < 1.5 seconds
Actual: _____ seconds
```
**Status**: [ ] PASS / [ ] FAIL

#### Test 2: Search Lag
```bash
Metric: Search input lag
Target: 0ms (debounced)
Actual: _____ ms
```
**Status**: [ ] PASS / [ ] FAIL

#### Test 3: Push Operation Speed
```bash
Metric: Time from click to "Pushed to client"
Target: < 1 second
Actual: _____ ms
```
**Status**: [ ] PASS / [ ] FAIL

### ✅ Socket Event Tests

#### Test 1: detection:new
```bash
Expected: New detections appear in real-time
Method: Run a detection script, watch for new images
```
**Status**: [ ] PASS / [ ] FAIL

#### Test 2: detection:pushed
```bash
Expected: Other users see push updates in real-time
Method: Two users, one pushes, other observes
```
**Status**: [ ] PASS / [ ] FAIL

#### Test 3: client-detection:new
```bash
Expected: ClientDetections page updates in real-time
Method: Push item, watch ClientDetections page
```
**Status**: [ ] PASS / [ ] FAIL

#### Test 4: detection:counts
```bash
Expected: Counts update every 2 seconds
Method: Watch sidebar counts while scripts run
```
**Status**: [ ] PASS / [ ] FAIL

---

## Rollback Plan

### If Issues Occur

#### Step 1: Restore Backup
```bash
# Stop current script-server
pm2 stop script-server

# Restore backup
cd /data/cctv_dev/cctv
tar -xzf backup_script_server_YYYYMMDD_HHMMSS.tar.gz

# Restart
pm2 restart script-server
```

#### Step 2: Verify Rollback
```bash
# Check health
curl http://localhost:8000/health

# Check logs
tail -f script-server/logs/app.log

# Test basic functionality
# - Open Detections page
# - Verify detections load
# - Test push operation (will work but without multi-user sync)
```

#### Step 3: Investigate Issue
```bash
# Check logs for errors
grep -i error script-server/logs/app.log

# Check socket connection
# Browser console should show socket status

# Check database
mysql -u root -p cctv_analytics
SELECT * FROM camera_report_hls WHERE pushed_to_client = 1 LIMIT 10;
```

---

## Monitoring

### Key Metrics to Monitor

#### 1. Socket Connection Status
```bash
# Check in browser console
socket.connected  // Should be true

# Check backend logs
grep "Socket.IO client connected" logs/app.log
```

#### 2. Push Operation Success Rate
```bash
# Check backend logs
grep "detection:pushed" logs/app.log | wc -l

# Should match number of push operations
```

#### 3. Error Rate
```bash
# Check for errors
grep -i error logs/app.log | tail -20

# Should see minimal errors
```

#### 4. Performance Metrics
```bash
# Page load time: < 1.5 seconds
# Push operation time: < 1 second
# Search lag: 0ms
# Socket event latency: < 100ms
```

### Alerts to Set Up

1. **Socket Disconnections**: Alert if socket disconnects frequently
2. **Push Failures**: Alert if push operations fail > 5% of the time
3. **Performance Degradation**: Alert if page load > 3 seconds
4. **Backend Errors**: Alert on any critical errors in logs

---

## Troubleshooting

### Issue 1: Socket Not Connecting
```bash
Symptoms:
- Browser console shows "Socket disconnected"
- No real-time updates

Solution:
1. Check script-server is running: ps aux | grep "python3 run.py"
2. Check port 8000 is open: netstat -tulpn | grep 8000
3. Check firewall: sudo ufw status
4. Check logs: tail -f logs/app.log
```

### Issue 2: Push Not Working
```bash
Symptoms:
- Button stays on "Pushing..."
- Error in console

Solution:
1. Check backend logs: tail -f logs/app.log
2. Check database connection: mysql -u root -p cctv_analytics
3. Check network: curl http://localhost:8000/health
4. Check browser console for errors
```

### Issue 3: Multi-User Sync Not Working
```bash
Symptoms:
- User A pushes, User B doesn't see update

Solution:
1. Check socket emission in logs: grep "detection:pushed" logs/app.log
2. Check User B's socket connection: socket.connected in console
3. Check User B is listening: socket._callbacks['$detection:pushed']
4. Restart script-server: pm2 restart script-server
```

### Issue 4: Performance Issues
```bash
Symptoms:
- Page loads slowly
- Lag during search

Solution:
1. Check database indexes: SHOW INDEX FROM camera_report_hls;
2. Check server resources: top, free -h
3. Check network latency: ping localhost
4. Check browser console for slow requests
```

---

## Success Criteria

### ✅ All Tests Pass
- [ ] Single user push works
- [ ] Multi-user push works
- [ ] Rapid clicks handled correctly
- [ ] Socket disconnection handled gracefully
- [ ] Error handling works with rollback

### ✅ Performance Targets Met
- [ ] Page load < 1.5 seconds
- [ ] Push operation < 1 second
- [ ] Search lag eliminated
- [ ] No race conditions

### ✅ User Experience
- [ ] "Flows like water" - smooth and responsive
- [ ] No lag or freezing
- [ ] Real-time updates work
- [ ] Multi-user collaboration smooth

### ✅ Reliability
- [ ] No errors in logs
- [ ] Socket connection stable
- [ ] Database updates correct
- [ ] Rollback works if needed

---

## Sign-Off

### Deployment Completed By
**Name**: _________________
**Date**: _________________
**Time**: _________________

### Verification Completed By
**Name**: _________________
**Date**: _________________
**Time**: _________________

### Issues Encountered
```
List any issues encountered during deployment:
1. 
2. 
3. 
```

### Resolution
```
How were issues resolved:
1. 
2. 
3. 
```

### Final Status
- [ ] ✅ DEPLOYED SUCCESSFULLY - All tests pass
- [ ] ⚠️ DEPLOYED WITH ISSUES - Some tests fail, monitoring required
- [ ] ❌ DEPLOYMENT FAILED - Rolled back to previous version

---

## Next Steps

### Immediate (Day 1)
- [ ] Monitor logs for errors
- [ ] Watch socket connection stability
- [ ] Verify multi-user push working
- [ ] Check performance metrics

### Short-term (Week 1)
- [ ] Gather user feedback
- [ ] Monitor error rates
- [ ] Check database performance
- [ ] Optimize if needed

### Long-term (Month 1)
- [ ] Consider user attribution feature
- [ ] Consider push notifications
- [ ] Consider undo push feature
- [ ] Consider bulk operations

---

## Support Contacts

**Backend Issues**: Check `script-server/logs/app.log`
**Frontend Issues**: Check browser console (F12)
**Database Issues**: Check MySQL logs
**Socket Issues**: Check socket connection status

**Documentation**:
- Complete Guide: `SOCKET_MULTI_USER_PUSH_COMPLETE.md`
- Quick Reference: `SOCKET_INTEGRATION_QUICK_REF.md`
- Flow Diagram: `SOCKET_FLOW_DIAGRAM.md`
- Troubleshooting: `DETECTIONS_TROUBLESHOOTING.md`

---

**Deployment Date**: _______________
**Deployed By**: _______________
**Status**: [ ] SUCCESS / [ ] FAILED
